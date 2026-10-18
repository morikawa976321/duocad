"""辺の連なり: フィレット・面取りで1本として振る舞う辺の組(画面の辺の選択単位)と、フィレットの事前検査。

画面は辺の折れ線(`kernel.tessellate_edges`)とは別に、指した辺の分だけ取り寄せる(`BlendChains`。`view.DocumentView.edge_chains`)。
添字は `kernel._edge_map` と同じ数え方(`indexed_map` の1始まり)。
"""
from __future__ import annotations

import math
from typing import Dict, Iterable, List, Optional, Tuple

from OCP.Bnd import Bnd_Box
from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepExtrema import BRepExtrema_DistShapeShape
from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
from OCP.BRepGProp import BRepGProp_Face
from OCP.ChFi3d import ChFi3d
from OCP.GeomAPI import GeomAPI_ProjectPointOnSurf
from OCP.GeomAbs import GeomAbs_C0, GeomAbs_G1, GeomAbs_SurfaceType
from OCP.gp import gp_Pnt, gp_Vec
from OCP.TopAbs import TopAbs_EDGE, TopAbs_EXTERNAL, TopAbs_FACE, TopAbs_INTERNAL, TopAbs_VERTEX
from OCP.TopExp import TopExp, TopExp_Explorer
from OCP.TopoDS import TopoDS, TopoDS_Shape, TopoDS_Vertex
from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

from .mesh_geometry import indexed_map


def blend_chains(shape: TopoDS_Shape) -> Dict[int, int]:
    """辺の添字 → その辺が属する連なりの代表(連なりの最小の添字)。全辺ぶんを一度に求める(`BlendChains` の全部)。

    連なりは OCCT のフィレットが1本の辺から広げる範囲そのもの(`MakeFillet.Add` の輪郭)。
    実測: 1本を渡すと、一直線上で割れた辺も、直線→円弧→直線の接線連続も、フィレット・面取りとも
    全部にかかる。画面の選択をこの単位にしないと「光った1本より広く丸まる」になる。
    """
    edge_count = indexed_map(shape, TopAbs_EDGE).Extent()
    groups = BlendChains(shape).of(range(1, edge_count + 1))
    return {index: group[0] for index, group in groups.items()}


class BlendChains:
    """1つの形の辺の連なりを、頼まれた辺の分だけ求めて覚える。形は不変(差し替えは別の形)なので、作り直すのは形が変わったときだけ。

    `Add` 1回は形の複雑さに比例して重い(実測: 1,212辺で 0.9ms、4,812辺で 3.8ms、27部品の PC ケース
    (`web/scripts/models.mjs` の `PC_CASE_STEP`)の最大ボディ 645辺で 9.5ms)ので、全辺に呼ぶと辺数の2乗で伸びる
    (円柱の穴を400個あけた板の4,812辺で 8.4 秒)。全辺ぶんを求めるときは伸びうる辺だけに呼ぶ(`_sieve`)。篩を入れた実測:
    同じ板 0.09 秒、PC ケースで最も重いボディ(471辺・候補256本)0.71 秒、27ボディすべてで全辺に `Add` した結果と一致。
    篩そのものも重い(最大ボディで 0.12 秒。`IsTangentFaces` が大半)ので、辺1本なら篩を通さず `Add` 1回で求める。
    """

    #: 一度に頼まれた未知の辺がこれより多ければ、先に篩で伸びうる辺を絞る(篩 0.12 秒 ≒ `Add` 12回ぶん。上の実測)。
    SIEVE_ABOVE = 12

    def __init__(self, shape: TopoDS_Shape) -> None:
        self._shape = shape
        self._edge_map = indexed_map(shape, TopAbs_EDGE)
        self._maker: Optional[BRepFilletAPI_MakeFillet] = None
        self._candidates: Optional[set] = None
        self._groups: Dict[int, Tuple[int, ...]] = {}

    def of(self, indices: Iterable[int]) -> Dict[int, Tuple[int, ...]]:
        """頼まれた辺それぞれ → その辺が属する連なりの添字の全部(昇順。連なりの無い辺は自分1本)。範囲外は ValueError。"""
        wanted = [int(index) for index in indices]
        extent = self._edge_map.Extent()
        for index in wanted:
            if not 1 <= index <= extent:
                raise ValueError(f"edge index out of range: {index} (1..{extent})")
        unknown = [index for index in dict.fromkeys(wanted) if index not in self._groups]
        if len(unknown) > self.SIEVE_ABOVE and self._candidates is None:
            self._candidates = set(_sieve(self._shape, self._edge_map))
        for index in unknown:
            if index in self._groups:
                continue
            if self._candidates is not None and index not in self._candidates:
                self._groups[index] = (index,)
                continue
            self._spread(index)
        return {index: self._groups[index] for index in wanted}

    def _spread(self, index: int) -> None:
        """`Add` 1回でその辺の連なりを求め、連なりの全部の辺に覚えさせる。"""
        if self._maker is None:
            self._maker = BRepFilletAPI_MakeFillet(self._shape)
        maker = self._maker
        contours = maker.NbContours()
        try:
            maker.Add(1.0, TopoDS.Edge_s(self._edge_map.FindKey(index)))
        except Exception:  # noqa: BLE001  OCCTはかけられない辺で例外を投げることがある
            self._groups[index] = (index,)
            return
        if maker.NbContours() == contours:
            self._groups[index] = (index,)
            return
        contour = contours + 1
        members = {index} | {
            self._edge_map.FindIndex(maker.Edge(contour, j)) for j in range(1, maker.NbEdges(contour) + 1)
        }
        members.discard(0)
        group = tuple(sorted(members))
        for member in group:
            self._groups[member] = group


