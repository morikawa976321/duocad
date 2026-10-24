"""面の削除と面のオフセットの幾何(OCPを触るカーネル層)。方式・実測・罠は docs/FACE_EDIT.md。

- 削除(`delete_faces`): `BOPAlgo_RemoveFeatures`。消した面の周りの面を、それぞれ自分の曲面のまま延ばして閉じ直す。
  OCCT が延ばせない曲面(押し出し・回転・オフセット)の隣は、消す間だけ同じ形の B-spline の面に置き換え、消した後に元の曲面へ戻す。
- オフセット(`offset_faces`): `BRepOffset_MakeOffset` を全体0・選んだ面だけ距離 d で。選んだ面に接線でつながる
  一定半径の丸めは、外して(削除と同じ仕組み)、尖った形で動かし、同じ半径で丸め直す(丸めが付いてくる)。

**OCCT の成功を信じない**: どちらも、閉じられない形や意図と違う形を、例外も `BRepCheck` 落ちも無く返す。結果を面ごとに
元の面と照らし合わせる関門を通ったときだけ返し、通らなければ `ValueError`(文書には何も触らない)。

計算は常駐の子プロセス(`kernel_worker`)で行い、`compute` は子の中で呼ばれる。依存は kernel_face_edit → kernel の一方向。
戻り値の規律は kernel と同じ(`"shape"` 以外は JSON 化できる素の値)。
"""
from __future__ import annotations

import math
from typing import Any, List, Optional, Sequence, Tuple

from OCP.BOPAlgo import BOPAlgo_RemoveFeatures
from OCP.BRep import BRep_Builder, BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy, BRepBuilderAPI_MakeSolid
from OCP.BRepCheck import BRepCheck_Shell, BRepCheck_Status
from OCP.BRepClass import BRepClass_FaceClassifier
from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
from OCP.BRepLib import BRepLib
from OCP.BRepLProp import BRepLProp_SLProps
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.BRepOffset import BRepOffset_MakeOffset, BRepOffset_Mode
from OCP.BRepTools import BRepTools, BRepTools_ReShape
from OCP.Bnd import Bnd_Box
from OCP.BRepBndLib import BRepBndLib
from OCP.ChFi3d import ChFi3d
from OCP.Geom import Geom_OffsetSurface, Geom_RectangularTrimmedSurface
from OCP.GeomAbs import GeomAbs_CurveType, GeomAbs_JoinType, GeomAbs_Shape, GeomAbs_SurfaceType
from OCP.GeomAPI import GeomAPI_ProjectPointOnSurf
from OCP.GeomConvert import GeomConvert, GeomConvert_ApproxSurface
from OCP.GeomLProp import GeomLProp_SLProps
from OCP.gp import gp_Pnt, gp_Pnt2d
from OCP.Message import Message_Gravity
from OCP.Precision import Precision
from OCP.ShapeFix import ShapeFix_Face
from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain
from OCP.TopAbs import TopAbs_Orientation, TopAbs_ShapeEnum, TopAbs_State
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS, TopoDS_Face, TopoDS_Iterator
from OCP.TopTools import TopTools_ListOfShape

from . import kernel, kernel_worker

from . import (
    brep_checks as _brep_checks,
    brep_geometry as _brep_geometry,
    brep_history as _brep_history,
    brep_topology as _brep_topology,
)


VERB_DELETE = "delete_faces"
VERB_OFFSET = "offset_faces"

#: 面積がこれ未満の面を「消えたのに位相に残った面」とみなす[mm²]。置いた値(合成形状で MakeOffset が残した面積0の面を拾う)。
#: 退化直前の細長い面は拾えず、元からある正当な微小面を断りうる。
MIN_FACE_AREA = 1e-6
#: 曲面の定義を比べる許容差[mm]と、向きの許容差(|cos| の 1 からのずれ)。置いた値
LINEAR_TOL = 1e-6
ANGULAR_TOL = 1e-9
#: 「残した面の元の範囲」を確かめる標本点の数
_REGION_SAMPLES = 49
#: `BRepLib::ExtendFace` が延ばさずにそのまま返す曲面(解析曲面でも `Geom_BoundedSurface` でもない。OCCT 7.9.3 の General case)。
#: 消す面の隣にあると RemoveFeatures が隙間を閉じられない(docs/FACE_EDIT.md「OCCT の罠」11)
_UNEXTENDABLE = (
    GeomAbs_SurfaceType.GeomAbs_SurfaceOfExtrusion,
    GeomAbs_SurfaceType.GeomAbs_SurfaceOfRevolution,
    GeomAbs_SurfaceType.GeomAbs_OffsetSurface,
)
#: 曲がりの半径を調べる格子の1辺の等間隔の点数(UV の範囲の両端を含む。B-spline の節点の区間ごとの点が加わる)と、最小を詰め直す厳しい標本の数
_CURVATURE_SAMPLES = 33
_CURVATURE_REFINED = 8
#: 解析曲面でない面をずらすとき、`1 + 曲率 × 距離` がこれ未満なら断る。置いた値(詰め直しても届かない局所的な最小の分の余裕)。
#: 0 で尖る: 30×15 の楕円柱(曲がりの半径の最小 7.5)を内へ 7.51 ずらした形は BRepCheck を通った(実測)
_CUSP_MARGIN = 0.01
#: B-spline の丸めを見分けるとき主曲率を見る等間隔の内側の点の数(1方向)と、主曲率から出した曲がりの半径が r から外れてよい差[mm]。
#: 差は置いた値で、これ未満しか半径の変わらない可変半径の丸めは一定半径とみなす。割合でなく差で見るのは、近似のずれが半径によらないから
#: (実測: 楕円柱の縁を半径 0.008〜7 で丸めた面で最大 6.5e-6、割合では r=0.008 で 6.0e-4。歯ブラシ立ての半径 0.4 の丸め 206 面で最大 4.6e-5。
#: 半径が 2.0 から 2.003 へ変わる丸め(断る側)で 1.5e-3)
_BLEND_SAMPLES = 9
_BLEND_RADIUS_TOL = 1e-4

_HINT_DELETE = "周りの面を延ばしても閉じない。穴や丸めは、それを作る面(底・段・縁の丸め、つながった丸め)をまとめて選ぶ"
_HINT_COLLAPSE = "消すと残す面が削られる(形がつぶれる)"
_HINT_TOO_FAR = "距離が大きすぎて面が消えるか、形のつながりが変わる"
_HINT_CANNOT_OFFSET = "この形では OCCT が面を動かせない(動かす面によらず、同じボディでは同じように断ることが多い)"
_HINT_TANGENT = "接線でつながる隣の面が一定半径の丸めでないので、付いてこさせられない"
_HINT_CUSP = "内側へずらす距離が面の曲がりの半径に近いか超えていて、面が折り返す"


class FaceEditError(ValueError):
    """関門で断った。`hint` は利用者向けの言い換え(断り文の末尾に付く)。"""

    def __init__(self, cause: str, hint: str = ""):
        super().__init__(cause)
        self.cause = cause
        self.hint = hint


# --------------------------------------------------------------------------------------
# 入口(親プロセス)
# --------------------------------------------------------------------------------------
def delete_faces(shape: Any, face_indices: Sequence[int], *, interactive: bool = True) -> dict:
    """`face_indices`(1始まり)の面を消し、周りの面を延ばして閉じ直した**新しい形状**を返す(計算は子プロセス)。

    `interactive` は画面・MCP からの操作(期限 `kernel_worker.INTERACTIVE_TIMEOUT_S`)。ログの再生は False で期限なし。戻り値:
        {"shape": <TopoDS_Shape>, "provenance": {...}}
    """
    indices = _checked_indices(shape, face_indices, VERB_DELETE)
    return kernel.remembered(
        VERB_DELETE,
        (shape,),
        (tuple(indices),),
        lambda: kernel_worker.run(VERB_DELETE, shape, {"faces": indices}, timeout=kernel_worker.time_limit(interactive)),
    )


