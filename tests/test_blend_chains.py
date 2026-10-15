"""`kernel_chains.blend_chains`: 画面で1本として選ぶ辺の連なりが、フィレット・面取りが実際にかかる範囲と一致する。"""
from __future__ import annotations

import pytest
from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.gp import gp_Pnt

from duocad import kernel, kernel_chains


def _stepped():
    """高さ5と高さ3の箱を x 方向に並べた段付き形状。底の手前と奥の辺が x=10 で2本に割れる。"""
    tall = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 10, 10, 5).Shape()
    low = BRepPrimAPI_MakeBox(gp_Pnt(10, 0, 0), 10, 10, 3).Shape()
    return BRepAlgoAPI_Fuse(tall, low).Shape()


def _bottom_edges_along_x(shape, y: float) -> list:
    found = []
    for item in kernel.tessellate_edges(shape):
        points = item["points"]
        ys, zs = points[1::3], points[2::3]
        if all(abs(v - y) < 1e-9 for v in ys) and all(abs(v) < 1e-9 for v in zs):
            found.append(item["index"])
    return found


def test_collinear_split_edges_share_one_chain() -> None:
    """落ちたら、一直線上で割れた辺が別々に選ばれ、光った1本より広く丸まる。"""
    shape = _stepped()
    front = _bottom_edges_along_x(shape, 0.0)
    assert len(front) == 2
    chains = kernel_chains.blend_chains(shape)
    assert chains[front[0]] == chains[front[1]] == min(front)


def test_chain_matches_what_the_fillet_actually_rounds() -> None:
    """落ちたら、連なりの1本だけを丸めた結果と、連なり全部を丸めた結果が食い違う(画面の単位がOCCTとずれた)。"""
    shape = _stepped()
    front = _bottom_edges_along_x(shape, 0.0)
    one = kernel.fillet_edges(shape, {front[0]: 1.0})
    whole = kernel.fillet_edges(shape, {index: 1.0 for index in front})
    assert one["volume_after"] == pytest.approx(whole["volume_after"], rel=1e-9)


def test_tangent_line_arc_line_is_one_chain() -> None:
    """落ちたら、角を丸めた後の底の輪郭(直線→円弧→直線)が1本ずつに戻る。"""
    box = BRepPrimAPI_MakeBox(10, 10, 5).Shape()
    vertical = next(
        item["index"]
        for item in kernel.tessellate_edges(box)
        if all(abs(v) < 1e-9 for v in item["points"][0::3] + item["points"][1::3])
    )
    rounded = kernel.fillet_edges(box, {vertical: 2.0})["shape"]
    described = kernel.tessellate_edges(rounded)
    chains = kernel_chains.blend_chains(rounded)
    bottom = [item for item in described if all(abs(v) < 1e-9 for v in item["points"][2::3])]
    arc = next(item for item in bottom if item["kind"] == "circle")
    together = [item["index"] for item in bottom if chains[item["index"]] == chains[arc["index"]]]
    assert len(together) == 3


def test_a_plain_box_has_one_chain_per_edge() -> None:
    """落ちたら、接線で続かない辺(直角の角)までまとめて選ばれる。"""
    chains = kernel_chains.blend_chains(BRepPrimAPI_MakeBox(10, 10, 5).Shape())
    assert sorted(chains) == list(range(1, 13))
    assert all(chains[index] == index for index in chains)


def _every_edge_chains(shape) -> dict:
    """篩を通さず全辺に `Add` する素朴な版(`blend_chains` の答え合わせ用)。"""
    from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
    from OCP.TopoDS import TopoDS

    edge_map = kernel._edge_map(shape)
    chains = {}
    for index in range(1, edge_map.Extent() + 1):
        maker = BRepFilletAPI_MakeFillet(shape)
        try:
            maker.Add(1.0, TopoDS.Edge_s(edge_map.FindKey(index)))
        except Exception:  # noqa: BLE001
            chains[index] = index
            continue
        if maker.NbContours() == 0:
            chains[index] = index
            continue
        members = [edge_map.FindIndex(maker.Edge(1, j)) for j in range(1, maker.NbEdges(1) + 1)]
        chains[index] = min([index, *members])
    return chains


