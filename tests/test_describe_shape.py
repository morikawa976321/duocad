"""`query.describe_shape` の数え方が、辺の選択で指せる添字の体系(`kernel.edge_count` / `query.describe_edges`)と同じであること。"""
from __future__ import annotations

import pytest
from OCP.BRepPrimAPI import BRepPrimAPI_MakeCone, BRepPrimAPI_MakeSphere

from duocad import kernel, query
from duocad.server import Session
from session_support import box


def _agrees_with_the_selectable_edges(shape) -> None:
    described = query.describe_shape(shape)
    assert described["edge_count"] == kernel.edge_count(shape) == len(query.describe_edges(shape))
    assert described["face_count"] == kernel.face_count(shape) == sum(described["faces_by_type"].values())


@pytest.mark.parametrize(
    "make",
    [lambda: BRepPrimAPI_MakeSphere(4.0), lambda: BRepPrimAPI_MakeCone(5.0, 0.0, 10.0)],
    ids=["sphere-poles", "cone-apex"],
)
def test_edge_count_includes_the_degenerate_edges_the_selection_can_point_at(make) -> None:
    """落ちたら、球の極・円錐の頂点の縮退辺を数えず、`edge_count` が辺の選択で指せる添字の上限より小さい(球で 1 と 3)。"""
    _agrees_with_the_selectable_edges(make().Shape())


def test_edge_count_of_a_filleted_cube_matches_the_selectable_edges(session: Session) -> None:
    """落ちたら、フィレットの角の縮退辺を数えず、`length_gt: 0` で全辺を選んだ件数と `edge_count` が合わない(56 と 48)。"""
    body, _sketch_id = box(session)
    session.set_selection(predicate={"kind": "edge", "where": []})
    session.fillet(3.0)

    _agrees_with_the_selectable_edges(session.document.get_body(body.id).shape)
