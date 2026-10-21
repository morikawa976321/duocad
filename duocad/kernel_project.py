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
from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps
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

#: 隠線処理の結果から集める線の種類(見える稜線・滑らかな稜線・輪郭線)。影の境界の点を通る視線は立体をかすめるだけで手前に
#: 遮るものが無いので、境界は必ず見える線に乗る。隠れた線は足さない(見える線とほぼ重なり、平面を割ると面積 0 の細片ができる。
#: 2026-10-22 実測: PC ケースを XY へ写すと 4558 面のうち 138 面が面積 1e-8 mm² 以下)。
_HLR_COMPOUNDS = ("VCompound", "Rg1LineVCompound", "OutLineVCompound")

#: 内側の点を探さず、隣の面で影かどうかを決める細片の平均の幅(面積 / 周長)[mm]。線の重なりで残る幅がほぼ 0 の面
#: (2026-10-22 実測: PC ケースを XZ へ写すと、長さ 105 mm・面積 0.00076 mm² の面ができ、内側の点が取れなかった)。
_SLIVER_WIDTH = 1.0e-4


def edge_entities(plane: dict, edges: Iterable[Any]) -> List[dict]:
    """3D の辺の並びを平面 `plane` へ写したスケッチ図形(点に潰れた辺は除き、重複を除く)。"""
    ax3 = _ax3_from_plane(plane)
    entities: List[dict] = []
    for edge in edges:
        entities.extend(_projected_edge_entities(ax3, edge))
    return _welded(_deduplicated(entities))


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
    shadow = _shadow_faces(ax3, _split_plane(ax3, edges), shapes)
    if not shadow:
        return []
    entities: List[dict] = []
    for face in _shape_faces(_unified(compound(_union_faces(shadow)))):
        entities.extend(kernel_sketch.face_boundary_entities(plane, face))
    return _welded(_deduplicated(entities))


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
    """写すと点に潰れる辺は `[]`、線に潰れる辺はその区間の線分の図形1つ、どちらでもなければ None。

    潰れたかは曲線の種類ごとに決める: 直線は両端、円・楕円は平面へ写した短い方の半径、ベジエ・B-スプラインは制御点
    (曲線は制御点の凸包の中にあるので、制御点が一直線なら曲線も一直線。標本の点では、ノットの区間の中で上下に振れる曲線を
    見落とす)。それ以外の曲線(オフセット曲線など)は標本の点で見る。
    """
    curve = BRepAdaptor_Curve(edge)
    first, last = curve.FirstParameter(), curve.LastParameter()
    kind = curve.GetType()

    def uv(parameter: float) -> Tuple[float, float]:
        point = curve.Value(parameter)
        return _to_uv(ax3, point.X(), point.Y(), point.Z())

    if kind == GeomAbs_CurveType.GeomAbs_Line:
        # 直線は往復しないので両端だけで決まる(隠線処理の線の多くは直線で、標本を取ると遅い)
        (x0, y0), (x1, y1) = uv(first), uv(last)
        if math.hypot(x1 - x0, y1 - y0) <= COLLAPSE_TOLERANCE:
            return []
        return [{"kind": "line", "params": {"x0": x0, "y0": y0, "x1": x1, "y1": y1}}]
    # 等間隔の標本に、曲線のノットとその間の中点を足す(線に潰れた曲線の端を探すのに使う)
    knots = _smooth_breaks(curve, first, last)
    params = sorted(
        {first + (last - first) * k / (_COLLAPSE_SAMPLES - 1) for k in range(_COLLAPSE_SAMPLES)}
        | set(knots)
        | {(a + b) / 2.0 for a, b in zip(knots, knots[1:])}
    )
    points = [uv(parameter) for parameter in params]
    if kind in (GeomAbs_CurveType.GeomAbs_Circle, GeomAbs_CurveType.GeomAbs_Ellipse):
        if _line_through(points) is None:
            return []  # 短い円弧が点に潰れる(円全体の向きでは決まらない)
        conic = curve.Circle() if kind == GeomAbs_CurveType.GeomAbs_Circle else curve.Ellipse()
        minor = conic.Radius() if kind == GeomAbs_CurveType.GeomAbs_Circle else conic.MinorRadius()
        if minor * abs(conic.Axis().Direction().Dot(ax3.Direction())) > COLLAPSE_TOLERANCE:
            return None
        shape = points
    elif kind in (GeomAbs_CurveType.GeomAbs_BezierCurve, GeomAbs_CurveType.GeomAbs_BSplineCurve):
        shape = _segment_poles(ax3, curve, kind, first, last)
    else:
        shape = points
    direction = _line_through(shape)
    if direction is None:
        return []
    start, unit = direction
    if any(abs(_cross(unit, (p[0] - start[0], p[1] - start[1]))) > COLLAPSE_TOLERANCE for p in shape):
        return None
    # 往復する曲線(縁から見た円)の端は標本の間にある。標本の最大・最小の近くで、線の向きの成分の極値まで詰める。
    along = [p[0] * unit[0] + p[1] * unit[1] for p in points]
    (x0, y0) = _extreme(uv, params, along, unit, -1.0)
    (x1, y1) = _extreme(uv, params, along, unit, 1.0)
    if math.hypot(x1 - x0, y1 - y0) <= COLLAPSE_TOLERANCE:
        return []
    return [{"kind": "line", "params": {"x0": x0, "y0": y0, "x1": x1, "y1": y1}}]


