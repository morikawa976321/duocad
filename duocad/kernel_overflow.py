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

import math
from typing import Dict, List, NamedTuple, Sequence, Tuple

from OCP.BRep import BRep_Builder, BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Curve2d, BRepAdaptor_Surface
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse, BRepAlgoAPI_Splitter
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakeVertex
from OCP.BRepClass import BRepClass_FaceClassifier
from OCP.BRepClass3d import BRepClass3d_SolidClassifier
from OCP.BRepExtrema import BRepExtrema_DistShapeShape
from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
from OCP.BRepGProp import BRepGProp
from OCP.BRepLProp import BRepLProp_SLProps
from OCP.BRepOffsetAPI import BRepOffsetAPI_MakeThickSolid
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakePrism
from OCP.BRepTools import BRepTools
from OCP.Bnd import Bnd_Box
from OCP.ChFi3d import ChFi3d
from OCP.ChFiDS import ChFiDS_TypeOfConcavity
from OCP.Geom import Geom_RectangularTrimmedSurface
from OCP.GeomAbs import GeomAbs_SurfaceType
from OCP.GeomAPI import GeomAPI_ProjectPointOnSurf
from OCP.GProp import GProp_GProps
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_IN, TopAbs_OUT, TopAbs_REVERSED, TopAbs_SOLID, TopAbs_VERTEX, TopAbs_WIRE
from OCP.TopExp import TopExp
from OCP.TopoDS import TopoDS, TopoDS_Shape
from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape, TopTools_ListOfShape
from OCP.gp import gp_Pnt, gp_Pnt2d, gp_Vec, gp_Vec2d

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
    # 柱の厚みは箱の余白(肉はこの中にしか無い)。ただし曲面を内側(軸の側)へ厚くするときは、曲面の半径の 9 割で止める:
    # 半径以上に厚くすると軸へ潰れて共通部分が失敗した(半径 4 の柱の上縁を半径 1 で、余白 4)。厚みを接点までの距離で
    # 絞ると、曲面の側の接点は辺の法線から求めた距離より遠いので肉が欠けた(半径 10 の半円柱の縦辺を半径 4 で、
    # 平面から 6.67 まで届く肉を 5 で切った。Codex レビューの反例)。
    heights = []
    for face, normal in ((first, normals[0]), (second, normals[1])):
        toward = normal if concave else normal.Reversed()
        room = _room_inside(face, point.Translated(toward.Multiplied(1.0e-3 * radius)))
        heights.append(margin if room is None else min(margin, 0.9 * room))
    trimmed = _trim(material, edges, (first, second), heights, concave, columns)
    _require_plausible(trimmed, edges, 1.5 * (reach + radius))
    return trimmed, concave


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
    """1回の吸収の中で、支える面の柱を面・厚み・向きごとに1度だけ作って使い回す。

    柱の多い板(柱 40 本の根元を一度に丸める)では、どの組も同じ板の上面の柱を作り直し、その上面は柱の数だけ足元の輪を
    持つので、組の数の2乗で遅くなった(1本あたり 5 本で 0.29 秒、40 本で 0.62 秒)。
    """

    def __init__(self, shape: TopoDS_Shape, edge_faces) -> None:
        self._faces = indexed_map(shape, TopAbs_FACE)
        self._edge_faces = edge_faces
        self._made: Dict[tuple, TopoDS_Shape] = {}

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
