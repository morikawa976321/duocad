"""押し出し・回転体・ロフトの幾何(OCPを触るカーネル層)。

面の押し出し(`extrude_faces`)、スケッチの領域の押し出し(`extrude_regions`)・回転体(`revolve_regions`)・
ロフト(`loft_regions`)を持つ。
位相・幾何量・形状検査は `brep_topology` / `brep_geometry` / `brep_checks` を参照する。
ビルダの履歴と面の同一性を使う由来処理は `brep_history` を参照する。

戻り値の規律は kernel と同じ(JSON化できる素の値だけ)。例外は `"shape"`(TopoDS_Shape。
document.Body.shape になる値)。
"""
from __future__ import annotations

import math
from typing import Any, List, Optional, Sequence, Tuple

from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepAlgoAPI import BRepAlgoAPI_Check, BRepAlgoAPI_Common, BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepBuilderAPI import (
    BRepBuilderAPI_MakeEdge,
    BRepBuilderAPI_MakeFace,
    BRepBuilderAPI_MakePolygon,
    BRepBuilderAPI_MakeSolid,
    BRepBuilderAPI_MakeVertex,
    BRepBuilderAPI_MakeWire,
    BRepBuilderAPI_RightCorner,
    BRepBuilderAPI_Sewing,
    BRepBuilderAPI_Transform,
)
from OCP.BRepLProp import BRepLProp_CLProps
from OCP.BRepOffsetAPI import BRepOffsetAPI_DraftAngle, BRepOffsetAPI_MakeDraft, BRepOffsetAPI_ThruSections
from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism, BRepPrimAPI_MakeRevol
from OCP.BRepTools import BRepTools
from OCP.Bnd import Bnd_Box
from OCP.GCPnts import GCPnts_AbscissaPoint
from OCP.Geom2dLProp import Geom2dLProp_CurAndInf2d
from OCP.LProp import LProp_NotDefined
from OCP.GeomAbs import GeomAbs_BezierCurve, GeomAbs_BSplineCurve, GeomAbs_Circle, GeomAbs_Line, GeomAbs_Plane
from OCP.Precision import Precision
from OCP.ShapeAnalysis import ShapeAnalysis_FreeBounds
from OCP.ShapeFix import ShapeFix_Solid
from OCP.TopAbs import (
    TopAbs_EDGE,
    TopAbs_FACE,
    TopAbs_REVERSED,
    TopAbs_SHELL,
    TopAbs_SOLID,
    TopAbs_VERTEX,
    TopAbs_WIRE,
)
from OCP.TopExp import TopExp, TopExp_Explorer
from OCP.TopTools import TopTools_IndexedMapOfShape  # noqa: F401  型注釈だけ
from OCP.TopoDS import TopoDS, TopoDS_Shape  # noqa: F401  TopoDS_Shape は型注釈だけ
from OCP.gp import gp_Ax1, gp_Dir, gp_Pln, gp_Pnt, gp_Trsf, gp_Vec

from . import (
    brep_checks as _brep_checks,
    brep_geometry as _brep_geometry,
    brep_history as _brep_history,
    brep_operations as _brep_operations,
    brep_planar as _brep_planar,
    brep_topology as _brep_topology,
)


#: 面の同一性を「重心の一致」で判定するときの相対許容差(after_face_index の探索)。
_MATCH_TOL = 1.0e-6

#: 軸の両側にまたがるかの判定で、少ない側の面積が全体のこの割合を超えたら「横切る」(軸上の辺の共通部分は面積0)。
_AXIS_SIDE_TOL = 1.0e-9


#: 押し出し量の絶対値の下限・上限[mm]。下限は OCCT の `Precision.Confusion`(1e-7)の10倍で、これ以下は
#: `BRepSweep_*` が `Standard_ConstructionError` を投げる(1e-7 以下)か体積0(1.1e-7)になる。上限は 1 km で、
#: 1e300 級は `gp_Dir` が `Standard_ConstructionError` を投げる。
MIN_DISTANCE = 1e-6
MAX_DISTANCE = 1e6


def check_distance(distance: Any) -> float:
    """押し出し量を数にして検査する。0・非有限・下限未満・上限超過は ValueError。

    NaN / ±inf は `BRepPrimAPI_MakePrism` が NaN 座標の形を返し、直後の体積計算 `brep_geometry.volume` が native のスタック
    オーバーフローで **プロセスごと落ちる**(Python の except では捕まらない)。入口の型検査 `catalog.check_arguments`
    が先に断るが、WebSocket の `preview` はそれを通らないので、ここでも断る。
    """
    value = float(distance)
    if not math.isfinite(value):
        raise ValueError(f"distance must be a finite number, got {distance!r}")
    if value == 0.0:
        raise ValueError("distance must not be 0 (押し出し量0は形状を変えない)")
    if not MIN_DISTANCE <= abs(value) <= MAX_DISTANCE:
        raise ValueError(f"|distance| must be between {MIN_DISTANCE} and {MAX_DISTANCE:g} mm, got {distance!r}")
    return value


#: テーパー角の絶対値の上限(度、含む)。`BRepOffsetAPI_MakeDraft` は 89.5° まで正しい形を作り、89.6〜89.7° は
#: 断り、89.8° 以上は別の形(20×10 を 89.9° で体積 2000)を成功として返す(角度だけで決まり、距離・輪郭の大きさによらない。
#: 2026-10-18 実測)。
MAX_TAPER = 89.0


def check_taper(taper: Any) -> float:
    """テーパー角(度。正で太る・負で細る)を数にして検査する。非有限・`|taper| > 89` は ValueError。"""
    value = float(taper)
    if not math.isfinite(value) or abs(value) > MAX_TAPER:
        raise ValueError(f"taper must satisfy |taper| <= {MAX_TAPER:g} degrees, got {taper!r}")
    return value


def extrude_regions(
    faces: Sequence[Any],
    distance: float,
    mode: str,
    normal: Sequence[float],
    target_shape: Any = None,
    taper: float = 0.0,
) -> dict:
    """領域の面群を法線方向へ押し出し、join/cut/new した形状を返す(束ね方は `_combine_region_solids`)。

    `taper` は側面の傾き(度)。正で押し出すにつれて領域が外へ太り、負で細る(`_tapered_prism`)。

    戻り値(`"shape"` 以外はJSON化できる素の値):
        {"shape", "unified": bool, "volume_before": float | None, "volume_after": float,
         "provenance": {"verb": "extrude", "mode": str, "distance": float, "unified": bool,
                        "face_map": {"<旧添字>": [新添字, ...]}, "deleted": [...], "new_faces": [...]}}
    `taper` が 0 でなければ provenance に `"taper"` も入る。
    `face_map` の旧添字は `target_shape` の面(相手が無ければ空)。
    """
    if not faces:
        raise ValueError("extrude_regions requires at least one face")
    distance = check_distance(distance)
    taper = check_taper(taper)
    _check_region_mode(mode, target_shape)

    nx, ny, nz = float(normal[0]), float(normal[1]), float(normal[2])
    offset = gp_Vec(nx * distance, ny * distance, nz * distance)

    prisms: List[TopoDS_Shape] = []
    if taper:
        merged = _merged_regions(faces)
        label = "region #{}" if len(merged) == len(faces) else "merged region #{}"
        for position, face in enumerate(merged, start=1):
            prisms.append(_tapered_prism(face, offset, taper, label.format(position)))
    else:
        for face in faces:
            prism = BRepPrimAPI_MakePrism(TopoDS.Face_s(face), offset)
            if not prism.IsDone():
                raise ValueError("failed to build a prism from a region face")
            prism_shape = prism.Shape()
            if prism_shape is None or _brep_topology.is_null(prism_shape):
                raise ValueError("prism from a region face is empty")
            # 押し出し方向が面の平面内に寝ると体積0の退化ソリッドができ、`IsDone()` も
            # `IsNull()` も `BRepCheck_Analyzer` も通る。体積を見るのが唯一の関門。
            if _brep_geometry.volume(prism_shape) <= 0:
                raise ValueError(
                    "prism from a region face has no volume "
                    "(押し出し方向が領域の平面に寝ている)"
                )
            prisms.append(prism_shape)

    head = {"verb": "extrude", "mode": mode, "distance": distance}
    if taper:
        head["taper"] = taper
    return _combine_region_solids(prisms, mode, target_shape, head)


# --------------------------------------------------------------------------------------
# テーパー
# --------------------------------------------------------------------------------------
def _merged_regions(faces: Sequence[Any]) -> List[Any]:
    """隣り合う領域を1枚の面にまとめる(つながった成分ごとに1枚)。

    テーパーは選んだ領域の和の縁にだけ掛ける。領域ごとに傾けて束ねると、領域の境目に傾いた壁が残る。
    """
    if len(faces) <= 1:
        return [TopoDS.Face_s(face) for face in faces]
    union = _brep_planar._union_faces([TopoDS.Face_s(face) for face in faces])
    return _brep_planar._shape_faces(_brep_planar._unified(_brep_topology.compound(union)))


# 押し出した距離 z の断面は「元の領域を z·tan(taper) だけ外へずらした領域」(正で外へ。角は尖ったまま)。
# 外周も穴も領域の縁として動くので、正なら外周は広がり穴は狭まる(Autodesk Fusion の押し出しのテーパーと同じ)。
#
# 輪郭線ごとに `BRepOffsetAPI_MakeDraft` で傾いた側面を作り、底とふたで閉じた立体にして、外周の立体から
# 穴の立体を cut する。`LocOpe_DPrism` は穴を無視し、`BRepOffsetAPI_DraftAngle` はベジエ・楕円の側面を
# 傾けられない(実測は docs/CAD_LIBRARY_PITFALLS.md「テーパー付き押し出しの2経路」)。