def _segment_poles(ax3: "gp_Ax3", curve: Any, kind: Any, first: float, last: float) -> List[Tuple[float, float]]:
    """ベジエ・B-スプラインの辺 `[first, last]` の部分の制御点(平面の (u, v))。辺の外の制御点を見ると、辺の部分が線に
    潰れていても潰れないと判定する。写しを切る(`Bezier()` / `BSpline()` は辺の曲線そのものを返し、`Segment` は形を書き換える)。"""
    spline = (curve.Bezier() if kind == GeomAbs_CurveType.GeomAbs_BezierCurve else curve.BSpline()).Copy()
    if first > spline.FirstParameter() or last < spline.LastParameter():
        spline.Segment(first, last)
    return [_to_uv(ax3, pole.X(), pole.Y(), pole.Z()) for pole in (spline.Pole(i) for i in range(1, spline.NbPoles() + 1))]


def _line_through(points: Sequence[Tuple[float, float]]) -> Any:
    """点の並びの最も離れた2点を通る直線の (始点, 単位方向)。全部が `COLLAPSE_TOLERANCE` の中に集まっていれば None。"""
    far = max(points, key=lambda point: math.dist(point, points[0]))
    start = max(points, key=lambda point: math.dist(point, far))
    end = max(points, key=lambda point: math.dist(point, start))
    length = math.dist(start, end)
    if length <= COLLAPSE_TOLERANCE:
        return None
    return start, ((end[0] - start[0]) / length, (end[1] - start[1]) / length)


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


def _shadow_faces(ax3: "gp_Ax3", faces: Sequence[Any], shapes: Sequence[Any]) -> List[Any]:
    """割った面のうち影の内側のもの。細片(`_sliver`)は内側の点を探さず、辺を共有する細片でない面のうち、共有する辺の長さの
    合計が長い側(影の内側か外側か)に揃える(溝の下の細片で影に裂け目を残さない。細片の短い端は外側の面にも接する)。"""
    thin = [_sliver(face) for face in faces]
    inside = [False if thin[index] else _covered(ax3, face, shapes) for index, face in enumerate(faces)]
    for index, shared in enumerate(_shared_lengths(faces)):
        if thin[index]:
            covered = sum(length for other, length in shared.items() if not thin[other] and inside[other])
            uncovered = sum(length for other, length in shared.items() if not thin[other] and not inside[other])
            inside[index] = covered > uncovered
    return [face for face, covered in zip(faces, inside) if covered]


def _shared_lengths(faces: Sequence[Any]) -> List[dict]:
    """面ごとの {辺を共有する面の添字: 共有する辺の長さの合計}。辺は hash で束ねて `IsSame` で確かめる(hash と `==` は
    一致しない。docs/CAD_LIBRARY_PITFALLS.md)。"""
    buckets: dict = {}
    for index, face in enumerate(faces):
        edge_map = indexed_map(face, TopAbs_ShapeEnum.TopAbs_EDGE)
        for i in range(1, edge_map.Extent() + 1):
            edge = edge_map.FindKey(i)
            buckets.setdefault(hash(edge), []).append((edge, index))
    shared: List[dict] = [{} for _ in faces]
    for bucket in buckets.values():
        for position, (edge, index) in enumerate(bucket):
            for other_edge, other in bucket[position + 1 :]:
                if other != index and edge.IsSame(other_edge):
                    props = GProp_GProps()
                    BRepGProp.LinearProperties_s(edge, props)
                    shared[index][other] = shared[index].get(other, 0.0) + props.Mass()
                    shared[other][index] = shared[other].get(index, 0.0) + props.Mass()
    return shared


