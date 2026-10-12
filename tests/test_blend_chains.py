"""`kernel.blend_chains`: 画面で1本として選ぶ辺の連なりが、フィレット・面取りが実際にかかる範囲と一致する。"""
from __future__ import annotations

import pytest
from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.gp import gp_Pnt

from duocad import kernel


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
    chains = kernel.blend_chains(shape)
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
    bottom = [item for item in described if all(abs(v) < 1e-9 for v in item["points"][2::3])]
    arc = next(item for item in bottom if item["kind"] == "circle")
    together = [item["index"] for item in bottom if item["chain"] == arc["chain"]]
    assert len(together) == 3


def test_a_plain_box_has_one_chain_per_edge() -> None:
    """落ちたら、接線で続かない辺(直角の角)までまとめて選ばれる。"""
    chains = kernel.blend_chains(BRepPrimAPI_MakeBox(10, 10, 5).Shape())
    assert sorted(chains) == list(range(1, 13))
    assert all(chains[index] == index for index in chains)