#: 輪郭を縫い合わせる許容差(mm)。細かい順に試す。楕円を傾けた側面(B スプラインの近似)は、押し出し量 0.1 mm で
#: ふた・底との縫い目が 1e-6 では閉じず 1e-5 で閉じる(殻の `Tolerance` は 1e-7 と答えるので当てにならない。2026-10-19 実測)。
_SEW_TOLS = (1.0e-6, 1.0e-5, 1.0e-4)

#: 曲線の辺の曲率半径を測る刻み(辺1本あたり)。最小の曲率半径はこの標本の極小のまわりを詰めて求める。
_CURVATURE_SAMPLES = 64

#: テーパー付きの押し出し量の絶対値の下限(mm)。`BRepOffsetAPI_MakeDraft` は押し出し量 7e-5 mm 以下で
#: access violation を起こしプロセスごと落ちる(1e-4 は通る。角度・輪郭の大きさ 0.01〜1万倍によらない。2026-10-18 実測)。
MIN_TAPER_DISTANCE = 1.0e-3


def _tapered_prism(face: Any, offset: "gp_Vec", taper: float, label: str) -> Any:
    """平面の面 `face` を `offset` へ押し出し、側面を `taper` 度傾けた立体。潰れる組み合わせは ValueError。

    `offset` は面に垂直であること(領域はスケッチの法線、面は面の法線へ押す)。OCCT の例外も ValueError にする
    (OCP の例外は共通の基底を持たず `Exception` を直に継ぐので、型のモジュールで見分ける)。
    """
    try:
        return _build_tapered_prism(face, offset, taper, label)
    except ValueError:
        raise
    except Exception as error:
        if not type(error).__module__.startswith("OCP"):
            raise
        raise ValueError(f"failed to taper {label}: {type(error).__name__}: {error}") from error


def _build_tapered_prism(face: Any, offset: "gp_Vec", taper: float, label: str) -> Any:
    height = offset.Magnitude()
    if height < MIN_TAPER_DISTANCE:
        raise ValueError(
            f"a tapered extrusion needs |distance| >= {MIN_TAPER_DISTANCE:g} mm, got {height:g} "
            "(それより短いテーパーは OCCT がプロセスごと落ちる)"
        )
    axis = gp_Dir(offset)
    angle = math.radians(abs(taper))
    reach = height * math.tan(angle)
    grow = taper > 0
    outer = BRepTools.OuterWire_s(face)
    body = _drafted_solid(outer, axis, height, angle, reach, inward=not grow, label=label)
    explorer = TopExp_Explorer(face, TopAbs_WIRE)
    holes = []
    while explorer.More():
        wire = TopoDS.Wire_s(explorer.Current())
        if not wire.IsSame(outer):
            holes.append(_drafted_solid(wire, axis, height, angle, reach, inward=grow, label=label))
        explorer.Next()
    for hole in holes:
        cut = BRepAlgoAPI_Cut(body, hole)
        cut.Build()
        if not cut.IsDone():
            raise ValueError(f"failed to cut a hole out of the tapered {label}")
        body = cut.Shape()
    _require_uncollapsed(body, face, offset, len(holes), label)
    return body


def _drafted_solid(
    wire: Any, axis: "gp_Dir", height: float, angle: float, reach: float, inward: bool, label: str
) -> Any:
    """閉じた輪郭線 `wire` の囲む領域を `axis` へ `height` 押し出し、側面を `angle`(ラジアン)だけ
    内向き(`inward`)か外向きに傾けた立体。

    直線と円弧だけの輪郭は `_analytic_drafted_solid`(柱の側面を `BRepOffsetAPI_DraftAngle` で傾ける。面は平面と円錐の
    ままで体積も正確)。楕円・ベジエ・B スプラインを含む輪郭は `DraftAngle` が傾けられないので `MakeDraft` で側面を作る。
    `MakeDraft` は長さ 0.14 mm の辺(0.1 mm の面取り)を持つ多角形で、角度によらず Standard_OutOfRange を投げる
    (sol レビューで再現。同じ輪郭を `DraftAngle` は作れ、面取り辺が消える角度では `IsDone()=False` を返す)。
    """
    wire = _regular_wire(wire)
    _require_curvature_room(wire, reach, inward, label)
    if all(BRepAdaptor_Curve(edge).GetType() in (GeomAbs_Line, GeomAbs_Circle) for edge in _brep_topology._extrude_edges(wire)):
        return _analytic_drafted_solid(wire, axis, height, angle, inward, label)
    wire = _split_closed_edge(wire)
    first_failure = None
    for start in (wire, *_midpoint_starts(wire)):
        try:
            return _swept_drafted_solid(start, axis, height, angle, inward, label)
        except Exception as error:
            if not isinstance(error, ValueError) and not type(error).__module__.startswith("OCP"):
                raise
            first_failure = first_failure or error
    raise first_failure


#: 輪郭の始点を辺の中点へ移して `MakeDraft` をやり直す回数の上限(長い辺から)。
_DRAFT_RESTARTS = 3


def _midpoint_starts(wire: Any) -> List[Any]:
    """`wire` の長い辺から順に、その辺の中点を始点にした同じ輪郭(最大 `_DRAFT_RESTARTS` 本)。

    `MakeDraft` は始点(=終点)の角で、最初と最後の側面をつながずに隙間を残すことがある(縫っても閉じない)。
    円と楕円の和の輪郭が角から始まると内外どちらへも失敗し、同じ輪郭を辺の中点から始めると通る(2026-10-31 実測)。
    中点は滑らかなので、そこで閉じる側面どうしは角の処理を要らない。
    """
    edges = _brep_topology._extrude_edges(wire)
    if len(edges) < 2:
        return []
    order = sorted(range(len(edges)), key=lambda k: -GCPnts_AbscissaPoint.Length_s(BRepAdaptor_Curve(edges[k])))
    starts = []
    for k in order[:_DRAFT_RESTARTS]:
        curve = BRepAdaptor_Curve(edges[k])
        head, tail = _split_edge(edges[k], [(curve.FirstParameter() + curve.LastParameter()) / 2.0])
        starts.append(_make_wire([tail, *edges[k + 1:], *edges[:k], head]))
    return starts


def _swept_drafted_solid(wire: Any, axis: "gp_Dir", height: float, angle: float, inward: bool, label: str) -> Any:
    """`MakeDraft` で傾いた側面を作り、底とふたで閉じた立体(`_drafted_solid` の曲線を含む輪郭の経路)。"""
    draft = BRepOffsetAPI_MakeDraft(wire, axis, angle)
    draft.SetOptions(BRepBuilderAPI_RightCorner)
    # 内か外かは角度の符号でも線の向きでもなく、この指定で決まる(既定は外)。Perform の長さは斜辺。
    draft.SetDraft(inward)
    draft.Perform(height / math.cos(angle))
    if not draft.IsDone():
        raise ValueError(_collapse_message(label))
    shell = draft.Shell()
    if not _brep_checks.is_valid(shell):
        raise ValueError(_collapse_message(label))

    base = BRepBuilderAPI_MakeFace(wire, True)
    if not base.IsDone():
        raise ValueError(f"failed to rebuild the base of the tapered {label}")
    tops = _far_closed_wires(shell, base.Face(), height)
    if len(tops) != 1:
        raise ValueError(_collapse_message(label))
    top = BRepBuilderAPI_MakeFace(tops[0], True)
    if not top.IsDone():
        raise ValueError(_collapse_message(label))

    for tolerance in _SEW_TOLS:
        solid = _sewn_solid((base.Face(), shell, top.Face()), tolerance)
        if solid is not None:
            return solid
    raise ValueError(_collapse_message(label))


def _analytic_drafted_solid(
    wire: Any, axis: "gp_Dir", height: float, angle: float, inward: bool, label: str
) -> Any:
    """直線と円弧だけの輪郭の柱を作り、上下のふた以外の面を底の平面を中立面として `angle` だけ傾ける。

    `DraftAngle.Add` の角度は正で押し出す向きに細る(実測)。
    """
    base = BRepBuilderAPI_MakeFace(wire, True)
    if not base.IsDone():
        raise ValueError(f"failed to rebuild the base of the tapered {label}")
    prism = BRepPrimAPI_MakePrism(base.Face(), gp_Vec(axis).Multiplied(height))
    if not prism.IsDone():
        raise ValueError(f"failed to build the prism of the tapered {label}")
    neutral = gp_Pln(BRepAdaptor_Surface(base.Face()).Plane().Location(), axis)
    draft = BRepOffsetAPI_DraftAngle(prism.Shape())
    explorer = TopExp_Explorer(prism.Shape(), TopAbs_FACE)
    while explorer.More():
        side = TopoDS.Face_s(explorer.Current())
        explorer.Next()
        surface = BRepAdaptor_Surface(side)
        if surface.GetType() == GeomAbs_Plane and surface.Plane().Axis().Direction().IsParallel(axis, Precision.Angular_s()):
            continue
        draft.Add(side, axis, angle if inward else -angle, neutral)
        if not draft.AddDone():
            raise ValueError(_collapse_message(label))
    draft.Build()
    if not draft.IsDone():
        raise ValueError(_collapse_message(label))
    solid = draft.Shape()
    if _brep_topology._extrude_count(solid, TopAbs_SOLID) != 1 or not _brep_geometry.volume(solid) > 0 or not _brep_checks.is_valid(solid):
        raise ValueError(_collapse_message(label))
    return TopoDS.Solid_s(TopExp_Explorer(solid, TopAbs_SOLID).Current())