def offset_faces(
    shape: Any,
    face_indices: Sequence[int],
    distance: float,
    *,
    interactive: bool = True,
) -> dict:
    """`face_indices` の面を外向き法線へ `distance` だけ動かした**新しい形状**を返す(正で太る。計算は子プロセス)。"""
    indices = _checked_indices(shape, face_indices, VERB_OFFSET)
    value = float(distance)
    if not math.isfinite(value) or value == 0.0:
        raise ValueError(f"{VERB_OFFSET}: distance must be finite and not 0, got {distance!r}")
    return kernel.remembered(
        VERB_OFFSET,
        (shape,),
        (tuple(indices), value),
        lambda: kernel_worker.run(
            VERB_OFFSET, shape, {"faces": indices, "distance": value}, timeout=kernel_worker.time_limit(interactive)
        ),
    )


def _checked_indices(shape: Any, face_indices: Sequence[int], verb: str) -> List[int]:
    if shape is None or _brep_topology.is_null(shape):
        raise ValueError(f"{verb}: the body is empty")
    indices = [int(index) for index in face_indices]
    if not indices:
        raise ValueError(f"{verb} requires at least one face")
    if len(set(indices)) != len(indices):
        raise ValueError(f"{verb} got the same face twice: {indices}")
    count = _brep_topology.face_count(shape)
    outside = [index for index in indices if not 1 <= index <= count]
    if outside:
        raise ValueError(f"{verb}: face index out of range: {outside} (1..{count})")
    return indices


# --------------------------------------------------------------------------------------
# 計算(子プロセス)
# --------------------------------------------------------------------------------------
def compute(verb: str, shape: Any, arguments: dict) -> dict:
    """子プロセスの入口。断ったら `ValueError`(文は原因と言い換え)。"""
    indices = [int(index) for index in arguments["faces"]]
    face_map = _brep_topology._face_map(shape)
    faces = [_brep_topology._face_at(face_map, index) for index in indices]
    try:
        if verb == VERB_DELETE:
            final, table, _ = _delete(shape, faces)
            provenance = {"verb": VERB_DELETE, "faces": indices}
        elif verb == VERB_OFFSET:
            distance = float(arguments["distance"])
            final, table, _ = _offset_following_blends(shape, faces, distance)
            provenance = {"verb": VERB_OFFSET, "faces": indices, "distance": distance}
        else:
            raise ValueError(f"unknown face edit {verb!r}")
    except FaceEditError as error:
        message = f"{verb} refused (faces {indices}): {error.cause}"
        raise ValueError(f"{message}: {error.hint}" if error.hint else message) from error
    successors = {str(key): positions for key, positions in table.items()}
    provenance.update(_brep_topology._face_map_projection(successors, _brep_topology._face_map(final).Extent()))
    return {"shape": final, "provenance": provenance}


# --------------------------------------------------------------------------------------
# 削除
# --------------------------------------------------------------------------------------
def _delete(shape: Any, faces: List[Any]):
    work, stand_ins = _with_extendable_neighbours(shape, faces)
    result, algo = _remove_features(work, [_in(stand_ins, face) for face in faces])
    history = algo.History()
    before = _brep_topology._face_map(shape)
    removed, images, stood_in, changed = [], {}, [], set()
    for i in range(1, before.Extent() + 1):
        old = TopoDS.Face_s(before.FindKey(i))
        current = _in(stand_ins, old)
        if any(old.IsSame(face) for face in faces):
            if not history.IsRemoved(current):
                raise FaceEditError(f"face {i} was selected but is not removed", _HINT_DELETE)
            removed.append(i)
            continue
        modified = [TopoDS.Face_s(x) for x in history.Modified(current)]
        images[i] = modified or ([] if history.IsRemoved(current) else [current])
        replaced = not current.IsSame(old)
        if replaced:
            stood_in.append(i)
        if modified or replaced:
            changed.add(i)
    result = _restore_stand_ins(before, stood_in, result, images)
    _structural_gate(result)
    after = _brep_topology._face_map(result)
    table = {i: [] for i in removed}
    for i, kept_images in images.items():
        positions = _brep_history.positions_in(after, kept_images)
        if not positions:
            raise FaceEditError(f"kept face {i} is lost", _HINT_DELETE)
        table[i] = positions
        if i not in changed:
            continue  # RemoveFeatures が触らなかった面は元の面そのもの(範囲も曲面も同じ)
        old = TopoDS.Face_s(before.FindKey(i))
        kept = [TopoDS.Face_s(after.FindKey(p)) for p in positions]
        if _describe(old) is not None and not all(_lies_on(image, old) for image in kept):
            raise FaceEditError(f"kept face {i} left its own surface", _HINT_DELETE)
        if not _region_kept(old, kept):
            raise FaceEditError(f"kept face {i} lost part of its original region", _HINT_COLLAPSE)
    covered = {p for positions in table.values() for p in positions}
    new = [j for j in range(1, after.Extent() + 1) if j not in covered]
    if new:
        raise FaceEditError(f"faces {new} do not come from any kept face", _HINT_DELETE)
    return result, table, new


def _with_extendable_neighbours(shape: Any, faces: List[Any]):
    """消す面と辺を共有する面(RemoveFeatures が延ばす面)のうち延ばせない曲面(`_UNEXTENDABLE`)の面を、同じ形の B-spline の
    面に置き換えた形と、その置き換えの表。置き換えが要らなければ元の形と空の表。"""
    stand_ins = BRepTools_ReShape()
    edge_faces = _brep_topology._face_edit_edge_faces(shape)
    targets: List[Any] = []
    for face in faces:
        for edge in _brep_topology._face_edit_edges(face):
            for neighbour in edge_faces.FindFromKey(edge):
                neighbour = TopoDS.Face_s(neighbour)
                if BRepAdaptor_Surface(neighbour).GetType() in _UNEXTENDABLE and not any(
                    neighbour.IsSame(other) for other in (*faces, *targets)
                ):
                    targets.append(neighbour)
    if not targets:
        return shape, stand_ins
    for face in targets:
        stand_in = _laid_on(face, *_as_bspline(face))
        if not _brep_checks.is_valid(stand_in):
            raise FaceEditError("a neighbouring face could not be rewritten as a B-spline", _HINT_DELETE)
        stand_ins.Replace(face, stand_in)
    return stand_ins.Apply(shape), stand_ins


def _restore_stand_ins(before: Any, stood_in: List[int], result: Any, images: dict) -> Any:
    """置き換えた面(`before` の添字 `stood_in`)の像を元の面の曲面の上に張り直した形(`images` の像も張り直した面へ差し替える)。
    像が元の曲面から外れていれば断る。"""
    if not stood_in:
        return result
    back = BRepTools_ReShape()
    for i in stood_in:
        # 張り替えた B-spline と同じく矩形トリムを外した曲面へ戻す(像はトリムの外へ延びている)
        surface, location = _untrimmed_surface(TopoDS.Face_s(before.FindKey(i)))
        in_world = surface if location.IsIdentity() else surface.Transformed(location.Transformation())
        for image in images[i]:
            if not _points_lie_on(image, in_world):
                raise FaceEditError(f"kept face {i} left its own surface", _HINT_DELETE)
            back.Replace(image, _laid_on(image, surface, location))
    restored = back.Apply(result)
    for i in stood_in:
        images[i] = [_in(back, image) for image in images[i]]
    # B-spline の面に張った 2D 曲線は、元の形と共有する辺にも書き込まれている
    BRepTools.RemoveUnusedPCurves_s(restored)
    return restored


def _in(reshape: Any, face: Any) -> Any:
    return TopoDS.Face_s(reshape.Value(face))


def _untrimmed_surface(face: Any):
    """面の曲面から矩形トリムを外したもの(オフセット面は基底のトリムも外す)と、その世界座標での置き場所。"""
    location = TopLoc_Location()
    return _without_trims(BRep_Tool.Surface_s(face, location)), location


def _without_trims(surface: Any) -> Any:
    if isinstance(surface, Geom_RectangularTrimmedSurface):
        return _without_trims(surface.BasisSurface())
    if isinstance(surface, Geom_OffsetSurface) and isinstance(surface.BasisSurface(), Geom_RectangularTrimmedSurface):
        return Geom_OffsetSurface(_without_trims(surface.BasisSurface()), surface.Offset())
    return surface