def _sieve(shape: TopoDS_Shape, edge_map) -> List[int]:
    """連なりが伸びうる辺の添字: 頂点で伸びを止める鋭い辺が2本以下のとき、その頂点の鋭い辺(2本以上あるとき)。

    OCCT 7.9.3 の伝播(`ChFi3d_Builder::PerformElement` と `FaceTangency`、src/ChFi3d/ChFi3d_Builder_1.cxx)は、
    頂点 V で辺 E0 から E1 へ伸ばす条件を「E1 が鋭く、V に集まる他の辺(E0・E1・退化辺・INTERNAL/EXTERNAL を除く)が
    すべて滑らか」とし、鋭くない辺からは始めない。向きによる除外は止める側の辺にだけ効き、E0・E1 は全出現が
    INTERNAL / EXTERNAL の辺でもよい。よって伸びるのは、V で止める側に数える鋭い辺が {E0, E1} に含まれるときに限る。
    鋭い = 両側の面が2枚で `ChFi3d::IsTangentFaces`(G1。角度の許容 0.1 rad)が偽、または面が2枚でない。
    辺の接線の角度では絞れない: 面の継ぎ目が 0.1 rad 未満なら、辺が5度以上折れていても伸びる(平面図で 5.5° 折れた輪郭の押し出しの底辺)。
    写像は OCCT と同じ `TopExp::MapShapesAndAncestors`(継ぎ目の辺は同じ面が2回入る)。
    """
    edge_faces = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, edge_faces)
    vertex_map = indexed_map(shape, TopAbs_VERTEX)

    # OCP の `TopTools_ListOfShape` を for で回すと1リスト約 0.3ms かかる(4,812辺の板で 2.5 秒)。
    # 面は `Extent` / `First` / `Last` で読み、頂点に集まる辺は辺の両端から組む。
    sides: Dict[int, object] = {}
    for number in range(1, edge_faces.Extent() + 1):
        faces = edge_faces.FindFromIndex(number)
        index = edge_map.FindIndex(edge_faces.FindKey(number))
        if faces.Extent() == 2:
            sides[index] = (TopoDS.Edge_s(edge_faces.FindKey(number)), TopoDS.Face_s(faces.First()), TopoDS.Face_s(faces.Last()))
    # OCCT は止める側の辺を出現ごとに見て INTERNAL / EXTERNAL の出現だけを飛ばす。同じ辺が通常の向きでも
    # 現れれば止める側に数えるので、数えないのは全出現が INTERNAL / EXTERNAL の辺だけ(代表キー1つの向きで決めない)。
    regular: set = set()
    explorer = TopExp_Explorer(shape, TopAbs_EDGE)
    while explorer.More():
        if explorer.Current().Orientation() not in (TopAbs_INTERNAL, TopAbs_EXTERNAL):
            regular.add(edge_map.FindIndex(explorer.Current()))
        explorer.Next()
    vertex_edges: Dict[int, List[int]] = {}
    for index in range(1, edge_map.Extent() + 1):
        edge = TopoDS.Edge_s(edge_map.FindKey(index))
        if BRep_Tool.Degenerated_s(edge):
            continue
        first, last = TopoDS_Vertex(), TopoDS_Vertex()
        TopExp.Vertices_s(edge, first, last)
        for vertex in {vertex_map.FindIndex(first), vertex_map.FindIndex(last)} - {0}:
            vertex_edges.setdefault(vertex, []).append(index)

    def maybe_sharp(index: int) -> bool:
        """連続性の印が C0 でない辺は滑らか(`IsTangentFaces` が G1 で最初に見る印。標本を取らずに真を返す)。"""
        side = sides.get(index)
        return side is None or BRep_Tool.Continuity_s(side[0], side[1], side[2]) == GeomAbs_C0

    sharp_cache: Dict[int, bool] = {}

    def sharp(index: int) -> bool:
        if index not in sharp_cache:
            side = sides.get(index)
            sharp_cache[index] = side is None or not ChFi3d.IsTangentFaces_s(side[0], side[1], side[2], GeomAbs_G1)
        return sharp_cache[index]

    found: set = set()
    for edges in vertex_edges.values():
        here = [index for index in edges if maybe_sharp(index)]
        # 印で滑らかと分かった辺を除いて2本未満なら、鋭い辺は2本になりえない(高い `IsTangentFaces` を呼ばない)
        if len(here) < 2:
            continue
        sharp_here = {index for index in here if sharp(index)}
        if len({index for index in sharp_here if index in regular}) <= 2 and len(sharp_here) >= 2:
            found |= sharp_here
    return sorted(found)