#: 1本の辺で閉じた輪郭を `MakeDraft` に渡す前に分ける数。
_CLOSED_EDGE_PIECES = 4


def _split_closed_edge(wire: Any) -> Any:
    """1本の辺だけで閉じた輪郭(楕円・閉じたベジエ)を、パラメータで等分した `_CLOSED_EDGE_PIECES` 本の辺に分けた線。

    継ぎ目が曲率のきつい点にあると `MakeDraft` が Standard_OutOfRange を投げる(幅120・高さ1 の楕円は長軸の端が継ぎ目で、
    角度・距離によらず失敗した。継ぎ目を短軸の端へ移すか、4本に分ければ通り、体積は Steiner の式と相対 1e-7 で合う。
    2026-10-19 実測、sol レビュー)。
    """
    edges = _brep_topology._extrude_edges(wire)
    if len(edges) != 1:
        return wire
    curve = BRepAdaptor_Curve(edges[0])
    first, last = curve.FirstParameter(), curve.LastParameter()
    cuts = [first + (last - first) * k / _CLOSED_EDGE_PIECES for k in range(1, _CLOSED_EDGE_PIECES)]
    return _make_wire(_split_edge(edges[0], cuts))


def _sewn_solid(parts: Sequence[Any], tolerance: float) -> Optional[Any]:
    """面を縫って閉じた立体にする。自由辺が残るか、体積が無いか、`BRepCheck` が偽なら None。"""
    sewing = BRepBuilderAPI_Sewing(tolerance)
    for part in parts:
        sewing.Add(part)
    sewing.Perform()
    shells = TopExp_Explorer(sewing.SewedShape(), TopAbs_SHELL)
    if sewing.NbFreeEdges() or not shells.More():
        return None
    solid = BRepBuilderAPI_MakeSolid(TopoDS.Shell_s(shells.Current())).Solid()
    fix = ShapeFix_Solid(solid)
    fix.Perform()
    solid = fix.Solid()
    if not _brep_geometry.volume(solid) > 0 or not _brep_checks.is_valid(solid):
        return None
    return solid


#: 端で重なった制御点をずらす量(mm)。ベジエ・B スプラインの形は端点を保ったままこれ未満しか動かない。
_POLE_NUDGE = 1.0e-7


def _regular_wire(wire: Any) -> Any:
    """端の制御点が重なったベジエ・B スプラインの辺を、重なった制御点を次の別の制御点の方へ `_POLE_NUDGE` だけ
    ずらした辺に置き換えた線(該当する辺が無ければ `wire` のまま)。

    端の1階微分が0の辺(SVG の `C 0 0 0 0 20 0` など。書き出しでよく出る)は `MakeDraft` が
    `gp_Vec::Normalize` の Standard_ConstructionError で失敗する(sol レビューで再現)。
    """
    edges = _brep_topology._extrude_edges(wire)
    rebuilt = [_regular_edge(edge) for edge in edges]
    if all(new is old for new, old in zip(rebuilt, edges)):
        return wire
    return _make_wire(rebuilt)


def _regular_edge(edge: Any) -> Any:
    curve = BRepAdaptor_Curve(edge)
    kind = curve.GetType()
    if kind not in (GeomAbs_BezierCurve, GeomAbs_BSplineCurve):
        return edge
    geometry = BRep_Tool.Curve_s(edge, curve.FirstParameter(), curve.LastParameter())
    if abs(curve.FirstParameter() - geometry.FirstParameter()) > 1e-12 or abs(
        curve.LastParameter() - geometry.LastParameter()
    ) > 1e-12:
        return edge  # 曲線の一部だけを使う辺は、端の制御点と辺の端が一致しない
    copy = geometry.Copy()
    poles = [copy.Pole(i) for i in range(1, copy.NbPoles() + 1)]
    first = TopExp.FirstVertex_s(edge)
    last = TopExp.LastVertex_s(edge)
    if _on_chord(poles):
        # 制御点がすべて弦の上なら形は線分そのもの。極をずらすと、両端の極が重なった直線(SVG の `C0 0 20 0 20 0`)で
        # 傾けた面の体積の積分が 2.4% ずれる(sol レビューで再現)ので、線分に置き換える。
        line = BRepBuilderAPI_MakeEdge(first, last).Edge()
        return TopoDS.Edge_s(line.Reversed()) if edge.Orientation() == TopAbs_REVERSED else line
    changed = False
    for end, step in ((0, 1), (len(poles) - 1, -1)):
        nearest = end + step
        if poles[end].Distance(poles[nearest]) > Precision.Confusion_s():
            continue
        other = nearest
        while 0 <= other < len(poles) and poles[other].Distance(poles[end]) <= Precision.Confusion_s():
            other += step
        if not 0 <= other < len(poles):
            continue
        toward = gp_Vec(poles[end], poles[other])
        toward.Normalize()
        copy.SetPole(nearest + 1, poles[end].Translated(toward.Multiplied(_POLE_NUDGE)))
        changed = True
    if not changed:
        return edge
    rebuilt = BRepBuilderAPI_MakeEdge(copy, first, last).Edge()
    return TopoDS.Edge_s(rebuilt.Reversed()) if edge.Orientation() == TopAbs_REVERSED else rebuilt


def _on_chord(poles: Sequence[Any]) -> bool:
    """制御点がすべて始点と終点を結ぶ線分の上(線分の外へ出ない)にあるか。"""
    start, end = poles[0], poles[-1]
    chord = gp_Vec(start, end)
    length = chord.Magnitude()
    if length <= Precision.Confusion_s():
        return False
    chord.Divide(length)
    for pole in poles:
        offset = gp_Vec(start, pole)
        along = offset.Dot(chord)
        if offset.Crossed(chord).Magnitude() > Precision.Confusion_s():
            return False
        if along < -Precision.Confusion_s() or along > length + Precision.Confusion_s():
            return False
    return True


def _far_closed_wires(shell: Any, base: Any, height: float) -> List[Any]:
    """殻の自由境界の閉じた線のうち、底面の平面から `height/2` より離れたもの(ふたの輪郭)。"""
    plane = BRepAdaptor_Surface(base).Plane()
    bounds = ShapeAnalysis_FreeBounds(shell, _SEW_TOLS[0], False, False)
    explorer = TopExp_Explorer(bounds.GetClosedWires(), TopAbs_WIRE)
    out = []
    while explorer.More():
        wire = TopoDS.Wire_s(explorer.Current())
        vertex = TopExp_Explorer(wire, TopAbs_VERTEX)
        point = BRep_Tool.Pnt_s(TopoDS.Vertex_s(vertex.Current()))
        if plane.Distance(point) > height / 2.0:
            out.append(wire)
        explorer.Next()
    return out


def _require_curvature_room(wire: Any, reach: float, inward: bool, label: str) -> None:
    """曲線の辺が、曲率の中心の側へ曲率半径以上ずれるなら ValueError(輪郭が尖点で折り返す)。

    円が頂点を越えて細ると砂時計形の円錐ができ、`BRepCheck` も自己交差の検査も通す(実測)ので、
    曲率で先に断る。中心が輪郭の内か外かは、面の上での辺の向き(面の法線について左が内側)で決める。
    中心寄りの点を面に分類させると、半円の円弧の端で点が直径の上に乗り「外」になる。
    """
    region = BRepBuilderAPI_MakeFace(wire, True).Face()
    up = BRepAdaptor_Surface(region).Plane().Axis().Direction()
    if region.Orientation() == TopAbs_REVERSED:
        up.Reverse()
    explorer = TopExp_Explorer(region, TopAbs_EDGE)
    while explorer.More():
        edge = TopoDS.Edge_s(explorer.Current())
        explorer.Next()
        curve = BRepAdaptor_Curve(edge)
        if curve.GetType() == GeomAbs_Line:
            continue
        for t in _sharpest_parameters(edge, region, curve):
            props = BRepLProp_CLProps(curve, t, 2, Precision.Confusion_s())
            # 極が重なったベジエ(形は直線)の端は1階微分が0で、接線は定まるが `Normal` が LProp_NotDefined を投げる。
            # その点は曲がっていないものとして飛ばす(近くの標本が実際の曲率を拾う)。
            if not props.IsTangentDefined():
                continue
            curvature = props.Curvature()
            if not math.isfinite(curvature) or curvature <= 0.0 or 1.0 / curvature > reach:
                continue
            towards, tangent = gp_Dir(), gp_Dir()
            try:
                props.Normal(towards)
            except LProp_NotDefined:
                continue
            props.Tangent(tangent)
            if edge.Orientation() == TopAbs_REVERSED:
                tangent.Reverse()
            inside = up.Crossed(tangent).Dot(towards) > 0.0
            if inside == inward:
                raise ValueError(_collapse_message(label))


