"""はみ出しを吸収するフィレット: 丸めの肉を別に作り、本体へ足す(凹の辺)/本体から引く(凸の辺)。

OCCT の `MakeFillet` は、転がる球が支える面の外へ出る半径(面からのはみ出し)で `IsDone()=False` になり、
丸めの面が隣り合わない別の形(同じボディの別の柱)に当たると、失敗するか、球をその形にも触れさせた別の形を返す。
Fusion 360 はどちらも、支える2面だけで丸めた面をはみ出した先で切り、当たった形は貫通させる。ここはその形を作る:

1. 辺の両側の面 F1・F2 の曲面を大きく延長し、辺のまわりの箱を割って、辺に沿うくさび W(凹なら空気側・凸なら物体側)を作る。
   W には障害物も面の縁も無いので、`MakeFillet` が必ず通る見込みの形になる。
2. 肉 M = W − fillet(W)。辺の端・はみ出した先は、F1・F2(別の形が立つ足元を塞いだもの)を法線方向に厚くした柱の外に出る。
3. 柱の外に出た部分のうち、辺の内側の点に触れないものを捨てる。はみ出して崖の先に出た肉、辺の端の先へ伸びた肉がこれに当たる。
   辺に触れる部分は捨てない(曲面の支えや傾いた壁では、柱の境界が辺の上から斜めに出て正しい肉を切る)。
   貫通する別の柱の足元は塞いだ柱の内側に入り、その上の肉は捨てない(和で本体に埋もれる)。本当の穴は塞がず、穴の上の肉は捨てる。

**対応する形だけを引き受け、それ以外は `Unsupported` で断る**(呼び出し側は OCCT の結果か元の失敗へ戻る):
両側の面が延長できる曲面(平面・円柱・円錐・球・トーラス・直線押し出し面)、選んだ辺の連なりどうしが頂点を共有しない
(角の頂点の丸めはしない。辺ごとの肉の和では Fusion の角にならない)、1本の連なりの中で半径が1つ。
"""
from __future__ import annotations

import bisect
import math
from typing import Dict, List, NamedTuple, Sequence, Tuple

from OCP.BRep import BRep_Builder, BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_CompCurve, BRepAdaptor_Curve, BRepAdaptor_Curve2d, BRepAdaptor_Surface
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse, BRepAlgoAPI_Splitter
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepBuilderAPI import (
    BRepBuilderAPI_MakeEdge,
    BRepBuilderAPI_MakeFace,
    BRepBuilderAPI_MakeVertex,
    BRepBuilderAPI_MakeWire,
)
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepClass import BRepClass_FaceClassifier
from OCP.BRepClass3d import BRepClass3d_SolidClassifier
from OCP.BRepExtrema import BRepExtrema_DistShapeShape, BRepExtrema_SupportType
from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
from OCP.BRepGProp import BRepGProp
from OCP.BRepLProp import BRepLProp_SLProps
from OCP.BRepOffsetAPI import BRepOffsetAPI_MakePipe, BRepOffsetAPI_MakeThickSolid
from OCP.BRepPrimAPI import (
    BRepPrimAPI_MakeBox,
    BRepPrimAPI_MakeCylinder,
    BRepPrimAPI_MakePrism,
    BRepPrimAPI_MakeSphere,
    BRepPrimAPI_MakeTorus,
)
from OCP.BRepTools import BRepTools
from OCP.Bnd import Bnd_Box
from OCP.ChFi3d import ChFi3d
from OCP.ChFiDS import ChFiDS_TypeOfConcavity
from OCP.Geom import Geom_RectangularTrimmedSurface
from OCP.GeomAbs import GeomAbs_SurfaceType
from OCP.GCPnts import GCPnts_AbscissaPoint
from OCP.GeomAdaptor import GeomAdaptor_Curve
from OCP.GeomAPI import GeomAPI_Interpolate, GeomAPI_ProjectPointOnSurf
from OCP.GeomLProp import GeomLProp_CLProps
from OCP.GeomFill import GeomFill_Trihedron
from OCP.GProp import GProp_GProps
from OCP.TColStd import TColStd_HArray1OfBoolean
from OCP.TColgp import TColgp_Array1OfVec, TColgp_HArray1OfPnt
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_IN, TopAbs_ON, TopAbs_OUT, TopAbs_REVERSED, TopAbs_SOLID, TopAbs_VERTEX, TopAbs_WIRE
from OCP.TopExp import TopExp
from OCP.TopoDS import TopoDS, TopoDS_Compound, TopoDS_Shape
from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape, TopTools_ListOfShape
from OCP.gp import gp_Ax2, gp_Circ, gp_Dir, gp_Pln, gp_Pnt, gp_Pnt2d, gp_Vec, gp_Vec2d

from .kernel_chains import BlendChains
from .mesh_geometry import indexed_map


class Unsupported(ValueError):
    """この方式で引き受けない形(理由は文面)。"""


class Absorbed(NamedTuple):
    """`absorb` の結果。`shape` は統合(UnifySameDomain)前。`builders` は本体に掛けた順のブーリアン
    (由来をたどる用。凹の肉の和 → 凸の肉の差。片方しか無ければ1つ)。"""

    shape: TopoDS_Shape
    builders: List[object]
    concave: bool
    convex: bool


_SURFACES = {
    GeomAbs_SurfaceType.GeomAbs_Plane,
    GeomAbs_SurfaceType.GeomAbs_Cylinder,
    GeomAbs_SurfaceType.GeomAbs_Cone,
    GeomAbs_SurfaceType.GeomAbs_Sphere,
    GeomAbs_SurfaceType.GeomAbs_Torus,
    GeomAbs_SurfaceType.GeomAbs_SurfaceOfExtrusion,
}


def absorb(shape: TopoDS_Shape, edge_map, radii: Dict[int, float]) -> Absorbed:
    """`radii`(`{辺の添字: 半径}`。添字は `edge_map` の1始まり)の辺を、はみ出しを吸収して丸めた形を返す。"""
    edge_faces = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, edge_faces)
    chains = BlendChains(shape).of(list(radii))
    radius_of: Dict[int, float] = {}
    for index, chain in chains.items():
        for member in chain:
            if member in radius_of and radius_of[member] != radii[index]:
                raise Unsupported(f"edges {sorted(chain)} form one chain but were given different radii")
            radius_of[member] = float(radii[index])
    _refuse_corners(shape, edge_map, {chain for chain in chains.values()})

    # 安い検査(凹凸・球が入るか)を全部の組で先に済ませる: 断る形で重い箱の分割まで進むと、断るだけで 5 秒かかった
    # (PC ケースの部品で半径 12、球が入らない辺)。
    groups = [
        (_probe([edge_map.FindKey(i) for i in members], first, second, radius), [edge_map.FindKey(i) for i in members], first, second, radius)
        for first, second, radius, members in _face_pairs(edge_map, edge_faces, radius_of)
    ]
    columns = _Columns(shape, edge_faces)
    pieces = [_material(shape, probe, edges, first, second, radius, columns) for probe, edges, first, second, radius in groups]

    added = [material for material, concave in pieces if concave]
    removed = [material for material, concave in pieces if not concave]
    builders: List[object] = []
    result = shape
    for kind, materials in (("fuse", added), ("cut", removed)):
        if not materials:
            continue
        tool = materials[0]
        for other in materials[1:]:
            tool = _boolean("fuse", tool, other).Shape()
        builder = _boolean(kind, result, tool)
        builders.append(builder)
        result = builder.Shape()
    return Absorbed(result, builders, bool(added), bool(removed))


def _refuse_corners(shape: TopoDS_Shape, edge_map, chains: set) -> None:
    """別々の連なりが頂点を共有していれば断る(角の頂点の丸めはしない)。"""
    vertex_map = indexed_map(shape, TopAbs_VERTEX)
    owner: Dict[int, Tuple[int, ...]] = {}
    for chain in chains:
        for index in chain:
            first, last = TopExp.FirstVertex_s(TopoDS.Edge_s(edge_map.FindKey(index))), TopExp.LastVertex_s(
                TopoDS.Edge_s(edge_map.FindKey(index))
            )
            for vertex in (first, last):
                key = vertex_map.FindIndex(vertex)
                if key and owner.setdefault(key, chain) != chain:
                    raise Unsupported(f"edge chains {sorted(owner[key])} and {sorted(chain)} meet at a corner")


def _face_pairs(edge_map, edge_faces, radius_of: Dict[int, float]) -> List[Tuple[TopoDS_Shape, TopoDS_Shape, float, List[int]]]:
    """辺を、両側の面の組と半径ごとにまとめる(同じ2面・同じ半径の辺は同じくさびから1回で肉が取れる。閉じた交線が
    継ぎ目で割れた2本など。同じ2面でも半径が違う辺は別にする)。"""
    groups: List[Tuple[TopoDS_Shape, TopoDS_Shape, float, List[int]]] = []
    for index in sorted(radius_of):
        radius = radius_of[index]
        edge = edge_map.FindKey(index)
        faces = edge_faces.FindFromKey(edge)
        if faces.Extent() != 2 or faces.First().IsSame(faces.Last()):
            raise Unsupported(f"edge {index} does not separate two faces")
        first, second = faces.First(), faces.Last()
        for face in (first, second):
            if BRepAdaptor_Surface(TopoDS.Face_s(face)).GetType() not in _SURFACES:
                raise Unsupported(f"edge {index} borders a surface that cannot be extended")
        for one, two, size, members in groups:
            if size == radius and (
                (_joined(one, first, edge_faces) and _joined(two, second, edge_faces))
                or (_joined(one, second, edge_faces) and _joined(two, first, edge_faces))
            ):
                members.append(index)
                break
        else:
            groups.append((first, second, radius, [index]))
    return groups


def _joined(one: TopoDS_Shape, other: TopoDS_Shape, edge_faces) -> bool:
    """同じ面か、同じ平面に載り、同じ平面の面だけをたどってつながっている面か(1つの支える面として扱える)。
    同じ平面でも離れた面(同じ高さに立つ別々の壁の側面)はつながっていない: まとめると、柱は最初の面からつながる範囲しか
    覆わず、もう一方の辺の肉が残った(Codex レビューの反例)。"""
    if one.IsSame(other):
        return True
    return coplanar(one, other) and any(other.IsSame(member) for member in _coplanar_region(one, edge_faces))