def _as_bspline(face: Any):
    """面の曲面を B-spline にしたものと、その世界座標での置き場所。曲面の定義域で切り、無限に延びる向き(押し出しの向き)だけ面の範囲で切る。

    ExtendFace は B-spline を定義域の外へ両端から接線の向きへ外挿で延ばし、閉じた向きは延ばさない。周期の向きは一周で切って
    閉じたままにし、面が一周を覆わなくても周方向へは延ばさない(半周の回転面の端の丸めは断る)。周方向を開くと、面の範囲で
    切れば外挿が元の曲面から外れ、広げて切れば延びた両端どうしが交わって重なった2枚の面を関門をすり抜けて返した。
    有限の向きを面の範囲で切っても外挿が元の曲面からずれた(どれも実測)。"""
    surface, location = _untrimmed_surface(face)
    own, used = surface.Bounds(), BRepTools.UVBounds_s(face)
    trimmed = Geom_RectangularTrimmedSurface(
        surface, *[used[k] if Precision.IsInfinite_s(own[k]) else own[k] for k in range(4)]
    )
    if not isinstance(surface, Geom_OffsetSurface):
        return GeomConvert.SurfaceToBSplineSurface_s(trimmed), location
    # GeomConvert はオフセット面を許容差を選べない近似で変換し、楕円柱の殻の内壁で 2.5e-6 mm ずれた(LINEAR_TOL を超え、
    # 戻すときに断る)。許容差を指定すると 1e-7 に対して 7e-9・3 ms(実測)
    approximation = GeomConvert_ApproxSurface(
        trimmed, Precision.Confusion_s(), GeomAbs_Shape.GeomAbs_C1, GeomAbs_Shape.GeomAbs_C1, 9, 9, 1000, 0
    )
    if not approximation.HasResult() or approximation.MaxError() > Precision.Confusion_s():
        raise FaceEditError("a neighbouring offset face could not be rewritten as a B-spline", _HINT_DELETE)
    return approximation.Surface(), location


def _laid_on(face: Any, surface: Any, location: Any) -> Any:
    """`face` と同じ輪郭(同じ辺)を、`location` に置いた `surface` の上に張った面。辺の 2D 曲線は `ShapeFix_Face` が作る。

    1辺ずつ射影して付け直すと、継ぎ目の辺が分かれた側壁で輪が 2D で閉じず、面の向きが決まらなくなった(楕円筒で実測)。
    `ShapeFix_Face` は輪郭が曲面に乗らなくても辺の許容差を広げて通す(延びた器の壁を矩形トリムの狭い曲面へ戻すと 1.16 mm まで
    広げ、BRepCheck も関門も通って体積が 1.7 % 違った)。正しい形では張り替えでも戻しでも許容差は増えなかったので、増えたら断る(実測)。"""
    edges = _brep_topology._face_edit_edges(face)
    tolerances = [BRep_Tool.Tolerance_s(edge) for edge in edges]
    laid = TopoDS_Face()
    builder = BRep_Builder()
    builder.MakeFace(laid, surface, face.Location().Inverted().Multiplied(location), BRep_Tool.Tolerance_s(face))
    wires = TopoDS_Iterator(face, False, False)
    while wires.More():
        builder.Add(laid, wires.Value())
        wires.Next()
    laid.Location(face.Location())
    laid.Orientation(face.Orientation())
    fix = ShapeFix_Face(laid)
    fix.Perform()
    laid = TopoDS.Face_s(fix.Face())
    for edge in _brep_topology._face_edit_edges(laid):
        before = next((t for e, t in zip(edges, tolerances) if e.IsSame(edge)), 0.0)
        if BRep_Tool.Tolerance_s(edge) > before + LINEAR_TOL:
            raise FaceEditError("an edge does not lie on the surface its face was laid on", _HINT_DELETE)
    return laid


def _remove_features(shape: Any, faces: List[Any]):
    """`BRepAlgoAPI_Defeaturing` の中身を直接使う(OCP の `BRepAlgoAPI_Defeaturing` は警告の問い合わせ口を出していない)。

    **閉じられない面は例外にも失敗にもならず、警告だけ出して元の形(か一部だけ消した形)を返す**(箱の上面で実測)。警告も失敗にする。
    """
    algo = BOPAlgo_RemoveFeatures()
    algo.SetShape(shape)
    to_remove = TopTools_ListOfShape()
    for face in faces:
        to_remove.Append(face)
    algo.AddFacesToRemove(to_remove)
    # 並列でも結果は BinTools のバイト列まで直列と同じで、歯ブラシ立ての丸め8枚が 8.0 → 約 4 s(実測。docs/FACE_EDIT.md)
    algo.SetRunParallel(True)
    algo.SetToFillHistory(True)
    algo.Perform()
    if algo.HasErrors():
        raise FaceEditError("RemoveFeatures failed: " + ", ".join(_alerts(algo, Message_Gravity.Message_Fail)), _HINT_DELETE)
    warnings = _alerts(algo, Message_Gravity.Message_Warning)
    if warnings:
        raise FaceEditError("RemoveFeatures warned: " + ", ".join(warnings), _HINT_DELETE)
    result = algo.Shape()
    if result is None or _brep_topology.is_null(result):
        raise FaceEditError("RemoveFeatures returned an empty shape", _HINT_DELETE)
    return result, algo


def _alerts(algo: Any, gravity: Any) -> List[str]:
    return [alert.GetMessageKey() for alert in algo.GetReport().GetAlerts(gravity)]


def _region_kept(old: Any, images: List[Any]) -> bool:
    """残した面の元の範囲(内部の標本点)が、どれかの像の面の上に残っているか。

    初版の削除は「残す面を延ばすだけで削らない」閉じ方に限る。標本点で確かめるだけで、面全体が残ることの証明ではない
    (標本の間の細い欠けは見逃し、残す面を正当に削る閉じ方は断る)。これが無いと、フィレット1辺の箱の下面と奥面を消したとき
    OCCT が警告なしに返す 214.6 mm³ の破片(元 23785 mm³)を、ほかの検査が全部通した。
    """
    for point in _interior_points(old, _REGION_SAMPLES):
        if not any(_point_on_face(point, image) for image in images):
            return False
    return True


def _point_on_face(point: tuple, face: Any) -> bool:
    projector = GeomAPI_ProjectPointOnSurf(gp_Pnt(*point), BRep_Tool.Surface_s(face))
    if projector.NbPoints() == 0 or projector.LowerDistance() > LINEAR_TOL:
        return False
    u, v = _into_face_period(face, *projector.LowerDistanceParameters())
    state = BRepClass_FaceClassifier(face, gp_Pnt2d(u, v), 1e-7).State()
    return state in (TopAbs_State.TopAbs_IN, TopAbs_State.TopAbs_ON)


# --------------------------------------------------------------------------------------
# オフセット
# --------------------------------------------------------------------------------------
def _offset_following_blends(shape: Any, faces: List[Any], distance: float):
    edge_faces = _brep_topology._face_edit_edge_faces(shape)
    if any(_blend_signature(face, edge_faces) is not None for face in faces):
        raise FaceEditError(
            "a selected face is a blend", "丸めの面そのものは動かせない。半径を変えるときは、面の削除で丸めを消してから丸め直す"
        )
    for face in faces:
        if _offset_definition(face, distance) is not None:
            continue
        if BRepAdaptor_Surface(face).GetType() not in _UNEXTENDABLE:
            raise FaceEditError(
                f"a selected face is a {_surface_type(face)}",
                "動かせるのは平面・円筒・球・トーラスと、押し出し・回転・オフセットの面だけ(ずらした位置を確かめられない)",
            )
        _require_no_cusp(face, distance)
    blends, supports = _structural_blends(shape, faces)
    if not blends:
        return _offset(shape, faces, distance)
    return _offset_with_blends(shape, faces, distance, blends, supports)