def _sharpest_parameters(edge: Any, region: Any, curve: Any) -> List[float]:
    """曲率を見るパラメータ: 曲率の極値(`Geom2dLProp_CurAndInf2d` で面の上の2次元曲線から式で求める)、
    等間隔の標本、標本の曲率の極大のまわりを黄金分割で詰めた点。

    標本だけでは継ぎ目をずらした楕円の長軸の端を、黄金分割だけでは区間に極大が複数ある鋭いベジエの山(曲率半径 0.001 mm)を
    見逃す(どちらも sol レビューで再現)。極値が求まらない曲線(極が重なった直線状のベジエ)は標本と黄金分割に任せる。
    """
    first, last = curve.FirstParameter(), curve.LastParameter()
    params = [first + (last - first) * k / _CURVATURE_SAMPLES for k in range(_CURVATURE_SAMPLES + 1)]
    values = [_curvature(curve, t) for t in params]
    out = list(params) + _curvature_extrema(edge, region, first, last)
    for k, value in enumerate(values):
        low = values[k - 1] if k > 0 else -1.0
        high = values[k + 1] if k < _CURVATURE_SAMPLES else -1.0
        if value >= low and value >= high and (value > low or value > high):
            out.append(_golden_max(curve, params[max(k - 1, 0)], params[min(k + 1, _CURVATURE_SAMPLES)]))
    return out


def _curvature_extrema(edge: Any, region: Any, first: float, last: float) -> List[float]:
    """辺の面の上の2次元曲線(平面の面では3次元の辺とパラメータが一致する)の曲率の極値のパラメータ。"""
    pcurve = BRep_Tool.CurveOnSurface_s(edge, region, first, last)
    if pcurve is None:
        return []
    finder = Geom2dLProp_CurAndInf2d()
    finder.PerformCurExt(pcurve)
    if not finder.IsDone():
        return []
    return [
        finder.Parameter(i)
        for i in range(1, finder.NbPoints() + 1)
        if first <= finder.Parameter(i) <= last
    ]


def _golden_max(curve: Any, low: float, high: float) -> float:
    """区間 [low, high] で曲率が最大になるパラメータ(黄金分割)。"""
    ratio = (math.sqrt(5.0) - 1.0) / 2.0
    a, b = low + (1.0 - ratio) * (high - low), low + ratio * (high - low)
    fa, fb = _curvature(curve, a), _curvature(curve, b)
    for _ in range(60):
        if fa >= fb:
            high, b, fb = b, a, fa
            a = low + (1.0 - ratio) * (high - low)
            fa = _curvature(curve, a)
        else:
            low, a, fa = a, b, fb
            b = low + ratio * (high - low)
            fb = _curvature(curve, b)
    return (low + high) / 2.0


def _curvature(curve: Any, t: float) -> float:
    props = BRepLProp_CLProps(curve, t, 2, Precision.Confusion_s())
    curvature = props.Curvature() if props.IsTangentDefined() else 0.0
    return curvature if math.isfinite(curvature) else 0.0


def _require_uncollapsed(body: Any, face: Any, offset: "gp_Vec", holes: int, label: str) -> None:
    """壁どうしがぶつかった形を断る: 立体が1つ、ふたが面1枚で穴の数が底と同じ、自己交差が無いこと。"""
    if _brep_topology._extrude_count(body, TopAbs_SOLID) != 1:
        raise ValueError(_collapse_message(label))
    lids = _lids(body, face, offset)
    if len(lids) != 1 or _brep_topology._extrude_count(lids[0], TopAbs_WIRE) != holes + 1:
        raise ValueError(_collapse_message(label))
    if not BRepAlgoAPI_Check(body, True, True).IsValid():
        raise ValueError(_collapse_message(label))


def _lids(body: Any, face: Any, offset: "gp_Vec") -> List[Any]:
    """`body` の平面の面のうち、重心が `face` の平面から `offset` の長さだけ離れたもの(押し出した先のふた)。"""
    plane = BRepAdaptor_Surface(face).Plane()
    height = offset.Magnitude()
    out = []
    explorer = TopExp_Explorer(body, TopAbs_FACE)
    while explorer.More():
        candidate = TopoDS.Face_s(explorer.Current())
        if BRepAdaptor_Surface(candidate).GetType() == GeomAbs_Plane:
            centre = _brep_geometry._face_props(candidate)[1]
            if abs(plane.Distance(gp_Pnt(*centre)) - height) <= _MATCH_TOL * max(1.0, height):
                out.append(candidate)
        explorer.Next()
    return out


def _collapse_message(label: str) -> str:
    return (
        f"taper collapses {label} (断面が潰れる): 側面どうしがぶつかるか、穴が閉じるか、輪郭が頂点を越える。"
        "テーパー角か押し出し量を小さくすること"
    )


def revolve_regions(
    faces: Sequence[Any],
    axis_point: Sequence[float],
    axis_dir: Sequence[float],
    normal: Sequence[float],
    angle: float,
    mode: str,
    target_shape: Any = None,
    symmetric: bool = False,
) -> dict:
    """領域の面群を、スケッチ平面上の軸(`axis_point` を通り `axis_dir` 向き)のまわりに `angle` 度回し、
    join/cut/new した形状を返す(束ね方は `_combine_region_solids`)。

    `angle` の正は `axis_dir` まわりの右ねじの向き、負はその逆。`0 < |angle| <= 360`。`symmetric` なら
    スケッチ平面から両側へ `|angle|/2` ずつ回す(符号は効かない)。`normal` はスケッチ平面の法線で、
    軸が領域を横切るかの判定にだけ使う。

    戻り値は `extrude_regions` と同じ形。provenance の `verb` は `"revolve"`、`distance` の代わりに `angle`。
    """
    if not faces:
        raise ValueError("revolve_regions requires at least one face")
    angle = float(angle)
    if not math.isfinite(angle) or angle == 0.0 or abs(angle) > 360.0:
        raise ValueError(f"angle must satisfy 0 < |angle| <= 360 (got {angle})")
    _check_region_mode(mode, target_shape)

    direction = gp_Dir(*(float(value) for value in axis_dir))
    origin = gp_Pnt(*(float(value) for value in axis_point))
    axis = gp_Ax1(origin, direction)
    plane_normal = gp_Dir(*(float(value) for value in normal))
    if abs(direction.Dot(plane_normal)) > Precision.Angular_s():
        raise ValueError("revolve axis must lie in the sketch plane")

    sweep = math.radians(abs(angle) if symmetric else angle)
    full = abs(angle) == 360.0
    solids: List[TopoDS_Shape] = []
    for position, face in enumerate(faces, start=1):
        profile = TopoDS.Face_s(face)
        _require_one_side_of_axis(profile, origin, direction, plane_normal, position)
        if symmetric and not full:
            turn = gp_Trsf()
            turn.SetRotation(axis, -sweep / 2.0)
            profile = TopoDS.Face_s(BRepBuilderAPI_Transform(profile, turn, True).Shape())
        # 360° は角度なしの形で作る(角度 2π を渡すと始端と終端の面が重なった閉じていない形になりうる)。
        maker = BRepPrimAPI_MakeRevol(profile, axis) if full else BRepPrimAPI_MakeRevol(profile, axis, sweep)
        if not maker.IsDone():
            raise ValueError(f"failed to revolve region #{position}")
        solid = maker.Shape()
        if solid is None or _brep_topology.is_null(solid) or _brep_geometry.volume(solid) <= 0:
            raise ValueError(f"revolving region #{position} produced no volume")
        solids.append(solid)

    return _combine_region_solids(
        solids,
        mode,
        target_shape,
        {"verb": "revolve", "mode": mode, "angle": angle, "symmetric": bool(symmetric)},
    )


def _require_one_side_of_axis(
    face: Any, origin: "gp_Pnt", direction: "gp_Dir", normal: "gp_Dir", position: int
) -> None:
    """面が軸の片側だけにあることを確かめる(軸上の辺・点で接するのは可)。横切れば ValueError。

    軸の両側に半平面を置き、面との共通部分の面積で決める。境界を折れ線で拾う方法は、軸をまたいで
    わずかに膨らむ円弧を取りこぼす。横切った面の回転体は自己交差し、`IsDone()` も体積検査も通りうる。
    """
    total, _ = _brep_geometry._face_props(face)
    box = Bnd_Box()
    BRepBndLib.Add_s(face, box)
    xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
    reach = 10.0 * (
        math.dist((xmin, ymin, zmin), (xmax, ymax, zmax))
        + math.dist((origin.X(), origin.Y(), origin.Z()), (xmin, ymin, zmin))
        + 1.0
    )
    along = gp_Vec(direction).Multiplied(reach)
    across = gp_Vec(normal).Crossed(gp_Vec(direction)).Multiplied(reach)
    sides = []
    for sign in (1.0, -1.0):
        corners = [
            origin.Translated(along.Reversed()),
            origin.Translated(along),
            origin.Translated(along).Translated(across.Multiplied(sign)),
            origin.Translated(along.Reversed()).Translated(across.Multiplied(sign)),
        ]
        polygon = BRepBuilderAPI_MakePolygon(*corners, True)
        half = BRepBuilderAPI_MakeFace(polygon.Wire(), True).Face()
        common = BRepAlgoAPI_Common(face, half)
        common.Build()
        if not common.IsDone():
            raise ValueError(f"failed to test region #{position} against the revolve axis")
        sides.append(_brep_geometry._extrude_shape_area(common.Shape()))
    if min(sides) > _AXIS_SIDE_TOL * total:
        raise ValueError(
            f"revolve axis crosses region #{position}: 軸の両側にまたがる領域は回せない"
            "(回転体が自己交差する)。軸の片側の領域だけを選ぶか、軸を領域の外か辺の上へ置くこと"
        )