def coplanar(one: TopoDS_Shape, other: TopoDS_Shape) -> bool:
    """同じ面か、同じ平面に載り外向きが揃った面か(つながっているかは見ない)。"""
    if one.IsSame(other):
        return True
    first, second = _outward_plane(one), _outward_plane(other)
    if first is None or second is None:
        return False
    (normal_a, offset_a), (normal_b, offset_b) = first, second
    return normal_a.IsParallel(normal_b, 1.0e-9) and normal_a.Dot(normal_b) > 0 and abs(offset_a - offset_b) <= _COPLANAR


#: 同じ平面と見なす距離[mm]。
_COPLANAR = 1.0e-7


def _outward_plane(face: TopoDS_Shape):
    """平面の面の外向き単位法線と、原点からの符号つき距離。平面でなければ None。"""
    surface = BRepAdaptor_Surface(TopoDS.Face_s(face))
    if surface.GetType() != GeomAbs_SurfaceType.GeomAbs_Plane:
        return None
    plane = surface.Plane()
    normal = gp_Vec(plane.Axis().Direction())
    if face.Orientation() == TopAbs_REVERSED:
        normal.Reverse()
    return normal, gp_Vec(plane.Location().XYZ()).Dot(normal)


def _coplanar_region(face: TopoDS_Shape, edge_faces) -> list:
    """`face` と、辺を共有して同じ平面に載る面をたどって集めた面(平面でなければ `face` だけ)。
    割れた平面の継ぎ目を崖として肉を切らないため(箱の上面を x=2 で割り、x=0 の辺を半径 3 で丸めると、継ぎ目の先の肉が
    捨てられて削り残した。Codex レビューの反例)。"""
    if _outward_plane(face) is None:
        return [face]
    region, queue = [face], [face]
    while queue:
        current = queue.pop()
        edges = indexed_map(current, TopAbs_EDGE)
        for number in range(1, edges.Extent() + 1):
            edge = edges.FindKey(number)
            if not edge_faces.Contains(edge):
                continue
            for other in edge_faces.FindFromKey(edge):
                if any(other.IsSame(known) for known in region) or not coplanar(face, other):
                    continue
                region.append(other)
                queue.append(other)
    return region


class _Probe(NamedTuple):
    """肉を作る前に1つの組について求める値(重い処理の前に断れるものはここで断る)。"""

    point: gp_Pnt
    normals: Tuple[gp_Vec, gp_Vec]
    concave: bool
    inside: gp_Pnt


def _probe(edges: list, first: TopoDS_Shape, second: TopoDS_Shape, radius: float) -> _Probe:
    """辺の中点・両側の外向き法線・凹凸・くさびの中の点を求め、球が曲面の内側に入らない半径を断る。"""
    edge = TopoDS.Edge_s(edges[0])
    curve = BRepAdaptor_Curve(edge)
    point = curve.Value(0.5 * (curve.FirstParameter() + curve.LastParameter()))
    normals = (_outward_normal(first, point), _outward_normal(second, point))
    kind = ChFi3d.DefineConnectType_s(edge, TopoDS.Face_s(first), TopoDS.Face_s(second), 1.0e-6, False)
    if kind == ChFiDS_TypeOfConcavity.ChFiDS_Concave:
        concave = True
    elif kind == ChFiDS_TypeOfConcavity.ChFiDS_Convex:
        concave = False
    else:
        raise Unsupported("the two faces meet tangentially")
    inward = normals[0] + normals[1]
    if inward.Magnitude() < 1.0e-9:
        raise Unsupported("the two faces are folded onto each other")
    inward.Normalize()
    if not concave:
        inward.Reverse()
    inside = point.Translated(inward.Multiplied(1.0e-3 * radius))
    for face in (first, second):
        room = _room_inside(face, inside)
        if room is not None and radius >= room:
            raise Unsupported(f"radius {radius:g} does not fit inside a curved face of radius {room:g}")
    return _Probe(point, normals, concave, inside)


def _material(
    shape: TopoDS_Shape, probe: _Probe, edges: list, first: TopoDS_Shape, second: TopoDS_Shape, radius: float, columns: "_Columns"
):
    """2面の間の辺(群)を半径 `radius` で丸める肉と、それが凹(足す)か。"""
    point, normals, concave, inside = probe

    # くさびの側から球が触れる点までの距離は radius·tan(φ/2)(φ は外向き法線のなす角。`kernel_chains._fillet_reach`)。
    # 箱はそれより十分広くとる: 箱の壁で肉が切れると、柱の外に出た部分として捨てられずに残る。
    cosine = max(-1.0, min(1.0, normals[0].Dot(normals[1])))
    reach = radius * math.tan(min(math.acos(cosine), math.pi - 0.05) / 2.0)
    margin = 2.0 * (reach + radius)
    box = Bnd_Box()
    for member in edges:
        BRepBndLib.Add_s(member, box)
    box.Enlarge(margin)
    x0, y0, z0, x1, y1, z1 = box.Get()
    block = BRepPrimAPI_MakeBox(gp_Pnt(x0, y0, z0), gp_Pnt(x1, y1, z1)).Shape()
    size = 10.0 * math.dist((x0, y0, z0), (x1, y1, z1))

    splitter = BRepAlgoAPI_Splitter()
    arguments, tools = TopTools_ListOfShape(), TopTools_ListOfShape()
    arguments.Append(block)
    tools.Append(_extended_face(first, size, point))
    tools.Append(_extended_face(second, size, point))
    splitter.SetArguments(arguments)
    splitter.SetTools(tools)
    splitter.Build()
    if not splitter.IsDone():
        raise Unsupported("could not split the box by the extended faces")
    wedge = _solid_containing(splitter.Shape(), inside)
    if wedge is None:
        raise Unsupported("no wedge next to the edge")

    # 柱の厚みは箱の余白(肉はこの中にしか無い)。ただし曲面を内側(軸の側)へ厚くするときは、曲面の半径の 9 割で止める:
    # 半径以上に厚くすると軸へ潰れて共通部分が失敗した(半径 4 の柱の上縁を半径 1 で、余白 4)。厚みを接点までの距離で
    # 絞ると、曲面の側の接点は辺の法線から求めた距離より遠いので肉が欠けた(半径 10 の半円柱の縦辺を半径 4 で、
    # 平面から 6.67 まで届く肉を 5 で切った。Codex レビューの反例)。
    heights = []
    for face, normal in ((first, normals[0]), (second, normals[1])):
        toward = normal if concave else normal.Reversed()
        room = _room_inside(face, point.Translated(toward.Multiplied(1.0e-3 * radius)))
        heights.append(margin if room is None else min(margin, 0.9 * room))
    # 縁を越える凹の辺は、縁に乗って転がる球で作る(越えない・作れないときだけ、くさびの `MakeFillet` へ)。
    if concave:
        rolled = _rolled_over_rims(wedge, edges, (first, second), heights, radius, columns, 1.5 * (reach + radius))
        if rolled is not None:
            return rolled, concave

    maker = BRepFilletAPI_MakeFillet(wedge)
    for spine in _edges_through(wedge, edges):
        maker.Add(radius, spine)
    try:
        maker.Build()
    except Exception as error:  # noqa: BLE001  OCCTは例外でも失敗しうる
        raise Unsupported(f"the wedge could not be rounded: {error}") from error
    if not maker.IsDone():
        raise Unsupported("the wedge could not be rounded")
    material = _boolean("cut", wedge, maker.Shape()).Shape()
    trimmed = _trim(material, edges, (first, second), heights, concave, columns)
    _require_plausible(trimmed, edges, 1.5 * (reach + radius))
    return trimmed, concave


# --------------------------------------------------------------------------------------
# 縁に乗って転がる球(Fusion 360: 支える面の縁を越えるとき、台座の高さを変えず、縁でフィレットの厚みがゼロになる)
# --------------------------------------------------------------------------------------
#: 辺1本に沿って球の中心を求める点の数(切り替わりの点は二分法で別に足す)。
_ROLL_SAMPLES = 64
#: 標本の間に収まる縁越え(小さい穴)の間に置く点の数。
_SHORT_STRETCH_SAMPLES = 8
#: 掃く球を半径の何倍だけ大きくするか(前から順に試す)。ちょうど半径だと球が両面に接するだけで、角の肉と上の空気が
#: 接線でつながったまま分かれなかった。食い込ませた分だけ丸めの面が角の側へずれ(半径 × 倍率)、面と接する所に角度
#: √(2 × 倍率) の折れ目が出る(1e-5 倍で 0.26°)。実測(縁を越える 8 つの形を2回ずつ): 1e-5 倍は全部通り、結果は毎回
#: 同じ。体積は厳密解のある形で相対 7〜10 × 倍率だけ少ない。1e-6 倍では半径 2 の穴へ転がり込む形が1度通らなかった。
_ROLL_SWELLS = (1.0e-5, 1.0e-3)


