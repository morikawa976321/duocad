"""`query.describe_edges` の `start` / `end` が、辺の向き(`TopAbs_REVERSED`)を込みの値であることの回帰テスト。"""

import pytest
from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.TopAbs import TopAbs_Orientation

from duocad import brep_topology, query


def _oriented_ends(edge):
    """曲線の両端を、辺の向きに合わせて(REVERSED なら入れ替えて)返す。`TopExp` を通らない独立の基準。"""
    curve = BRepAdaptor_Curve(edge)
    first, last = curve.Value(curve.FirstParameter()), curve.Value(curve.LastParameter())
    if edge.Orientation() == TopAbs_Orientation.TopAbs_REVERSED:
        first, last = last, first
    return [first.X(), first.Y(), first.Z()], [last.X(), last.Y(), last.Z()]


def test_describe_edges_start_and_end_follow_the_edge_orientation() -> None:
    """落ちたら: `TopExp.FirstVertex_s` の既定(辺の向きを見ない)に戻り、REVERSED の辺で始点と終点が逆に出る
    (箱の12辺中6本。`measure` が返す `start` / `end` もこれと同じ値)。"""
    shape = BRepPrimAPI_MakeBox(20.0, 30.0, 40.0).Shape()
    reversed_edges = 0
    for item in query.describe_edges(shape):
        edge = brep_topology.edge_of(shape, item["index"])
        start, end = _oriented_ends(edge)
        assert item["start"] == pytest.approx(start, abs=1e-9)
        assert item["end"] == pytest.approx(end, abs=1e-9)
        reversed_edges += edge.Orientation() == TopAbs_Orientation.TopAbs_REVERSED
    assert reversed_edges > 0, "REVERSED の辺が1本も無い題材では、この検査は向きを見ていない"