def _check_region_mode(mode: str, target_shape: Any) -> None:
    """領域の押し出し・回転体に共通する mode と相手の組の検査。"""
    if mode not in ("join", "cut", "new"):
        raise ValueError(f"unknown mode: {mode!r} (one of ['join', 'cut', 'new'])")
    if mode == "cut" and (target_shape is None or _brep_topology.is_null(target_shape)):
        raise ValueError("cut requires a target shape")
    if mode == "new" and target_shape is not None and not _brep_topology.is_null(target_shape):
        # 相手を黙って無視すると「足したつもりが別ボディ」が成功として通る。
        raise ValueError("new must not take a target shape (新規ボディに行き先は無い)")


def _combine_region_solids(
    solids: Sequence[TopoDS_Shape], mode: str, target_shape: Any, head: dict
) -> dict:
    """領域から作った立体(柱・回転体)を束ね、相手へ join / cut する(new は束ねたものを返す)。

    同一平面の面の統合(`_unify_same_domain`)は、join / new で立体を束ねたとき(複数か、相手への join)にだけ掛ける。
    戻り値は `extrude_regions` の docstring の形。`head` は provenance の先頭に置く(`verb` ほか)。
    """
    combined = _brep_operations.fuse_all(solids, "region solids")

    volume_before: Optional[float] = None
    builder = None
    if mode in ("join", "new"):
        if target_shape is None or _brep_topology.is_null(target_shape):
            result = combined
        else:
            volume_before = _brep_geometry.volume(target_shape)
            builder = BRepAlgoAPI_Fuse(target_shape, combined)
            builder.Build()
            if not builder.IsDone():
                raise ValueError("join (fuse) failed")
            result = builder.Shape()
    else:
        volume_before = _brep_geometry.volume(target_shape)
        builder = BRepAlgoAPI_Cut(target_shape, combined)
        builder.Build()
        if not builder.IsDone():
            raise ValueError("cut failed")
        result = builder.Shape()

    volume_after = _brep_checks._checked_volume(result, mode, volume_before=volume_before)
    final, history, unified = result, None, False
    # 柱1本の new は束ねていないので統合しない。統合すると輪郭の分割で割れた側面までまとまり、
    # 後の操作が記録した面・辺の番号がずれる(実測: SVG を取り込んだ輪郭の柱1本で 38面 -> 26面)。
    if mode in ("join", "new") and (len(solids) > 1 or builder is not None):
        final, history, unified = _brep_operations.unify_same_domain(result, volume_after)
    _brep_checks._require_valid(final, mode)
    final_map = _brep_topology._face_map(final)
    successors = {} if builder is None else _brep_history.target_successors(builder, target_shape, history, final_map)

    return {
        "shape": final,
        "unified": unified,
        "volume_before": volume_before,
        "volume_after": _brep_geometry.volume(final),
        "provenance": {
            **head,
            "unified": unified,
            **_brep_topology._face_map_projection(successors, final_map.Extent()),
        },
    }


def extrude_faces(
    source_shape: "TopoDS_Shape",
    face_indices: Sequence[int],
    distance: float,
    mode: str,
    target_shape: Any = None,
    taper: float = 0.0,
) -> dict:
    """`source_shape` の**同一平面・同じ向きの平面の面**を `face_indices`(1始まり・選択順)で
    まとめて法線方向へ押し出し、`target_shape` へ join / cut する(`new` は柱だけを返す)。

    `distance > 0` が面の外向き法線方向。`mode` は符号と独立なので、面から削るには**負の距離で `cut`**
    (正で cut すると外側を削るだけで何も変わらず、「変化なし」の例外になる)。
    `taper` は `extrude_regions` と同じ(押し出す柱そのものが進むにつれて太る/細る)。

    許容値は OCCT の `Precision`(kernel に押し出し用の定数は無い。`_MATCH_TOL` は重心の突き合わせ用で
    桁が緩い): 法線のずれは `Precision.Angular_s`、平面までの距離は `Precision.Confusion_s`。

    同一平面の面の統合は `_unify_same_domain`(コピーで試して採否を決める)を join / cut と、柱が複数の `new` に掛ける。
    掛けないと押すたびに面が分裂して増える。柱1本の `new` は束ねていないので統合しない。

    戻り値(`"shape"` 以外はJSON化できる素の値):
        {"shape", "unified": bool, "normal": [nx,ny,nz], "distance": float,
         "volume_before": float | None, "volume_after": float,
         "after_face_indices": [int | None, ...],   # 押した面ごと。統合で消えたら None
         "provenance": {"verb": "extrude", "faces": [...], "mode": str, "distance": float,
                        "unified": bool, "face_map": {"<旧添字>": [新添字, ...]}, "deleted": [...],
                        "new_faces": [...], "after_face_indices": [...]}}
    `face_map` の旧添字は `target_shape` の面(`new` では空)。`faces` は `source_shape` の添字。
    """
    if source_shape is None or _brep_topology.is_null(source_shape):
        raise ValueError("cannot extrude faces of an empty shape")
    indices = [int(index) for index in face_indices]
    if not indices:
        raise ValueError("extrude_faces requires at least one face")
    if len(set(indices)) != len(indices):
        raise ValueError(f"face indices must be distinct, got {indices}")
    distance = check_distance(distance)
    taper = check_taper(taper)
    if mode not in ("join", "cut", "new"):
        raise ValueError(f"unknown extrude mode: {mode!r}")
    has_target = target_shape is not None and not _brep_topology.is_null(target_shape)
    if mode == "new" and has_target:
        raise ValueError("new must not take a target shape (新規ボディに行き先は無い)")
    if mode != "new" and not has_target:
        raise ValueError(f"{mode} requires a target shape")

    source_map = _brep_topology._face_map(source_shape)
    faces = [_brep_topology._face_at(source_map, index) for index in indices]
    normal, centroids = _check_coplanar_planes(faces, indices)

    prisms: List[TopoDS_Shape] = []
    # 押した面の行き先(ふたの重心)。テーパーではふたの重心が底の重心の平行移動にならない(L字など)。
    lid_centroids: List[tuple] = []
    for index, face, centroid in zip(indices, faces, centroids):
        offset = gp_Vec(normal[0] * distance, normal[1] * distance, normal[2] * distance)
        if taper:
            prism_shape = _tapered_prism(face, offset, taper, f"face {index}")
            prisms.append(prism_shape)
            lid_centroids.append(_brep_geometry._face_props(_lids(prism_shape, face, offset)[0])[1])
            continue
        lid_centroids.append(tuple(centroid[axis] + normal[axis] * distance for axis in range(3)))
        prism = BRepPrimAPI_MakePrism(face, offset)
        if not prism.IsDone():
            raise ValueError(f"failed to build the prism for face {index} (distance={distance})")
        prism_shape = prism.Shape()
        if prism_shape is None or _brep_topology.is_null(prism_shape):
            raise ValueError(f"prism for face {index} is empty (distance={distance})")
        prisms.append(prism_shape)
    combined = _brep_operations.fuse_all(prisms, "face prisms")

    context = f"faces {indices}, distance={distance}"
    volume_before: Optional[float] = None
    builder = None
    if mode == "new":
        result = combined
    else:
        volume_before = _brep_geometry.volume(target_shape)
        builder = (
            BRepAlgoAPI_Fuse(target_shape, combined)
            if mode == "join"
            else BRepAlgoAPI_Cut(target_shape, combined)
        )
        builder.Build()
        if not builder.IsDone():
            raise ValueError(f"{mode} failed for faces {indices} (distance={distance})")
        result = builder.Shape()

    try:
        volume_result = _brep_checks._checked_volume(result, mode, volume_before=volume_before, context=context)
    except ValueError as error:
        if mode == "cut" and distance > 0 and "did not change" in str(error):
            raise ValueError(
                f"{error}: 正の距離は面の外向き(ボディの外側)なので cut では何も削れない。"
                "面から削るには負の距離を使う"
            ) from None
        raise

    if mode == "new" and len(prisms) == 1:
        final, history, unified = result, None, False
    else:
        # 順序は 無変化 → 統合 → BRepCheck(統合は形状を壊しうるので、検査は統合後に行う)。
        final, history, unified = _brep_operations.unify_same_domain(result, volume_result)
    _brep_checks._require_valid(final, mode, context)
    final_map = _brep_topology._face_map(final)

    successors = {} if builder is None else _brep_history.target_successors(builder, target_shape, history, final_map)

    after_face_indices = [
        _find_translated_face(final_map, target=target, normal=normal, scale=abs(distance))
        for target in lid_centroids
    ]
    provenance = {
        "verb": "extrude",
        "faces": indices,
        "mode": mode,
        "distance": distance,
        **({"taper": taper} if taper else {}),
        "unified": unified,
        **_brep_topology._face_map_projection(successors, final_map.Extent()),
        "after_face_indices": after_face_indices,
    }
    return {
        "shape": final,
        "unified": unified,
        "normal": list(normal),
        "distance": distance,
        "volume_before": volume_before,
        "volume_after": _brep_geometry.volume(final),
        "after_face_indices": after_face_indices,
        "provenance": provenance,
    }