def _rolled_over_rims(wedge, edges: list, faces, heights, radius: float, columns: "_Columns", spread: float):
    """球が支える面の縁を越える組を、縁の辺に乗って転がる球で丸め直した肉。越えなければ None(今の肉のまま)。

    ふつうの丸めは延長した面に接する球で作るので、縁の先では縁の側面が肉の高さまで伸びる(円板 r30 の上の柱 r9 を
    r=30 で、側面が 1.4mm 伸びた)。Fusion は縁を越えた先で、球を相手の面と縁の辺の両方に接させて転がす。ここでは辺に
    沿って球の中心を求め(`_RollingPath`)、中心の道に球を掃いた管を、くさびと両面の柱の共通部分から引いて、辺に触れる
    部分を肉にする。OCCT の `MakeFillet` は使わない(縁に乗る球の面を作れない)。

    道が作れない形(両方の面の縁を同時に越える、ニュートン法が収まらない)や、どの膨らませ方でも管を引くブーリアンが
    壊れる形は None を返し、呼び出し側はふつうの丸め(縁の側面が伸びる形)を使う。
    """
    try:
        supports = [_Support(face, edges, columns) for face in faces]
        paths = [_RollingPath(wire, supports, radius) for wire in _smooth_wires(edges)]
        if not any(path.crossed for path in paths):
            return None
        first_column, second_column = (columns.of(face, height, True) for face, height in zip(faces, heights))
        corner = _boolean("common", _boolean("common", wedge, first_column).Shape(), second_column).Shape()
        # 傾いた壁の柱は境界が辺から斜めに出て、辺に沿う薄い層を2面の柱の共通部分から外す(円板の上の先細りの柱で、
        # 管の下の角が辺に触れなくなり、肉が丸ごと捨てられた)。片方の柱の中だけにあって辺に触れる部分を角に戻す
        # (戻した層のうち丸めの面より上は、次の管で引かれる)。
        # 相手の面が縁の外へ張り出す所では、縁に乗る面の柱の外にも肉がある(縁の外へ 45° で張り出す壁の根元で、縁の真上に
        # 縦の面が残り、肉が 0.8% 欠けた。Codex レビューの反例)。断面で肉は「辺の点・縁の点・相手の面の接点」の三角形の中に
        # しかないので、接点が縁の外にある範囲(`overhangs`)の横だけを戻す。接点が縁の内側の所まで戻すと、縁の外の空気を
        # 肉にした(板の縁にまたがって立つ柱の根元。Codex レビューの反例)。
        size = 10.0 * _diagonal(wedge)
        for rim_state, inside, outside in ((2, first_column, second_column), (1, second_column, first_column)):
            only = _boolean("cut", _boolean("common", wedge, inside).Shape(), outside).Shape()
            parts = [part for part in _solids(only) if _touches_inside_of(part, edges)]
            overhanging = [path for path in paths if path.overhangs[rim_state]]
            if overhanging:
                parts = parts + [part for part in _alongside(only, overhanging, rim_state, size)
                                 if not _touches_inside_of(part, edges)]
            for part in parts:
                corner = _boolean("fuse", corner, part).Shape()
    except Exception:  # noqa: BLE001  Unsupported と、OCCT の補間・ブーリアンの Standard_Failure 系。ふつうの丸めに戻る
        return None
    for swell in _ROLL_SWELLS:
        try:
            material = _rolled_material(corner, paths, radius, swell, edges)
            if material is not None:
                _require_plausible(material, edges, spread)
                return material
        except Exception:  # noqa: BLE001  同上。次の膨らませ方を試す
            continue
    return None


def _alongside(region: TopoDS_Shape, paths: list, rim_state: int, size: float) -> list:
    """`region` を、相手の面が縁の外へ張り出す範囲(`path.overhangs[rim_state]`)の両端の法平面で割り、その範囲の横にある
    (いちばん近いワイヤーの点がその範囲の中にある)部分を返す。"""
    planes = []
    for path in paths:
        for span in path.overhangs[rim_state]:
            for at in span:
                point, tangent = gp_Pnt(), gp_Vec()
                path.curve.D1(at, point, tangent)
                planes.append(BRepBuilderAPI_MakeFace(gp_Pln(point, gp_Dir(tangent)), -size, size, -size, size).Face())
    if planes:
        splitter = BRepAlgoAPI_Splitter()
        arguments, tools = TopTools_ListOfShape(), TopTools_ListOfShape()
        arguments.Append(region)
        for plane in planes:
            tools.Append(plane)
        splitter.SetArguments(arguments)
        splitter.SetTools(tools)
        splitter.Build()
        if not splitter.IsDone():
            raise Unsupported("could not cut the region at the ends of the edges")
        region = splitter.Shape()
    kept = []
    for part in _solids(region):
        props = GProp_GProps()
        BRepGProp.VolumeProperties_s(part, props)
        centre = BRepBuilderAPI_MakeVertex(props.CentreOfMass()).Vertex()
        nearest = min(paths, key=lambda path: BRepExtrema_DistShapeShape(centre, path.wire).Value())
        at = nearest.param_of(BRepExtrema_DistShapeShape(centre, nearest.wire).PointOnShape2(1))
        if any(low < at < high for low, high in nearest.overhangs[rim_state]):
            kept.append(part)
    return kept


def _diagonal(shape: TopoDS_Shape) -> float:
    box = Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    x0, y0, z0, x1, y1, z1 = box.Get()
    return math.dist((x0, y0, z0), (x1, y1, z1))


def _rolled_material(corner, paths, radius: float, swell: float, edges: list):
    """角から、半径を (1 + swell) 倍にした球を掃いた立体を引き、辺に触れる部分を返す。壊れた結果なら None。"""
    # 外部の罠: 管と球の束を先に結合してから引くと、結合が遅く(板の角を越える半径 22 で 6.6 秒)、肉の体積も1つずつ
    # 引いた結果と 0.8% 食い違った(半径 2 の穴へ転がり込む半径 6)。部品を全部1回の引き算に道具として渡すと、
    # 横倒しの円柱に立つ柱の根元(連なり3本)を半径 12 で、`Build()` がアクセス違反でプロセスごと落ちた(部品を
    # 1つずつ・2つずつなら落ちない)。1つずつ順に引く。
    rest = corner
    for piece in _swept_balls(paths, radius, swell):
        rest = _boolean("cut", rest, piece).Shape()
    kept = [part for part in _solids(rest) if _touches_inside_of(part, edges)]
    if not kept or any(_volume(part) <= 0.0 for part in kept):
        return None
    material = kept[0]
    for other in kept[1:]:
        material = _boolean("fuse", material, other).Shape()
    # 外部の罠: 球が縁にちょうど接するので、角から管を引くブーリアンが壊れることがある。半径 2 の穴の縁へ半径 6 の球が
    # 転がり込む形では、体積が負の立体や、引く前の角より大きい立体が返った(どちらも BRepCheck は真)。
    volume = _volume(material)
    if volume <= 0.0 or volume > _volume(corner) * (1.0 + 1.0e-6) or not BRepCheck_Analyzer(material).IsValid():
        return None
    # 球の向こう側(辺から中心へ向かう先)の空気が肉に入っていれば、掃いた立体に隙間があって肉と空気が分かれていない
    classifier = BRepClass3d_SolidClassifier(material)
    for path in paths:
        for foot, center in path.probes:
            away = gp_Vec(foot, center)
            if away.Magnitude() < 1.0e-12:
                continue
            classifier.Perform(center.Translated(away.Normalized().Multiplied(_BEYOND * radius)), 1.0e-7)
            if classifier.State() == TopAbs_IN:
                return None
    return material


#: 球の向こう側を見る点の、中心からの距離(半径に対して)。掃いた立体(半径の 1 + `_CHAIN_SWELL` 倍まで)の外。
_BEYOND = 1.01


class _Support:
    """支える面の片側: 延長した曲面への符号つきの距離(空気側が正)、足が面(同じ平面の続き・足元を塞いだもの)の上か、
    面の縁(丸める辺を除く境界)までの距離。"""

    def __init__(self, face: TopoDS_Shape, rounded: list, columns: "_Columns") -> None:
        self.face = face
        surface = BRep_Tool.Surface_s(TopoDS.Face_s(face))
        self.surface = surface.BasisSurface() if isinstance(surface, Geom_RectangularTrimmedSurface) else surface
        self.flip = -1.0 if face.Orientation() == TopAbs_REVERSED else 1.0
        edge_faces = columns.edge_faces
        self.filled = [_without_footprints(member, edge_faces) for member in _coplanar_region(face, edge_faces)]
        seen: Dict[int, list] = {}
        for member in self.filled:
            member_edges = indexed_map(member, TopAbs_EDGE)
            for number in range(1, member_edges.Extent() + 1):
                candidate = member_edges.FindKey(number)
                if BRep_Tool.Degenerated_s(TopoDS.Edge_s(candidate)):
                    continue
                key = columns.edge_index(candidate)
                seen.setdefault(key, []).append(candidate)
        builder = BRep_Builder()
        self.boundary = TopoDS_Compound()
        builder.MakeCompound(self.boundary)
        self.has_boundary = False
        for found in seen.values():
            # 同じ平面の面どうしの継ぎ目は2回現れる(境界ではない)。丸める辺そのものも境界に数えない。
            if len(found) != 1 or any(found[0].IsSame(edge) for edge in rounded):
                continue
            builder.Add(self.boundary, found[0])
            self.has_boundary = True

    def signed(self, point: gp_Pnt):
        """延長した曲面への符号つきの距離(外向き法線の側が正)と、曲面上の足。"""
        distance, foot, _ = self._projected(point)
        return distance, foot

    def normal(self, point: gp_Pnt) -> gp_Vec:
        """点の足での外向き法線(符号つきの距離の勾配)。"""
        return self._projected(point)[2]

    def _projected(self, point: gp_Pnt):
        projection = GeomAPI_ProjectPointOnSurf(point, self.surface)
        if projection.NbPoints() == 0:
            raise Unsupported("a point could not be projected onto a supporting surface")
        u, v = projection.LowerDistanceParameters()
        foot, d1u, d1v = gp_Pnt(), gp_Vec(), gp_Vec()
        self.surface.D1(u, v, foot, d1u, d1v)
        normal = d1u.Crossed(d1v)
        if normal.Magnitude() < 1.0e-12:
            raise Unsupported("a supporting surface has no normal at the foot")
        normal.Normalize()
        normal.Multiply(self.flip)
        return gp_Vec(foot, point).Dot(normal), foot, normal

    def covers(self, foot: gp_Pnt) -> bool:
        return any(
            BRepClass_FaceClassifier(TopoDS.Face_s(member), foot, 1.0e-7).State() in (TopAbs_IN, TopAbs_ON)
            for member in self.filled
        )

    def to_boundary(self, point: gp_Pnt) -> float:
        return self._nearest_on_boundary(point).Value()

    def away_from_boundary(self, point: gp_Pnt) -> gp_Vec:
        """面の縁の最も近い点から点への向き(縁までの距離の勾配)。"""
        away = gp_Vec(self._nearest_on_boundary(point).PointOnShape2(1), point)
        if away.Magnitude() < 1.0e-12:
            raise Unsupported("the point is on the face boundary")
        return away.Normalized()

    def _nearest_on_boundary(self, point: gp_Pnt) -> BRepExtrema_DistShapeShape:
        measure = BRepExtrema_DistShapeShape(BRepBuilderAPI_MakeVertex(point).Vertex(), self.boundary)
        if not measure.IsDone() or measure.NbSolution() == 0:
            raise Unsupported("distance to a face boundary failed")
        return measure


