"""ソリッドの辺・面・ボディを、スケッチ平面へ正投影したスケッチ図形にする(issue #184 の幾何)。

- 辺: 3D の曲線を平面へ投影し(`GeomProjLib.ProjectOnPlane`。正確)、その曲線を `kernel_sketch.edge_entities` に渡す
  (線分・円・円弧・閉じた楕円・3次ベジエは正確に、斜めの円弧が写った楕円弧などは3次ベジエの列で近似)。
- 平面に垂直な辺のように**点へ潰れる**ものは図形を作らない。平面に垂直な平面に乗る円のように**線へ潰れる**ものは、
  覆う区間の線分1本にする(往復する曲線を図形にすると重なった線が2本できる)。
- ボディ: 平面の法線方向から見た**影の境界**(外周と、向こうが透けて見える穴)。隠線処理(`HLRBRep_Algo`)で見える線・隠れた線・
  輪郭線を平面へ写し、平面をその線で割った面のうち、法線方向の直線がボディに当たるものを集めて和を取り、その境界を図形にする。
  止まり穴の底や段の線のように影の内側にある線は出さない。

返す図形は1回の呼び出しの中で重複を除く(箱の上面と下面を写すと同じ長方形になる)。
"""
from __future__ import annotations

import math
from typing import Any, Iterable, List, Sequence, Tuple

from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.BRepAlgoAPI import BRepAlgoAPI_Splitter
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_Transform
from OCP.BRepLib import BRepLib
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.Geom import Geom_Plane, Geom_TrimmedCurve
from OCP.GeomAbs import GeomAbs_CurveType
from OCP.GeomProjLib import GeomProjLib
from OCP.HLRAlgo import HLRAlgo_Projector
from OCP.HLRBRep import HLRBRep_Algo, HLRBRep_HLRToShape
from OCP.IntCurvesFace import IntCurvesFace_ShapeIntersector
from OCP.BRepClass import BRepClass_FaceClassifier
from OCP.TopAbs import TopAbs_ShapeEnum, TopAbs_State
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS
from OCP.TopTools import TopTools_ListOfShape
from OCP.gp import gp_Ax2, gp_Ax3, gp_Lin, gp_Pln, gp_Pnt, gp_Trsf, gp_Vec

from . import kernel_sketch
from .kernel_sketch import (
    _ax3_from_plane,
    _plane_face,
    _shape_faces,
    _smooth_breaks,
    _to_pnt,
    _to_uv,
    _unified,
    _union_faces,
)
from .mesh_geometry import compound, indexed_map

#: 写した辺が点・線へ潰れているとみなす距離[mm](`kernel_sketch` の領域の結合と同じ桁)。
COLLAPSE_TOLERANCE = 1.0e-6

#: 潰れの判定に使う標本の数(端点を含む)。
_COLLAPSE_SAMPLES = 33

#: 隠線処理の結果から集める線の種類(見える・隠れた × 稜線・滑らかな稜線・輪郭線)。影の境界はこのどれかに乗る。
_HLR_COMPOUNDS = (
    "VCompound",
    "Rg1LineVCompound",
    "OutLineVCompound",
    "HCompound",
    "Rg1LineHCompound",
    "OutLineHCompound",
)


def edge_entities(plane: dict, edges: Iterable[Any]) -> List[dict]:
    """3D の辺の並びを平面 `plane` へ写したスケッチ図形(点に潰れた辺は除き、重複を除く)。"""
    ax3 = _ax3_from_plane(plane)
    entities: List[dict] = []
    for edge in edges:
        entities.extend(_projected_edge_entities(ax3, edge))
    return _deduplicated(entities)


def face_edges(face: Any) -> List[Any]:
    """面の外周と穴のループの辺。円筒などの継ぎ目の辺(面の境界ではない)は除く。"""
    edge_map = indexed_map(face, TopAbs_ShapeEnum.TopAbs_EDGE)
    edges = [TopoDS.Edge_s(edge_map.FindKey(i)) for i in range(1, edge_map.Extent() + 1)]
    return [edge for edge in edges if not BRep_Tool.IsClosed_s(edge, TopoDS.Face_s(face))]