#: フィレットの半径と、辺から同じ面の別の辺までの距離が「一致」とみなす差[mm]。OCCT の `Precision::Confusion`。
_COINCIDENT = 1.0e-7


def _outward_normal_at(face: TopoDS_Shape, point) -> Optional[Tuple[float, float, float]]:
    """面の `point`(面の縁の上の点)での外向き単位法線。取れなければ None。`BRepGProp_Face` は面の向きを見る。"""
    face = TopoDS.Face_s(face)
    projection = GeomAPI_ProjectPointOnSurf(point, BRep_Tool.Surface_s(face))
    if projection.NbPoints() == 0:
        return None
    u, v = projection.LowerDistanceParameters()
    foot, normal = gp_Pnt(), gp_Vec()
    BRepGProp_Face(face).Normal(u, v, foot, normal)
    if normal.Magnitude() <= 0.0:
        return None
    normal.Normalize()
    return normal.X(), normal.Y(), normal.Z()


def _fillet_reach(radius: float, edge: TopoDS_Shape, faces: list) -> float:
    """半径 `radius` のフィレットが、辺の両側の面の中へ使う幅(接線までの距離)。

    辺をはさむ2つの面の、辺の中点での外向き法線のなす角を φ とすると `radius * tan(φ/2)`(直角の辺は φ=90 度で
    `radius`、鈍角の辺は `radius` より短く、鋭角の辺は長い)。隣の面が曲面でも法線は取れる(平面と円錐面の組で
    実測)。法線が取れない(面が1枚)ときは `radius` とみなす。
    """
    if len(faces) != 2:
        return radius
    curve = BRepAdaptor_Curve(TopoDS.Edge_s(edge))
    middle = curve.Value((curve.FirstParameter() + curve.LastParameter()) / 2.0)
    first, second = _outward_normal_at(faces[0], middle), _outward_normal_at(faces[1], middle)
    if first is None or second is None:
        return radius
    cosine = max(-1.0, min(1.0, sum(a * b for a, b in zip(first, second))))
    return radius * math.tan(min(math.acos(cosine), math.pi - 1.0e-6) / 2.0)