def _shapes() -> dict:
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir

    box = BRepPrimAPI_MakeBox(20, 10, 5).Shape()
    every = {index: 1.0 for index in range(1, kernel._edge_map(box).Extent() + 1)}
    plate = BRepPrimAPI_MakeBox(30, 30, 5).Shape()
    for x in (8, 22):
        hole = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(x, 15, -1), gp_Dir(0, 0, 1)), 3, 7).Shape()
        plate = BRepAlgoAPI_Cut(plate, hole).Shape()
    corner = next(
        item["index"]
        for item in kernel.tessellate_edges(box)
        if all(abs(v) < 1e-9 for v in item["points"][0::3] + item["points"][1::3])
    )
    import math

    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon
    from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
    from OCP.gp import gp_Vec

    # 平面図で 5.5° 折れた輪郭。側面の継ぎ目が OCCT の接線の許容(0.1 rad)に入り、底辺が折れていても連なる。
    outline = BRepBuilderAPI_MakePolygon()
    for x, y in [(0, 0), (10, 0), (20, 10 * math.tan(math.radians(5.5))), (20, 10), (0, 10)]:
        outline.Add(gp_Pnt(x, y, 0))
    outline.Close()
    kinked = BRepPrimAPI_MakePrism(BRepBuilderAPI_MakeFace(outline.Wire()).Face(), gp_Vec(0, 0, 5)).Shape()
    return {
        "kinked_5_5_degrees": kinked,
        "stepped": _stepped(),
        "one_corner_rounded": kernel.fillet_edges(box, {corner: 2.0})["shape"],
        "all_edges_rounded": kernel.fillet_edges(box, every)["shape"],
        "plate_with_holes": plate,
        "cylinder": BRepPrimAPI_MakeCylinder(5, 10).Shape(),
    }


@pytest.mark.parametrize(
    "name", ["kinked_5_5_degrees", "stepped", "one_corner_rounded", "all_edges_rounded", "plate_with_holes", "cylinder"]
)
def test_the_sieve_loses_no_chain(name: str) -> None:
    """落ちたら、`Add` を呼ぶ辺を絞る篩(`_sieve`)が連なりの辺を取りこぼし、画面で連なりが割れる。"""
    shape = _shapes()[name]
    assert kernel_chains.blend_chains(shape) == _every_edge_chains(shape)


def test_an_edge_also_listed_as_internal_still_chains() -> None:
    """落ちたら、INTERNAL 向きでも現れる辺を篩が丸ごと除き、OCCT がつなぐ連なりが割れる。"""
    from OCP.BRep import BRep_Builder
    from OCP.TopAbs import TopAbs_INTERNAL, TopAbs_SHELL, TopAbs_SOLID
    from OCP.TopoDS import TopoDS, TopoDS_Solid

    from duocad.mesh_geometry import indexed_map

    stepped = _stepped()
    front = _bottom_edges_along_x(stepped, 0.0)
    solid = TopoDS.Solid_s(indexed_map(stepped, TopAbs_SOLID).FindKey(1))
    builder, rebuilt = BRep_Builder(), TopoDS_Solid()
    builder.MakeSolid(rebuilt)
    builder.Add(rebuilt, kernel._edge_map(stepped).FindKey(front[0]).Oriented(TopAbs_INTERNAL))
    builder.Add(rebuilt, indexed_map(solid, TopAbs_SHELL).FindKey(1))
    assert kernel_chains.blend_chains(rebuilt) == _every_edge_chains(rebuilt)


def test_an_internal_only_edge_can_start_a_chain() -> None:
    """落ちたら、全出現が INTERNAL の辺を始点・伸びる先の候補からも除き、OCCT がつなぐ連なりが割れる。"""
    from OCP.BRep import BRep_Builder
    from OCP.TopAbs import TopAbs_INTERNAL, TopAbs_SHELL
    from OCP.TopoDS import TopoDS_Solid

    from duocad.mesh_geometry import indexed_map

    outer = BRepPrimAPI_MakeBox(gp_Pnt(-10, -10, -10), 40, 30, 30).Shape()
    builder, solid = BRep_Builder(), TopoDS_Solid()
    builder.MakeSolid(solid)
    builder.Add(solid, indexed_map(outer, TopAbs_SHELL).FindKey(1))
    builder.Add(solid, indexed_map(_stepped(), TopAbs_SHELL).FindKey(1).Oriented(TopAbs_INTERNAL))
    assert kernel_chains.blend_chains(solid) == _every_edge_chains(solid)


@pytest.mark.parametrize(
    "name", ["kinked_5_5_degrees", "stepped", "one_corner_rounded", "all_edges_rounded", "plate_with_holes", "cylinder"]
)
def test_asking_one_edge_at_a_time_gives_the_same_chains(name: str) -> None:
    """落ちたら、画面がホバーした辺1本ずつ頼む連なり(篩を通さない `Add` 1回)が、全辺ぶんを一度に求めた連なりと食い違う。"""
    shape = _shapes()[name]
    whole = kernel_chains.blend_chains(shape)
    finder = kernel_chains.BlendChains(shape)
    for index in reversed(sorted(whole)):  # 頼む順に依らない(連なりの後ろの辺から先に頼む)
        group = finder.of([index])[index]
        assert index in group
        assert {member: whole[member] for member in group} == {member: whole[index] for member in group}
        assert sorted(member for member in whole if whole[member] == whole[index]) == list(group)


def test_out_of_range_edges_are_refused() -> None:
    """落ちたら、存在しない辺の連なりを頼むと黙って空や別の辺が返り、画面が別の辺を選ぶ。"""
    finder = kernel_chains.BlendChains(BRepPrimAPI_MakeBox(10, 10, 5).Shape())
    with pytest.raises(ValueError):
        finder.of([13])
    with pytest.raises(ValueError):
        finder.of([0])