def _sliver(face: Any) -> bool:
    """平均の幅(面積 / 周長)が `_SLIVER_WIDTH` 以下の細片か。"""
    area = kernel_sketch._face_props(face)[0]
    props = GProp_GProps()
    BRepGProp.LinearProperties_s(face, props)
    perimeter = props.Mass()
    return area <= 0.0 or perimeter <= 0.0 or area / perimeter <= _SLIVER_WIDTH


def _covered(ax3: "gp_Ax3", face: Any, shapes: Sequence[Any]) -> bool:
    """面の内側の1点を通る法線方向の直線が、どれかのソリッドに当たるか(当たれば影の内側)。"""
    point = _interior_point(face, ax3)
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


def _interior_point(face: Any, ax3: "gp_Ax3") -> "gp_Pnt":
    """面の確かに内側にある点。三角形分割の三角形の重心を広い順に、面の分類で内側(`TopAbs_IN`)と確かめて選ぶ。

    外部の罠: 曲線の縁の三角形は弦で縁を切るので、重心が面の外に出ることがある(実測: 外半径 10・内半径 9.99 の環で、
    最も広い三角形の重心は半径 9.932 の穴の中)。分類がどの重心にも内側と答えない面もある(実測: PC ケースを XZ へ写すと、
    穴を 10 個持つ面積 22,974 mm² の面)。そのときは、内接円の半径が分割の偏差の 2 倍より大きい三角形の重心を使う(弦の
    ずれでは面の外に出ない)。三角形分割そのものが失敗する面(実測: 同じ PC ケースで BRepCheck は正しいと言う面)と、分割の偏差より
    細い面(肉厚 0.001 mm の管の環)は、縁の点から面の上で縁に垂直に少しずつ内へずらした点を分類で確かめる。
    """
    roomy = None
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
            perimeter = pa.Distance(pb) + pb.Distance(pc) + pc.Distance(pa)
            inradius = area / perimeter if perimeter > 0.0 else 0.0  # 三角形の内接円の半径 = 2·面積 / 周長(`area` は2倍の面積)
            candidates.append((area, inradius, centre.Transformed(location.Transformation())))
        for _, inradius, point in sorted(candidates, key=lambda item: -item[0]):
            if BRepClass_FaceClassifier(face, point, COLLAPSE_TOLERANCE).State() == TopAbs_State.TopAbs_IN:
                return point
            if roomy is None and inradius > 2.0 * deflection:
                roomy = point
    near = _point_inside_near_boundary(face, ax3)
    if near is not None:
        return near
    if roomy is not None:
        return roomy
    raise ValueError("ボディの外形を求められない(割った面の内側の点が見つからない)")


#: 縁から内へずらす距離[mm](大きい方から試す)。
_BOUNDARY_OFFSETS = (1.0e-1, 1.0e-2, 1.0e-3, 1.0e-4, 1.0e-5)

#: 縁の点を試す辺の数の上限(どの辺の内側も面の内側なので、数本で足りる)。
_BOUNDARY_EDGES = 8


def _point_inside_near_boundary(face: Any, ax3: "gp_Ax3") -> Any:
    """縁の辺の中点から、面の上で辺に垂直に `_BOUNDARY_OFFSETS` だけずらした点のうち、面の分類で内側の点(無ければ None)。"""
    normal = gp_Vec(ax3.Direction())
    for edge in face_edges(face)[:_BOUNDARY_EDGES]:
        curve = BRepAdaptor_Curve(edge)
        point, tangent = gp_Pnt(), gp_Vec()
        curve.D1((curve.FirstParameter() + curve.LastParameter()) / 2.0, point, tangent)
        side = normal.Crossed(tangent)
        if side.Magnitude() <= 0.0:
            continue
        side.Normalize()
        for offset in _BOUNDARY_OFFSETS:
            for sign in (1.0, -1.0):
                candidate = point.Translated(side.Multiplied(sign * offset))
                if BRepClass_FaceClassifier(face, candidate, offset * 1.0e-2).State() == TopAbs_State.TopAbs_IN:
                    return candidate
    return None


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
        # 畳んでから丸め、丸めで周期の端に乗った値をもう一度畳む(丸めてから畳むと 200.123456 % 180 = 20.123456000000004 が残る)
        return round(float(value) % period[key], _DEDUP_DIGITS) % period[key] + 0.0 if key in period else r(value)

    return (kind, *sorted((key, folded(key, value)) for key, value in params.items()))


