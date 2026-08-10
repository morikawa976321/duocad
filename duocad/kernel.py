"""OCP(OCCT)を直接叩く層(実装計画書 §2 アーキテクチャ、§6予防4)。

**duocad配下でOCP.*をimportしてよいのはこのモジュールだけ**
(tests/test_boundaries.py が機械的に検査する。verbが直接OCPを叩き始めるのを防ぐための境界)。

M0の契約:
    read_step(path) -> TopoDS_Shape
        STEPファイルを読み、形状を返す。**ReadFile()の戻り値を必ず先に検査し**、
        失敗(IFSelect_RetDone以外)なら TransferRoots()/OneShape() を呼ぶ前に例外を送出すること。
        理由: 失敗した状態のままこれらを呼ぶと、Python例外ではなく**プロセスごと落ちる**
        (Windowsで 0xC0000005。パス無し=IFSelect_RetError / 中身が壊れている=IFSelect_RetFail の
        どちらでも同じ。2026-08-10実測・監査で再現確認。io_step.py のモジュールdocstring参照)。
    write_step(shape, path) -> None
    write_stl(shape, path, deviation) -> None
        deviation は**絶対**の線形偏差(mm)。BRepMesh_IncrementalMesh に isRelative=False で渡す
        (isRelative=Trueだと偏差を変えてもメッシュが変わらない。2026-08-10監査で実測)。

M0で追加した補助(query.py が問い合わせを組み立てるために使う。OCPを触るのでここに置く):
    volume(shape) -> float          BRepGProp.VolumeProperties_s(Eps無し)
    classify_face(face) -> str      面の種別(平面/全周円筒/…)を FACE_KINDS のどれかへ分類
    normalize_provenance(...)       由来マップ(Modified/Generated/History)の正規化アダプタ
"""
from __future__ import annotations

import math
import os
from typing import Any, Iterable, List, Optional

from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.BRepGProp import BRepGProp
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.BRepTools import BRepTools
from OCP.GeomAbs import GeomAbs_SurfaceType
from OCP.GProp import GProp_GProps
from OCP.IFSelect import IFSelect_ReturnStatus
from OCP.STEPControl import (
    STEPControl_Reader,
    STEPControl_StepModelType,
    STEPControl_Writer,
)
from OCP.StlAPI import StlAPI_Writer
from OCP.TopAbs import TopAbs_ShapeEnum
from OCP.TopExp import TopExp
from OCP.TopoDS import TopoDS, TopoDS_Shape
from OCP.TopTools import TopTools_IndexedMapOfShape

#: describe_shape の faces_by_type が持つキー(この順序が正。query.py が初期化に使う)
FACE_KINDS = (
    "plane",
    "cylinder_full",
    "cylinder_partial",
    "torus",
    "cone",
    "bspline",
    "other",
)

#: 全周円筒(=穴)と見なすU範囲のしきい値[度](実装計画書§8)。
#: shelf_attachment_GD.step の円筒22面のU範囲は全て正確に360.0度で、しきい値付近に一件も無い
#: (2026-08-10実測)ため、この値の選び方に結果は依存しない。
FULL_CYLINDER_DEGREES = 350.0


# --------------------------------------------------------------------------------------
# STEP / STL
# --------------------------------------------------------------------------------------
def read_step(path: str) -> "TopoDS_Shape":
    """STEPファイルを読み、TopoDS_Shapeを返す。失敗時は例外を送出する(黙って失敗しない)。

    パスが存在しない -> FileNotFoundError / 中身がSTEPとして壊れている -> ValueError。

    **ReadFile()の戻り値を必ず先に検査する**。失敗した状態で TransferRoots()/OneShape() を
    呼ぶとPython例外にならずプロセスごと落ちる(0xC0000005)ため、ここが唯一の安全弁になる。
    存在検査は「どちらの例外を投げるか」を決めるためだけに使い、続行の判断には使わない
    (Path.exists() だけでは壊れたファイルを止められない)。
    """
    filename = os.fspath(path)
    reader = STEPControl_Reader()
    status = reader.ReadFile(filename)
    if status != IFSelect_ReturnStatus.IFSelect_RetDone:
        # ここで必ず止まる。以降(TransferRoots/OneShape)は読み込み成功時のみ到達する。
        if not os.path.exists(filename):
            raise FileNotFoundError(f"STEP file not found: {filename} (ReadFile -> {status})")
        raise ValueError(f"not a readable STEP file: {filename} (ReadFile -> {status})")

    roots = reader.TransferRoots()
    if roots < 1:
        raise ValueError(f"STEP file has no transferable root: {filename}")

    shape = reader.OneShape()
    if shape is None or shape.IsNull():
        raise ValueError(f"STEP file produced an empty shape: {filename}")
    return shape


