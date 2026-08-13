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

M1で追加(2026-08-10。ビューポートと extrude_face のためにOCPが要る処理はすべてここ):
    face_count(shape) -> int
    tessellate(shape, deviation) -> dict    **面ごと**の三角形群(ピッキングに面単位のグループが要る)
    extrude_face_on_shape(shape, i, d) -> dict   面iを距離dだけ押し出した新形状＋由来の射影

D(2026-08-14)で追加:
    tessellate_edges(shape, deviation) -> list  **辺ごと**の折れ線(辺のピッキングに要る)

M2で追加(2026-08-11。スケッチ平面・領域検出・領域押し出しの幾何):
    require_planar_face(shape, face_index) -> None
    face_of(shape, face_index) -> TopoDS_Face
    sketch_plane_normal(plane, face=None) -> (nx, ny, nz)
    sketch_regions(plane, entities, face=None) -> list[dict]
    extrude_regions(faces, distance, mode, normal, target_shape=None) -> dict

**ocp_tessellate は使わない**(実装計画書§4は使うと書いているが、この venv に入っておらず、
かつ面ピッキングには面ごとの三角形グループが要るので自前実装の方が制御できる。2026-08-10判断)。

戻り値はいずれも **JSON化できる素の値(float/int/str/list/dict)だけ**にする。OCPのオブジェクトを
kernel の外へ出さないための規律で、境界テスト(importの静的検査)では捕まえられない側の約束。
唯一の例外は `extrude_face_on_shape` / `sketch_regions` / `extrude_regions` /
`face_of` の戻りに含まれる TopoDS_*(document.Body.shape や領域押し出しの入力になる値)。
"""
from __future__ import annotations

import math
import os
from typing import Any, Iterable, List, Optional, Sequence, Tuple

from OCP.Bnd import Bnd_Box
from OCP.BRep import BRep_Builder, BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse, BRepAlgoAPI_Splitter
from OCP.BRepBuilderAPI import (
    BRepBuilderAPI_Copy,
    BRepBuilderAPI_MakeEdge,
    BRepBuilderAPI_MakeFace,
    BRepBuilderAPI_MakeWire,
)
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepFilletAPI import BRepFilletAPI_MakeChamfer, BRepFilletAPI_MakeFillet
from OCP.BRepGProp import BRepGProp, BRepGProp_Face
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
from OCP.BRepTools import BRepTools
from OCP.GCPnts import GCPnts_AbscissaPoint, GCPnts_QuasiUniformDeflection
from OCP.GeomAbs import GeomAbs_CurveType, GeomAbs_SurfaceType
from OCP.GProp import GProp_GProps
from OCP.IFSelect import IFSelect_ReturnStatus
from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain
from OCP.STEPControl import (
    STEPControl_Reader,
    STEPControl_StepModelType,
    STEPControl_Writer,
)
from OCP.StlAPI import StlAPI_Writer
from OCP.TopAbs import TopAbs_Orientation, TopAbs_ShapeEnum
from OCP.TopExp import TopExp
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS, TopoDS_Compound, TopoDS_Shape
from OCP.TopTools import TopTools_IndexedMapOfShape, TopTools_ListOfShape
from OCP.gp import gp_Ax2, gp_Ax3, gp_Circ, gp_Dir, gp_Pnt, gp_Vec

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

#: 基準平面の (origin, normal, xdir)。gp_Ax3 で組み立てると v は表のとおりになる。
_BASE_PLANE_AXES = {
    "XY": ((0.0, 0.0, 0.0), (0.0, 0.0, 1.0), (1.0, 0.0, 0.0)),  # v -> +Y
    "XZ": ((0.0, 0.0, 0.0), (0.0, -1.0, 0.0), (1.0, 0.0, 0.0)),  # v -> +Z
    "YZ": ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),  # v -> +Z
}


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


def bounding_box(shape: "TopoDS_Shape") -> dict:
    """形状の境界箱(mm)。**幾何から求める**(三角形分割に依存しない)。

    M4のMCPで足した(2026-08-11)。AIに「どの部品が手前か・どれが大きいか」を渡す最小の
    空間情報で、これが無いと名前と面数だけで部品を選ぶことになる。

    戻り値: {"min":[x,y,z], "max":[x,y,z], "center":[x,y,z], "size":[dx,dy,dz], "diagonal": float}

    `BRepBndLib.Add_s(..., useTriangulation=False)` を使う。True(既定)だと**既存の
    三角形分割があればそれを使う**ため、テセレーションの粗さで箱の値が変わる
    (同じ形状でも `tessellate` を呼んだ後だけ違う値になる、という再現性の無い挙動になる)。
    `SetGap(0.0)` を入れても OCCT は内部許容差ぶん(実測 1e-7mm)膨らませた値を返すので、
    **厳密な寸法の根拠には使わない**(mm単位の部品では無視できる大きさ)。
    `kernel.tessellate` の戻りにある `bbox` は三角形の節点から作った別物で、あちらは
    アプリ層のカメラ合わせ用。
    """
    if shape is None or shape.IsNull():
        raise ValueError("cannot compute the bounding box of an empty shape")
    box = Bnd_Box()
    box.SetGap(0.0)
    BRepBndLib.Add_s(shape, box, False)
    if box.IsVoid():
        raise ValueError("bounding box is void (形状に頂点が無い)")
    x_min, y_min, z_min, x_max, y_max, z_max = box.Get()
    lows = [x_min, y_min, z_min]
    highs = [x_max, y_max, z_max]
    return {
        "min": lows,
        "max": highs,
        "center": [(lows[i] + highs[i]) / 2.0 for i in range(3)],
        "size": [highs[i] - lows[i] for i in range(3)],
        "diagonal": math.dist(lows, highs),
    }


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


# --------------------------------------------------------------------------------------
# M1: テセレーション — 面ごとの三角形群(2026-08-10追加)
# --------------------------------------------------------------------------------------
#: 既定の線形偏差(mm)。write_stl と同じ「絶対」の偏差。
DEFAULT_DEVIATION = 0.2
#: 既定の角度偏差。曲面(穴の円筒)が多角形に見えない程度に細かくする。
DEFAULT_ANGULAR_DEFLECTION = 0.35

#: 面の同一性を「重心の一致」で判定するときの相対許容差(after_face_index の探索)。
_MATCH_TOL = 1.0e-6


def face_count(shape: "TopoDS_Shape") -> int:
    """重複を除いた面の数。**この数え方の1始まりの添字が、選択の識別子そのもの**。"""
    return _face_map(shape).Extent()


def tessellate(
    shape: "TopoDS_Shape",
    deviation: float = DEFAULT_DEVIATION,
    angular_deflection: float = DEFAULT_ANGULAR_DEFLECTION,
) -> dict:
    """形状を三角形分割し、**面ごとに分けた**三角形群を素の値だけで返す。

    面ごとに分けるのは、アプリ層が面をクリックで選ぶために「どの三角形がどの面か」を
    必要とするため(1つの塊にまとめると当たった三角形から面へ戻れない)。添字は
    `face_count` と同じ `TopExp.MapShapes_s` の1始まりで、`extrude_face_on_shape` の
    `face_index` とも一致する。

    三角形の巻き方向は、面の向きが `TopAbs_REVERSED` のとき入れ替える(外向きに揃える)。
    これをやらないと、アプリ側で法線が内側を向いた面が黒く落ちる。

    戻り値:
        {
          "deviation": float, "face_count": int,
          "bbox": {"min": [x,y,z], "max": [x,y,z], "center": [x,y,z], "diagonal": float},
          "faces": [{"index": int, "kind": str, "area": float, "centroid": [..],
                     "normal": [..], "positions": [x,y,z, ...], "indices": [i,j,k, ...]}],
          "untriangulated": [int, ...],   # 三角形が取れなかった面の添字(通常は空)
        }

    `untriangulated` を戻り値に持たせているのは、**分割に失敗した面を黙って落とさない**ため
    (§6予防6)。1面でも欠けると画面上はただ穴が空いて見え、原因の分からない不具合になる。
    全面が失敗した場合だけは形状として成立しないので例外にする。
    """
    if shape is None or shape.IsNull():
        raise ValueError("cannot tessellate an empty shape")
    if not deviation > 0:
        raise ValueError(f"deviation must be positive (mm), got {deviation!r}")
    if not angular_deflection > 0:
        raise ValueError(f"angular_deflection must be positive, got {angular_deflection!r}")

    # 既存の三角形分割を捨ててから測り直す(前回の粗いメッシュが残っていると偏差が効かない。
    # write_stl と同じ理由・同じ順序)。
    BRepTools.Clean_s(shape)
    mesh = BRepMesh_IncrementalMesh(shape, float(deviation), False, float(angular_deflection), True)
    if not mesh.IsDone():
        raise ValueError(f"meshing failed for deviation={deviation} (BRepMesh_IncrementalMesh)")

    shape_map = _face_map(shape)
    faces: List[dict] = []
    untriangulated: List[int] = []
    for index in range(1, shape_map.Extent() + 1):
        face = TopoDS.Face_s(shape_map.FindKey(index))
        positions, indices = _face_triangles(face)
        if not indices:
            untriangulated.append(index)
        area, centroid = _face_props(face)
        faces.append(
            {
                "index": index,
                "kind": classify_face(face),
                "area": area,
                "centroid": list(centroid),
                "normal": list(_face_normal(face)),
                "positions": positions,
                "indices": indices,
            }
        )

    if faces and len(untriangulated) == len(faces):
        raise ValueError(
            f"三角形分割が1面も取れなかった(deviation={deviation}, angular={angular_deflection})"
        )

    return {
        "deviation": float(deviation),
        "face_count": len(faces),
        "bbox": _bounding_box(faces),
        "faces": faces,
        "untriangulated": untriangulated,
    }


# --------------------------------------------------------------------------------------
# M1: extrude_face の幾何(7つ目のverb。実装計画書§1.1)
# --------------------------------------------------------------------------------------
def extrude_face_on_shape(
    shape: "TopoDS_Shape",
    face_index: int,
    distance: float,
) -> dict:
    """面 `face_index` を `distance` だけ押し出した**新しい形状**と、由来の射影を返す。

    符号の意味: `distance > 0` は面の**外向き法線方向へ肉を足す**(Fuse)、`distance < 0` は
    同じ向きへ**削る**(Cut)。どちらも面から直接プリズムを作ってブーリアンする(実装計画書§1.1
    「実装は安い(面から直接プリズムを作る)」)。入力の `shape` は変更しない。

    **M1では平面の面だけ押し出せる**(それ以外は ValueError)。曲面は「面の中央の法線」方向へ
    掃引しても意味のある操作にならず、**成功したまま壊れた形状を返す**ことを実測した
    (2026-08-10: `shelf_attachment_GD.step` の穴の円筒面を+1mm押し出すと `IsDone()` は真・
    体積も変わるが `BRepCheck_Analyzer.IsValid()` が偽になる。-1mmでは体積が
    91018→38597 と激減する「有効だが無意味な」形状になる)。M1の計測対象は平面なので、
    ここで前提として弾くのが正しい。曲面の押し出し(オフセット)はM1の範囲外。

    **黙って失敗させない**(§6予防6・§7。OCCTのブーリアンは例外を投げずに失敗する):
    プリズム生成とブーリアンの `IsDone()`、結果が空でないこと、**体積が実際に変わったこと**、
    **最後に `BRepCheck_Analyzer` で形状が妥当であること**を毎回検査し、
    どれか1つでも欠ければ例外にして操作ごと捨てる。

    同一平面の面は `ShapeUpgrade_UnifySameDomain` で統合する。統合しないと押し出すたびに
    面が分裂して増え(実測: 20mm立方体の上面を+10 → 統合なし10面 / 統合後6面)、
    **同じ面を続けて押し出す**という計測の手続き自体が成立しなくなる。ただし統合は
    **入力の形状を壊す**うえ利得が無い場合があるので、コピーで試してから採否を決める
    (機序と実測値は `_unify_same_domain` のdocstring。ここを素直に書くと正常な押し出しが
    壊れた形状として残る)。

    戻り値(`"shape"` 以外はすべてJSON化できる素の値):
        {
          "shape": <TopoDS_Shape>, "unified": bool,
          "face_index": int, "distance": float, "normal": [nx,ny,nz],
          "after_face_index": int | None,   # 押し出した面が新形状のどれになったか
          "volume_before": float, "volume_after": float,
          "provenance": {...}               # 下記
        }

    `provenance` は `Operation.provenance` にそのまま入れられる**JSON化可能な射影**
    (`normalize_provenance` の戻りは TopoDS_Shape を含むメモリ上の形なので `to_json()` で
    落ちる。M0のQAが申し送った穴をここで埋める):

        {"verb": "extrude_face", "face_index": int, "distance": float, "unified": bool,
         "face_map": {"<旧添字>": [新添字, ...]}, "deleted": [旧添字, ...],
         "new_faces": [新添字, ...], "after_face_index": int | None}
    """
    if shape is None or shape.IsNull():
        raise ValueError("cannot extrude a face of an empty shape")
    distance = float(distance)
    if distance == 0.0:
        raise ValueError("distance must not be 0 (押し出し量0は形状を変えない)")

    shape_map = _face_map(shape)
    if not 1 <= int(face_index) <= shape_map.Extent():
        raise ValueError(
            f"face index out of range: {face_index} (1..{shape_map.Extent()})"
        )
    face_index = int(face_index)
    face = TopoDS.Face_s(shape_map.FindKey(face_index))

    kind = classify_face(face)
    if kind != "plane":
        raise ValueError(
            f"face {face_index} is {kind}: M1で押し出せるのは平面の面だけ"
            "(曲面は掃引しても壊れた形状になることを実測済み)"
        )

    normal = _face_normal(face)
    _, centroid_before = _face_props(face)
    volume_before = volume(shape)

    prism = BRepPrimAPI_MakePrism(
        face, gp_Vec(normal[0] * distance, normal[1] * distance, normal[2] * distance)
    )
    if not prism.IsDone():
        raise ValueError(
            f"failed to build the prism for face {face_index} (distance={distance})"
        )
    prism_shape = prism.Shape()
    if prism_shape is None or prism_shape.IsNull():
        raise ValueError(f"prism for face {face_index} is empty (distance={distance})")

    # 正なら足す / 負なら削る。負のときプリズムは内側を向いているので、そのまま引けばよい。
    builder = (
        BRepAlgoAPI_Fuse(shape, prism_shape)
        if distance > 0
        else BRepAlgoAPI_Cut(shape, prism_shape)
    )
    builder.Build()
    operation = "fuse" if distance > 0 else "cut"
    if not builder.IsDone():
        raise ValueError(f"{operation} failed for face {face_index} (distance={distance})")
    result = builder.Shape()
    if result is None or result.IsNull():
        raise ValueError(f"{operation} produced an empty shape (face {face_index})")

    volume_result = volume(result)
    if volume_result <= 0:
        raise ValueError(
            f"{operation} left nothing solid (volume={volume_result}, face {face_index})"
        )
    if abs(volume_result - volume_before) <= 1e-9 * max(1.0, abs(volume_before)):
        # OCCTは「成功したが何も起きていない」を返せる。ここで止めないと静かに空振りする。
        raise ValueError(
            f"{operation} did not change the shape "
            f"(volume {volume_before} -> {volume_result}, face {face_index}, distance={distance})"
        )

    final, history, unified = _unify_same_domain(result, volume_result)
    if not BRepCheck_Analyzer(final).IsValid():
        # ここまでの検査を全部通っても壊れていることがある(曲面の掃引で実測)。最後の関門。
        raise ValueError(
            f"{operation} produced an invalid shape "
            f"(BRepCheck, face {face_index}, distance={distance})"
        )
    final_map = _face_map(final)

    successors: dict = {}
    for index in range(1, shape_map.Extent() + 1):
        old_face = shape_map.FindKey(index)
        candidates = _shape_list(builder, "Modified", old_face)
        if not candidates and not _is_removed(builder, old_face):
            candidates = [old_face]  # ブーリアンが触らなかった面は同じ形状として残る
        resolved: List[int] = []
        for candidate in candidates:
            for survivor in _through_history(history, candidate):
                position = final_map.FindIndex(survivor)
                if position > 0 and position not in resolved:
                    resolved.append(position)
        successors[str(index)] = resolved

    mapped = {position for positions in successors.values() for position in positions}
    provenance = {
        "verb": "extrude_face",
        "face_index": face_index,
        "distance": distance,
        "unified": unified,
        "face_map": successors,
        "deleted": [int(key) for key, value in successors.items() if not value],
        "new_faces": [i for i in range(1, final_map.Extent() + 1) if i not in mapped],
        "after_face_index": None,
    }

    after_face_index = _find_translated_face(
        final_map,
        target=(
            centroid_before[0] + normal[0] * distance,
            centroid_before[1] + normal[1] * distance,
            centroid_before[2] + normal[2] * distance,
        ),
        normal=normal,
        scale=abs(distance),
    )
    provenance["after_face_index"] = after_face_index

    return {
        "shape": final,
        "unified": unified,
        "face_index": face_index,
        "distance": distance,
        "normal": list(normal),
        "after_face_index": after_face_index,
        "volume_before": volume_before,
        "volume_after": volume(final),
        "provenance": provenance,
    }


# --------------------------------------------------------------------------------------
# M2: スケッチ平面・領域検出・領域押し出し
# --------------------------------------------------------------------------------------
def require_planar_face(shape: "TopoDS_Shape", face_index: int) -> None:
    """面 `face_index` が平面でなければ ValueError(スケッチを貼れない)。"""
    face = face_of(shape, face_index)
    kind = classify_face(face)
    if kind != "plane":
        raise ValueError(
            f"face {face_index} is {kind}: スケッチを貼れるのは平面の面だけ"
        )


def face_of(shape: "TopoDS_Shape", face_index: int) -> Any:
    """形状の1始まり添字の面を返す。範囲外は ValueError。"""
    shape_map = _face_map(shape)
    if not 1 <= int(face_index) <= shape_map.Extent():
        raise ValueError(
            f"face index out of range: {face_index} (1..{shape_map.Extent()})"
        )
    return TopoDS.Face_s(shape_map.FindKey(int(face_index)))


def sketch_plane_normal(plane: dict, face: Any = None) -> Tuple[float, float, float]:
    """スケッチ平面の法線(単位)。押し出し方向の正の向き。"""
    ax3 = _ax3_from_plane(plane, face=face)
    direction = ax3.Direction()
    return (direction.X(), direction.Y(), direction.Z())


def sketch_plane_frame(plane: dict, face: Any = None) -> dict:
    """スケッチ平面の3Dの枠(原点と2軸と法線)を素の数値で返す(C・2026-08-13)。

    戻り値: {"origin": [x,y,z], "u": [..], "v": [..], "normal": [..]}

    **スケッチ平面上の (u, v) と3D座標を行き来する規則そのもの**である
    (`p = origin + u*u_axis + v*v_axis`)。アプリ層はこれを受け取って、
    画面のクリックを (u, v) へ落とし、図形を3Dへ描く。同じ計算を
    アプリ層が独自に持つと、基準平面3面の軸の取り方と面に貼ったスケッチの
    `ZReverse` の扱いが必ずずれるので、**枠はコアが配る**。
    """
    ax3 = _ax3_from_plane(plane, face=face)
    origin = ax3.Location()
    xdir = ax3.XDirection()
    ydir = ax3.YDirection()
    normal = ax3.Direction()
    return {
        "origin": [origin.X(), origin.Y(), origin.Z()],
        "u": [xdir.X(), xdir.Y(), xdir.Z()],
        "v": [ydir.X(), ydir.Y(), ydir.Z()],
        "normal": [normal.X(), normal.Y(), normal.Z()],
    }


def sketch_regions(
    plane: dict, entities: list, face: Any = None
) -> List[dict]:
    """スケッチの閉領域を返す。

    戻り値: [{"index": int, "area": float, "face": <TopoDS_Face>, "centroid": [x,y,z]}, ...]
    添字は1始まり。順序は面積の降順、同値なら重心の (u, v) の昇順(決定的)。
    """
    if not entities:
        return []

    ax3 = _ax3_from_plane(plane, face=face)
    edges = []
    for entity in entities:
        edges.extend(_entity_edges(ax3, entity))
    if not edges:
        return []

    u_min, u_max, v_min, v_max = _entities_bbox_2d(entities)
    diagonal = math.hypot(u_max - u_min, v_max - v_min)
    margin = max(10.0, diagonal)
    aux = _plane_face(
        ax3,
        u_min - margin,
        u_max + margin,
        v_min - margin,
        v_max + margin,
    )

    arguments = TopTools_ListOfShape()
    arguments.Append(aux)
    tools = TopTools_ListOfShape()
    for edge in edges:
        tools.Append(edge)

    splitter = BRepAlgoAPI_Splitter()
    splitter.SetArguments(arguments)
    splitter.SetTools(tools)
    splitter.Build()
    if not splitter.IsDone():
        raise ValueError("BRepAlgoAPI_Splitter failed (IsDone is false)")
    result = splitter.Shape()
    if result is None or result.IsNull():
        raise ValueError("BRepAlgoAPI_Splitter produced an empty shape")

    tol = 1.0e-6 * max(1.0, diagonal)
    kept: List[tuple] = []
    result_map = _face_map(result)
    for index in range(1, result_map.Extent() + 1):
        region_face = TopoDS.Face_s(result_map.FindKey(index))
        if not _face_vertices_inside_bbox_2d(
            region_face, ax3, u_min, u_max, v_min, v_max, tol
        ):
            continue
        area, centroid = _face_props(region_face)
        if area <= 0:
            continue
        u, v = _to_uv(ax3, centroid[0], centroid[1], centroid[2])
        # 第1キーは**丸めた**面積。生の面積で比べると、幾何的に等しい2領域でも
        # 1e-16級の積分残差で先に決着してしまい、docstringが約束するタイブレーク
        # (重心の u,v)が事実上発火しない(2026-08-11のQAが実測: y=0で割った半円が
        # 628.3185307179587 と ...584 になる)。順序は選択集合が指す先そのものなので、
        # 残差ではなく座標で決まるようにする。
        kept.append((-round(area, 6), u, v, area, centroid, region_face))

    kept.sort(key=lambda item: (item[0], item[1], item[2]))
    regions: List[dict] = []
    for position, item in enumerate(kept, start=1):
        _neg_area, _u, _v, area, centroid, region_face = item
        regions.append(
            {
                "index": position,
                "area": float(area),
                "face": region_face,
                "centroid": [float(centroid[0]), float(centroid[1]), float(centroid[2])],
            }
        )
    return regions


def extrude_regions(
    faces: Sequence[Any],
    distance: float,
    mode: str,
    normal: Sequence[float],
    target_shape: Any = None,
) -> dict:
    """領域の面群を法線方向へ押し出し、join/cut した形状を返す。

    戻り値: {"shape": TopoDS_Shape, "volume_before": float | None, "volume_after": float}
    """
    if not faces:
        raise ValueError("extrude_regions requires at least one face")
    distance = float(distance)
    if distance == 0.0:
        raise ValueError("distance must not be 0 (押し出し量0は形状を変えない)")
    if mode not in ("join", "cut"):
        raise ValueError(f"unknown extrude mode: {mode!r}")
    if mode == "cut" and (target_shape is None or target_shape.IsNull()):
        raise ValueError("cut requires a target shape")

    nx, ny, nz = float(normal[0]), float(normal[1]), float(normal[2])
    offset = gp_Vec(nx * distance, ny * distance, nz * distance)

    prisms: List[TopoDS_Shape] = []
    for face in faces:
        prism = BRepPrimAPI_MakePrism(TopoDS.Face_s(face), offset)
        if not prism.IsDone():
            raise ValueError("failed to build a prism from a region face")
        prism_shape = prism.Shape()
        if prism_shape is None or prism_shape.IsNull():
            raise ValueError("prism from a region face is empty")
        # 押し出し方向が面の平面内に寝ていると**体積0の退化ソリッド**ができる。
        # `IsDone()` も `IsNull()` も `BRepCheck_Analyzer` も全部通ってしまうので
        # (2026-08-11のQAで実測)、体積を直接見るのがここでの唯一の関門。
        if volume(prism_shape) <= 0:
            raise ValueError(
                "prism from a region face has no volume "
                "(押し出し方向が領域の平面に寝ている)"
            )
        prisms.append(prism_shape)

    combined = prisms[0]
    for other in prisms[1:]:
        fused = BRepAlgoAPI_Fuse(combined, other)
        fused.Build()
        if not fused.IsDone():
            raise ValueError("failed to fuse region prisms")
        combined = fused.Shape()
        if combined is None or combined.IsNull():
            raise ValueError("fusing region prisms produced an empty shape")

    volume_before: Optional[float] = None
    if mode == "join":
        if target_shape is None or target_shape.IsNull():
            result = combined
        else:
            volume_before = volume(target_shape)
            builder = BRepAlgoAPI_Fuse(target_shape, combined)
            builder.Build()
            if not builder.IsDone():
                raise ValueError("join (fuse) failed")
            result = builder.Shape()
            operation = "fuse"
    else:
        volume_before = volume(target_shape)
        builder = BRepAlgoAPI_Cut(target_shape, combined)
        builder.Build()
        if not builder.IsDone():
            raise ValueError("cut failed")
        result = builder.Shape()
        operation = "cut"

    if result is None or result.IsNull():
        raise ValueError(f"{mode} produced an empty shape")

    volume_after = volume(result)
    if volume_after <= 0:
        raise ValueError(f"{mode} left nothing solid (volume={volume_after})")
    if volume_before is not None:
        if abs(volume_after - volume_before) <= 1e-9 * max(1.0, abs(volume_before)):
            raise ValueError(
                f"{operation} did not change the shape "
                f"(volume {volume_before} -> {volume_after})"
            )
    if not BRepCheck_Analyzer(result).IsValid():
        raise ValueError(f"{mode} produced an invalid shape (BRepCheck)")

    return {
        "shape": result,
        "volume_before": volume_before,
        "volume_after": volume_after,
    }


def _ax3_from_plane(plane: dict, face: Any = None) -> "gp_Ax3":
    """PlaneRef から gp_Ax3 を作る。面参照で平面でなければ ValueError。"""
    kind = plane.get("kind")
    if kind == "base":
        name = plane.get("name")
        if name not in _BASE_PLANE_AXES:
            raise ValueError(f"unknown base plane: {name!r}")
        origin, normal, xdir = _BASE_PLANE_AXES[name]
        return gp_Ax3(gp_Pnt(*origin), gp_Dir(*normal), gp_Dir(*xdir))
    if kind == "face":
        if face is None:
            raise ValueError("face plane requires a TopoDS_Face")
        return _ax3_from_face(face)
    raise ValueError(f"invalid sketch plane: {plane!r}")


def _ax3_from_face(face: Any) -> "gp_Ax3":
    """平面の面から gp_Ax3 を取る。平面でなければ ValueError。REVERSED なら法線を反転。"""
    face = TopoDS.Face_s(face)
    adaptor = BRepAdaptor_Surface(face)
    if adaptor.GetType() != GeomAbs_SurfaceType.GeomAbs_Plane:
        raise ValueError(
            f"face is {classify_face(face)}: スケッチ平面にできるのは平面だけ"
        )
    # `Position()` は既に gp_Ax3 だが、OCP には gp_Ax3(gp_Ax3) のコンストラクタが無いので
    # 位置・法線・X方向から組み直す。反転面は `ZReverse`(OCCTの綴り。`ReverseZ` は存在しない)
    # で法線だけを裏返す — XDirection/YDirection は変わらないので2D座標の意味は保たれる。
    # 2026-08-11のQAで、ここが `TypeError`/`AttributeError` になり
    # 「面に貼ったスケッチ」の経路が一度も動かないまま緑だったのを実測して直した。
    position = adaptor.Plane().Position()
    ax3 = gp_Ax3(position.Location(), position.Direction(), position.XDirection())
    if face.Orientation() == TopAbs_Orientation.TopAbs_REVERSED:
        ax3.ZReverse()
    return ax3


def _to_pnt(ax3: "gp_Ax3", u: float, v: float) -> "gp_Pnt":
    """スケッチ平面上の (u, v) を 3D 点へ。"""
    origin = ax3.Location()
    xdir = ax3.XDirection()
    ydir = ax3.YDirection()
    return gp_Pnt(
        origin.X() + u * xdir.X() + v * ydir.X(),
        origin.Y() + u * xdir.Y() + v * ydir.Y(),
        origin.Z() + u * xdir.Z() + v * ydir.Z(),
    )


def _to_uv(ax3: "gp_Ax3", x: float, y: float, z: float) -> Tuple[float, float]:
    """3D 点をスケッチ平面の (u, v) へ。"""
    origin = ax3.Location()
    xdir = ax3.XDirection()
    ydir = ax3.YDirection()
    dx = x - origin.X()
    dy = y - origin.Y()
    dz = z - origin.Z()
    u = dx * xdir.X() + dy * xdir.Y() + dz * xdir.Z()
    v = dx * ydir.X() + dy * ydir.Y() + dz * ydir.Z()
    return u, v


def _entity_edges(ax3: "gp_Ax3", entity: dict) -> List[Any]:
    """1図形から 3D の辺リストを作る。"""
    kind = entity["kind"]
    params = entity["params"]
    if kind == "line":
        p0 = _to_pnt(ax3, float(params["x0"]), float(params["y0"]))
        p1 = _to_pnt(ax3, float(params["x1"]), float(params["y1"]))
        maker = BRepBuilderAPI_MakeEdge(p0, p1)
        if not maker.IsDone():
            raise ValueError("failed to build a line edge")
        return [maker.Edge()]
    if kind == "circle":
        center = _to_pnt(ax3, float(params["cx"]), float(params["cy"]))
        radius = float(params["radius"])
        if radius <= 0:
            raise ValueError(f"circle radius must be positive, got {radius}")
        axis = gp_Ax2(center, ax3.Direction(), ax3.XDirection())
        maker = BRepBuilderAPI_MakeEdge(gp_Circ(axis, radius))
        if not maker.IsDone():
            raise ValueError("failed to build a circle edge")
        return [maker.Edge()]
    if kind == "arc":
        center = _to_pnt(ax3, float(params["cx"]), float(params["cy"]))
        radius = float(params["radius"])
        if radius <= 0:
            raise ValueError(f"arc radius must be positive, got {radius}")
        start_deg = float(params["start_deg"])
        end_deg = float(params["end_deg"])
        # 反時計回りに start→end。0度またぎは (end-start)%360 の掃引角で表現する。
        sweep_deg = (end_deg - start_deg) % 360.0
        start_rad = math.radians(start_deg)
        end_rad = start_rad + math.radians(sweep_deg)
        # gp_Ax2(center, N, X): P(U)=O+R*cos(U)*X+R*sin(U)*(N×X) なので
        # U=0 がスケッチX、U増加が反時計回り（右手系）。
        axis = gp_Ax2(center, ax3.Direction(), ax3.XDirection())
        maker = BRepBuilderAPI_MakeEdge(gp_Circ(axis, radius), start_rad, end_rad)
        if not maker.IsDone():
            raise ValueError("failed to build an arc edge")
        return [maker.Edge()]
    if kind == "rectangle":
        x = float(params["x"])
        y = float(params["y"])
        width = float(params["width"])
        height = float(params["height"])
        corners = [
            _to_pnt(ax3, x, y),
            _to_pnt(ax3, x + width, y),
            _to_pnt(ax3, x + width, y + height),
            _to_pnt(ax3, x, y + height),
        ]
        edges = []
        for index in range(4):
            maker = BRepBuilderAPI_MakeEdge(corners[index], corners[(index + 1) % 4])
            if not maker.IsDone():
                raise ValueError("failed to build a rectangle edge")
            edges.append(maker.Edge())
        return edges
    raise ValueError(f"unknown entity kind: {kind!r}")


def _entities_bbox_2d(entities: list) -> Tuple[float, float, float, float]:
    """図形群の 2D バウンディングボックス (u_min, u_max, v_min, v_max)。"""
    u_min = float("inf")
    u_max = float("-inf")
    v_min = float("inf")
    v_max = float("-inf")

    def include(u: float, v: float) -> None:
        nonlocal u_min, u_max, v_min, v_max
        u_min = min(u_min, u)
        u_max = max(u_max, u)
        v_min = min(v_min, v)
        v_max = max(v_max, v)

    for entity in entities:
        kind = entity["kind"]
        params = entity["params"]
        if kind == "line":
            include(float(params["x0"]), float(params["y0"]))
            include(float(params["x1"]), float(params["y1"]))
        elif kind == "circle":
            cx = float(params["cx"])
            cy = float(params["cy"])
            radius = float(params["radius"])
            include(cx - radius, cy - radius)
            include(cx + radius, cy + radius)
        elif kind == "arc":
            # 安全側: 円と同じ外接箱で囲む（補助面が少し大きくなるだけ）。
            cx = float(params["cx"])
            cy = float(params["cy"])
            radius = float(params["radius"])
            include(cx - radius, cy - radius)
            include(cx + radius, cy + radius)
        elif kind == "rectangle":
            x = float(params["x"])
            y = float(params["y"])
            include(x, y)
            include(x + float(params["width"]), y + float(params["height"]))
        else:
            raise ValueError(f"unknown entity kind: {kind!r}")

    if u_min == float("inf"):
        return 0.0, 0.0, 0.0, 0.0
    return u_min, u_max, v_min, v_max


def _plane_face(
    ax3: "gp_Ax3", u0: float, u1: float, v0: float, v1: float
) -> Any:
    """スケッチ平面上の軸平行矩形を TopoDS_Face にする(補助面用)。"""
    corners = [
        _to_pnt(ax3, u0, v0),
        _to_pnt(ax3, u1, v0),
        _to_pnt(ax3, u1, v1),
        _to_pnt(ax3, u0, v1),
    ]
    wire_maker = BRepBuilderAPI_MakeWire()
    for index in range(4):
        edge_maker = BRepBuilderAPI_MakeEdge(corners[index], corners[(index + 1) % 4])
        if not edge_maker.IsDone():
            raise ValueError("failed to build an auxiliary edge")
        wire_maker.Add(edge_maker.Edge())
    if not wire_maker.IsDone():
        raise ValueError("failed to build an auxiliary wire")
    face_maker = BRepBuilderAPI_MakeFace(wire_maker.Wire(), True)
    if not face_maker.IsDone():
        raise ValueError("failed to build an auxiliary face")
    face = face_maker.Face()
    if face is None or face.IsNull():
        raise ValueError("auxiliary face is empty")
    return face


def _face_vertices_inside_bbox_2d(
    face: Any,
    ax3: "gp_Ax3",
    u_min: float,
    u_max: float,
    v_min: float,
    v_max: float,
    tol: float,
) -> bool:
    """面の頂点がすべて 2D bbox(+tol) の内側なら True(補助面の余りを落とす)。"""
    shape_map = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(TopoDS.Face_s(face), TopAbs_ShapeEnum.TopAbs_VERTEX, shape_map)
    if shape_map.Extent() < 1:
        return False
    for index in range(1, shape_map.Extent() + 1):
        point = BRep_Tool.Pnt_s(TopoDS.Vertex_s(shape_map.FindKey(index)))
        u, v = _to_uv(ax3, point.X(), point.Y(), point.Z())
        if not (
            u_min - tol <= u <= u_max + tol and v_min - tol <= v <= v_max + tol
        ):
            return False
    return True


# --------------------------------------------------------------------------------------
# 3verb前倒し(フィレット・面取り・円弧)の幾何 — 2026-08-11。**このタスクではスタブ**
# --------------------------------------------------------------------------------------
#: 辺の種別(`classify_edge` が返す値。面の FACE_KINDS と同じ流儀で、判定はここに固定する)
EDGE_KINDS = ("line", "circle", "other")


def edge_count(shape: "TopoDS_Shape") -> int:
    """重複を除いた辺の数。**この数え方の1始まりの添字が、辺の選択の識別子そのもの**
    (面の `face_count` と同じ規律。`TopExp.MapShapes_s` を使う)。"""
    return _edge_map(shape).Extent()


def classify_edge(edge: Any) -> str:
    """1つの辺を EDGE_KINDS のいずれかへ分類する。

    フィレット・面取りは「どの辺か」を選べないと使えず、辺の選択は
    「まっすぐな辺」「円い辺」の区別が要る(円い辺のフィレットはトーラスを、
    円い辺の面取りは円錐を作る。まっすぐな辺の面取りは平面になる。2026-08-11実測)。
    """
    adaptor = BRepAdaptor_Curve(TopoDS.Edge_s(edge))
    curve_type = adaptor.GetType()
    if curve_type == GeomAbs_CurveType.GeomAbs_Line:
        return "line"
    if curve_type == GeomAbs_CurveType.GeomAbs_Circle:
        return "circle"
    return "other"


def edge_of(shape: "TopoDS_Shape", edge_index: int) -> Any:
    """形状の1始まり添字の辺を返す。範囲外は ValueError(面の `face_of` と同じ流儀)。"""
    return _edge_at(_edge_map(shape), edge_index)


def edge_index_of(shape: "TopoDS_Shape", edge: Any) -> int:
    """辺が形状の何番目か(1始まり)を `IsSame` で返す。無ければ ValueError。

    build123d の列挙順が `_edge_map` と一致する保証は無いので、`query.edges_of_face` は
    形状の同一性で引き当てる(既存の `edge_of` / `edge_count` のふるまいは変えない)。
    """
    shape_map = _edge_map(shape)
    for index in range(1, shape_map.Extent() + 1):
        if shape_map.FindKey(index).IsSame(edge):
            return index
    raise ValueError("edge is not part of the shape")


def tessellate_edges(
    shape: "TopoDS_Shape",
    deviation: float = DEFAULT_DEVIATION,
) -> List[dict]:
    """辺を1始まりの添字つきの**折れ線**にする(D・2026-08-14)。

    `tessellate` の辺版。面がクリックで選べるのは面ごとの三角形を配っているからで、
    **辺も同じように配らないと人は辺を指せない**(それまでの画面に出ていた輪郭線は
    three.js の `EdgesGeometry` による見た目であって、コアの辺の添字とは対応しない)。
    フィレット・面取りは「どの辺か」を選べないと使えないので、ここが無いと
    **その2つのverbは人間側から永久に使えない**(実装計画書§8b D)。

    戻り値(素の値だけ):
        [{"index": int, "kind": "line"|"circle"|"other", "length": float,
          "closed": bool, "points": [x,y,z, x,y,z, ...]}, ...]

    `index` は `edge_count` / `edge_of` / `fillet(edges=[...])` と**同じ数え方**
    (`_edge_map` の1始まり)。ここがずれると「指した辺と違う辺が丸まる」という、
    例外も出ない静かな事故になる —— `query.describe_edges` が build123d の列挙順を
    使わないのと同じ理由で、添字は必ず `_edge_map` から取る。

    折れ線は `GCPnts_QuasiUniformDeflection` で `deviation`(mm)以内に収める
    (面の三角形分割と同じ偏差を渡せば、線が面から浮かない)。直線は2点、円は
    偏差ぶんの分割数になる。**分割に失敗した辺は端点だけの2点で返す**(黙って
    落とすと、その辺だけ画面から消えて「指せない辺」になり原因が分からない)。
    """
    if shape is None or shape.IsNull():
        raise ValueError("cannot tessellate the edges of an empty shape")
    if not deviation > 0:
        raise ValueError(f"deviation must be positive (mm), got {deviation!r}")

    shape_map = _edge_map(shape)
    described: List[dict] = []
    for index in range(1, shape_map.Extent() + 1):
        edge = TopoDS.Edge_s(shape_map.FindKey(index))
        adaptor = BRepAdaptor_Curve(edge)
        points: List[float] = []
        sampler = GCPnts_QuasiUniformDeflection(adaptor, float(deviation))
        if sampler.IsDone() and sampler.NbPoints() >= 2:
            for number in range(1, sampler.NbPoints() + 1):
                point = sampler.Value(number)
                points.extend([float(point.X()), float(point.Y()), float(point.Z())])
        else:
            for parameter in (adaptor.FirstParameter(), adaptor.LastParameter()):
                point = adaptor.Value(parameter)
                points.extend([float(point.X()), float(point.Y()), float(point.Z())])
        described.append(
            {
                "index": index,
                "kind": classify_edge(edge),
                "length": float(GCPnts_AbscissaPoint.Length_s(adaptor)),
                "closed": bool(edge.Closed()),
                "points": points,
            }
        )
    return described


def _edge_at(shape_map: "TopTools_IndexedMapOfShape", edge_index: int) -> Any:
    """既に作った辺マップから引く(まとめて引くときにマップを作り直さないため)。"""
    if not 1 <= int(edge_index) <= shape_map.Extent():
        raise ValueError(
            f"edge index out of range: {edge_index} (1..{shape_map.Extent()})"
        )
    return TopoDS.Edge_s(shape_map.FindKey(int(edge_index)))


def fillet_edges(shape: "TopoDS_Shape", radii: dict) -> dict:
    """辺(1始まりの添字)を丸めた**新しい形状**と由来の射影を返す。

    `radii` は `{辺の添字: 半径}`。入力の `shape` は変更しない。

    **必ず `IsDone()` を確認する**。OCCTのフィレットは例外を投げずに失敗し、しかも
    半径に対して単調でない(2026-08-11に20mm立方体の全辺で実測: r=3.0 成功 / r=10.0
    `IsDone()=False` / r=10.5 成功 / r=30.0 `IsDone()=False`)。失敗は例外にして
    呼び出し側が操作ごと捨てられるようにする。

    戻り値(`"shape"` 以外はJSON化できる素の値):
        {"shape": <TopoDS_Shape>, "provenance": {...}, "volume_before": float,
         "volume_after": float}
    """
    return _blend_edges(shape, radii, verb="fillet")


def chamfer_edges(shape: "TopoDS_Shape", distances: dict) -> dict:
    """辺(1始まりの添字)を面取りした新しい形状と由来の射影を返す。

    `distances` は `{辺の添字: 距離}`。`fillet_edges` と同じ契約(`IsDone()` の確認・
    入力を変更しない・戻り値の形)。`BRepFilletAPI_MakeChamfer` は同じ系統なので、
    **黙って失敗する性質も同じ**。
    """
    return _blend_edges(shape, distances, verb="chamfer")


def _blend_edges(shape: "TopoDS_Shape", values: dict, verb: str) -> dict:
    """フィレットと面取りの共通実装(違うのはビルダの型だけ)。

    **同じ系統(`BRepFilletAPI_*`)なので失敗の仕方も由来の取り出し方も同じ**であり、
    2つに書き分けると片方だけ検査が抜ける(実際、面取りは「まっすぐな辺なら平面が増えるだけ」
    という無害に見える操作なので、検査を省きたくなる誘惑がある)。
    """
    if shape is None or shape.IsNull():
        raise ValueError(f"cannot {verb} an empty shape")
    if not values:
        raise ValueError(f"{verb} requires at least one edge")

    edge_map = _edge_map(shape)
    volume_before = volume(shape)
    maker = (
        BRepFilletAPI_MakeFillet(shape)
        if verb == "fillet"
        else BRepFilletAPI_MakeChamfer(shape)
    )
    edge_indices: List[int] = []
    for raw_index, raw_value in values.items():
        index = int(raw_index)
        maker.Add(float(raw_value), _edge_at(edge_map, index))
        edge_indices.append(index)

    try:
        maker.Build()
    except Exception as error:  # noqa: BLE001  OCCTは例外でも失敗しうる
        raise ValueError(f"{verb} failed while building (edges {edge_indices}): {error}") from error
    if not maker.IsDone():
        # **ここが最重要**: OCCTは例外を投げずに失敗する。半径/距離の範囲検査では代用できない
        raise ValueError(
            f"{verb} failed (IsDone is false) for edges {edge_indices} with {values}"
        )

    final = maker.Shape()
    if final is None or final.IsNull():
        raise ValueError(f"{verb} produced an empty shape (edges {edge_indices})")
    volume_after = volume(final)
    if volume_after <= 0:
        raise ValueError(f"{verb} left nothing solid (volume={volume_after})")
    if not BRepCheck_Analyzer(final).IsValid():
        raise ValueError(f"{verb} produced an invalid shape (BRepCheck, edges {edge_indices})")

    return {
        "shape": final,
        "provenance": _blend_provenance(maker, shape, final, verb, edge_indices),
        "volume_before": float(volume_before),
        "volume_after": float(volume_after),
    }


def _blend_provenance(
    maker: Any,
    shape: "TopoDS_Shape",
    final: "TopoDS_Shape",
    verb: str,
    edge_indices: List[int],
) -> dict:
    """由来を**正規化アダプタ経由で**回収し、JSON化できる射影へ落とす。

    フィレット系は `History()` を持たず `Modified()` / `Generated()` / `IsDeleted()` だけを
    持つ(2026-08-11に `BRepFilletAPI_MakeFillet` で実測)。ブーリアンは `History()` を持つので
    取り出し方が違い、**その差を吸収するのが `normalize_provenance` の存在理由**である。
    ここでアダプタを通さずに `maker.Modified()` を直接呼ぶと、verb ごとに同じ分岐を写経する
    ことになる(§6予防2)。

    アダプタの戻りは `TopoDS_Shape` を含む**メモリ上の形**なので、そのままでは
    `OpLog.to_json()` で落ちる。ここで添字の射影に変換する(`extrude_face_on_shape` と同じ形)。
    """
    normalized = normalize_provenance(maker, [shape]) or {}
    modified = {}
    for entry in normalized.get("modified", []):
        modified[entry["input"]] = entry["results"]
    deleted_inputs = normalized.get("deleted", [])

    shape_map = _face_map(shape)
    final_map = _face_map(final)
    successors: dict = {}
    for index in range(1, shape_map.Extent() + 1):
        old_face = shape_map.FindKey(index)
        candidates = _lookup_by_identity(modified, old_face)
        if candidates is None:
            # アダプタが「触っていない」と言った面は、そのまま残っているはず
            candidates = [] if _contains_identity(deleted_inputs, old_face) else [old_face]
        resolved: List[int] = []
        for candidate in candidates:
            position = final_map.FindIndex(candidate)
            if position > 0 and position not in resolved:
                resolved.append(position)
        successors[str(index)] = resolved

    mapped = {position for positions in successors.values() for position in positions}
    return {
        "verb": verb,
        "edges": list(edge_indices),
        "face_map": successors,
        "deleted": [int(key) for key, value in successors.items() if not value],
        "new_faces": [i for i in range(1, final_map.Extent() + 1) if i not in mapped],
    }


def _lookup_by_identity(mapping: dict, key: Any) -> Optional[List[Any]]:
    """`TopoDS_Shape` は辞書の鍵として同一性で引けないので `IsSame` で探す。"""
    for candidate, results in mapping.items():
        if candidate.IsSame(key):
            return results
    return None


def _contains_identity(shapes: Sequence[Any], key: Any) -> bool:
    return any(candidate.IsSame(key) for candidate in shapes)


def _edge_map(shape: "TopoDS_Shape") -> "TopTools_IndexedMapOfShape":
    """辺の重複除去済みマップ(1始まり)。添字が選択の識別子になるので数え方はここに固定する。"""
    if shape is None or shape.IsNull():
        raise ValueError("cannot map the edges of an empty shape")
    shape_map = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_ShapeEnum.TopAbs_EDGE, shape_map)
    return shape_map


# --------------------------------------------------------------------------------------
# M3.5: ソリッド分解・複数形状の書き出し(2026-08-11)
# --------------------------------------------------------------------------------------
def split_solids(shape: "TopoDS_Shape") -> List["TopoDS_Shape"]:
    """形状をSOLIDごとに分解する(M3.5)。

    `TopExp.MapShapes_s(shape, TopAbs_SOLID)` で重複除去済みのSOLID群を取り出し、
    各SOLIDを個別の TopoDS_Shape として返す。**SOLIDが1つも無い形状(裸の面・シェルのみ)は
    形状全体を1要素のリストにして返す**(0個のBodyを黙って作らない。io_step.pyの契約)。

    列挙順は `TopExp.MapShapes_s` の走査順(同じ形状に対して決定的)。
    """
    if shape is None or shape.IsNull():
        raise ValueError("cannot split an empty shape")
    shape_map = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_ShapeEnum.TopAbs_SOLID, shape_map)
    if shape_map.Extent() == 0:
        return [shape]
    return [
        TopoDS.Solid_s(shape_map.FindKey(index))
        for index in range(1, shape_map.Extent() + 1)
    ]


def make_compound(shapes: Sequence["TopoDS_Shape"]) -> "TopoDS_Shape":
    """複数の形状を1つの TopoDS_Compound へまとめる(複数Bodyを1つのSTEP/STLへ書き出すため)。"""
    if not shapes:
        raise ValueError("cannot build a compound from an empty sequence of shapes")
    builder = BRep_Builder()
    compound = TopoDS_Compound()
    builder.MakeCompound(compound)
    for shape in shapes:
        builder.Add(compound, shape)
    return compound


# --------------------------------------------------------------------------------------
# M1の内部ヘルパ
# --------------------------------------------------------------------------------------
def _face_map(shape: "TopoDS_Shape") -> "TopTools_IndexedMapOfShape":
    """面の重複除去済みマップ(1始まり)。添字が選択の識別子になるので数え方はここに固定する。"""
    if shape is None or shape.IsNull():
        raise ValueError("cannot map the faces of an empty shape")
    shape_map = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, TopAbs_ShapeEnum.TopAbs_FACE, shape_map)
    return shape_map


def _face_normal(face: Any) -> tuple:
    """面の中央での**外向き**単位法線。`BRepGProp_Face` は面の向きを見るので反転面でも外を向く
    (20mm立方体の6面で実測: x=0の面が (-1,0,0)、x=20の面が (1,0,0))。"""
    gface = BRepGProp_Face(TopoDS.Face_s(face))
    u_min, u_max, v_min, v_max = gface.Bounds()
    point = gp_Pnt()
    normal = gp_Vec()
    gface.Normal((u_min + u_max) / 2.0, (v_min + v_max) / 2.0, point, normal)
    if normal.Magnitude() <= 0:
        raise ValueError("face normal is degenerate (magnitude 0)")
    normal.Normalize()
    return (normal.X(), normal.Y(), normal.Z())


def _face_props(face: Any) -> tuple:
    """(面積, 重心)。`BRepGProp.SurfaceProperties_s` の既定積分を使う(volume と同じ流儀)。"""
    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(TopoDS.Face_s(face), props)
    centre = props.CentreOfMass()
    return props.Mass(), (centre.X(), centre.Y(), centre.Z())


def _face_triangles(face: Any) -> tuple:
    """1面の三角形を (positions, indices) で返す。indices はこの面の中での0始まり。

    節点は面のローカル座標なので `TopLoc_Location` の変換を必ず適用する(単位変換のかかった
    STEPや配置された部品で位置がずれる)。巻き方向は `TopAbs_REVERSED` のとき入れ替える。
    """
    location = TopLoc_Location()
    triangulation = BRep_Tool.Triangulation_s(TopoDS.Face_s(face), location)
    if triangulation is None:
        return [], []

    is_identity = location.IsIdentity()
    transformation = location.Transformation()
    positions: List[float] = []
    for node_index in range(1, triangulation.NbNodes() + 1):
        node = triangulation.Node(node_index)
        if not is_identity:
            node = node.Transformed(transformation)
        positions.extend((node.X(), node.Y(), node.Z()))

    is_reversed = face.Orientation() == TopAbs_Orientation.TopAbs_REVERSED
    indices: List[int] = []
    for triangle_index in range(1, triangulation.NbTriangles() + 1):
        first, second, third = triangulation.Triangle(triangle_index).Get()
        if is_reversed:
            second, third = third, second
        indices.extend((first - 1, second - 1, third - 1))
    return positions, indices


def _bounding_box(faces: List[dict]) -> dict:
    """三角形の節点から境界箱を作る(アプリ層のカメラ合わせ用)。面が無ければ原点の退化した箱。"""
    lows = [float("inf")] * 3
    highs = [float("-inf")] * 3
    for face in faces:
        positions = face["positions"]
        for offset in range(0, len(positions), 3):
            for axis in range(3):
                value = positions[offset + axis]
                lows[axis] = min(lows[axis], value)
                highs[axis] = max(highs[axis], value)
    if any(low == float("inf") for low in lows):
        lows, highs = [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]
    diagonal = math.dist(lows, highs)
    return {
        "min": lows,
        "max": highs,
        "center": [(lows[i] + highs[i]) / 2.0 for i in range(3)],
        "diagonal": diagonal,
    }


def _unify_same_domain(shape: "TopoDS_Shape", volume_before: float) -> tuple:
    """同一平面の面を統合した形状・その履歴・採用したかを返す。

    採用しない場合は**統合を一度も通していない元の形状**と履歴 None を返す
    (由来の射影はブーリアンの結果へ直接張る)。

    **`ShapeUpgrade_UnifySameDomain` は入力の形状を壊す**(2026-08-10実測。最重要):

        fuse の結果        -> IsValid()=True
        unify.Build() した後、**同じ fuse の結果を測り直すと** IsValid()=False
        unify の前に取った BRepBuilderAPI_Copy は unify 後も IsValid()=True

    つまり「統合してみて、ダメだったら統合前に戻る」を素直に書くと、**戻り先が既に壊れている**。
    そのため必ず**コピーで試してから**本番を行う:

    1. コピーに対して統合を試し、(a)妥当 (b)体積が変わらない (c)**面が実際に減る** を確かめる
    2. 3つとも満たすときだけ、元の形状に対して本番の統合を行い、その出力と履歴を返す
       (由来の射影は「ブーリアンの結果の部分形状」を鍵に引くので、本番は必ず元の形状に対して
       行う必要がある。コピーに対して統合すると部分形状の同一性が切れて射影が壊れる)
    3. 満たさないときは元の形状をそのまま返す(統合を通していないので無傷)

    (c)を条件に入れているのは、面が減らない統合は利得ゼロで**入力を壊す危険だけが残る**ため。
    `shelf_attachment_GD.step` の面36・43を+2mmしたときが実際にこれで、面数は57のまま変わらず、
    統合後の形状だけが壊れていた。20mm立方体の上面+10では 10面 -> 6面 に減るので採用される。
    """
    trial = ShapeUpgrade_UnifySameDomain(
        BRepBuilderAPI_Copy(shape).Shape(), True, True, False
    )
    trial.Build()
    candidate = trial.Shape()
    if candidate is None or candidate.IsNull():
        return shape, None, False
    if abs(volume(candidate) - volume_before) > 1e-6 * max(1.0, abs(volume_before)):
        return shape, None, False
    if not BRepCheck_Analyzer(candidate).IsValid():
        return shape, None, False
    if _face_map(candidate).Extent() >= _face_map(shape).Extent():
        return shape, None, False

    unify = ShapeUpgrade_UnifySameDomain(shape, True, True, False)
    unify.Build()
    unified = unify.Shape()
    if unified is None or unified.IsNull() or not BRepCheck_Analyzer(unified).IsValid():
        # 試行では通ったのに本番で壊れた場合。入力(shape)はもう壊れているので操作ごと捨てる。
        raise ValueError("ShapeUpgrade_UnifySameDomain が試行と異なる結果を返した")
    return unified, unify.History(), True


def _through_history(history: Any, sub: "TopoDS_Shape") -> List["TopoDS_Shape"]:
    """`BRepTools_History` を1段たどる(消えたら空、触られていなければ自分自身)。"""
    if history is None:
        return [sub]
    modified = _shape_list(history, "Modified", sub)
    if modified:
        return modified
    if _is_removed(history, sub):
        return []
    return [sub]


def _find_translated_face(
    shape_map: "TopTools_IndexedMapOfShape",
    target: tuple,
    normal: tuple,
    scale: float,
) -> Optional[int]:
    """重心が `target`・法線が `normal` と同じ向きの面の添字(無ければNone)。

    押し出した面が新形状のどれになったかを幾何で突き合わせる。由来マップ(`Modified`)は
    ブーリアンでは空を返すことが多く(実測: Fuseは押し出した面を `IsDeleted` にするだけ)、
    「押し出した面を選択したまま続けて押す」という計測の手続きには足りないため。
    """
    tolerance = _MATCH_TOL * max(1.0, abs(scale), *(abs(value) for value in target))
    best: Optional[tuple] = None
    for index in range(1, shape_map.Extent() + 1):
        face = TopoDS.Face_s(shape_map.FindKey(index))
        candidate_normal = _face_normal(face)
        alignment = sum(candidate_normal[axis] * normal[axis] for axis in range(3))
        if alignment < 1.0 - _MATCH_TOL:
            continue
        _, centroid = _face_props(face)
        distance = math.dist(centroid, target)
        if distance <= tolerance and (best is None or distance < best[0]):
            best = (distance, index)
    return None if best is None else best[1]