# --------------------------------------------------------------------------------------
# 端点を揃える
# --------------------------------------------------------------------------------------
#: 写した図形の端点を1点に揃える距離[mm]。外部の罠: STEP から来た形の辺の曲線は、共有する頂点から形状許容差の分だけ
#: ずれた所で終わる(PC ケースの部品の最大許容差は実測 0.0097 mm)。辺ごとに写すと端点が一致せず、スケッチの領域が閉じない
#: (2026-10-22 実測: PC ケースの平らな面 1155 枚のうち 118 枚)。外形も、和集合の結果の辺の端点が 4e-7 mm ずれて閉じない
#: (sol の再現: トーラスの外形)。印刷の精度(0.1 mm)より十分小さい。
WELD_TOLERANCE = 1.0e-2


def _endpoint_slots(entity: dict) -> List[str]:
    """端点を持つ図形の端点の名前(x のキー)。閉じた図形(円・楕円)は端点を持たない。"""
    return {"line": ["x0", "x1"], "bezier": ["x0", "x1"], "arc": ["start", "end"]}.get(entity["kind"], [])


def _endpoint(entity: dict, slot: str) -> Tuple[float, float]:
    p = entity["params"]
    if entity["kind"] == "arc":
        angle = math.radians(p["start_deg" if slot == "start" else "end_deg"])
        return (p["cx"] + p["radius"] * math.cos(angle), p["cy"] + p["radius"] * math.sin(angle))
    return (p[slot], p["y" + slot[1:]])


def _move_endpoint(entity: dict, slot: str, target: Tuple[float, float]) -> None:
    p = entity["params"]
    if entity["kind"] == "arc":
        # 円弧は端の角度を揃え先の向きへ回す(半径は変えない。揃え先が円の上に無ければ、その分の隙間は残る)
        angle = math.degrees(math.atan2(target[1] - p["cy"], target[0] - p["cx"]))
        p["start_deg" if slot == "start" else "end_deg"] = angle
        return
    x_key, y_key = slot, "y" + slot[1:]
    dx, dy = target[0] - p[x_key], target[1] - p[y_key]
    p[x_key], p[y_key] = target
    if entity["kind"] == "bezier":
        # 隣の制御点も同じだけ動かして、端の接線の向きを保つ
        inner_x, inner_y = ("cx1", "cy1") if slot == "x0" else ("cx2", "cy2")
        p[inner_x] += dx
        p[inner_y] += dy


def _welded(entities: Sequence[dict], tolerance: float = WELD_TOLERANCE) -> List[dict]:
    """`tolerance` 以内の端点を近い組から順に1点へ揃えた図形の並び。短い図形(差し渡しが `tolerance` の 10 倍以下)の
    両端は1点にまとめない(半径の違う円弧を結ぶ 0.005 mm の線分も本物の辺)。`COLLAPSE_TOLERANCE` 以下の残りかすは両端をまとめて除き、
    両端がまとまった長い円弧は円にする(ほぼ一周する円弧)。円の上で揃えきれない円弧の端は線分でつなぐ(並びの最後に足す)。"""
    out = [{"kind": e["kind"], "params": dict(e["params"]), **({"approximated": True} if e.get("approximated") else {})} for e in entities]
    extents = [_extent(entity) for entity in out]
    slots = [(index, slot) for index, entity in enumerate(out) for slot in _endpoint_slots(entity)]
    points = [_endpoint(out[index], slot) for index, slot in slots]
    parent = list(range(len(slots)))
    owners = [{index} for index, _slot in slots]
    rigid = {index for index, extent in enumerate(extents) if COLLAPSE_TOLERANCE < extent <= 10.0 * tolerance}

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    grid: dict = {}
    for i, (x, y) in enumerate(points):
        grid.setdefault((math.floor(x / tolerance), math.floor(y / tolerance)), []).append(i)
    pairs = []
    for (gx, gy), members in grid.items():
        for i in members:
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for j in grid.get((gx + dx, gy + dy), ()):
                        if j > i and math.dist(points[i], points[j]) <= tolerance:
                            pairs.append((math.dist(points[i], points[j]), i, j))
    for _distance, i, j in sorted(pairs):
        ri, rj = root(i), root(j)
        if ri != rj and not owners[ri] & owners[rj] & rigid:
            parent[rj] = ri
            owners[ri] |= owners[rj]
    clusters: dict = {}
    for i in range(len(slots)):
        clusters.setdefault(root(i), []).append(i)
    bridges: List[Tuple[int, dict]] = []
    for members in clusters.values():
        if len(members) < 2:
            continue
        arc_ends = [i for i in members if out[slots[i][0]]["kind"] == "arc"]
        target = _arc_meeting(out, slots, points, arc_ends, tolerance) if arc_ends else points[members[0]]
        for i in members:
            index, slot = slots[i]
            if math.dist(points[i], target) > 0.0 and _keeps_sweep(out[index], slot, target):
                _move_endpoint(out[index], slot, target)
            reached = _endpoint(out[index], slot)
            # 円弧の端は円の上でしか動かせない(同心で半径の違う円弧など)し、回すと向きが逆になる短い円弧もある。残った隙間は
            # 線分でつなぐ。実測: 領域を作る General Fuse がつなぐ隙間は 2e-7 mm まで(5e-7 mm で閉じない)、1e-8 mm の線分でつないでも領域になる
            if math.dist(reached, target) > 1.0e-9:
                bridges.append((index, {"kind": "line", "params": {"x0": reached[0], "y0": reached[1], "x1": target[0], "y1": target[1]}}))
    kept = []
    gone = set()
    for index, entity in enumerate(out):
        ends = [root(i) for i, (owner, _slot) in enumerate(slots) if owner == index]
        if len(ends) == 2 and ends[0] == ends[1]:
            gone.add(index)
            if extents[index] <= 10.0 * tolerance:
                continue
            if entity["kind"] == "arc":
                p = entity["params"]
                entity = {"kind": "circle", "params": {"cx": p["cx"], "cy": p["cy"], "diameter": 2.0 * p["radius"]}}
        kept.append(entity)
    return kept + [bridge for owner, bridge in bridges if owner not in gone]