def write_step(shape: "TopoDS_Shape", path: str) -> None:
    """形状をSTEPとして書き出す(モードは AsIs)。書き出しに失敗したらファイルを残さない。"""
    if shape is None or shape.IsNull():
        raise ValueError("cannot write an empty shape to STEP")

    filename = os.fspath(path)
    writer = STEPControl_Writer()
    # Transfer も IFSelect_ReturnStatus を返す。ここを捨てると「転送に失敗したのに
    # Write は成功した(=中身の無いSTEP)」を成功として通しうる(§7「例外を投げずに黙って失敗する」)。
    transfer_status = writer.Transfer(shape, STEPControl_StepModelType.STEPControl_AsIs)
    if transfer_status != IFSelect_ReturnStatus.IFSelect_RetDone:
        raise ValueError(f"failed to transfer shape to STEP: {filename} (Transfer -> {transfer_status})")

    status = writer.Write(filename)
    if status != IFSelect_ReturnStatus.IFSelect_RetDone:
        _unlink_quietly(filename)
        raise ValueError(f"failed to write STEP: {filename} (Write -> {status})")


def write_stl(shape: "TopoDS_Shape", path: str, deviation: float) -> None:
    """形状をSTLとしてテセレーションして書き出す(バイナリSTL)。

    deviation は**絶対**の線形偏差(mm)。BRepMesh_IncrementalMesh の第3引数 isRelative は
    必ず False にする(True にすると 0.2/0.05/0.01 のどれでも同じメッシュになる。2026-08-10実測)。
    既存の三角形分割は BRepTools.Clean_s で捨ててから測り直す — 同じ形状に対して偏差を変えて
    複数回書き出すとき、前回のメッシュが残っていると偏差が効かないことがあるため。
    """
    if shape is None or shape.IsNull():
        raise ValueError("cannot write an empty shape to STL")
    if not deviation > 0:
        raise ValueError(f"deviation must be positive (mm), got {deviation!r}")

    filename = os.fspath(path)
    BRepTools.Clean_s(shape)
    mesh = BRepMesh_IncrementalMesh(shape, float(deviation), False, 0.5, False)
    if not mesh.IsDone():
        raise ValueError(f"meshing failed for deviation={deviation} (BRepMesh_IncrementalMesh)")

    writer = StlAPI_Writer()
    writer.ASCIIMode = False
    if not writer.Write(shape, filename):
        _unlink_quietly(filename)
        raise ValueError(f"failed to write STL: {filename}")


def _unlink_quietly(filename: str) -> None:
    """書き出し失敗時に半端なファイルを残さない(0バイトの成果物を作らない。§6予防6)。"""
    try:
        os.remove(filename)
    except OSError:
        pass


# --------------------------------------------------------------------------------------
# 計測・分類(query.py から使う。OCPを触るのでここに置く)
# --------------------------------------------------------------------------------------
def volume(shape: "TopoDS_Shape") -> float:
    """形状の体積(mm^3)。

    `BRepGProp.VolumeProperties_s(shape, props)` の**既定の積分**を使う(`Eps` を渡さない)。
    `Eps` 付きの適応積分にすると精度が落ち(Eps=1e-9で相対1.8e-10、Eps=1e-6で相対3.7e-8。
    2026-08-10監査で実測)、STEP往復の体積比較が体積の求め方に依存してしまう。
    """
    if shape is None or shape.IsNull():
        raise ValueError("cannot compute the volume of an empty shape")
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props)
    return props.Mass()


def classify_face(face: Any) -> str:
    """1つの面を FACE_KINDS のいずれかへ分類する。

    円筒は `BRepAdaptor_Surface` のU範囲(度)が FULL_CYLINDER_DEGREES を超えるかで
    全周(=穴)と部分に分ける(実装計画書§8)。
    """
    adaptor = BRepAdaptor_Surface(TopoDS.Face_s(face))
    surface_type = adaptor.GetType()

    if surface_type == GeomAbs_SurfaceType.GeomAbs_Plane:
        return "plane"
    if surface_type == GeomAbs_SurfaceType.GeomAbs_Cylinder:
        span = math.degrees(adaptor.LastUParameter() - adaptor.FirstUParameter())
        return "cylinder_full" if span > FULL_CYLINDER_DEGREES else "cylinder_partial"
    if surface_type == GeomAbs_SurfaceType.GeomAbs_Torus:
        return "torus"
    if surface_type == GeomAbs_SurfaceType.GeomAbs_Cone:
        return "cone"
    if surface_type in (
        GeomAbs_SurfaceType.GeomAbs_BSplineSurface,
        GeomAbs_SurfaceType.GeomAbs_BezierSurface,
    ):
        return "bspline"
    return "other"