def _require_no_cusp(face: Any, distance: float) -> None:
    """解析曲面でない面を `distance` ずらしても折り返さないこと。面の UV の範囲の格子(B-spline の節点の区間ごとの点を含む)で
    主曲率を見て、厳しい標本の周りで最小を詰める。等間隔の格子だけでは、楕円の先端が格子の間に来ると見逃し(始点を 0.05 ずらした
    楕円の B-spline で実測)、詰め直しを足しても、節点を密に入れて幅 0.006 の区間だけ曲げた尖りを見逃した(sol の指摘で実測)。
    `BRepLProp` の曲率は曲面の法線の側へ曲がると正なので、外向き法線に対しては面の向きで符号を返す(円柱の側面で実測)。"""
    adaptor = BRepAdaptor_Surface(face)
    sign = -1.0 if face.Orientation() == TopAbs_Orientation.TopAbs_REVERSED else 1.0
    u1, u2, v1, v2 = BRepTools.UVBounds_s(face)

    def margin(u: float, v: float) -> float:
        props = BRepLProp_SLProps(adaptor, min(max(u, u1), u2), min(max(v, v1), v2), 2, 1e-7)
        if not props.IsCurvatureDefined():
            return math.inf  # 回転面の極など。周りの標本が見る
        return min(1.0 - sign * curvature * distance for curvature in (props.MaxCurvature(), props.MinCurvature()))

    us, vs = _sample_grid(adaptor, (u1, u2, v1, v2), _CURVATURE_SAMPLES - 1)
    values = {(i, j): margin(u, v) for i, u in enumerate(us) for j, v in enumerate(vs)}
    worst = min(values.values())
    # 格子の局所的な最小から詰める。押し出し面は曲率が片方向に変わらず同じ値が列に並ぶので、行・列の重ならないものを選ぶ
    minima = sorted(
        (value, i, j)
        for (i, j), value in values.items()
        if all(value <= values.get((i + a, j + b), math.inf) for a in (-1, 0, 1) for b in (-1, 0, 1))
    )
    starts, used_i, used_j = [], set(), set()
    for value, i, j in minima:
        if len(starts) == _CURVATURE_REFINED:
            break
        if i in used_i or j in used_j:
            continue
        starts.append((value, i, j))
        used_i.add(i)
        used_j.add(j)
    for value, i, j in starts:
        u, v = us[i], vs[j]
        du = max(us[min(i + 1, len(us) - 1)] - u, u - us[max(i - 1, 0)])
        dv = max(vs[min(j + 1, len(vs) - 1)] - v, v - vs[max(j - 1, 0)])
        while du > (u2 - u1) * 1e-9 or dv > (v2 - v1) * 1e-9:
            candidates = [(margin(u + a * du, v + b * dv), u + a * du, v + b * dv) for a in (-1, 0, 1) for b in (-1, 0, 1)]
            best = min(candidates)
            if best[0] < value:
                value, u, v = best[0], min(max(best[1], u1), u2), min(max(best[2], v1), v2)
            else:
                du, dv = du / 2, dv / 2
        worst = min(worst, value)
    if worst < _CUSP_MARGIN:
        raise FaceEditError(f"the moved face would fold (1 + curvature * distance = {worst:.3g})", _HINT_CUSP)


def _offset(shape: Any, faces: List[Any], distance: float):
    raw, algo = _make_offset(shape, faces, distance)
    result, unwrapped = _without_zero_offsets(_wrap_checked(raw))
    _structural_gate(result, hint=_HINT_TOO_FAR)
    table = _offset_correspondence(shape, faces, distance, result, lambda old: [unwrapped.Value(x) for x in algo.Generated(old)])
    return result, table, []


def _make_offset(shape: Any, faces: List[Any], distance: float):
    """全体の距離を 0 にして、選んだ面だけ `distance` だけ法線方向へずらす。

    `Intersection=False`(B0)。`True` は OCCT 7.9.3 の説明で未完成とされる。**接線でつながる面(丸めとその先の面)は
    選んでいなくても一緒にずれる**ので、丸めに接する面は `_offset_with_blends` を通す。結果はシェルで返ることがある。
    """
    algo = BRepOffset_MakeOffset()
    algo.Initialize(
        shape, 0.0, 1e-7, BRepOffset_Mode.BRepOffset_Skin, False, False, GeomAbs_JoinType.GeomAbs_Intersection, False, False
    )
    for face in faces:
        algo.SetOffsetOnFace(face, distance)
    algo.MakeOffsetShape()
    if not algo.IsDone():
        raise FaceEditError(f"MakeOffset failed ({algo.Error()})", _HINT_CANNOT_OFFSET)
    result = algo.Shape()
    if result is None or _brep_topology.is_null(result):
        raise FaceEditError("MakeOffset returned an empty shape", _HINT_CANNOT_OFFSET)
    return result, algo


def _wrap_checked(raw: Any) -> Any:
    """MakeOffset の生の結果を1ソリッドにする。**包む前に**、ソリッド1つか閉じたシェル1つだけで、その外に面も辺も無いことを見る
    (シェルを包むと、外に付いた独立の面や辺が後の検査から見えなくなる)。"""
    solids = _brep_topology._face_edit_solids(raw)
    if solids:
        if len(solids) != 1 or _brep_topology._face_edit_count(raw, "face") != _brep_topology._face_edit_count(solids[0], "face") or _brep_topology._face_edit_count(raw, "edge") != _brep_topology._face_edit_count(solids[0], "edge"):
            raise FaceEditError("raw result is not exactly one solid", _HINT_CANNOT_OFFSET)
        return raw
    shells = _brep_topology.subshapes(raw, TopAbs_ShapeEnum.TopAbs_SHELL)
    if len(shells) != 1 or _brep_topology._face_edit_count(raw, "face") != _brep_topology._face_edit_count(shells[0], "face") or _brep_topology._face_edit_count(raw, "edge") != _brep_topology._face_edit_count(shells[0], "edge"):
        raise FaceEditError("raw result is not exactly one shell", _HINT_CANNOT_OFFSET)
    shell = TopoDS.Shell_s(shells[0])
    if BRepCheck_Shell(shell).Closed() != BRepCheck_Status.BRepCheck_NoError:
        raise FaceEditError("raw shell is not closed", _HINT_CANNOT_OFFSET)
    solid = BRepBuilderAPI_MakeSolid(shell).Solid()
    if not BRepLib.OrientClosedSolid_s(solid):
        raise FaceEditError("could not orient the closed shell", _HINT_CANNOT_OFFSET)
    return solid


def _without_zero_offsets(shape: Any):
    """MakeOffset は動かさない面のうち解析曲面でないもの(押し出し面・B-spline の丸め)を、距離0のオフセット面に包んで返す(実測)。
    包んだままだと曲面の種類が変わり、丸めとも見分けられない。元の曲面へ張り直した形と、その置き換えの表。"""
    reshape = BRepTools_ReShape()
    for face in _brep_topology._face_edit_faces(shape):
        location = TopLoc_Location()
        surface = BRep_Tool.Surface_s(face, location)
        if isinstance(surface, Geom_OffsetSurface) and abs(surface.Offset()) < Precision.Confusion_s():
            reshape.Replace(face, _laid_on(face, surface.BasisSurface(), location))
    return reshape.Apply(shape), reshape


def _offset_correspondence(shape: Any, faces: List[Any], distance: float, result: Any, images_of) -> dict:
    """旧面 → 新面が1対1で全部を覆い、各新面が対応元の曲面(選んだ面ならずらした曲面)に乗る。"""
    before, after = _brep_topology._face_map(shape), _brep_topology._face_map(result)
    table = {}
    for i in range(1, before.Extent() + 1):
        old = TopoDS.Face_s(before.FindKey(i))
        positions = _brep_history.positions_in(after, images_of(old))
        if len(positions) != 1:
            raise FaceEditError(f"face {i} maps to {len(positions)} faces (topology changed)", _HINT_TOO_FAR)
        selected = any(old.IsSame(face) for face in faces)
        if not _lies_on(TopoDS.Face_s(after.FindKey(positions[0])), old, distance if selected else 0.0):
            raise FaceEditError(f"face {i} is not where it should be ({'moved' if selected else 'kept'} face)", _HINT_TOO_FAR)
        table[i] = positions
    covered = sorted(p for positions in table.values() for p in positions)
    if len(set(covered)) != len(covered) or len(covered) != after.Extent():
        raise FaceEditError("old and new faces are not one-to-one", _HINT_TOO_FAR)
    _require_volume_follows(shape, result, distance)
    return table


def _require_volume_follows(shape: Any, result: Any, distance: float) -> None:
    change = _brep_geometry.volume(result) - _brep_geometry.volume(shape)
    if change * distance <= 0:
        raise FaceEditError(f"volume moved the wrong way ({change:+.6g} for distance {distance:+g})", _HINT_TOO_FAR)