def _arc_meeting(out: Sequence[dict], slots: Sequence[Tuple[int, str]], points: Sequence[Tuple[float, float]],
                 arc_ends: Sequence[int], tolerance: float) -> Tuple[float, float]:
    """円弧の端が集まった点の揃え先。円弧の端は円の上でしか動かせないので、違う円の円弧が2つ以上あればその2円の交点
    (集まった円弧の端のどれかから `tolerance` より遠ければ最初の円弧の端)。"""
    first = points[arc_ends[0]]
    circles: List[Tuple[float, float, float]] = []
    for i in arc_ends:
        p = out[slots[i][0]]["params"]
        circle = (p["cx"], p["cy"], p["radius"])
        if all(max(abs(a - b) for a, b in zip(circle, other)) > 1.0e-9 for other in circles):
            circles.append(circle)
    if len(circles) < 2:
        return first
    (x1, y1, r1), (x2, y2, r2) = circles[:2]
    d = math.hypot(x2 - x1, y2 - y1)
    if d <= 1.0e-12:
        return first
    a = (d * d + r1 * r1 - r2 * r2) / (2.0 * d)
    h = math.sqrt(max(r1 * r1 - a * a, 0.0))
    ux, uy = (x2 - x1) / d, (y2 - y1) / d
    mx, my = x1 + a * ux, y1 + a * uy
    crossing = min(((mx - h * uy, my + h * ux), (mx + h * uy, my - h * ux)), key=lambda c: math.dist(c, first))
    return crossing if all(math.dist(crossing, points[i]) <= tolerance for i in arc_ends) else first


def _keeps_sweep(entity: dict, slot: str, target: Tuple[float, float]) -> bool:
    """端点を `target` へ動かしても図形が裏返らないか。円弧は端を回して始点が終点を追い越すと、ほぼ一周の円弧に化ける
    (0.01 mm ほどの円弧の両端を別々の点へ回したとき)。"""
    if entity["kind"] != "arc":
        return True
    p = entity["params"]
    old = p["start_deg" if slot == "start" else "end_deg"]
    delta = (math.degrees(math.atan2(target[1] - p["cy"], target[0] - p["cx"])) - old + 180.0) % 360.0 - 180.0
    sweep = (p["end_deg"] - p["start_deg"]) % 360.0
    moved = sweep - delta if slot == "start" else sweep + delta
    return 0.0 < moved < 360.0


def _extent(entity: dict) -> float:
    """図形の差し渡し(端点・制御点の広がり。円弧は弦の長さと矢の高さの大きい方)。"""
    p = entity["params"]
    if entity["kind"] == "circle":
        return p["diameter"]
    if entity["kind"] == "ellipse":
        return p["width"]
    if entity["kind"] == "arc":
        sweep = math.radians((p["end_deg"] - p["start_deg"]) % 360.0)
        return p["radius"] * max(2.0 * math.sin(min(sweep, math.pi) / 2.0), 1.0 - math.cos(min(sweep, math.pi) / 2.0))
    points = [(p[kx], p[ky]) for kx, ky in (("x0", "y0"), ("cx1", "cy1"), ("cx2", "cy2"), ("x1", "y1")) if kx in p]
    return max(math.dist(a, b) for a in points for b in points)