def count_subshapes(shape: "TopoDS_Shape", kind: str) -> int:
    """重複を除いた部分形状の数(`TopExp.MapShapes_s`)。kind は "face" | "edge" | "vertex"。

    `TopExp_Explorer` で素朴に列挙すると隣接2面から同じ辺を2回拾う
    (shelf_attachment_GD.step 実測: 重複除去66 / 素朴な列挙132)。
    """
    enum = {
        "face": TopAbs_ShapeEnum.TopAbs_FACE,
        "edge": TopAbs_ShapeEnum.TopAbs_EDGE,
        "vertex": TopAbs_ShapeEnum.TopAbs_VERTEX,
    }.get(kind)
    if enum is None:
        raise ValueError(f"unknown subshape kind: {kind!r}")
    shape_map = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, enum, shape_map)
    return shape_map.Extent()


# --------------------------------------------------------------------------------------
# 由来(provenance)の正規化アダプタ — day1で置き場所だけ作る(§6予防2)
# --------------------------------------------------------------------------------------
def normalize_provenance(
    builder: Any,
    inputs: Iterable["TopoDS_Shape"] = (),
) -> Optional[dict]:
    """OCCTの由来マップを1つの形へ正規化して返す(`Operation.provenance` に入れる値)。

    **なぜここに置くのか(実装計画書§6予防2)**: 由来の取り出し方は操作ごとに違う。
    ブーリアン(`BRepAlgoAPI_*`)は `History()` -> `BRepTools_History` を持ち、
    フィレット等の `BRepBuilderAPI_MakeShape` 系は自分自身が `Modified()`/`Generated()`/
    `IsDeleted()` を持つ(2026-08-10に両方の存在を実測)。verb側にこの分岐を書くと、
    verbが増えるたびに同じ分岐が写経される。**正規化はこの1関数に閉じる**。

    戻り値の形(由来が1件も取れなければ None):

        {
            "modified":  [{"input": <TopoDS_Shape>, "results": [<TopoDS_Shape>, ...]}, ...],
            "generated": [{"input": <TopoDS_Shape>, "results": [<TopoDS_Shape>, ...]}, ...],
            "deleted":   [<TopoDS_Shape>, ...],
        }

    **M0の位置づけ**: M0で実在するverbは `import_step` だけで、これは既存形状を変形しないので
    由来は発生しない(`Operation.provenance` は None のまま)。スロットと正規化の置き場所を
    day1に用意しておくのが目的で、後付けすると全操作の書き直しになる。
    M1以降で `extrude_face` 等を足すときに、この関数の戻り値をそのまま `provenance` へ入れる。

    **M1への申し送り(2026-08-10のQAで指摘)**: 戻り値は `TopoDS_Shape` を直接含む
    **メモリ上の形**であり、そのままでは `OpLog.to_json()` でJSONにできない
    (M0は常に None なので今は壊れない)。M1で由来を実際に積むときは、
    永続化用の射影(添字や安定名などのJSON化できる表現)をここに足すこと。
    """
    source = builder.History() if hasattr(builder, "History") else builder

    modified: List[dict] = []
    generated: List[dict] = []
    deleted: List[Any] = []

    for input_shape in inputs:
        if input_shape is None or input_shape.IsNull():
            continue
        for sub in _iter_faces_and_edges(input_shape):
            for bucket, getter in (("modified", "Modified"), ("generated", "Generated")):
                results = _shape_list(source, getter, sub)
                if results:
                    (modified if bucket == "modified" else generated).append(
                        {"input": sub, "results": results}
                    )
            if _is_removed(source, sub):
                deleted.append(sub)

    if not (modified or generated or deleted):
        return None
    return {"modified": modified, "generated": generated, "deleted": deleted}


def _iter_faces_and_edges(shape: "TopoDS_Shape") -> List["TopoDS_Shape"]:
    """由来を問い合わせる対象の部分形状(面・辺)を重複なしで列挙する。"""
    out: List[TopoDS_Shape] = []
    for enum in (TopAbs_ShapeEnum.TopAbs_FACE, TopAbs_ShapeEnum.TopAbs_EDGE):
        shape_map = TopTools_IndexedMapOfShape()
        TopExp.MapShapes_s(shape, enum, shape_map)
        out.extend(shape_map.FindKey(i) for i in range(1, shape_map.Extent() + 1))
    return out


def _shape_list(source: Any, getter: str, sub: "TopoDS_Shape") -> List["TopoDS_Shape"]:
    """`Modified(sub)` / `Generated(sub)` の戻り(TopTools_ListOfShape)をPythonのlistへ。"""
    method = getattr(source, getter, None)
    if method is None:
        return []
    try:
        result = method(sub)
    except Exception:  # OCCTは対象外の部分形状で例外を投げることがある
        return []
    return list(result)


def _is_removed(source: Any, sub: "TopoDS_Shape") -> bool:
    """削除されたか。`BRepTools_History` は IsRemoved、MakeShape系は IsDeleted を持つ。"""
    for name in ("IsRemoved", "IsDeleted"):
        method = getattr(source, name, None)
        if method is not None:
            try:
                return bool(method(sub))
            except Exception:
                return False
    return False
