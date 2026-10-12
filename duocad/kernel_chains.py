"""辺の連なり: フィレット・面取りで1本として振る舞う辺の組(画面の辺の選択単位)。

`kernel.tessellate_edges` が各辺の `chain` に使う。添字は `kernel._edge_map` と同じ数え方(`indexed_map` の1始まり)。
"""
from __future__ import annotations

from typing import Dict, List

from OCP.BRep import BRep_Tool
from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
from OCP.ChFi3d import ChFi3d
from OCP.GeomAbs import GeomAbs_C0, GeomAbs_G1
from OCP.TopAbs import TopAbs_EDGE, TopAbs_EXTERNAL, TopAbs_FACE, TopAbs_INTERNAL, TopAbs_VERTEX
from OCP.TopExp import TopExp, TopExp_Explorer
from OCP.TopoDS import TopoDS, TopoDS_Shape, TopoDS_Vertex
from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape

from .mesh_geometry import indexed_map


def blend_chains(shape: TopoDS_Shape) -> Dict[int, int]:
    """辺の添字 → その辺が属する連なりの代表(連なりの最小の添字)。

    連なりは OCCT のフィレットが1本の辺から広げる範囲そのもの(`MakeFillet.Add` の輪郭)。
    実測: 1本を渡すと、一直線上で割れた辺も、直線→円弧→直線の接線連続も、フィレット・面取りとも
    全部にかかる。画面の選択をこの単位にしないと「光った1本より広く丸まる」になる。

    `Add` 1回は形状全体に比例して重い(実測: 1,212辺で 0.9ms、4,812辺で 3.8ms)ので、全辺に呼ぶと辺数の2乗で
    伸びる(円柱の穴を400個あけた板の4,812辺で 8.4 秒)。伸びうる辺だけに呼ぶ(`_sieve`)。篩を入れた実測: 同じ板 0.09 秒、
    27部品の PC ケース(`web/scripts/models.mjs` の `PC_CASE_STEP`)で最も重いボディ(471辺・候補256本)0.71 秒、
    27ボディすべてで全辺に `Add` した結果と一致。
    """
    edge_map = indexed_map(shape, TopAbs_EDGE)
    chains: Dict[int, int] = {index: index for index in range(1, edge_map.Extent() + 1)}
    candidates = _sieve(shape, edge_map)
    if not candidates:
        return chains
    maker = BRepFilletAPI_MakeFillet(shape)
    done: set = set()
    for index in candidates:
        if index in done:
            continue
        contours = maker.NbContours()
        try:
            maker.Add(1.0, TopoDS.Edge_s(edge_map.FindKey(index)))
        except Exception:  # noqa: BLE001  OCCTはかけられない辺で例外を投げることがある
            continue
        if maker.NbContours() == contours:
            continue
        contour = contours + 1
        members = {index} | {edge_map.FindIndex(maker.Edge(contour, j)) for j in range(1, maker.NbEdges(contour) + 1)}
        members.discard(0)
        for member in members:
            chains[member] = min(members)
        done |= members
    return chains


def _sieve(shape: TopoDS_Shape, edge_map) -> List[int]:
    """連なりが伸びうる辺の添字: **鋭い辺がちょうど2本集まる頂点**の、その2本。

    OCCT 7.9.3 の伝播(`ChFi3d_Builder::PerformElement` と `FaceTangency`、src/ChFi3d/ChFi3d_Builder_1.cxx)は、
    頂点 V で辺 E0 から E1 へ伸ばす条件を「E1 が鋭く、V に集まる他の辺(E0・E1・退化辺・INTERNAL/EXTERNAL を除く)が
    すべて滑らか」とし、鋭くない辺からは始めない。よって伸びるのは V の鋭い辺が {E0, E1} の2本だけのときに限る。
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
    # OCCT は頂点に集まる辺を出現ごとに見て INTERNAL / EXTERNAL の出現だけを飛ばす。同じ辺が通常の向きでも
    # 現れれば数えるので、除くのは全出現が INTERNAL / EXTERNAL の辺だけ(代表キー1つの向きで決めない)。
    regular: set = set()
    explorer = TopExp_Explorer(shape, TopAbs_EDGE)
    while explorer.More():
        if explorer.Current().Orientation() not in (TopAbs_INTERNAL, TopAbs_EXTERNAL):
            regular.add(edge_map.FindIndex(explorer.Current()))
        explorer.Next()
    vertex_edges: Dict[int, List[int]] = {}
    for index in range(1, edge_map.Extent() + 1):
        edge = TopoDS.Edge_s(edge_map.FindKey(index))
        if index not in regular or BRep_Tool.Degenerated_s(edge):
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
        if len(sharp_here) == 2:
            found |= sharp_here
    return sorted(found)