# --------------------------------------------------------------------------------------
# 丸めが付いてくる(構造で認識した一定半径の丸めだけ)
# --------------------------------------------------------------------------------------
def _structural_blends(shape: Any, faces: List[Any]):
    """選んだ面に接線でつながる丸めの連なり(角の球を含む)と、各丸めの (半径, 支えの面2枚)。

    丸めの認識は構造で、半径の大小では決めない(意匠の大きな丸めも丸めとして扱う。設計者がフィレットとして作ったことの証明ではない)。
    丸めと認めない接線の隣があれば断る(MakeOffset の接線伝播へは切り替えない)。
    """
    edge_faces = _brep_topology._face_edit_edge_faces(shape)
    blends: List[Any] = []
    supports: List[tuple] = []
    frontier, seen = list(faces), list(faces)
    own_supports: dict = {}
    radius_of: dict = {}
    while frontier:
        current = frontier.pop(0)
        for edge in _brep_topology._face_edit_edges(current):
            other = _other_face(edge_faces, edge, current)
            if other is None or any(other.IsSame(s) for s in seen) or not _tangent(edge, current, other):
                continue
            if any(other.IsSame(s) for s in own_supports.get(id(current), ())):
                continue
            kind = _surface_type(other)
            if kind == "sphere" and any(current.IsSame(b) for b in blends):
                current_radius = radius_of.get(id(current))
                if current_radius is None or abs(_describe(other)[2] - current_radius) > LINEAR_TOL:
                    raise FaceEditError("a sphere next to a blend has a different radius", _HINT_TANGENT)
                radius_of[id(other)] = current_radius
                seen.append(other)
                blends.append(other)
                frontier.append(other)
                continue
            signature = _blend_signature(other, edge_faces)
            if signature is None:
                if any(current.IsSame(face) for face in faces) or kind != "plane":
                    raise FaceEditError(f"a tangent neighbour ({kind}) is not a constant-radius blend", _HINT_TANGENT)
                continue  # 丸めの反対側の支えの平面(選んでいない)。たどらない
            seen.append(other)
            blends.append(other)
            supports.append(signature)
            own_supports[id(other)] = signature[1:]
            radius_of[id(other)] = signature[0]
            frontier.append(other)
    for sphere in [b for b in blends if _surface_type(b) == "sphere"]:
        radius = _describe(sphere)[2]
        for edge in _brep_topology._face_edit_edges(sphere):
            other = _other_face(edge_faces, edge, sphere)
            if other is None or not any(other.IsSame(b) for b in blends) or _surface_type(other) == "sphere":
                continue
            signature = _blend_signature(other, edge_faces)
            if signature is None or abs(signature[0] - radius) > LINEAR_TOL:
                raise FaceEditError("a sphere next to a blend has a different radius", _HINT_TANGENT)
    for _, first, second in supports:
        if any(first.IsSame(b) or second.IsSame(b) for b in blends):
            raise FaceEditError("a blend rests on another blend", _HINT_TANGENT)
    return blends, supports


def blend_chain(shape: Any, face_index: int) -> List[int]:
    """`face_index`(1始まり)の面が丸めなら、辺を共有してつながる丸めの連なり(角の球を含む)の添字を昇順で。丸めでなければその面だけ。

    丸めどうしは接線でなくてよい(箱の上面4辺の丸めは角で折れて会う)。丸めの認識は `_blend_signature` と同じ構造の規則で、
    互いの支えの面へはたどらない。計算は親で行う(位相をたどるだけで、RemoveFeatures を呼ばない)。
    """
    face_map = _brep_topology._face_map(shape)
    edge_faces = _brep_topology._face_edit_edge_faces(shape)
    signatures: dict = {}

    def signature(index: int, face: Any):
        if index not in signatures:
            signatures[index] = _blend_signature(face, edge_faces)
        return signatures[index]

    def is_corner_sphere(edge: Any, sphere: Any, blend: Any) -> bool:
        return _surface_type(sphere) == "sphere" and _tangent(edge, sphere, blend)

    start = _brep_topology._face_at(face_map, face_index)
    if signature(face_index, start) is None:
        neighbours = [(e, _other_face(edge_faces, e, start)) for e in _brep_topology._face_edit_edges(start)]
        if not any(
            other is not None and signature(face_map.FindIndex(other), other) is not None and is_corner_sphere(e, start, other)
            for e, other in neighbours
        ):
            return [face_index]
    chain, frontier = {face_index: start}, [face_index]
    while frontier:
        index = frontier.pop()
        current = chain[index]
        own = signature(index, current)
        for edge in _brep_topology._face_edit_edges(current):
            other = _other_face(edge_faces, edge, current)
            if other is None:
                continue
            other_index = face_map.FindIndex(other)
            if other_index in chain or (own is not None and any(other.IsSame(s) for s in own[1:])):
                continue
            found = signature(other_index, other)
            if found is not None:
                if any(current.IsSame(s) for s in found[1:]):
                    continue  # 丸めの上に乗った丸め。支えの側から上の丸めへは広げない
            elif own is None or not is_corner_sphere(edge, other, current):
                continue
            chain[other_index] = other
            frontier.append(other_index)
    return sorted(chain)


def _blend_signature(face: Any, edge_faces: Any):
    """`face` が一定半径の丸めなら (半径, 支えの面2枚)、でなければ None。

    丸め = 一定半径の丸めの曲面(`_blend_radius`)で、**長手の辺**(`_springs`)が、ちょうど2枚の別の面に G1 で接する。
    """
    radius = _blend_radius(face)
    if radius is None:
        return None
    supports: List[Any] = []
    for edge, other in _springs(face, edge_faces):
        if other is None or other.IsSame(face) or not _tangent(edge, face, other):
            return None
        if not any(other.IsSame(s) for s in supports):
            supports.append(other)
    if len(supports) != 2:
        return None
    return radius, supports[0], supports[1]