def _smooth_wires(edges: list) -> list:
    """辺を、頂点で接線のままつながる組に分け、組ごとに1本のワイヤーにする。

    外部の罠: つながった辺ごとに管を作ると、継ぎ目で管の端の円が重なり、引き算がプロセスごと落ちた(`_rolled_material`)。
    角丸長方形の柱の根元(辺 8 本)では、辺ごとの管と継ぎ目の球で 18.7 秒かかった。"""
    groups: List[Tuple[list, list]] = []
    for edge in edges:
        ends = [TopExp.FirstVertex_s(TopoDS.Edge_s(edge)), TopExp.LastVertex_s(TopoDS.Edge_s(edge))]
        joined = [
            group for group in groups
            if any(end.IsSame(known) and _tangent_at(edge, end).IsParallel(_tangent_at(group[0][k // 2], known), _SMOOTH_ANGLE)
                   for end in ends for k, known in enumerate(group[1]))
        ]
        members, vertices = [edge], list(ends)
        for group in joined:
            members.extend(group[0])
            vertices.extend(group[1])
            groups.remove(group)
        groups.append((members, vertices))
    wires = []
    for members, _ in groups:
        listed = TopTools_ListOfShape()
        for member in members:
            listed.Append(member)
        maker = BRepBuilderAPI_MakeWire()
        maker.Add(listed)
        if not maker.IsDone():
            raise Unsupported("the edges between the same two faces do not join into wires")
        wires.append(maker.Wire())
    return wires


#: 継ぎ目で接線とみなす角度(ラジアン)。
_SMOOTH_ANGLE = 1.0e-4


def _tangent_at(edge: TopoDS_Shape, vertex: TopoDS_Shape) -> gp_Vec:
    curve = BRepAdaptor_Curve(TopoDS.Edge_s(edge))
    point, tangent = gp_Pnt(), gp_Vec()
    curve.D1(BRep_Tool.Parameter_s(TopoDS.Vertex_s(vertex), TopoDS.Edge_s(edge)), point, tangent)
    if tangent.Magnitude() < 1.0e-12:
        raise Unsupported("an edge has no tangent at its end")
    return tangent


class _Run(NamedTuple):
    """球の中心の道の滑らかな1区間。`start`・`end` は区間の端での道の向き(状態が変わる点。辺の端なら None)。"""

    points: List[gp_Pnt]
    start: object
    end: object


class _RollingPath:
    """1本のワイヤー(接線でつながる辺の列)に沿った球の中心の道。`runs` はワイヤーの向きに並んだ滑らかな区間(`_Run`)、
    `loop` は区間が1周して閉じるか、`crossed` は縁を越えたか、`states` は現れた状態(0 ふつう・1/2 その面の縁に乗る)、
    `overhangs[状態]` は球がその面の縁に乗り、相手の面の接点が縁の外にある媒介変数の範囲、`probes` は間引いた
    `(辺の点, 球の中心)`。

    辺の点 s での法平面の中で、ふつうは両面から半径の点(両方の足が面の上)。片方の足が面の外なら、その面の縁から半径・
    もう片方の面から半径の点(球が縁の辺に乗る)。両方の足が外なら引き受けない。状態が変わる所は二分法で詰め、
    その点と向きを前後の区間の両方の端にする(区間を1本のワイヤーにつないで1本の管に掃ける)。
    """

    def __init__(self, wire: TopoDS_Shape, supports, radius: float) -> None:
        self.supports, self.radius, self.wire = supports, radius, wire
        # 媒介変数は弧長にする(既定だと辺ごとの媒介変数をつなぐので、円弧の辺に点がほとんど来ない)
        self.curve = BRepAdaptor_CompCurve(TopoDS.Wire_s(wire), True)
        first, last = self.curve.FirstParameter(), self.curve.LastParameter()
        self.loop = self.curve.Value(first).Distance(self.curve.Value(last)) < 1.0e-7
        samples = _ROLL_SAMPLES * indexed_map(wire, TopAbs_EDGE).Extent()
        count = samples if self.loop else samples + 1
        params = [first + (last - first) * i / samples for i in range(count)]
        params, solved = self._with_missed_crossings(params, [self._center(u) for u in params], last)
        states = [state for state, _, _ in solved]
        self.states = set(states)
        self.samples = [(u, self.curve.Value(u)) for u in params]
        self.overhangs = {1: self._overhangs(params, solved, 1, last), 2: self._overhangs(params, solved, 2, last)}
        self.probes = [(self.curve.Value(params[i]), solved[i][1]) for i in range(0, len(params), 8)]
        self.crossed = any(state != 0 for state in states)
        runs: List[Tuple[List[gp_Pnt], int, object, object]] = []
        for i, (state, center, _) in enumerate(solved):
            if runs and runs[-1][1] == state:
                runs[-1][0].append(center)
                continue
            if runs:
                joint, direction = self._joint(params[i - 1], params[i], states[i - 1])
                runs[-1][0].append(joint)
                runs[-1][3] = direction
                runs.append([[joint, center], state, direction, None])
            else:
                runs.append([[center], state, None, None])
        if self.loop and len(runs) > 1:
            if states[-1] == states[0]:
                # 1周の継ぎ目(last = first)をまたいで同じ状態: 最後の区間の後ろに最初の区間をつなぐ
                head = runs.pop(0)
                runs[-1][0].extend(head[0])
                runs[-1][3] = head[3]
            else:
                joint, direction = self._joint(params[-1], last, states[-1])
                runs[-1][0].append(joint)
                runs[-1][3] = direction
                runs[0][0].insert(0, joint)
                runs[0][2] = direction
        self.runs = [_Run(points, start, end) for points, _, start, end in runs]

    def _overhangs(self, params: list, solved: list, rim_state: int, last: float) -> list:
        """状態 `rim_state` の標本のうち、相手の面の接点(球の足)を縁に乗る面へ下ろした点が面の外にある標本の範囲。
        両隣の標本まで広げる(範囲の端で縁の外の肉は厚みゼロへ向かう)。1周して全部なら `[(始め, 終わり)]`。"""
        rim, other = (self.supports[0], self.supports[1]) if rim_state == 1 else (self.supports[1], self.supports[0])
        beyond = []
        for state, center, _ in solved:
            if state != rim_state:
                beyond.append(False)
                continue
            contact = other.signed(center)[1]
            beyond.append(not rim.covers(rim.signed(contact)[1]))
        count = len(params)
        if not any(beyond):
            return []
        if all(beyond):
            return [(params[0], last if self.loop else params[-1])]
        period = last - params[0]

        def at(index):  # 1周の継ぎ目をまたいだ添字も、続けた媒介変数で返す
            if not self.loop:
                return params[max(0, min(count - 1, index))]
            return params[index % count] + period * (index // count)

        # 1周する道は、範囲の外(偽)の標本から数え始めて、継ぎ目をまたぐ範囲を1つにする
        begin = beyond.index(False) if self.loop else 0
        spans, index = [], begin
        while index < begin + count:
            if not beyond[index % count]:
                index += 1
                continue
            first = index
            while index + 1 < begin + count and beyond[(index + 1) % count]:
                index += 1
            low, high = at(first - 1), at(index + 1)
            if self.loop and low >= last:
                low, high = low - period, high - period
            if self.loop and high > last:
                spans.extend([(low, last), (params[0], high - period)])
            else:
                spans.append((low, high))
            index += 1
        return spans

    def param_of(self, point: gp_Pnt) -> float:
        """ワイヤーの上の点の媒介変数(標本の辺の点を結んだ折れ線で近似)。"""
        points = [p for _, p in self.samples] + ([self.samples[0][1]] if self.loop else [])
        params = [u for u, _ in self.samples] + ([self.curve.LastParameter()] if self.loop else [])
        return _param_on_polyline(point, points, params)

    def _with_missed_crossings(self, params: list, solved: list, last: float):
        """足(ふつうの中心から支える面へ下ろした点)の軌跡が面の縁をまたぐ所を探し、またぐ所どうしの間に標本を足す。

        標本だけを見ると、標本の間に収まる小さな穴を見逃した(半径 0.5 の穴を 64 点の間に置くと、縁を越えない形として
        ふつうの肉になり、穴の壁が肉の高さまで伸びた。Codex レビューの反例)。またぐ所の間は同じ状態なので、間に1点足せば
        その状態が標本に現れ、切り替わりの点は `_joint` の二分法が詰める。
        足の軌跡は足を通る B スプラインで表す: 足を結んだ折れ線は、半径 1001 の軌跡を 64 分割すると弦が 1.2mm 内側を通り、
        半径 0.5 の穴を外した(Codex レビューの反例)。"""
        ends = params + [last] if self.loop else params
        crossings = []
        for support in self.supports:
            if not support.has_boundary:
                continue
            feet = [support.signed(regular)[1] for _, _, regular in solved]
            closed = feet + [feet[0]] if self.loop else feet
            chords = [0.0]
            for a, b in zip(closed, closed[1:]):
                chords.append(chords[-1] + a.Distance(b))
            if any(b - a < 1.0e-12 for a, b in zip(chords, chords[1:])):
                continue  # 足が動かない(補間できない)所がある
            array = TColgp_HArray1OfPnt(1, len(feet))
            for number, foot in enumerate(feet, 1):
                array.SetValue(number, foot)
            interpolation = GeomAPI_Interpolate(array, self.loop, 1.0e-9)
            interpolation.Perform()
            if not interpolation.IsDone():
                raise Unsupported("the track of the ball's feet could not be interpolated")
            track = BRepBuilderAPI_MakeEdge(interpolation.Curve()).Edge()
            measure = BRepExtrema_DistShapeShape(track, support.boundary)
            if not measure.IsDone() or measure.Value() > 1.0e-7 * max(1.0, self.radius):
                continue
            for number in range(1, measure.NbSolution() + 1):
                # 補間の媒介変数は弦の長さの累計(`_interpolated` と同じ)なので、どの標本の間かはそこから分かる。間のどこかは、
                # 足がその点にいちばん近くなる辺の点を詰めて求める(比例配分すると、足の進む速さが変わる楕円柱の根元で
                # 足した標本が穴の外に置かれ、縁越えを見逃した。Codex レビューの反例)
                crossing = measure.PointOnShape1(number)
                if measure.SupportTypeShape1(number) == BRepExtrema_SupportType.BRepExtrema_IsOnEdge:
                    (along,) = measure.ParOnEdgeS1(number)  # OCP は参照で返す引数をタプルで返す
                    k = max(0, min(len(chords) - 2, bisect.bisect_right(chords, along) - 1))
                else:
                    k = min(range(len(closed)), key=lambda i: closed[i].Distance(crossing))
                    k = max(0, min(len(ends) - 2, k))
                low, high = ends[max(0, k - 1)], ends[min(len(ends) - 1, k + 2)]
                crossings.append(_golden_minimum(lambda u: support.signed(self._center(u)[2])[1].Distance(crossing), low, high))
        if not crossings:
            return params, solved
        crossings.sort()
        period = last - params[0]
        pairs = list(zip(crossings, crossings[1:]))
        if self.loop:
            pairs.append((crossings[-1], crossings[0] + period))
        # 交点の間が標本の間隔より短い(小さい穴)ときは、間に `_SHORT_STRETCH_SAMPLES` 点置く: 1点だけだと、穴へ 0.02mm
        # 落ちる球の道を3点で補間することになり、道がずれて管と面の間にすき間が残った(半径 0.5 の穴へ半径 6 の球)
        step = period / max(1, len(params) - (0 if self.loop else 1))
        between = []
        for a, b in pairs:
            count = _SHORT_STRETCH_SAMPLES if b - a < step else 1
            between.extend(a + (b - a) * (i + 0.5) / count for i in range(count))
        between = [u - period if self.loop and u >= last else u for u in between]
        known = dict(zip(params, solved))
        spacing = 1.0e-9 * period
        for u in between:
            if all(abs(u - p) > spacing for p in known):
                known[u] = self._center(u)
        ordered = sorted(known)
        return ordered, [known[u] for u in ordered]

    def _center(self, u: float):
        """辺の点 u での `(状態, 球の中心, ふつうの中心)`。状態は 0 がふつう、1・2 は1つ目・2つ目の面の縁に乗る。"""
        s, tangent = gp_Pnt(), gp_Vec()
        self.curve.D1(u, s, tangent)
        if tangent.Magnitude() < 1.0e-12:
            raise Unsupported("the edge has no tangent")
        tangent.Normalize()
        one, two = self.supports
        n1, n2 = _outward_normal(one.face, s), _outward_normal(two.face, s)
        across = n1 - tangent.Multiplied(n1.Dot(tangent))
        if across.Magnitude() < 1.0e-9:
            raise Unsupported("a supporting face is tangent to the edge direction")
        across.Normalize()
        axes = (across, tangent.Crossed(across).Normalized())
        r = self.radius
        regular = _newton(
            s.Translated((n1 + n2).Multiplied(r)),
            lambda c: one.signed(c)[0] - r,
            lambda c: two.signed(c)[0] - r,
            axes,
            r,
        )
        on_one = one.covers(one.signed(regular)[1])
        on_two = two.covers(two.signed(regular)[1])
        if on_one and on_two:
            return 0, regular, regular
        if on_one == on_two:
            raise Unsupported("the ball would leave both supporting faces")
        rim, other = (two, one) if on_one else (one, two)
        if not rim.has_boundary:
            raise Unsupported("the supporting face has no edge to roll over")
        center = _newton(regular, lambda c: rim.to_boundary(c) - r, lambda c: other.signed(c)[0] - r, axes, r)
        if not other.covers(other.signed(center)[1]) or rim.signed(center)[0] <= 0.0:
            raise Unsupported("the ball rolling over a rim leaves the other face")
        return (2 if on_one else 1), center, regular

    def _joint(self, low: float, high: float, low_state: int):
        """状態が変わる辺の上の点を二分法で詰め、そこでの中心(ふつうの中心と縁に乗る中心が一致する点)と道の向き。

        縁に乗る中心の面(縁の辺から半径の管)は、切り替わりの点でふつうの中心の面(延長した面から半径)に接するので、
        道は向きまでつながる(曲がりだけが跳ぶ)。前後の状態の向きが食い違えば、道が折れているので引き受けない。"""
        for _ in range(40):
            middle = 0.5 * (low + high)
            if self._center(middle)[0] == low_state:
                low = middle
            else:
                high = middle
        before, center, _ = self._center(low)
        after = self._center(high)[0]
        along = self.curve.DN(low, 1)
        directions = [self._direction(state, center, along) for state in (before, after)]
        if directions[0].Angle(directions[1]) > 1.0e-3:
            raise Unsupported("the ball path bends sharply where it starts rolling over a rim")
        return center, (directions[0] + directions[1]).Normalized()

    def _direction(self, state: int, center: gp_Pnt, along: gp_Vec) -> gp_Vec:
        """状態 `state` の2つの条件(距離 = 半径)の勾配の外積 = 道の向き。辺の向き `along` にそろえる。"""
        one, two = self.supports
        if state == 0:
            first, second = one.normal(center), two.normal(center)
        else:
            rim, other = (one, two) if state == 1 else (two, one)
            first, second = rim.away_from_boundary(center), other.normal(center)
        direction = first.Crossed(second)
        if direction.Magnitude() < 1.0e-9:
            raise Unsupported("the ball path has no direction")
        direction.Normalize()
        return direction if direction.Dot(along) > 0.0 else direction.Reversed()


def _golden_minimum(f, low: float, high: float, steps: int = 48) -> float:
    """[low, high] で f を最小にする点(黄金分割)。"""
    ratio = (math.sqrt(5.0) - 1.0) / 2.0
    a, b = low, high
    c, d = b - ratio * (b - a), a + ratio * (b - a)
    fc, fd = f(c), f(d)
    for _ in range(steps):
        if fc < fd:
            b, d, fd = d, c, fc
            c = b - ratio * (b - a)
            fc = f(c)
        else:
            a, c, fc = c, d, fd
            d = a + ratio * (b - a)
            fd = f(d)
    return 0.5 * (a + b)


def _param_on_polyline(point: gp_Pnt, vertices: list, params: list) -> float:
    """折れ線(`vertices`、各点の媒介変数 `params`)の上の点 `point` の媒介変数(線分の中で比例配分)。"""
    best = (math.inf, params[0])
    for k in range(len(vertices) - 1):
        start, step = vertices[k], gp_Vec(vertices[k], vertices[k + 1])
        length = step.SquareMagnitude()
        t = 0.0 if length < 1.0e-24 else max(0.0, min(1.0, gp_Vec(start, point).Dot(step) / length))
        distance = start.Translated(step.Multiplied(t)).Distance(point)
        if distance < best[0]:
            best = (distance, params[k] + t * (params[k + 1] - params[k]))
    return best[1]


def _newton(start: gp_Pnt, f, g, axes, scale: float, steps: int = 40) -> gp_Pnt:
    """法平面の2つの向き `axes` で点を動かし f=g=0 を解く(数値微分のニュートン法)。収まらなければ Unsupported。"""
    point = gp_Pnt(start.XYZ())
    h = 1.0e-6 * scale
    tolerance = 1.0e-9 * max(1.0, scale)
    for _ in range(steps):
        fx, gx = f(point), g(point)
        if abs(fx) < tolerance and abs(gx) < tolerance:
            return point
        columns = []
        for axis in axes:
            moved = point.Translated(axis.Multiplied(h))
            columns.append(((f(moved) - fx) / h, (g(moved) - gx) / h))
        (a11, a21), (a12, a22) = columns
        det = a11 * a22 - a12 * a21
        if abs(det) < 1.0e-14:
            raise Unsupported("the ball center could not be solved")
        dx = (-fx * a22 + gx * a12) / det
        dy = (-a11 * gx + a21 * fx) / det
        step = axes[0].Multiplied(dx) + axes[1].Multiplied(dy)
        if step.Magnitude() > 0.5 * scale:
            step.Multiply(0.5 * scale / step.Magnitude())
        point = point.Translated(step)
    if abs(f(point)) < 1.0e-6 * max(1.0, scale) and abs(g(point)) < 1.0e-6 * max(1.0, scale):
        return point
    raise Unsupported("the ball center did not converge")


def _swept_balls(paths, radius: float, swell: float) -> list:
    """中心の道ごとに半径 radius × (1 + swell) の球を掃いた立体を、重なり合う部品(管と、球を結合した束)の列で返す。
    辺1本の道は区間をつないだ1本のワイヤーに沿う管にし、曲がりのきつい所だけ球を並べる。管の切れ目(開いた道の両端・
    球の列との境)には球を置く。

    外部の罠: `BRepOffsetAPI_MakePipe` の既定の向き(補正フレネ)は、まっすぐな道で断面がずれて体積が 2% 小さくなる
    (長さ 30・半径 10 で実測)。離散の向き(`GeomFill_IsDiscreteTrihedron`)は直線・円・螺旋とも 1e-7 以内で合う。
    道の曲がりの半径が球の半径より小さい所では管が自分に折れ込み、体積 0 の立体になる(半径 2 の穴の縁を越える半径 6 の
    球の道。板の角の頂点を回り込む道は、頂点を中心とする半径 r の球面に載るので、曲がりの半径が r を超えない)。
    そういう所は管にせず、道に沿って球を細かく並べる(`_ball_chain`)。
    外部の罠: 区間ごとに管を作って継ぎ目に球を置き、順に結合すると、端の円が重なる管どうしの結合が遅く(正方形の板の
    上の柱で 1 回の吸収に 13〜33 秒)、管の端と同じ半径の球(管の面が球に接する)ではブーリアンが壊れて体積が 0 や
    数分の1になった。切れ目の球は管よりわずかに大きくする(`_CAP_FACTOR`)。
    """
    tube_radius = radius * (1.0 + swell)
    cap_radius = radius * (1.0 + _CAP_FACTOR * swell)
    chain_radius = radius * (1.0 + max(_CHAIN_SWELL, _CAP_FACTOR * swell))
    pipes, balls, caps = [], [], []
    for path in paths:
        if len(path.runs) == 1:
            exact = _exact_tube(path.runs[0].points, path.loop, tube_radius)
            if exact is not None:
                pipes.append(exact)
                if not path.loop:
                    caps.extend(((path.runs[0].points[0], cap_radius), (path.runs[0].points[-1], cap_radius)))
                continue
        # 道を、曲がりのきつい範囲とそれ以外の (曲線, 始め, 終わり) に分け、同じ種類が続く範囲を組にする。
        # 1周する道は最後と最初の組をつなぐ
        groups: List[Tuple[bool, list]] = []
        for run in path.runs:
            curve = _interpolated(run, path.loop and len(path.runs) == 1)
            for first, last, tight in _stretches(curve, tube_radius):
                if groups and groups[-1][0] == tight:
                    groups[-1][1].append((curve, first, last))
                else:
                    groups.append((tight, [(curve, first, last)]))
        if path.loop and len(groups) > 1 and groups[0][0] == groups[-1][0]:
            groups[-1][1].extend(groups.pop(0)[1])
        closed = path.loop and len(groups) == 1
        for tight, stretches in groups:
            if tight:
                for curve, first, last in stretches:
                    balls.extend(_ball_chain(curve, first, last, chain_radius))
                    caps.extend(((curve.Value(first), chain_radius), (curve.Value(last), chain_radius)))
                continue
            pipes.append(_pipe(stretches, tube_radius))
            if not closed:
                (curve, first, _), (end_curve, _, last) = stretches[0], stretches[-1]
                caps.extend(((curve.Value(first), cap_radius), (end_curve.Value(last), cap_radius)))
    # 同じ所の球は1つにまとめ、大きい方を残す(球の列と管の境では列の球)
    distinct: List[List] = []
    for point, size in caps:
        for known in distinct:
            if point.Distance(known[0]) <= 1.0e-6 * radius:
                known[1] = max(known[1], size)
                break
        else:
            distinct.append([point, size])
    balls.extend(BRepPrimAPI_MakeSphere(point, size).Shape() for point, size in distinct)
    if not balls:
        return pipes
    # 球は先に球どうしで順に結合して1つの束にする(角から引く回数を減らす)。外部の罠: 球を1回のブーリアン(引数を全部
    # 渡す)で結合するのは、順に1つずつより遅く重かった(球 140 個で 3 秒 → 47 秒・3.7GB)。
    bundle = balls[0]
    for ball in balls[1:]:
        bundle = _boolean("fuse", bundle, ball).Shape()
    return pipes + [bundle]


def _exact_tube(points: List[gp_Pnt], loop: bool, radius: float):
    """道が円か直線に載るなら、球を掃いた立体を厳密な形(トーラス・円柱)で作る。載らなければ None。

    B スプラインの管より速く正確: 円板の上の先細りの柱を半径 20 で、角から引くブーリアンが 5.5 秒 → 0.07 秒、管の近似に
    よる体積の差(相対 2.7e-4)が無くなる。"""
    count = len(points)
    scale = max(1.0, max(points[0].Distance(point) for point in points))
    tolerance = 1.0e-7 * scale
    first, middle, last = points[0], points[count // 3], points[(2 * count) // 3]
    a, b = gp_Vec(last, first), gp_Vec(last, middle)
    normal = a.Crossed(b)
    if normal.Magnitude() < 1.0e-9 * scale * scale:
        if loop:
            return None
        along = gp_Vec(points[0], points[-1])
        length = along.Magnitude()
        if length < tolerance:
            return None
        along.Normalize()
        if any(gp_Vec(points[0], point).Crossed(along).Magnitude() > tolerance for point in points):
            return None
        return BRepPrimAPI_MakeCylinder(gp_Ax2(points[0], gp_Dir(along)), radius, length).Shape()
    # 3点を通る円の中心
    offset = b.Multiplied(a.SquareMagnitude()) - a.Multiplied(b.SquareMagnitude())
    center = last.Translated(offset.Crossed(normal).Divided(2.0 * normal.SquareMagnitude()))
    normal.Normalize()
    circle_radius = center.Distance(first)
    if circle_radius < _TIGHT_BEND * radius:
        return None  # 管が自分に折れ込む(紡錘形のトーラス)
    for point in points:
        if abs(center.Distance(point) - circle_radius) > tolerance or abs(gp_Vec(center, point).Dot(normal)) > tolerance:
            return None
    start = gp_Vec(center, points[0])
    if start.Crossed(gp_Vec(center, points[1])).Dot(normal) < 0.0:
        normal.Reverse()
    frame = gp_Ax2(center, gp_Dir(normal), gp_Dir(start))
    if loop:
        return BRepPrimAPI_MakeTorus(frame, circle_radius, radius).Shape()
    end = gp_Vec(center, points[-1])
    angle = math.atan2(start.Crossed(end).Dot(normal), start.Dot(end))
    if angle <= 0.0:
        angle += 2.0 * math.pi
    return BRepPrimAPI_MakeTorus(frame, circle_radius, radius, angle).Shape()


def _pipe(stretches: list, radius: float) -> TopoDS_Shape:
    """つながった `(曲線, 始め, 終わり)` の列(1本のワイヤー)に沿って半径 `radius` の円を掃いた管。"""
    wire = BRepBuilderAPI_MakeWire()
    for curve, first, last in stretches:
        wire.Add(BRepBuilderAPI_MakeEdge(curve, first, last).Edge())
    if not wire.IsDone():
        raise Unsupported("the ball path did not join into one wire")
    curve, first, _ = stretches[0]
    start, tangent = gp_Pnt(), gp_Vec()
    curve.D1(first, start, tangent)
    disk = BRepBuilderAPI_MakeFace(
        BRepBuilderAPI_MakeWire(BRepBuilderAPI_MakeEdge(gp_Circ(gp_Ax2(start, gp_Dir(tangent)), radius)).Edge()).Wire()
    ).Face()
    pipe = BRepOffsetAPI_MakePipe(wire.Wire(), disk, GeomFill_Trihedron.GeomFill_IsDiscreteTrihedron, False)
    pipe.Build()
    if not pipe.IsDone() or _volume(pipe.Shape()) <= 0.0:
        raise Unsupported("the ball could not be swept along its path")
    return pipe.Shape()


def _interpolated(run: "_Run", periodic: bool):
    """区間の点を通る B スプライン。端の向きがわかっていればそれに合わせる(隣の区間と向きまでつながる)。"""
    array = TColgp_HArray1OfPnt(1, len(run.points))
    for number, point in enumerate(run.points, 1):
        array.SetValue(number, point)
    interpolation = GeomAPI_Interpolate(array, periodic, 1.0e-9)
    if run.start is not None or run.end is not None:
        count = len(run.points)
        tangents, known = TColgp_Array1OfVec(1, count), TColStd_HArray1OfBoolean(1, count)
        for number in range(1, count + 1):
            tangents.SetValue(number, gp_Vec(0.0, 0.0, 0.0))
            known.SetValue(number, False)
        for number, direction in ((1, run.start), (count, run.end)):
            if direction is not None:
                tangents.SetValue(number, direction)
                known.SetValue(number, True)
        # 外部の罠: 補間の媒介変数は弦の長さなので、向きは長さ 1 のまま渡す(Scale=False)。Scale=True だと端で道が
        # 波打ち、曲がりの半径が 21 から 11 に縮んだ(正方形の板の上の柱 r=14 の、縁に乗る区間)。
        interpolation.Load(tangents, known, False)
    interpolation.Perform()
    if not interpolation.IsDone():
        raise Unsupported("the ball path could not be interpolated")
    return interpolation.Curve()


#: 道の曲がりを調べる点の数(曲線1本あたり)。
_BEND_SAMPLES = 64
#: 曲がりの半径が球の半径の何倍より小さければ球を並べるか。管が自分に折れ込むのは管の半径(球の 1 + 膨らませ方 倍)を
#: 下回るときだけ。1.05 倍にしたときは、楕円柱(半軸 20・2)の両端(道の曲がりの半径が球の 1.02 倍)まで球を 60 個並べ、
#: 1回の丸めに 147 秒かかった。
_TIGHT_BEND = 1.01
#: 曲がりのきつい道に球を並べる間隔(球の半径に対して)。並べた球の間のくぼみの深さは半径の 間隔²/8 ≈ 1e-3 倍。
_CHAIN_SPACING = 0.09
#: 並べる球の膨らませ方(半径に対して)。くぼみより浅く膨らませると、球が支える面に食い込む円どうしの間に隙間が残り、
#: 角の肉と上の空気がつながったまま分かれなかった(板の角を越える半径 22・半径 2 の穴へ転がり込む半径 6 を 1e-4 倍で)。
#: くぼみの深さの 2 倍にする。
_CHAIN_SWELL = _CHAIN_SPACING ** 2 / 4.0


def _stretches(curve, radius: float) -> List[Tuple[float, float, bool]]:
    """曲線を、曲がりの半径が球の半径の `_TIGHT_BEND` 倍より小さい範囲(きつい)とそれ以外に分けた `(始め, 終わり, きついか)`。
    きつい範囲は、調べた点の間隔1つぶんずつ両側へ広げる(点と点の間で曲がりがきつくなっても管にしない)。"""
    props = GeomLProp_CLProps(curve, 2, 1.0e-9)
    first, last = curve.FirstParameter(), curve.LastParameter()
    params = [first + (last - first) * number / _BEND_SAMPLES for number in range(_BEND_SAMPLES + 1)]
    sharp = []
    for param in params:
        props.SetParameter(param)
        curvature = props.Curvature()
        sharp.append(curvature > 1.0e-12 and 1.0 / curvature < _TIGHT_BEND * radius)
    tight = [sharp[i] or sharp[i + 1] for i in range(_BEND_SAMPLES)]
    tight = [any(tight[max(0, i - 1):i + 2]) for i in range(_BEND_SAMPLES)]
    stretches: List[Tuple[float, float, bool]] = []
    for i, kind in enumerate(tight):
        if stretches and stretches[-1][2] == kind:
            stretches[-1] = (stretches[-1][0], params[i + 1], kind)
        else:
            stretches.append((params[i], params[i + 1], kind))
    return stretches


#: 管の切れ目に置く球の膨らませ方を、管の何倍にするか(管の面と球を接させないため)。
_CAP_FACTOR = 3.0


def _ball_chain(curve, first: float, last: float, radius: float) -> list:
    """曲線の `first`〜`last` に沿って間隔 `_CHAIN_SPACING * radius` で球を並べる(両端は `_swept_balls` が置くので除く)。"""
    adaptor = GeomAdaptor_Curve(curve)
    length = GCPnts_AbscissaPoint.Length_s(adaptor, first, last)
    count = max(2, int(math.ceil(length / (_CHAIN_SPACING * radius))) + 1)
    spheres = []
    for number in range(1, count - 1):
        place = GCPnts_AbscissaPoint(adaptor, length * number / (count - 1), first)
        if not place.IsDone():
            raise Unsupported("the ball path could not be divided")
        spheres.append(BRepPrimAPI_MakeSphere(curve.Value(place.Parameter()), radius).Shape())
    return spheres


def _require_plausible(material: TopoDS_Shape, edges: list, spread: float) -> None:
    """肉が辺のまわりの管(半径 `spread` = 接点までの距離 + 半径)に収まる大きさと場所かを確かめる。

    外部の罠: くさびの延長面が箱を割り切れなかったり、くさびのフィレットが変な面を作ったりすると、OCCT は
    エラーを出さずに見当違いの肉を返す。そのまま本体と合わせると、統合(UnifySameDomain)が仮想メモリを使い尽くし、
    PC ごと落ちた(PC ケースの部品で 84GB。2026-10-22)。肉の各点は辺から `spread` より離れないので、
    体積は辺の長さ × 管の断面積を超えず、外接箱は辺の外接箱を `spread` 広げた箱に収まる。
    """
    length = sum(_edge_length(edge) for edge in edges)
    volume = _volume(material)
    if volume > 1.5 * math.pi * spread * spread * length:
        raise Unsupported(f"the rounding material ({volume:.4g} mm3) is larger than any fillet along {length:.4g} mm")
    reach_box = Bnd_Box()
    for edge in edges:
        BRepBndLib.Add_s(edge, reach_box)
    reach_box.Enlarge(1.1 * spread)
    found = Bnd_Box()
    BRepBndLib.AddOptimal_s(material, found, False, False)
    if found.IsVoid():
        return
    if any(reach_box.IsOut(corner) for corner in (found.CornerMin(), found.CornerMax())):
        raise Unsupported("the rounding material reaches far from the edge")


def _edge_length(edge: TopoDS_Shape) -> float:
    props = GProp_GProps()
    BRepGProp.LinearProperties_s(edge, props)
    return props.Mass()


def _room_inside(face: TopoDS_Shape, point: gp_Pnt):
    """`point` が面の曲面の内側(円柱の軸側、球の中心側)にあれば、その曲面の半径。外側・平面・それ以外の曲面は None。

    凸の柱の上縁(物体側のくさびが円柱の内側)や止まり穴の底(空気側のくさびが穴の内側)は、曲面の半径以上の球が
    入らない。それでも延長したくさびの `MakeFillet` は通ることがあり、半径 0.56 の細いピンの上縁を半径 12 で丸めると
    見当違いの肉ができ、統合で仮想メモリを使い尽くした(PC ケースの部品)。
    円錐は見ない: 球の中心は辺より奥にあり、下へ広がる円錐ではそこの半径が辺の半径より大きいので、辺での半径は上限に
    ならない(上面半径 5・下面半径 15・高さ 10 の円錐台の上縁を半径 6 で丸めるのを OCCT は通すのに、断っていた。Codex レビューの反例)。
    """
    surface = BRepAdaptor_Surface(TopoDS.Face_s(face))
    kind = surface.GetType()
    if kind == GeomAbs_SurfaceType.GeomAbs_Cylinder:
        axis, radius = surface.Cylinder().Axis(), surface.Cylinder().Radius()
        return radius if _distance_to_axis(axis, point) < radius else None
    if kind == GeomAbs_SurfaceType.GeomAbs_Sphere:
        sphere = surface.Sphere()
        return sphere.Radius() if point.Distance(sphere.Location()) < sphere.Radius() else None
    return None


def _distance_to_axis(axis, point: gp_Pnt) -> float:
    offset = gp_Vec(axis.Location(), point)
    along = offset.Dot(gp_Vec(axis.Direction()))
    return math.sqrt(max(0.0, offset.SquareMagnitude() - along * along))


def _trim(material, edges, faces, heights, concave: bool, columns: "_Columns"):
    """肉のうち、支える面(何かが立っている足元を塞いだもの)の柱の外にあり、辺の内側の点に触れない部分を捨てる。

    支える面の内側の輪のうち、別の形が立っている足元(凹の辺の輪)は塞ぐ: その上の肉は、和で本体に埋もれるか、
    低い柱の上を越えて丸めが続く部分なので捨てない。本当の穴(凸の辺の輪)は塞がない: 穴の上の肉は穴の壁の延長で
    切れる(全部の輪を塞いだときは、柱の脇の貫通穴の上に肉が残って穴を塞いだ。Codex レビューの反例)。
    足元を塞がず「本体に重なれば残す」で救っていたときは、辺の端の先へ伸びた肉が床の上の別の突起に少し重なっただけで
    丸ごと残り、外接箱を大きく越えた(同じく反例)。

    面ごとの柱の外を別々に見る: 2面の柱の共通部分の外をまとめて見ると、傾いた壁の柱の境界が辺から斜めに出て残す
    「辺に触れる薄い層」と、もう一方の面の崖の先へはみ出した肉が1つにつながり、崖の先まで残った(円板 r25 の上の
    先細りの柱の根元を r=20 で丸めると、縁から 1.66mm はみ出した)。
    """
    scale = max(_volume(material), 1.0e-12)
    dropped = []
    for face, height in zip(faces, heights):
        outside = _boolean("cut", material, columns.of(face, height, concave)).Shape()
        for part in _solids(outside):
            if _volume(part) > 1.0e-9 * scale and not _touches_inside_of(part, edges):
                dropped.append(part)
    for part in dropped:
        material = _boolean("cut", material, part).Shape()
    return material


class _Columns:
    """1回の吸収の中で、支える面の柱を面・厚み・向きごとに1度だけ作って使い回す(形の辺と面の表も持つ)。

    柱の多い板(柱 40 本の根元を一度に丸める)では、どの組も同じ板の上面の柱を作り直していた。使い回しで 24.8 → 23.7 秒
    (伸びの主な元ではなかった。docs/FILLET_OVERFLOW.md「時間」)。
    """

    def __init__(self, shape: TopoDS_Shape, edge_faces) -> None:
        self._faces = indexed_map(shape, TopAbs_FACE)
        self._edges = indexed_map(shape, TopAbs_EDGE)
        self._edge_faces = edge_faces
        self._made: Dict[tuple, TopoDS_Shape] = {}

    @property
    def edge_faces(self):
        return self._edge_faces

    def edge_index(self, edge: TopoDS_Shape) -> int:
        """形の辺の通し番号(足元を塞いだ面の写しでも同じ辺なら同じ番号。形に無ければ 0)。"""
        return self._edges.FindIndex(edge)

    def of(self, face: TopoDS_Shape, height: float, concave: bool) -> TopoDS_Shape:
        key = (self._faces.FindIndex(face), round(height, 9), concave)
        if key[0] == 0:
            return _region_column(face, self._edge_faces, height, concave)
        if key not in self._made:
            self._made[key] = _region_column(face, self._edge_faces, height, concave)
        return self._made[key]


def _region_column(face: TopoDS_Shape, edge_faces, height: float, concave: bool) -> TopoDS_Shape:
    """`face` と同じ平面に続く面すべての柱を合わせた立体(足元は塞ぐ)。"""
    columns = [_column(_without_footprints(member, edge_faces), height, concave) for member in _coplanar_region(face, edge_faces)]
    combined = columns[0]
    for other in columns[1:]:
        combined = _boolean("fuse", combined, other).Shape()
    return combined


def _without_footprints(face: TopoDS_Shape, edge_faces) -> TopoDS_Shape:
    """面の内側の輪のうち、別の形が立っている足元(隣の面と凹で接する輪)を外した面。本当の穴の輪は残す。
    残す輪だけを、同じ曲面・同じ位置の空の面へ入れ直す(辺の pcurve をそのまま使える)。"""
    wires = indexed_map(face, TopAbs_WIRE)
    if wires.Extent() <= 1:
        return face
    outer = BRepTools.OuterWire_s(TopoDS.Face_s(face))
    filled = TopoDS.Face_s(face.EmptyCopied())
    builder = BRep_Builder()
    for number in range(1, wires.Extent() + 1):
        wire = wires.FindKey(number)
        if wire.IsSame(outer) or not _is_footprint(wire, face, edge_faces):
            builder.Add(filled, wire)
    return filled


def _is_footprint(wire: TopoDS_Shape, face: TopoDS_Shape, edge_faces) -> bool:
    """内側の輪の上に別の形が立っているか(穴でなく足元か)。輪の最初の辺で見る。

    鋭い辺は凹凸で決める(凹なら足元、凸なら穴)。接線で続く辺(根元を先に丸めた柱の足元、入口を丸めた穴)は凹凸が
    出ないので、辺から向こうの面の中へ少し進んだ点が `face` の外側(空気の側)にあるかで決める。
    凹凸だけで決めると、根元を丸めた柱の足元を穴と見なして柱の脇に幅 0.05 の溝を残した。向こうの面全体の重心で決めると、
    板から立ち上がって板の裏へ回り込むトーラスの足元を穴と見なした(どちらも Codex レビューの反例)。
    """
    edges = indexed_map(wire, TopAbs_EDGE)
    for number in range(1, edges.Extent() + 1):
        edge = TopoDS.Edge_s(edges.FindKey(number))
        if BRep_Tool.Degenerated_s(edge) or not edge_faces.Contains(edge):
            continue
        neighbours = [other for other in edge_faces.FindFromKey(edge) if not other.IsSame(face)]
        if not neighbours:
            return False
        neighbour = TopoDS.Face_s(neighbours[0])
        kind = ChFi3d.DefineConnectType_s(edge, TopoDS.Face_s(face), neighbour, 1.0e-6, False)
        if kind == ChFiDS_TypeOfConcavity.ChFiDS_Concave:
            return True
        if kind == ChFiDS_TypeOfConcavity.ChFiDS_Convex:
            return False
        return _rises_beside(edge, face, neighbour)
    return False


def _rises_beside(edge, face: TopoDS_Shape, neighbour) -> bool:
    """辺の中点から `neighbour` の中へ少し進んだ点が、`face` の外側(外向き法線の側)にあるか。"""
    curve = BRepAdaptor_Curve(edge)
    middle = 0.5 * (curve.FirstParameter() + curve.LastParameter())
    on_edge = curve.Value(middle)
    pcurve = BRepAdaptor_Curve2d(edge, neighbour)
    uv, tangent = gp_Pnt2d(), gp_Vec2d()
    pcurve.D1(middle, uv, tangent)
    if tangent.Magnitude() < 1.0e-12:
        return False
    across = gp_Vec2d(-tangent.Y(), tangent.X()).Normalized()
    surface = BRepAdaptor_Surface(neighbour)
    span = max(surface.LastUParameter() - surface.FirstUParameter(), surface.LastVParameter() - surface.FirstVParameter())
    step = 1.0e-2 * min(span, 1.0e3)
    normal = _outward_normal(face, on_edge)
    for sign in (1.0, -1.0):
        probe = gp_Pnt2d(uv.X() + sign * step * across.X(), uv.Y() + sign * step * across.Y())
        if BRepClass_FaceClassifier(neighbour, probe, 1.0e-9).State() == TopAbs_IN:
            return gp_Vec(on_edge, surface.Value(probe.X(), probe.Y())).Dot(normal) > 0.0
    return False


def _column(face: TopoDS_Shape, height: float, concave: bool) -> TopoDS_Shape:
    """面を法線方向に `height` だけ厚くした立体(凹なら外へ、凸なら内へ)。

    平面は押し出しで作る: 側面が円柱・平面になり、はみ出した肉を切った面が崖の側面と同じ曲面になって統合で1枚に
    戻る(厚み付けの側面は Bスプラインで、円板の縁に高さ 0.0015mm の別の面が残った)。
    """
    surface = BRepAdaptor_Surface(TopoDS.Face_s(face))
    if surface.GetType() == GeomAbs_SurfaceType.GeomAbs_Plane:
        direction = gp_Vec(surface.Plane().Axis().Direction())
        if (face.Orientation() == TopAbs_REVERSED) == concave:
            direction.Reverse()
        # 外部の罠: 統合(UnifySameDomain)は軸が逆向きの円柱を同じ面と見なさない。円の辺から押し出した円柱面の軸の
        # 向きは押し出す向きで決まり、スケッチの押し出しで作った円板の壁と揃うのはこの向き(面から外へ)。
        # `BRepPrimAPI_MakeCylinder` の円柱とは逆になり、縁に細い帯の面が残る(r=21.25 で高さ 0.0015mm)。
        return BRepPrimAPI_MakePrism(face, direction.Multiplied(height)).Shape()
    maker = BRepOffsetAPI_MakeThickSolid()
    maker.MakeThickSolidBySimple(face, height if concave else -height)
    maker.Build()
    if not maker.IsDone():
        raise Unsupported("could not thicken a supporting face")
    column = maker.Shape()
    # 外部の罠: 面の向きによって、体積が負(裏返し)の立体が返る。
    if _volume(column) < 0:
        column.Reverse()
    return column


def _touches_inside_of(part: TopoDS_Shape, edges: list) -> bool:
    """`part` が辺の内側の点(端点でない)に触れるか。辺の端の先へ伸びた肉は端点でだけ触れる。

    辺の上の点を並べて、`part` の境界に載るかで見る。外部の罠: `BRepExtrema_DistShapeShape` は辺に沿って弧で
    触れていても解を頂点にしか返さないことがある(曲面の上の柱の根元で、弧で接する部分が頂点の接触と出た)。
    """
    for edge in edges:
        curve = BRepAdaptor_Curve(TopoDS.Edge_s(edge))
        first, last = curve.FirstParameter(), curve.LastParameter()
        tolerance = max(1.0e-6, 10.0 * BRep_Tool.Tolerance_s(TopoDS.Edge_s(edge)))
        classifier = BRepClass3d_SolidClassifier(part)
        for step in range(1, _EDGE_SAMPLES):
            point = curve.Value(first + (last - first) * step / _EDGE_SAMPLES)
            classifier.Perform(point, tolerance)
            if classifier.State() != TopAbs_OUT:
                return True
    return False


#: 辺の内側を何等分して `part` に触れるか見るか(端点を除く `_EDGE_SAMPLES - 1` 点)。
_EDGE_SAMPLES = 48


def _edges_through(wedge: TopoDS_Shape, edges: list) -> list:
    """くさびの辺のうち、元の辺の中点を通るもの(箱を延長面で割ると、元の辺は延長されて1本の辺になる)。"""
    found = []
    wedge_edges = indexed_map(wedge, TopAbs_EDGE)
    for edge in edges:
        curve = BRepAdaptor_Curve(TopoDS.Edge_s(edge))
        middle = BRepBuilderAPI_MakeVertex(curve.Value(0.5 * (curve.FirstParameter() + curve.LastParameter()))).Vertex()
        for number in range(1, wedge_edges.Extent() + 1):
            candidate = TopoDS.Edge_s(wedge_edges.FindKey(number))
            if BRepExtrema_DistShapeShape(candidate, middle).Value() < 1.0e-6 and not any(candidate.IsSame(e) for e in found):
                found.append(candidate)
    if not found:
        raise Unsupported("the wedge has no edge along the selected edge")
    return found


def _outward_normal(face: TopoDS_Shape, point: gp_Pnt) -> gp_Vec:
    surface = BRep_Tool.Surface_s(TopoDS.Face_s(face))
    u, v = GeomAPI_ProjectPointOnSurf(point, surface).LowerDistanceParameters()
    props = BRepLProp_SLProps(BRepAdaptor_Surface(TopoDS.Face_s(face)), u, v, 1, 1.0e-7)
    if not props.IsNormalDefined():
        raise Unsupported("the face has no normal at the edge")
    normal = gp_Vec(props.Normal())
    if face.Orientation() == TopAbs_REVERSED:
        normal.Reverse()
    return normal


def _extended_face(face: TopoDS_Shape, size: float, near: gp_Pnt) -> TopoDS_Shape:
    """面の曲面を、辺のまわりの箱を貫く大きさまで延長した面(向きは元の面と同じ)。"""
    surface = BRep_Tool.Surface_s(TopoDS.Face_s(face))
    if isinstance(surface, Geom_RectangularTrimmedSurface):
        surface = surface.BasisSurface()
    adaptor = BRepAdaptor_Surface(TopoDS.Face_s(face))
    kind = adaptor.GetType()
    u_first, u_last, v_first, v_last = surface.Bounds()
    u0, v0 = GeomAPI_ProjectPointOnSurf(near, surface).LowerDistanceParameters()
    if kind == GeomAbs_SurfaceType.GeomAbs_Plane:
        bounds = (u0 - size, u0 + size, v0 - size, v0 + size)
    elif kind in (GeomAbs_SurfaceType.GeomAbs_Cylinder, GeomAbs_SurfaceType.GeomAbs_SurfaceOfExtrusion):
        bounds = (u_first, u_last, v0 - size, v0 + size)
    elif kind == GeomAbs_SurfaceType.GeomAbs_Cone:
        # 頂点を越えて延ばすと面が自分と交わる。面のある側だけ延ばす。
        cone = adaptor.Cone()
        apex_v = -cone.RefRadius() / math.sin(cone.SemiAngle())
        # 頂点で止めると、頂点が箱の中にあっても面が閉じた杯になって箱を内と外に割り切る(頂点の手前で止めると
        # 小さな穴が残り、箱が割れずにくさびが取れなかった。円板の上の先細りの柱の根元を r=20)。
        bounds = (0.0, 2.0 * math.pi, apex_v, v0 + size) if v0 > apex_v else (0.0, 2.0 * math.pi, v0 - size, apex_v)
    else:
        bounds = (u_first, u_last, v_first, v_last)
    made = BRepBuilderAPI_MakeFace(surface, *bounds, 1.0e-7)
    if not made.IsDone():
        raise Unsupported("could not extend a supporting face")
    extended = made.Face()
    if face.Orientation() == TopAbs_REVERSED:
        extended.Reverse()
    return extended


def _solid_containing(shape: TopoDS_Shape, point: gp_Pnt):
    for solid in _solids(shape):
        if BRepClass3d_SolidClassifier(solid, point, 1.0e-7).State() == TopAbs_IN:
            return solid
    return None


def _solids(shape: TopoDS_Shape) -> list:
    found = indexed_map(shape, TopAbs_SOLID)
    return [found.FindKey(i) for i in range(1, found.Extent() + 1)]


def _boolean(kind: str, first: TopoDS_Shape, second: TopoDS_Shape):
    builder = {"fuse": BRepAlgoAPI_Fuse, "cut": BRepAlgoAPI_Cut, "common": BRepAlgoAPI_Common}[kind](first, second)
    builder.Build()
    if not builder.IsDone():
        raise Unsupported(f"{kind} of the rounding material failed")
    return builder


def _volume(shape: TopoDS_Shape) -> float:
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, props)
    return props.Mass()