def _check_coplanar_planes(faces: Sequence[Any], indices: Sequence[int]) -> tuple:
    """全部が平面・同じ外向き法線・同一平面上であることを確かめ、`(法線, 各面の重心)` を返す(基準は先頭の面)。違反は ValueError。"""
    normals: List[tuple] = []
    centroids: List[tuple] = []
    for position, (index, face) in enumerate(zip(indices, faces), start=1):
        kind = _brep_geometry.classify_face(face)
        if kind != "plane":
            raise ValueError(
                f"selected face #{position} (index {index}) is {kind}: 押し出せるのは平面の面だけ"
                "(曲面は掃引すると壊れた形状になることを実測済み。docs/CAD_LIBRARY_PITFALLS.md)"
            )
        normals.append(_brep_geometry._face_normal(face))
        centroids.append(_brep_geometry._face_props(face)[1])

    first_normal, first_centroid = normals[0], centroids[0]
    for position in range(2, len(faces) + 1):
        index, normal, centroid = indices[position - 1], normals[position - 1], centroids[position - 1]
        cross = math.dist(
            (
                first_normal[1] * normal[2] - first_normal[2] * normal[1],
                first_normal[2] * normal[0] - first_normal[0] * normal[2],
                first_normal[0] * normal[1] - first_normal[1] * normal[0],
            ),
            (0.0, 0.0, 0.0),
        )
        dot = sum(first_normal[axis] * normal[axis] for axis in range(3))
        where = f"selected face #{position} (index {index}) vs #1 (index {indices[0]})"
        if cross > Precision.Angular_s():
            raise ValueError(f"{where}: 平行でない(別の平面)。同一平面の面だけ押し出せる")
        if dot < 0:
            raise ValueError(f"{where}: 外向き法線が逆向き。同じ向きの面だけ押し出せる")
        offset = abs(sum((centroid[axis] - first_centroid[axis]) * first_normal[axis] for axis in range(3)))
        if offset > Precision.Confusion_s():
            raise ValueError(
                f"{where}: 同一平面上にない(平面までの距離 {offset:.6g} mm)"
            )
    return first_normal, centroids


def _find_translated_face(
    shape_map: "TopTools_IndexedMapOfShape",
    target: tuple,
    normal: tuple,
    scale: float,
) -> Optional[int]:
    """重心が `target`・法線が `normal` と同じ向きの面の添字(無ければNone)。

    押し出した面が新形状のどれになったかを幾何で突き合わせる。由来マップ(`Modified`)はブーリアンでは
    空を返すことが多い(実測: Fuseは押し出した面を `IsDeleted` にするだけ)ため。
    """
    tolerance = _MATCH_TOL * max(1.0, abs(scale), *(abs(value) for value in target))
    best: Optional[tuple] = None
    for index in range(1, shape_map.Extent() + 1):
        face = TopoDS.Face_s(shape_map.FindKey(index))
        candidate_normal = _brep_geometry._face_normal(face)
        alignment = sum(candidate_normal[axis] * normal[axis] for axis in range(3))
        if alignment < 1.0 - _MATCH_TOL:
            continue
        _, centroid = _brep_geometry._face_props(face)
        distance = math.dist(centroid, target)
        if distance <= tolerance and (best is None or distance < best[0]):
            best = (distance, index)
    return None if best is None else best[1]

# ======================================================================================
# ロフト
# ======================================================================================
# 別々の平面にある断面を順につなぐ立体(`loft_regions`)。
#
# `BRepOffsetAPI_ThruSections` の自動の揃え(`CheckCompatibility`)には任せない。断面の向き・始点・辺の数を
# ここで揃えてから渡す(任せたときに壊れる条件の実測は docs/CAD_LIBRARY_PITFALLS.md「ロフト」)。
#
# - 向き: 各断面の法線を進む向き(隣の断面・点へ)に合わせ、外周の線をその法線について反時計回りにする
# - 始点: 前の断面の始点の向きを、前の法線 → この法線の最小回転で運び、それに最も近い向きの点を始点にする
# - 辺の数: 全断面を、どれかの断面の頂点がある向き(始点からの極角)すべてで割る。極角が線に沿って
#   単調でない断面(重心から見て裏に回り込む凹み)があれば、長さの割合で割る
# - `shifts`: 揃えた後の始点を断面ごとに頂点 k 個ぶん進める(負は戻す。頂点の無い閉曲線は 90° 単位)

Vec = Tuple[float, float, float]

#: 極角の比較(度)。これより近い向きは同じ頂点とみなす。
_ANGLE_TOL = 1.0e-4

#: 線に沿った極角を測る刻み(辺1本あたり)。始点の探索・単調性の判定・割る位置の二分法の初期区間に使う。
_SAMPLES_PER_EDGE = 64

#: 2つの平面を「同じ平面」とみなす距離(mm)。
_PLANE_TOL = 1.0e-7


def loft_regions(
    sections: Sequence[Tuple[Any, Sequence[float]]],
    mode: str,
    target_shape: Any = None,
    start_point: Optional[Sequence[float]] = None,
    end_point: Optional[Sequence[float]] = None,
    shifts: Optional[Sequence[int]] = None,
) -> dict:
    """断面(`(面, スケッチ平面の法線)` の並び)を順につないだ立体を作り、join/cut/new した形状を返す。

    `start_point` / `end_point` を渡すと、最初の断面の前・最後の断面の後をその点へ収束させる(尖らせる)。
    断面1つでも点が1つあればよい。穴のある面は拒む。`shifts` は断面ごとの整数(省略は全部0)。

    戻り値は `kernel_extrude.extrude_regions` と同じ形。provenance の `verb` は `"loft"`。
    """
    faces = [TopoDS.Face_s(face) for face, _ in sections]
    normals = [_unit(tuple(float(v) for v in normal)) for _, normal in sections]
    start = None if start_point is None else tuple(float(v) for v in start_point)
    end = None if end_point is None else tuple(float(v) for v in end_point)
    if not faces:
        raise ValueError("loft requires at least one section")
    if len(faces) + (start is not None) + (end is not None) < 2:
        raise ValueError("loft requires at least two sections, or one section and a point")
    shifts = [0] * len(faces) if shifts is None else [int(k) for k in shifts]
    if len(shifts) != len(faces):
        raise ValueError(f"shifts must have one entry per section ({len(faces)}), got {len(shifts)}")
    _check_region_mode(mode, target_shape)

    wires = []
    centroids = []
    for position, face in enumerate(faces, start=1):
        if _wire_count(face) > 1:
            raise ValueError(
                f"loft section #{position} has a hole: 穴のある領域は断面にできない"
                "(外周をロフトしてから、内側の領域を cut でロフトする)"
            )
        wires.append(BRepTools.OuterWire_s(face))
        centroids.append(_brep_geometry._extrude_area_centroid(face))
    _require_separate_planes(centroids, normals, start, end)

    oriented = _oriented_normals(centroids, normals, start, end)
    aligned = _aligned_wires(wires, centroids, oriented, shifts)
    failure: Optional[ValueError] = None
    candidates = _matched_candidates(aligned, centroids, oriented)
    for matched, own in candidates:
        try:
            solid = _thru_sections(matched, start, end, own)
            _require_sound_loft(solid, faces)
        except ValueError as error:
            failure = failure or error
            continue
        break
    else:
        if len(candidates) > 1:
            raise ValueError(
                f"{failure} —— 重心から見て裏に回り込む凹みのある断面(U字など)は対応が決まらないことがある。"
                "shifts でその断面の始点をずらすか、断面を凹みの無い形に分けること"
            )
        raise failure

    return _combine_region_solids(
        [solid], mode, target_shape, {"verb": "loft", "mode": mode, "sections": len(faces)}
    )


def _thru_sections(wires: Sequence[Any], start: Optional[Vec], end: Optional[Vec], own: bool) -> Any:
    """断面(と端の点)を `ThruSections` でつなぐ。`own` なら揃えは済んでいる(OCCT の揃えを切る)。"""
    maker = BRepOffsetAPI_ThruSections(True, False, 1.0e-6)
    maker.CheckCompatibility(not own)
    if start is not None:
        maker.AddVertex(BRepBuilderAPI_MakeVertex(gp_Pnt(*start)).Vertex())
    for wire in wires:
        maker.AddWire(wire)
    if end is not None:
        maker.AddVertex(BRepBuilderAPI_MakeVertex(gp_Pnt(*end)).Vertex())
    maker.Build()
    if not maker.IsDone():
        raise ValueError("failed to loft the sections (OCCT ThruSections)")
    return maker.Shape()


def _require_sound_loft(solid: Any, faces: Sequence[Any]) -> None:
    """潰れ・裏返り・自己交差を拒む。

    実測: 始点がずれて潰れた・裏返ったロフトは `BRepCheck_Analyzer` を通す。`BRepAlgoAPI_Check` は第3引数
    `bTestSI`(自己干渉)を True にしたときだけ捕まえる(25〜80 ms)。体積は断面の最大面積^1.5 を目安に比べる。
    """
    if solid is None or _brep_topology.is_null(solid):
        raise ValueError("loft produced an empty shape")
    scale = max(_brep_geometry._extrude_shape_area(face) for face in faces) ** 1.5
    if not _brep_geometry.volume(solid) > 1.0e-6 * scale:
        raise ValueError(
            "loft collapsed (体積が0か負): 断面どうしの対応がねじれて潰れている。"
            "shifts で断面の始点をずらすか、断面の間に断面を足すこと"
        )
    if not _brep_checks.is_valid(solid):
        raise ValueError("loft produced an invalid shape (BRepCheck)")
    if not BRepAlgoAPI_Check(solid, True, True).IsValid():
        raise ValueError(
            "loft intersects itself (自己交差): 断面の順番・置き場所か、shifts を見直すこと"
        )