def _blend_radius(face: Any) -> Optional[float]:
    """一定半径の丸めの曲面なら半径。円筒・トーラスは定義から。B-spline・ベジエは、片方の主曲率の大きさが面全体で同じ 1/r であること
    (半径 r の球が転がった包絡面)から。楕円などの縁を製品のフィレットで丸めると B-spline の面になる。"""
    definition = _describe(face)
    if definition is not None:
        if definition[0] == "cylinder":
            return definition[3]
        if definition[0] == "torus":
            return definition[4]
        return None
    if BRepAdaptor_Surface(face).GetType() not in (GeomAbs_SurfaceType.GeomAbs_BSplineSurface, GeomAbs_SurfaceType.GeomAbs_BezierSurface):
        return None
    adaptor = BRepAdaptor_Surface(face)
    u1, u2, v1, v2 = BRepTools.UVBounds_s(face)
    us, vs = _sample_grid(adaptor, (u1, u2, v1, v2), _BLEND_SAMPLES + 1)
    samples = []
    for u in us:
        for v in vs:
            props = BRepLProp_SLProps(adaptor, u, v, 2, 1e-7)
            if props.IsCurvatureDefined():
                samples.append((abs(props.MaxCurvature()), abs(props.MinCurvature())))
    if not samples:
        return None
    for candidate in (*samples[len(samples) // 2], *samples[0]):
        if candidate <= 1e-9:
            continue
        matched = [min(pair, key=lambda k: abs(k / candidate - 1.0)) for pair in samples]
        if all(k > 0.0 and abs(1.0 / k - 1.0 / candidate) < _BLEND_RADIUS_TOL for k in matched):
            return len(matched) / sum(matched)
    return None


def _sample_grid(adaptor: Any, bounds: tuple, intervals: int) -> Tuple[List[float], List[float]]:
    """曲率を見る U・V の位置(`_sample_parameters`)。周期の面は、節点を周期ずつずらして面の範囲へ持ってくる
    (節点が 0.05〜6.33 にある面を U の範囲 [2π, 4π] で張ると、ずらさなければ節点の点が1つも入らない。sol の指摘)。"""
    u1, u2, v1, v2 = bounds
    u_knots, v_knots = _knots(adaptor)
    u_period = adaptor.UPeriod() if adaptor.IsUPeriodic() else 0.0
    v_period = adaptor.VPeriod() if adaptor.IsVPeriodic() else 0.0
    return _sample_parameters(u1, u2, intervals, u_knots, u_period), _sample_parameters(v1, v2, intervals, v_knots, v_period)


def _sample_parameters(low: float, high: float, intervals: int, knots: List[float], period: float) -> List[float]:
    """曲率を見る1方向の位置: 範囲を `intervals` 等分する点(両端を含む)と、範囲にかかる節点の区間ごとの端と中点。
    等間隔の点だけでは、端の 3 % で半径が 2.2 から 2 へ変わる可変半径の丸めを一定半径と見誤った(sol の指摘で実測)。"""
    found = {low + (high - low) * i / intervals for i in range(intervals + 1)}
    if period > 0.0 and knots:
        shifts = range(math.floor((low - knots[-1]) / period), math.ceil((high - knots[0]) / period) + 1)
        knots = sorted({knot + n * period for n in shifts for knot in knots})
    for a, b in zip(knots, knots[1:]):
        a, b = max(a, low), min(b, high)
        if a < b:
            found.update((a, (a + b) / 2))
    return sorted(found)


def _knots(adaptor: Any) -> Tuple[List[float], List[float]]:
    """面の U・V の B-spline の節点。押し出し・回転の面は元の曲線の節点が、押し出しは U・回転は V の向きに出る。"""
    kind = adaptor.GetType()
    if kind == GeomAbs_SurfaceType.GeomAbs_BSplineSurface:
        surface = adaptor.BSpline()
        return [surface.UKnot(i) for i in range(1, surface.NbUKnots() + 1)], [surface.VKnot(i) for i in range(1, surface.NbVKnots() + 1)]
    if kind == GeomAbs_SurfaceType.GeomAbs_OffsetSurface:
        return _knots(adaptor.BasisSurface())
    if kind in (GeomAbs_SurfaceType.GeomAbs_SurfaceOfExtrusion, GeomAbs_SurfaceType.GeomAbs_SurfaceOfRevolution):
        curve = adaptor.BasisCurve()
        knots = []
        if curve.GetType() == GeomAbs_CurveType.GeomAbs_BSplineCurve:
            bspline = curve.BSpline()
            knots = [bspline.Knot(i) for i in range(1, bspline.NbKnots() + 1)]
        return (knots, []) if kind == GeomAbs_SurfaceType.GeomAbs_SurfaceOfExtrusion else ([], knots)
    return [], []


def _springs(face: Any, edge_faces: Any) -> List[tuple]:
    """丸めの長手の辺と、その向こうの面(無ければ None)。円筒・トーラスは定義で(軸に平行な直線、同軸の円)、B-spline の丸めは
    接線でつながる別の面との辺で決める(輪になった丸めの継ぎ目と、端で折れて会う面との辺は入らない)。"""
    definition = _describe(face)
    found = []
    for edge in _brep_topology._face_edit_edges(face):
        if BRep_Tool.Degenerated_s(edge):
            continue
        other = _other_face(edge_faces, edge, face)
        if definition is not None:
            if _is_spring_edge(edge, definition):
                found.append((edge, other))
        elif other is not None and _tangent(edge, face, other):
            found.append((edge, other))
    return found


def _offset_with_blends(shape: Any, faces: List[Any], distance: float, blends: List[Any], supports: List[tuple]):
    """丸めを外す → 尖った形で動かす → 支えの面の共有辺を元の半径で丸め直す → 半径・接線・対応を確かめる。

    由来: 旧丸めは deleted、丸め直した面は new_faces(旧丸めと新丸めを対応させる根拠を持たない)。
    """
    # 削除と同じ経路で外す(支えが押し出し面などの延ばせない曲面でも閉じ、削除の関門も通る)
    try:
        sharp, removal_table, _ = _delete(shape, blends)
    except FaceEditError as error:
        raise FaceEditError(error.cause, _HINT_TANGENT) from error
    original_map, removed_map = _brep_topology._face_map(shape), _brep_topology._face_map(sharp)
    # 外した丸めの境目の頂点が支えの辺に残る。同じ半径で丸め直すとその頂点をわずかに跨ぎ、無効な形になる(楕円柱の上縁 r2 を外すと
    # 壁の継ぎ目が z=28 で2本に分かれ、丸め直しの半径 2.00001 で壁の輪郭が UV で閉じなかった。2.0・1.99999 は有効。実測)。辺をつなぐ
    unifier = ShapeUpgrade_UnifySameDomain(sharp, True, False, True)
    unifier.Build()
    unified, unify_history = unifier.Shape(), unifier.History()
    _structural_gate(unified, hint=_HINT_TANGENT)

    def through_removal(face: Any) -> Any:
        positions = removal_table[original_map.FindIndex(face)]
        if len(positions) != 1:
            raise FaceEditError("a face split while removing the blends", _HINT_TANGENT)
        removed = TopoDS.Face_s(removed_map.FindKey(positions[0]))
        images = [TopoDS.Face_s(x) for x in unify_history.Modified(removed)] or [removed]
        if len(images) != 1:
            raise FaceEditError("a face split while joining edges", _HINT_TANGENT)
        return _as_in(unified, images[0])

    sharp = unified

    moved_faces = [through_removal(face) for face in faces]
    raw, offset = _make_offset(sharp, moved_faces, distance)
    moved, unwrapped = _without_zero_offsets(_wrap_checked(raw))
    _structural_gate(moved, hint=_HINT_TOO_FAR)
    sharp_table = _offset_correspondence(
        sharp, moved_faces, distance, moved, lambda old: [unwrapped.Value(x) for x in offset.Generated(old)]
    )
    sharp_map, moved_map = _brep_topology._face_map(sharp), _brep_topology._face_map(moved)

    def to_moved(face: Any) -> Any:
        return TopoDS.Face_s(moved_map.FindKey(sharp_table[sharp_map.FindIndex(through_removal(face))][0]))

    edge_faces = _brep_topology._face_edit_edge_faces(moved)
    rebuilt = []
    for radius, first, second in supports:
        a, b = to_moved(first), to_moved(second)
        common = [
            edge
            for edge in _brep_topology._face_edit_edges(a)
            if (other := _other_face(edge_faces, edge, a)) is not None
            and other.IsSame(b)
            and not any(edge.IsSame(e) for e, *_ in rebuilt)
        ]
        if len(common) != 1:
            raise FaceEditError(f"supports of a blend share {len(common)} edges after the move", _HINT_TOO_FAR)
        rebuilt.append((common[0], radius, a, b))
    maker = BRepFilletAPI_MakeFillet(moved)
    for edge, radius, _, _ in rebuilt:
        maker.Add(radius, edge)
    maker.Build()
    if not maker.IsDone():
        raise FaceEditError("re-blending failed", _HINT_TOO_FAR)
    final = maker.Shape()
    _structural_gate(final, hint=_HINT_TOO_FAR)
    final_map = _brep_topology._face_map(final)
    regenerated = _check_reblended(maker, final, final_map, rebuilt)

    before = _brep_topology._face_map(shape)
    table = {}
    for i in range(1, before.Extent() + 1):
        old = TopoDS.Face_s(before.FindKey(i))
        if any(old.IsSame(b) for b in blends):
            table[i] = []
            continue
        in_moved = to_moved(old)
        positions = _brep_history.positions_in(final_map, [TopoDS.Face_s(x) for x in maker.Modified(in_moved)] or [in_moved])
        if len(positions) != 1:
            raise FaceEditError(f"face {i} does not map to exactly one final face", _HINT_TOO_FAR)
        selected = any(old.IsSame(face) for face in faces)
        if not _lies_on(TopoDS.Face_s(final_map.FindKey(positions[0])), old, distance if selected else 0.0):
            raise FaceEditError(f"face {i} is not where it should be after re-blending", _HINT_TOO_FAR)
        table[i] = positions
    covered = [p for positions in table.values() for p in positions]
    if len(set(covered)) != len(covered):
        raise FaceEditError("two old faces map to the same final face", _HINT_TOO_FAR)
    if set(covered) & set(regenerated):
        raise FaceEditError("a kept face is also counted as re-blended", _HINT_TOO_FAR)
    stray = [j for j in range(1, final_map.Extent() + 1) if j not in covered and j not in regenerated]
    if stray:
        raise FaceEditError(f"faces {stray} come from nowhere", _HINT_TOO_FAR)
    _require_volume_follows(shape, final, distance)
    return final, table, regenerated


def _check_reblended(maker: Any, final: Any, final_map: Any, rebuilt: list) -> List[int]:
    """丸め直した面が、その辺の半径の丸めの曲面(`_blend_radius`)で、長手の辺で支えの面の像の**両方**に G1 で接すること。
    角は辺でなく頂点から生まれる(全辺丸めの上面で実測)ので、頂点から生まれた面は同じ半径の球であること。"""
    final_edge_faces = _brep_topology._face_edit_edge_faces(final)
    regenerated: List[int] = []
    for edge, radius, first, second in rebuilt:
        made = sorted({final_map.FindIndex(x) for x in maker.Generated(edge)} - {0})
        if not made:
            raise FaceEditError("re-blending produced no face for an edge", _HINT_TOO_FAR)
        support_a = list(maker.Modified(first)) or [first]
        support_b = list(maker.Modified(second)) or [second]
        for position in made:
            face = TopoDS.Face_s(final_map.FindKey(position))
            made_radius = _blend_radius(face)
            if made_radius is None:
                raise FaceEditError("a re-blended face is not a constant-radius blend", _HINT_TOO_FAR)
            # B-spline の丸めの半径は主曲率から出すので、近似の分だけ定義の照合より緩める
            if abs(made_radius - radius) > _BLEND_RADIUS_TOL:
                raise FaceEditError("a re-blended face has the radius of another blend", _HINT_TOO_FAR)
            touched_a = touched_b = False
            for spring, neighbour in _springs(face, final_edge_faces):
                on_a = neighbour is not None and any(neighbour.IsSame(x) for x in support_a)
                on_b = neighbour is not None and any(neighbour.IsSame(x) for x in support_b)
                if not (on_a or on_b) or not _tangent(spring, face, neighbour):
                    raise FaceEditError("a re-blended face is not tangent to its supports", _HINT_TOO_FAR)
                touched_a, touched_b = touched_a or on_a, touched_b or on_b
            if not (touched_a and touched_b):
                raise FaceEditError("a re-blended face does not rest on both supports", _HINT_TOO_FAR)
        regenerated += made
        for vertex in _brep_topology.subshapes(edge, TopAbs_ShapeEnum.TopAbs_VERTEX):
            for position in sorted({final_map.FindIndex(x) for x in maker.Generated(vertex)} - {0}):
                definition = _describe(TopoDS.Face_s(final_map.FindKey(position)))
                if definition is None or definition[0] != "sphere" or abs(definition[2] - radius) > LINEAR_TOL:
                    raise FaceEditError("a corner made by re-blending is not a sphere of the blend radius", _HINT_TOO_FAR)
                regenerated.append(position)
    return sorted(set(regenerated))


def _as_in(shape: Any, face: Any) -> Any:
    """`shape` の中での向きのまま `face` を引く(履歴の像は向きを持たず、MakeOffset に渡すと外向きを取り違える)。"""
    for occurrence in _brep_topology.subshapes(shape, TopAbs_ShapeEnum.TopAbs_FACE):
        if occurrence.IsSame(face):
            return TopoDS.Face_s(occurrence)
    raise FaceEditError("a face is not in the shape after removing the blends", _HINT_TANGENT)


# --------------------------------------------------------------------------------------
# 構造の関門(削除・オフセット共通)
# --------------------------------------------------------------------------------------
def _structural_gate(result: Any, *, hint: str = _HINT_DELETE) -> None:
    """結果全体がちょうど1ソリッドで、外に面が無く、壊れておらず、面積0の面を残していない。"""
    solids = _brep_topology._face_edit_solids(result)
    if len(solids) != 1:
        raise FaceEditError(f"result has {len(solids)} solids", hint)
    if _brep_topology._face_edit_count(result, "face") != _brep_topology._face_edit_count(solids[0], "face"):
        raise FaceEditError("result has faces outside its solid", hint)
    if not _brep_checks.is_valid(result):
        raise FaceEditError("result is invalid (BRepCheck)", hint)
    if _brep_checks._face_edit_free_edge_count(result):
        raise FaceEditError("result has free edges", hint)
    if not _brep_geometry.volume(result) > 0:
        raise FaceEditError("result has no volume", hint)
    tiny = [face for face in _brep_topology._face_edit_faces(result) if _brep_geometry._face_edit_face_area(face) < MIN_FACE_AREA]
    if tiny:
        raise FaceEditError(f"{len(tiny)} face(s) vanished but stayed in the shape (area < {MIN_FACE_AREA:g})", hint)


# --------------------------------------------------------------------------------------
# 曲面の定義の比較
# --------------------------------------------------------------------------------------
def _describe(face: Any):
    """解析曲面の定義(世界座標)。自由曲面は None。"""
    adaptor = BRepAdaptor_Surface(face)
    kind = adaptor.GetType()
    if kind == GeomAbs_SurfaceType.GeomAbs_Plane:
        plane = adaptor.Plane()
        return ("plane", _vec(plane.Location()), _vec(plane.Axis().Direction()))
    if kind == GeomAbs_SurfaceType.GeomAbs_Cylinder:
        cylinder = adaptor.Cylinder()
        return ("cylinder", *_axis(cylinder.Axis()), cylinder.Radius())
    if kind == GeomAbs_SurfaceType.GeomAbs_Cone:
        cone = adaptor.Cone()
        return ("cone", _vec(cone.Apex()), _vec(cone.Axis().Direction()), cone.SemiAngle())
    if kind == GeomAbs_SurfaceType.GeomAbs_Sphere:
        sphere = adaptor.Sphere()
        return ("sphere", _vec(sphere.Location()), sphere.Radius())
    if kind == GeomAbs_SurfaceType.GeomAbs_Torus:
        torus = adaptor.Torus()
        return ("torus", *_axis(torus.Axis()), torus.MajorRadius(), torus.MinorRadius())
    return None


def _same_surface(a: Optional[tuple], b: Optional[tuple]) -> bool:
    if a is None or b is None or a[0] != b[0]:
        return False
    kind = a[0]
    if kind == "plane":
        return _parallel(a[2], b[2]) and abs(_dot(_sub(b[1], a[1]), a[2])) < LINEAR_TOL
    if kind == "cylinder":
        return _parallel(a[2], b[2]) and _point_line_distance(b[1], a[1], a[2]) < LINEAR_TOL and abs(a[3] - b[3]) < LINEAR_TOL
    if kind == "cone":
        return _parallel(a[2], b[2]) and math.dist(a[1], b[1]) < LINEAR_TOL and abs(abs(a[3]) - abs(b[3])) < ANGULAR_TOL
    if kind == "sphere":
        return math.dist(a[1], b[1]) < LINEAR_TOL and abs(a[2] - b[2]) < LINEAR_TOL
    if kind == "torus":
        return (
            _parallel(a[2], b[2])
            and math.dist(a[1], b[1]) < LINEAR_TOL
            and abs(a[3] - b[3]) < LINEAR_TOL
            and abs(a[4] - b[4]) < LINEAR_TOL
        )
    return False


def _offset_definition(face: Any, distance: float) -> Optional[tuple]:
    """外向き法線へ `distance` ずらした曲面の定義。円錐(ずらすと頂点が動く)と自由曲面は None。"""
    definition = _describe(face)
    if definition is None:
        return None
    kind = definition[0]
    if kind == "plane":
        normal = _brep_geometry._face_edit_face_normal(face)
        return ("plane", tuple(definition[1][i] + normal[i] * distance for i in range(3)), definition[2])
    if kind == "cylinder":
        return ("cylinder", definition[1], definition[2], definition[3] + _convexity(face, definition) * distance)
    if kind == "sphere":
        return ("sphere", definition[1], definition[2] + _convexity(face, definition) * distance)
    if kind == "torus":
        return ("torus", definition[1], definition[2], definition[3], definition[4] + _convexity(face, definition) * distance)
    return None


def _convexity(face: Any, definition: tuple) -> float:
    """外向き法線が中心(軸)から離れる向きなら +1(凸。太ると半径が増える)、近づく向きなら −1(穴)。"""
    point = _interior_points(face, 1)[0]
    normal = _brep_geometry._face_edit_face_normal(face)
    kind = definition[0]
    if kind == "sphere":
        radial = _sub(point, definition[1])
    else:
        origin, direction = definition[1], definition[2]
        v = _sub(point, origin)
        along = _dot(v, direction)
        radial = tuple(v[i] - along * direction[i] for i in range(3))
        if kind == "torus":
            length = math.sqrt(_dot(radial, radial)) or 1.0
            ring = tuple(origin[i] + radial[i] / length * definition[3] for i in range(3))
            radial = _sub(point, ring)
    return 1.0 if _dot(radial, normal) > 0 else -1.0


def _lies_on(face: Any, source: Any, distance: float = 0.0) -> bool:
    """`face` が `source`(を `distance` ずらした曲面)に乗っているか。解析曲面は定義の照合、自由曲面は内部の最大9点の射影
    (曲面の同一性の証明ではない)。"""
    expected = _offset_definition(source, distance) if distance else _describe(source)
    if expected is not None:
        return _same_surface(expected, _describe(face))
    if distance:
        return _points_offset_from(face, source, distance)
    return _points_lie_on(face, BRep_Tool.Surface_s(source))


def _points_offset_from(face: Any, source: Any, distance: float) -> bool:
    """`face` の内部の最大9点が、`source` の曲面の最寄りの点から外向き法線へ `distance` の位置(`LINEAR_TOL` 以内)にあるか。
    最寄りの点が足になるのは、ずらしが折り返さないとき(`_require_no_cusp`)。"""
    surface = BRep_Tool.Surface_s(source)
    sign = -1.0 if source.Orientation() == TopAbs_Orientation.TopAbs_REVERSED else 1.0
    for point in _interior_points(face, 9):
        projector = GeomAPI_ProjectPointOnSurf(gp_Pnt(*point), surface)
        if projector.NbPoints() == 0:
            return False
        props = GeomLProp_SLProps(surface, *projector.LowerDistanceParameters(), 1, 1e-7)
        if not props.IsNormalDefined():
            return False
        foot, normal = props.Value(), props.Normal()
        expected = tuple(getattr(foot, a)() + sign * distance * getattr(normal, a)() for a in ("X", "Y", "Z"))
        if math.dist(point, expected) > LINEAR_TOL:
            return False
    return True


def _points_lie_on(face: Any, surface: Any) -> bool:
    """`face` の内部の最大9点が、世界座標の `surface` から `LINEAR_TOL` 以内か。"""
    for point in _interior_points(face, 9):
        projector = GeomAPI_ProjectPointOnSurf(gp_Pnt(*point), surface)
        if projector.NbPoints() == 0 or projector.LowerDistance() > LINEAR_TOL:
            return False
    return True


def _interior_points(face: Any, count: int) -> List[tuple]:
    """面の内側の点を最大 `count` 個、**面全体から均等に間引いて**返す
    (格子の先頭から取ると UV の片側に寄り、段の削除で面の上半分が消えたのを見逃した)。"""
    u1, u2, v1, v2 = BRepTools.UVBounds_s(face)
    adaptor = BRepAdaptor_Surface(face)
    found = []
    steps = max(3, int(math.ceil(math.sqrt(count * 4))))
    for i in range(1, steps):
        for j in range(1, steps):
            u = u1 + (u2 - u1) * i / steps
            v = v1 + (v2 - v1) * j / steps
            if BRepClass_FaceClassifier(face, gp_Pnt2d(u, v), 1e-7).State() == TopAbs_State.TopAbs_IN:
                point = adaptor.Value(u, v)
                found.append((point.X(), point.Y(), point.Z()))
    if not found:
        found = _points_from_mesh(face)
    if not found:
        raise FaceEditError("could not find an interior point of a face", "細い帯状の面は確かめられないので断る")
    if len(found) <= count:
        return found
    return [found[round(k * (len(found) - 1) / max(1, count - 1))] for k in range(count)]


def _points_from_mesh(face: Any) -> List[tuple]:
    """細い帯状の面では UV の格子が内側を1点も拾えない(PC ケースの実物で実測)。面の写しを三角形に分け、
    三角形の重心を面の曲面へ射影して、面の内側にあるものを使う(写すのは、三角形分割が面に書き込まれるため)。"""
    copy = TopoDS.Face_s(BRepBuilderAPI_Copy(face).Shape())
    box = Bnd_Box()
    BRepBndLib.Add_s(copy, box)
    xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
    BRepMesh_IncrementalMesh(copy, max(xmax - xmin, ymax - ymin, zmax - zmin, 1e-3) / 50.0, False, 0.2, True)
    location = TopLoc_Location()
    triangulation = BRep_Tool.Triangulation_s(copy, location)
    if triangulation is None:
        return []
    transform = location.Transformation()
    surface = BRep_Tool.Surface_s(face)
    found = []
    for t in range(1, triangulation.NbTriangles() + 1):
        corners = [triangulation.Node(k).Transformed(transform) for k in triangulation.Triangle(t).Get()]
        centre = gp_Pnt(*(sum(getattr(p, axis)() for p in corners) / 3 for axis in ("X", "Y", "Z")))
        projector = GeomAPI_ProjectPointOnSurf(centre, surface)
        if projector.NbPoints() == 0:
            continue
        u, v = _into_face_period(face, *projector.LowerDistanceParameters())
        if BRepClass_FaceClassifier(face, gp_Pnt2d(u, v), 1e-7).State() == TopAbs_State.TopAbs_IN:
            point = projector.NearestPoint()
            found.append((point.X(), point.Y(), point.Z()))
    return found


def _into_face_period(face: Any, u: float, v: float):
    """周期面では、射影の角度が面の持つ範囲から一周ずれて返ることがある。面の UV の範囲へ寄せる
    (寄せないと面の内側の点を「外」と判定し、実物の STEP の穴・丸めの正しい削除を断った)。"""
    u1, _, v1, _ = BRepTools.UVBounds_s(face)
    adaptor = BRepAdaptor_Surface(face)
    if adaptor.IsUPeriodic():
        period = adaptor.UPeriod()
        u = u1 + math.fmod(math.fmod(u - u1, period) + period, period)
    if adaptor.IsVPeriodic():
        period = adaptor.VPeriod()
        v = v1 + math.fmod(math.fmod(v - v1, period) + period, period)
    return u, v


# --------------------------------------------------------------------------------------
# 小道具
# --------------------------------------------------------------------------------------
_SURFACE_NAMES = {
    GeomAbs_SurfaceType.GeomAbs_Plane: "plane",
    GeomAbs_SurfaceType.GeomAbs_Cylinder: "cylinder",
    GeomAbs_SurfaceType.GeomAbs_Cone: "cone",
    GeomAbs_SurfaceType.GeomAbs_Sphere: "sphere",
    GeomAbs_SurfaceType.GeomAbs_Torus: "torus",
    GeomAbs_SurfaceType.GeomAbs_BSplineSurface: "bspline",
    GeomAbs_SurfaceType.GeomAbs_BezierSurface: "bezier",
}


def _surface_type(face: Any) -> str:
    return _SURFACE_NAMES.get(BRepAdaptor_Surface(face).GetType(), "other")


def _other_face(edge_faces: Any, edge: Any, face: Any) -> Optional[Any]:
    index = edge_faces.FindIndex(edge)
    if index == 0:
        return None
    others = [TopoDS.Face_s(f) for f in edge_faces.FindFromIndex(index) if not f.IsSame(face)]
    return others[0] if len(others) == 1 else None


def _tangent(edge: Any, a: Any, b: Any) -> bool:
    return ChFi3d.IsTangentFaces_s(edge, a, b, GeomAbs_Shape.GeomAbs_G1)


def _is_spring_edge(edge: Any, definition: tuple) -> bool:
    """丸めの長手の辺(円筒なら軸に平行な直線、トーラスなら軸と同軸の円)か。"""
    curve = BRepAdaptor_Curve(edge)
    kind = definition[0]
    if kind == "cylinder" and curve.GetType() == GeomAbs_CurveType.GeomAbs_Line:
        return _parallel(_vec(curve.Line().Direction()), definition[2])
    if kind == "torus" and curve.GetType() == GeomAbs_CurveType.GeomAbs_Circle:
        circle = curve.Circle()
        return _parallel(_vec(circle.Axis().Direction()), definition[2]) and (
            _point_line_distance(_vec(circle.Location()), definition[1], definition[2]) < LINEAR_TOL
        )
    return False


def _vec(d: Any) -> tuple:
    return (d.X(), d.Y(), d.Z())


def _axis(axis: Any) -> tuple:
    return _vec(axis.Location()), _vec(axis.Direction())


def _sub(a: tuple, b: tuple) -> tuple:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot(a: tuple, b: tuple) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _parallel(a: tuple, b: tuple) -> bool:
    return abs(abs(_dot(a, b)) - 1.0) < ANGULAR_TOL


def _point_line_distance(point: tuple, origin: tuple, direction: tuple) -> float:
    v = _sub(point, origin)
    along = _dot(v, direction)
    return math.sqrt(max(0.0, _dot(v, v) - along * along))
