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

**ocp_tessellate は使わない**(実装計画書§4は使うと書いているが、この venv に入っておらず、
かつ面ピッキングには面ごとの三角形グループが要るので自前実装の方が制御できる。2026-08-10判断)。

戻り値はいずれも **JSON化できる素の値(float/int/str/list/dict)だけ**にする。OCPのオブジェクトを
kernel の外へ出さないための規律で、境界テスト(importの静的検査)では捕まえられない側の約束。
唯一の例外は `extrude_face_on_shape` の戻り値の `"shape"`(新しい TopoDS_Shape そのもの。
document.Body.shape に入る値であり、これは M0 から `Any` として通っている)。
"""
from __future__ import annotations

import math
import os
from typing import Any, Iterable, List, Optional

from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepGProp import BRepGProp, BRepGProp_Face
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
from OCP.BRepTools import BRepTools
from OCP.GeomAbs import GeomAbs_SurfaceType
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
from OCP.TopoDS import TopoDS, TopoDS_Shape
from OCP.TopTools import TopTools_IndexedMapOfShape
from OCP.gp import gp_Pnt, gp_Vec

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