def fillet_consumes_face(shape: TopoDS_Shape, edge_map, radii: Dict[int, float]) -> Optional[Tuple[int, int]]:
    """フィレットが辺の隣の平面の面を過不足なく食い尽くすものを探す。あれば `(辺の添字, 反対側の辺の添字)`。

    フィレットは辺から面の中へ幅 `w`(`_fillet_reach`。直角の辺では半径そのもの)を使う。その幅が、同じ面の別の辺
    (辺と頂点を共有しない辺)までの距離と一致すると面を過不足なく食い尽くし、OCCT は `IsDone()=False` で断るか、
    **ネイティブの access violation でプロセスごと落ちる**(Python の例外にならず `except` で捕まらない)。
    落ちる実測: `BRepPrimAPI_MakeBox` の箱の上面を厚さ 2 で抜き、縁(厚さ 2 の環状の面)の外側の辺を半径 2 で丸める
    (縁の外側4辺・縁の8辺・X に平行な辺全部・Y に平行な辺全部・全辺)。半径が 2 の 1.000000001 倍・0.999999999 倍では
    落ちない(窓は 2e-9 mm 未満)。同じ形を押し出しで作ると `IsDone()=False`。
    鈍角の辺は幅が半径より短いので、半径が面の幅と一致しても面は食い尽くされない(平行四辺形(0,0)(10,0)(13,4)(3,4)の
    柱の(10,0)の縦辺は R=5 で体積 1172.7 の正常な形になり、幅 5 を食い尽くす R=10 で `IsDone()=False`。円錐台の
    穴あきの上面の外周(平面の環の幅 5、隣は円錐面)も R=5 で `IsDone()=True`。2026-10-18 実測)。
    **平面の面でだけ測る**(曲面の面は実測していない)。面取りは同じ距離で落ちなかったので対象にしない。
    """
    edge_faces = TopTools_IndexedDataMapOfShapeListOfShape()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, edge_faces)
    vertex_map = indexed_map(shape, TopAbs_VERTEX)
    face_map = indexed_map(shape, TopAbs_FACE)
    boundaries: Dict[int, List[tuple]] = {}

    def vertices_of(edge: TopoDS_Shape) -> set:
        first, last = TopoDS_Vertex(), TopoDS_Vertex()
        TopExp.Vertices_s(TopoDS.Edge_s(edge), first, last)
        return {vertex_map.FindIndex(first), vertex_map.FindIndex(last)} - {0}

    def boundary_of(face: TopoDS_Shape) -> List[tuple]:
        """面の縁の辺ごとの `(辺の添字, 辺, 外接箱, 頂点の添字の集合)`(面ごとに1回だけ求める)。"""
        key = face_map.FindIndex(face)
        if key not in boundaries:
            entries = []
            face_edges = indexed_map(face, TopAbs_EDGE)
            for number in range(1, face_edges.Extent() + 1):
                edge = face_edges.FindKey(number)
                if BRep_Tool.Degenerated_s(TopoDS.Edge_s(edge)):
                    continue
                box = Bnd_Box()
                BRepBndLib.Add_s(edge, box)
                entries.append((edge_map.FindIndex(edge), edge, box, vertices_of(edge)))
            boundaries[key] = entries
        return boundaries[key]

    for index, radius in radii.items():
        edge = edge_map.FindKey(int(index))
        if not edge_faces.Contains(edge):
            continue
        ancestors = edge_faces.FindFromKey(edge)
        if ancestors.Extent() == 0:
            continue
        faces = [ancestors.First(), ancestors.Last()] if ancestors.Extent() > 1 else [ancestors.First()]
        box = Bnd_Box()
        BRepBndLib.Add_s(edge, box)
        mine = vertices_of(edge)
        reach = _fillet_reach(float(radius), edge, faces)
        for face in faces:
            if BRepAdaptor_Surface(TopoDS.Face_s(face)).GetType() != GeomAbs_SurfaceType.GeomAbs_Plane:
                continue
            for other_index, other, other_box, theirs in boundary_of(face):
                if other_index == int(index) or mine & theirs:
                    continue
                # 外接箱の距離は真の距離以下。半径より遠い辺は、正確な距離を測らずに除く。
                if box.Distance(other_box) > reach + _COINCIDENT:
                    continue
                measure = BRepExtrema_DistShapeShape(edge, other)
                if measure.IsDone() and abs(measure.Value() - reach) <= _COINCIDENT:
                    return int(index), other_index
    return None