def outline_entities(plane: dict, shapes: Sequence[Any]) -> List[dict]:
    """ソリッドの並び `shapes` を平面 `plane` の法線方向から見た影の境界のスケッチ図形(重複を除く)。"""
    ax3 = _ax3_from_plane(plane)
    edges = [flat for edge in _hlr_edges(ax3, shapes) for flat in _flattened_edges(ax3, edge)]
    if not edges:
        return []
    faces = _split_plane(ax3, edges)
    shadow = [face for face in faces if _covered(ax3, face, shapes)]
    if not shadow:
        return []
    entities: List[dict] = []
    for face in _shape_faces(_unified(compound(_union_faces(shadow)))):
        entities.extend(kernel_sketch.face_boundary_entities(plane, face))
    return _deduplicated(entities)


# --------------------------------------------------------------------------------------
# 辺の投影
# --------------------------------------------------------------------------------------
def _projected_edge_entities(ax3: "gp_Ax3", edge: Any) -> List[dict]:
    """3D の辺1本を平面へ写した図形。点に潰れれば空、線に潰れればその区間の線分1本。"""
    collapsed = _collapse(ax3, edge)
    if collapsed is not None:
        return collapsed
    first, last = BRep_Tool.Range_s(edge)
    curve = BRep_Tool.Curve_s(edge, first, last)
    if curve is None:
        return []
    projected = GeomProjLib.ProjectOnPlane_s(
        Geom_TrimmedCurve(curve, first, last), Geom_Plane(gp_Pln(ax3)), ax3.Direction(), True
    )
    maker = BRepBuilderAPI_MakeEdge(projected)
    if not maker.IsDone():
        raise ValueError("辺を平面へ写せない(投影した曲線から辺を作れない)")
    return kernel_sketch.edge_entities(ax3, maker.Edge())


def _collapse(ax3: "gp_Ax3", edge: Any) -> Any:
    """写すと点に潰れる辺は `[]`、線に潰れる辺はその区間の線分の図形1つ、どちらでもなければ None。"""
    curve = BRepAdaptor_Curve(edge)
    first, last = curve.FirstParameter(), curve.LastParameter()

    def uv(parameter: float) -> Tuple[float, float]:
        point = curve.Value(parameter)
        return _to_uv(ax3, point.X(), point.Y(), point.Z())

    # 等間隔の標本に、曲線のノットとその間の中点を足す(ノットの区間より細い山を等間隔の標本が飛び越えないため)
    knots = _smooth_breaks(curve, first, last)
    params = sorted(
        {first + (last - first) * k / (_COLLAPSE_SAMPLES - 1) for k in range(_COLLAPSE_SAMPLES)}
        | set(knots)
        | {(a + b) / 2.0 for a, b in zip(knots, knots[1:])}
    )
    points = [uv(parameter) for parameter in params]
    far = max(points, key=lambda point: math.dist(point, points[0]))
    if math.dist(far, points[0]) <= COLLAPSE_TOLERANCE:
        other = max(points, key=lambda point: math.dist(point, far))
        if math.dist(other, far) <= COLLAPSE_TOLERANCE:
            return []
    start = max(points, key=lambda point: math.dist(point, far))
    end = max(points, key=lambda point: math.dist(point, start))
    length = math.dist(start, end)
    if length <= COLLAPSE_TOLERANCE:
        return []
    direction = ((end[0] - start[0]) / length, (end[1] - start[1]) / length)
    if any(abs(_cross(direction, (p[0] - start[0], p[1] - start[1]))) > COLLAPSE_TOLERANCE for p in points):
        return None
    if curve.GetType() == GeomAbs_CurveType.GeomAbs_Line:
        (x0, y0), (x1, y1) = points[0], points[-1]
    else:
        # 往復する曲線(縁から見た円)の端は標本の間にある。標本の最大・最小の近くで、線の向きの成分の極値まで詰める。
        along = [p[0] * direction[0] + p[1] * direction[1] for p in points]
        low = _extreme(uv, params, along, direction, -1.0)
        high = _extreme(uv, params, along, direction, 1.0)
        (x0, y0), (x1, y1) = low, high
    return [{"kind": "line", "params": {"x0": x0, "y0": y0, "x1": x1, "y1": y1}}]