# --------------------------------------------------------------------------------------
# 断面の置き場所
# --------------------------------------------------------------------------------------
def _require_separate_planes(
    centroids: Sequence[Vec], normals: Sequence[Vec], start: Optional[Vec], end: Optional[Vec]
) -> None:
    for i in range(len(centroids) - 1):
        if _same_plane(centroids[i], normals[i], centroids[i + 1], normals[i + 1]):
            raise ValueError(
                f"loft sections #{i + 1} and #{i + 2} lie on the same plane: 隣り合う断面は別の平面に置くこと"
            )
    for point, index, label in ((start, 0, "start_point"), (end, len(centroids) - 1, "end_point")):
        if point is not None and abs(_dot(_sub(point, centroids[index]), normals[index])) <= _PLANE_TOL:
            raise ValueError(f"loft {label} lies on the plane of section #{index + 1}: 点は断面の平面の外に置くこと")


def _same_plane(c1: Vec, n1: Vec, c2: Vec, n2: Vec) -> bool:
    if _length(_cross(n1, n2)) > 1.0e-9:
        return False
    return abs(_dot(_sub(c2, c1), n1)) <= _PLANE_TOL


def _oriented_normals(
    centroids: Sequence[Vec], normals: Sequence[Vec], start: Optional[Vec], end: Optional[Vec]
) -> List[Vec]:
    """各断面の法線を進む向き(次の断面・終点へ。最後の断面は前から来る向き)にそろえる。

    進む向きが平面に寝ていて決まらないときは、前の断面の法線と同じ側を取る。
    """
    count = len(centroids)
    out: List[Vec] = []
    for i in range(count):
        if i + 1 < count:
            travel = _sub(centroids[i + 1], centroids[i])
        elif end is not None:
            travel = _sub(end, centroids[i])
        elif i > 0:
            travel = _sub(centroids[i], centroids[i - 1])
        else:
            travel = _sub(centroids[i], start)
        side = _dot(normals[i], _unit(travel)) if _length(travel) > 0.0 else 0.0
        if abs(side) < 1.0e-6:
            side = _dot(normals[i], out[-1]) if out else 1.0
        out.append(normals[i] if side >= 0.0 else _scale(normals[i], -1.0))
    return out


# --------------------------------------------------------------------------------------
# 始点と向き
# --------------------------------------------------------------------------------------
def _aligned_wires(
    wires: Sequence[Any], centroids: Sequence[Vec], normals: Sequence[Vec], shifts: Sequence[int]
) -> List[Any]:
    """向きを法線に合わせ、始点を前の断面から運んだ向きに合わせ、`shifts` だけ進めた線の並び。

    辺の数が全断面で同じなら始点は頂点から選ぶ(角どうしがつながる)。違うなら運んだ向きのちょうどの点で
    辺を割って始点にする(三角形 → 正方形で、角を最寄りの角へ寄せると 45° ねじれる)。
    """
    same_count = len({len(_brep_topology._extrude_edges(wire)) for wire in wires}) == 1
    out: List[Any] = []
    reference: Optional[Vec] = None
    for i, wire in enumerate(wires):
        if _dot(_newell(_wire_points(wire)), normals[i]) < 0.0:
            wire = _reversed(wire)
        if i == 0:
            reference = _sub(_start(wire), centroids[0])
        else:
            reference = _rotate_between(reference, normals[i - 1], normals[i])
        wire = _start_toward(wire, centroids[i], normals[i], reference, shifts[i], vertex_only=same_count or i == 0)
        reference = _sub(_start(wire), centroids[i])
        out.append(wire)
    return out


def _start_toward(wire: Any, centre: Vec, normal: Vec, reference: Vec, shift: int, vertex_only: bool) -> Any:
    """`reference` の向き(`centre` から見た極角)に最も近い点を始点にし、そこから `shift` 頂点進めた線。"""
    edges = _brep_topology._extrude_edges(wire)
    if len(edges) == 1:
        turned = reference
        for _ in range(shift % 4):
            turned = _cross(normal, turned)
        return _closed_from(edges[0], _nearest_parameter(edges[0], centre, normal, turned))
    starts = [_point_on(edge, _traversal_parameters(edge)[0]) for edge in edges]
    nearest = min(range(len(edges)), key=lambda k: abs(_angle(_sub(starts[k], centre), normal, reference)))
    if not vertex_only and abs(_angle(_sub(starts[nearest], centre), normal, reference)) > _ANGLE_TOL:
        rotated = _split_at_direction(wire, centre, normal, reference)
        if rotated is not None:
            return _rolled(rotated, _shift_from_split(len(rotated), shift))
    return _rolled(edges, (nearest + shift) % len(edges))


def _shift_from_split(split_count: int, shift: int) -> int:
    """割って始点にした点から数えて shift 番目の元の頂点の添字(割った点は頂点に数えない)。

    割った線の辺は [割った点から, 元の頂点1から, 元の頂点2から, ..., 割った点の直前の元の頂点から] の順。
    """
    originals = split_count - 1
    if shift == 0:
        return 0
    if shift > 0:
        return (shift - 1) % originals + 1
    return originals - (-shift - 1) % originals


def _split_at_direction(wire: Any, centre: Vec, normal: Vec, reference: Vec) -> Optional[List[Any]]:
    """`reference` の向きの点で辺を割り、その点から始まる辺の並びを返す。その向きに線が無ければ None。"""
    edges = _brep_topology._extrude_edges(wire)
    for index, edge in enumerate(edges):
        params = _traversal_parameters(edge)
        angles = [_angle(_sub(_point_on(edge, t), centre), normal, reference) for t in params]
        for k in range(len(params) - 1):
            a, b = angles[k], angles[k + 1]
            if a <= 0.0 <= b and b - a < 90.0:
                t = _bisect_angle(edge, centre, normal, reference, params[k], params[k + 1], a, b, 0.0)
                head, tail = _split_edge(edge, [t])
                return [tail] + edges[index + 1:] + edges[:index] + [head]
    return None


def _rolled(edges: Sequence[Any], start: int) -> Any:
    return _make_wire(list(edges[start:]) + list(edges[:start]))


# --------------------------------------------------------------------------------------
# 辺の数をそろえる
# --------------------------------------------------------------------------------------
def _matched_candidates(
    wires: Sequence[Any], centroids: Sequence[Vec], normals: Sequence[Vec]
) -> List[Tuple[List[Any], bool]]:
    """辺の数をそろえた断面の並びの候補 `(線の並び, 自前で揃えたか)` を、試す順に返す。

    極角が単調でない断面(重心から見て裏に回り込む凹み)があるときだけ候補が2つになる: 長さの割合で割ったもの、
    だめなら OCCT の揃え(`CheckCompatibility`)。実測: U字 → 正方形は長さの割合だと側面が自己交差し OCCT の揃えなら
    通る。U字 → 円は逆で、OCCT の揃えは `IsDone()=False`。OCCT の揃えでは `shifts` が効かないことがある。
    """
    counts = {len(_brep_topology._extrude_edges(wire)) for wire in wires}
    if len(counts) == 1:
        return [(list(wires), True)]
    profiles = [_polar_profile(w, c, n) for w, c, n in zip(wires, centroids, normals)]
    if all(profile is not None for profile in profiles):
        angles = _merged([row[0][1] for profile in profiles for row in profile if row[0][1] > _ANGLE_TOL])
        matched = [
            _split_at_angles(w, c, n, profile, angles)
            for w, c, n, profile in zip(wires, centroids, normals, profiles)
        ]
        candidates = [(matched, True)]
    else:
        fractions = _merged([f for wire in wires for f in _vertex_fractions(wire)], tol=1.0e-9)
        candidates = [([_split_at_fractions(wire, fractions) for wire in wires], True), (list(wires), False)]
    usable = [(ws, own) for ws, own in candidates if not own or len({len(_brep_topology._extrude_edges(w)) for w in ws}) == 1]
    if not usable:
        raise ValueError("failed to match the number of edges between loft sections")
    return usable


def _polar_profile(wire: Any, centre: Vec, normal: Vec) -> Optional[List[List[Tuple[float, float]]]]:
    """辺ごとの `[(パラメータ, 始点からの極角(巻き戻し済み・0..360))]`(走査順)。単調でなければ None。"""
    reference = _sub(_start(wire), centre)
    out: List[List[Tuple[float, float]]] = []
    previous: Optional[float] = None
    for edge in _brep_topology._extrude_edges(wire):
        row = []
        for t in _traversal_parameters(edge):
            raw = _angle(_sub(_point_on(edge, t), centre), normal, reference) % 360.0
            if previous is None:
                value = 0.0 if raw > 360.0 - _ANGLE_TOL else raw
            else:
                value = raw + 360.0 * round((previous - raw) / 360.0)
                if value < previous - 1.0e-7:
                    value += 360.0
                if value < previous - 1.0e-7:
                    return None
            row.append((t, value))
            previous = value
        out.append(row)
    if previous is None or abs(previous - 360.0) > 1.0e-3:
        return None
    return out


def _split_at_angles(
    wire: Any, centre: Vec, normal: Vec, profile: Sequence[Sequence[Tuple[float, float]]], angles: Sequence[float]
) -> Any:
    reference = _sub(_start(wire), centre)
    vertices = [row[0][1] for row in profile] + [360.0]
    out: List[Any] = []
    for edge, row in zip(_brep_topology._extrude_edges(wire), profile):
        low, high = row[0][1], row[-1][1]
        cuts = [
            a for a in angles
            if low + _ANGLE_TOL < a < high - _ANGLE_TOL and all(abs(a - v) > _ANGLE_TOL for v in vertices)
        ]
        if not cuts:
            out.append(edge)
            continue
        params = []
        for a in cuts:
            k = next(k for k in range(len(row) - 1) if row[k][1] <= a <= row[k + 1][1])
            params.append(
                _bisect_angle(edge, centre, normal, reference, row[k][0], row[k + 1][0], row[k][1], row[k + 1][1], a)
            )
        out += _split_edge(edge, params)
    return _make_wire(out)


def _bisect_angle(
    edge: Any, centre: Vec, normal: Vec, reference: Vec, t_low: float, t_high: float, a_low: float, a_high: float,
    target: float,
) -> float:
    """辺の上で、極角が `target` になるパラメータを二分法で求める(区間の両端の極角は巻き戻し済み)。"""
    for _ in range(60):
        t_mid = (t_low + t_high) / 2.0
        guess = (a_low + a_high) / 2.0
        raw = _angle(_sub(_point_on(edge, t_mid), centre), normal, reference)
        value = raw + 360.0 * round((guess - raw) / 360.0)
        if value < target:
            t_low, a_low = t_mid, value
        else:
            t_high, a_high = t_mid, value
    return (t_low + t_high) / 2.0


def _vertex_fractions(wire: Any) -> List[float]:
    lengths = [_brep_geometry._extrude_edge_length(edge) for edge in _brep_topology._extrude_edges(wire)]
    total = sum(lengths)
    out, acc = [], 0.0
    for length in lengths[:-1]:
        acc += length
        out.append(acc / total)
    return out


def _split_at_fractions(wire: Any, fractions: Sequence[float]) -> Any:
    """線を、始点からの長さの割合 `fractions` の位置で割る(既にある頂点の位置は割らない)。"""
    edges = _brep_topology._extrude_edges(wire)
    lengths = [_brep_geometry._extrude_edge_length(edge) for edge in edges]
    total = sum(lengths)
    out: List[Any] = []
    acc = 0.0
    for edge, length in zip(edges, lengths):
        low, high = acc / total, (acc + length) / total
        inner = [f for f in fractions if low + 1.0e-6 < f < high - 1.0e-6]
        if not inner:
            out.append(edge)
        else:
            curve = BRepAdaptor_Curve(edge)
            first, last = curve.FirstParameter(), curve.LastParameter()
            if edge.Orientation() == TopAbs_REVERSED:
                params = [GCPnts_AbscissaPoint(curve, -(f - low) * total, last).Parameter() for f in inner]
            else:
                params = [GCPnts_AbscissaPoint(curve, (f - low) * total, first).Parameter() for f in inner]
            out += _split_edge(edge, params)
        acc += length
    return _make_wire(out)


def _merged(values: Sequence[float], tol: float = _ANGLE_TOL) -> List[float]:
    out: List[float] = []
    for value in sorted(values):
        if not out or value - out[-1] > tol:
            out.append(value)
    return out


# --------------------------------------------------------------------------------------
# 辺と線の小道具
# --------------------------------------------------------------------------------------


def _make_wire(edges: Sequence[Any]) -> Any:
    builder = BRepBuilderAPI_MakeWire()
    for edge in edges:
        builder.Add(TopoDS.Edge_s(edge))
    if not builder.IsDone():
        raise ValueError("failed to rebuild a loft section wire")
    return builder.Wire()


def _reversed(wire: Any) -> Any:
    return _make_wire([TopoDS.Edge_s(edge.Reversed()) for edge in reversed(_brep_topology._extrude_edges(wire))])


def _traversal_parameters(edge: Any, count: int = _SAMPLES_PER_EDGE) -> List[float]:
    curve = BRepAdaptor_Curve(edge)
    first, last = curve.FirstParameter(), curve.LastParameter()
    params = [first + (last - first) * i / count for i in range(count + 1)]
    return params[::-1] if edge.Orientation() == TopAbs_REVERSED else params


def _point_on(edge: Any, t: float) -> Vec:
    p = BRepAdaptor_Curve(edge).Value(t)
    return (p.X(), p.Y(), p.Z())


def _start(wire: Any) -> Vec:
    edge = _brep_topology._extrude_edges(wire)[0]
    return _point_on(edge, _traversal_parameters(edge, 1)[0])


def _wire_points(wire: Any) -> List[Vec]:
    points: List[Vec] = []
    for edge in _brep_topology._extrude_edges(wire):
        points += [_point_on(edge, t) for t in _traversal_parameters(edge, 24)[:-1]]
    return points


def _split_edge(edge: Any, params: Sequence[float]) -> List[Any]:
    """辺を `params`(どの順でもよい)で割り、走査順の辺の並びを返す。"""
    curve = BRepAdaptor_Curve(edge)
    first, last = curve.FirstParameter(), curve.LastParameter()
    geometry = BRep_Tool.Curve_s(edge, first, last)
    bounds = [first] + sorted(params) + [last]
    pieces = [BRepBuilderAPI_MakeEdge(geometry, bounds[k], bounds[k + 1]).Edge() for k in range(len(bounds) - 1)]
    if edge.Orientation() == TopAbs_REVERSED:
        pieces = [TopoDS.Edge_s(piece.Reversed()) for piece in reversed(pieces)]
    return pieces


def _nearest_parameter(edge: Any, centre: Vec, normal: Vec, reference: Vec) -> float:
    params = _traversal_parameters(edge, 720)
    best = min(params, key=lambda t: abs(_angle(_sub(_point_on(edge, t), centre), normal, reference)))
    step = abs(params[1] - params[0])
    return _bisect_toward(edge, centre, normal, reference, best - step, best + step)


def _bisect_toward(edge: Any, centre: Vec, normal: Vec, reference: Vec, low: float, high: float) -> float:
    for _ in range(60):
        third = (high - low) / 3.0
        a = abs(_angle(_sub(_point_on(edge, low + third), centre), normal, reference))
        b = abs(_angle(_sub(_point_on(edge, high - third), centre), normal, reference))
        if a < b:
            high -= third
        else:
            low += third
    return (low + high) / 2.0


def _closed_from(edge: Any, t: float) -> Any:
    """閉じた1辺(周期曲線)を、パラメータ `t` の点から1周する辺の線に作り直す(向きは保つ)。"""
    curve = BRepAdaptor_Curve(edge)
    first, last = curve.FirstParameter(), curve.LastParameter()
    geometry = BRep_Tool.Curve_s(edge, first, last)
    if not geometry.IsPeriodic():
        # 周期でない閉曲線(閉じたベジエ等)は継ぎ目を動かせないので、その点で2本に割る。
        if abs(t - first) < 1.0e-9 or abs(t - last) < 1.0e-9:
            return _make_wire([edge])
        head, tail = _split_edge(edge, [t])
        return _make_wire([tail, head])
    period = geometry.Period()
    rebuilt = BRepBuilderAPI_MakeEdge(geometry, t, t + period).Edge()
    if edge.Orientation() == TopAbs_REVERSED:
        rebuilt = TopoDS.Edge_s(rebuilt.Reversed())
    return _make_wire([rebuilt])


def _wire_count(face: Any) -> int:
    explorer = TopExp_Explorer(face, TopAbs_WIRE)
    count = 0
    while explorer.More():
        count += 1
        explorer.Next()
    return count


# --------------------------------------------------------------------------------------
# ベクトル
# --------------------------------------------------------------------------------------
def _sub(a: Vec, b: Vec) -> Vec:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _scale(a: Vec, s: float) -> Vec:
    return (a[0] * s, a[1] * s, a[2] * s)


def _dot(a: Vec, b: Vec) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a: Vec, b: Vec) -> Vec:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _length(a: Vec) -> float:
    return math.sqrt(_dot(a, a))


def _unit(a: Vec) -> Vec:
    length = _length(a)
    if not length > 0.0:
        raise ValueError("loft section normal has zero length")
    return _scale(a, 1.0 / length)


def _newell(points: Sequence[Vec]) -> Vec:
    total = (0.0, 0.0, 0.0)
    for i in range(len(points)):
        c = _cross(points[i], points[(i + 1) % len(points)])
        total = (total[0] + c[0], total[1] + c[1], total[2] + c[2])
    return total


def _angle(vector: Vec, normal: Vec, reference: Vec) -> float:
    """法線 `normal` の平面で、`reference` から `vector` への反時計回りの角度(度、-180..180)。"""
    v = _sub(vector, _scale(normal, _dot(vector, normal)))
    r = _sub(reference, _scale(normal, _dot(reference, normal)))
    return math.degrees(math.atan2(_dot(_cross(r, v), normal), _dot(r, v)))


def _rotate_between(vector: Vec, n1: Vec, n2: Vec) -> Vec:
    """`n1` を `n2` へ運ぶ最小回転を `vector` に施す(ロドリゲス)。"""
    axis = _cross(n1, n2)
    s = _length(axis)
    c = _dot(n1, n2)
    if s < 1.0e-12:
        return vector if c > 0.0 else _scale(vector, -1.0)
    k = _scale(axis, 1.0 / s)
    kv = _cross(k, vector)
    kd = _dot(k, vector) * (1.0 - c)
    return (
        vector[0] * c + kv[0] * s + k[0] * kd,
        vector[1] * c + kv[1] * s + k[1] * kd,
        vector[2] * c + kv[2] * s + k[2] * kd,
    )