def _cross(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    return a[0] * b[1] - a[1] * b[0]


def _extreme(uv: Any, params: Sequence[float], along: Sequence[float], direction: Tuple[float, float], sign: float) -> Tuple[float, float]:
    """`direction` の成分が最大(`sign` が負なら最小)になる曲線上の点。標本の極値の両隣の区間を黄金分割で詰める。"""
    best = max(range(len(params)), key=lambda k: sign * along[k])
    lo = params[max(best - 1, 0)]
    hi = params[min(best + 1, len(params) - 1)]

    def value(parameter: float) -> float:
        point = uv(parameter)
        return sign * (point[0] * direction[0] + point[1] * direction[1])

    ratio = (math.sqrt(5.0) - 1.0) / 2.0
    for _ in range(80):
        m1, m2 = hi - ratio * (hi - lo), lo + ratio * (hi - lo)
        if value(m1) > value(m2):
            hi = m2
        else:
            lo = m1
    middle = (lo + hi) / 2.0
    candidates = [params[best], middle]
    return uv(max(candidates, key=value))


# --------------------------------------------------------------------------------------
# ボディの影
# --------------------------------------------------------------------------------------
def _hlr_edges(ax3: "gp_Ax3", shapes: Sequence[Any]) -> List[Any]:
    """隠線処理で平面の法線方向から見た線(見える・隠れた・輪郭線)を、世界座標の平面上の辺として返す。

    外部の罠: `HLRBRep_HLRToShape` の線は射影の局所座標(`gp_Ax2` の X 軸と、法線 × X 軸を y、z = 0)で返り、3D の曲線を
    持たない辺がある(`BRepAdaptor_Curve` が "No geometry")。`BRepLib.BuildCurves3d` で作ってから世界座標へ戻す
    (2026-10-22 実測。局所座標は左手系のスケッチの枠でも右手系)。
    """
    ax2 = gp_Ax2(ax3.Location(), ax3.Direction(), ax3.XDirection())
    algo = HLRBRep_Algo()
    for shape in shapes:
        algo.Add(shape)
    algo.Projector(HLRAlgo_Projector(ax2))
    algo.Update()
    algo.Hide()
    result = HLRBRep_HLRToShape(algo)
    to_world = gp_Trsf()
    to_world.SetTransformation(gp_Ax3(ax2))
    to_world.Invert()
    edges: List[Any] = []
    for name in _HLR_COMPOUNDS:
        lines = getattr(result, name)()
        if lines is None or lines.IsNull():
            continue
        BRepLib.BuildCurves3d_s(lines)
        moved = BRepBuilderAPI_Transform(lines, to_world, True).Shape()
        edge_map = indexed_map(moved, TopAbs_ShapeEnum.TopAbs_EDGE)
        edges.extend(TopoDS.Edge_s(edge_map.FindKey(i)) for i in range(1, edge_map.Extent() + 1))
    return edges


def _flattened_edges(ax3: "gp_Ax3", edge: Any) -> List[Any]:
    """平面上の辺を、点に潰れていれば除き、線に潰れていれば(往復する縁から見た円など)その区間の直線の辺に置き換える。"""
    collapsed = _collapse(ax3, edge)
    if collapsed is None:
        return [edge]
    edges = []
    for entity in collapsed:
        p = entity["params"]
        maker = BRepBuilderAPI_MakeEdge(_to_pnt(ax3, p["x0"], p["y0"]), _to_pnt(ax3, p["x1"], p["y1"]))
        if maker.IsDone():
            edges.append(maker.Edge())
    return edges


def _split_plane(ax3: "gp_Ax3", edges: Sequence[Any]) -> List[Any]:
    """平面を辺の並びで割った面(辺を全部含む矩形の補助面を、辺で切り分ける。`kernel_sketch.sketch_regions` と同じ方法)。"""
    points = []
    for edge in edges:
        curve = BRepAdaptor_Curve(edge)
        for k in range(5):
            point = curve.Value(curve.FirstParameter() + (curve.LastParameter() - curve.FirstParameter()) * k / 4)
            points.append(_to_uv(ax3, point.X(), point.Y(), point.Z()))
    u_min, u_max = min(p[0] for p in points), max(p[0] for p in points)
    v_min, v_max = min(p[1] for p in points), max(p[1] for p in points)
    margin = max(10.0, math.hypot(u_max - u_min, v_max - v_min))
    arguments = TopTools_ListOfShape()
    arguments.Append(_plane_face(ax3, u_min - margin, u_max + margin, v_min - margin, v_max + margin))
    tools = TopTools_ListOfShape()
    for edge in edges:
        tools.Append(edge)
    splitter = BRepAlgoAPI_Splitter()
    splitter.SetArguments(arguments)
    splitter.SetTools(tools)
    splitter.SetFuzzyValue(COLLAPSE_TOLERANCE)
    splitter.Build()
    if not splitter.IsDone():
        raise ValueError("ボディの外形を求められない(平面を線で割れない)")
    return _shape_faces(splitter.Shape())


def _covered(ax3: "gp_Ax3", face: Any, shapes: Sequence[Any]) -> bool:
    """面の内側の1点を通る法線方向の直線が、どれかのソリッドに当たるか(当たれば影の内側)。"""
    point = _interior_point(face)
    line = gp_Lin(point, ax3.Direction())
    for shape in shapes:
        intersector = IntCurvesFace_ShapeIntersector()
        intersector.Load(shape, COLLAPSE_TOLERANCE)
        intersector.Perform(line, -math.inf, math.inf)
        if intersector.IsDone() and intersector.NbPnt() > 0:
            return True
    return False


#: 割った面の内側の点を探す三角形分割の偏差[mm]。細い面で三角形が曲線の外へはみ出したら、細かくして探し直す。
_INTERIOR_DEFLECTIONS = (0.5, 0.05, 0.005)


def _interior_point(face: Any) -> "gp_Pnt":
    """面の確かに内側にある点。三角形分割の三角形の重心を広い順に、面の分類で内側(`TopAbs_IN`)と確かめて選ぶ。

    外部の罠: 曲線の縁の三角形は弦で縁を切るので、重心が面の外に出ることがある(実測: 外半径 10・内半径 9.99 の環で、
    最も広い三角形の重心は半径 9.932 の穴の中)。
    """
    for deflection in _INTERIOR_DEFLECTIONS:
        BRepMesh_IncrementalMesh(face, deflection, False, 0.5, True)
        location = TopLoc_Location()
        triangulation = BRep_Tool.Triangulation_s(face, location)
        if triangulation is None:
            continue
        candidates = []
        for index in range(1, triangulation.NbTriangles() + 1):
            a, b, c = triangulation.Triangle(index).Get()
            pa, pb, pc = triangulation.Node(a), triangulation.Node(b), triangulation.Node(c)
            area = gp_Vec(pa, pb).Crossed(gp_Vec(pa, pc)).Magnitude()
            centre = gp_Pnt((pa.X() + pb.X() + pc.X()) / 3.0, (pa.Y() + pb.Y() + pc.Y()) / 3.0, (pa.Z() + pb.Z() + pc.Z()) / 3.0)
            candidates.append((area, centre.Transformed(location.Transformation())))
        for _, point in sorted(candidates, key=lambda item: -item[0]):
            if BRepClass_FaceClassifier(face, point, COLLAPSE_TOLERANCE).State() == TopAbs_State.TopAbs_IN:
                return point
    raise ValueError("ボディの外形を求められない(割った面の内側の点が見つからない)")


# --------------------------------------------------------------------------------------
# 重複の除去
# --------------------------------------------------------------------------------------
#: 重複とみなす座標の丸めの桁(小数6桁 = 1e-6 mm)。
_DEDUP_DIGITS = 6


def _deduplicated(entities: Sequence[dict]) -> List[dict]:
    """同じ図形(線分・ベジエは向きを問わない)を1つにする。最初に出た順を保つ。"""
    seen = set()
    kept: List[dict] = []
    for entity in entities:
        key = _entity_key(entity)
        if key in seen:
            continue
        seen.add(key)
        kept.append(entity)
    return kept


def _entity_key(entity: dict) -> tuple:
    params = entity["params"]

    def r(value: float) -> float:
        return round(float(value), _DEDUP_DIGITS) + 0.0

    kind = entity["kind"]
    if kind == "line":
        ends = sorted([(r(params["x0"]), r(params["y0"])), (r(params["x1"]), r(params["y1"]))])
        return (kind, *ends)
    if kind == "bezier":
        points = [(r(params[kx]), r(params[ky])) for kx, ky in kernel_sketch.BEZIER_POINT_KEYS]
        return (kind, *min(points, points[::-1]))
    # 角度は周期で畳む(楕円は軸の向きで 180 度、円弧の端は 360 度。同じ図形が 0 度と 180 度で別に残る)
    period = {"ellipse": {"angle": 180.0}, "arc": {"start_deg": 360.0, "end_deg": 360.0}}.get(kind, {})

    def folded(key: str, value: float) -> float:
        return r(value) % period[key] if key in period else r(value)

    return (kind, *sorted((key, folded(key, value)) for key, value in params.items()))
