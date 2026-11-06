"""切り取りで1つのボディに離れた塊が2つ以上入ったら警告で知らせる。ボディは分けない(保存の形を変えない)。"""
from __future__ import annotations

import pytest

from duocad import brep_topology
from duocad.server import Session
from session_support import select


def _bar_with_groove_sketch(session: Session, groove_y: tuple[float, float]) -> tuple[str, str]:
    """40×10×10 の棒を作り、全幅の溝(x 15..25)の長方形を描いて `(body_id, groove_sketch_id)` を返す。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_rectangle(sketch_id, [0.0, 0.0], 40.0, 10.0)
    select(session, [{"sketch": sketch_id, "index": 1}])
    body_id = session.apply("extrude", {"distance": 10.0, "mode": "new"})["body_id"]
    groove = session.sketch_create("XY")["sketch_id"]
    session.add_rectangle(groove, [15.0, groove_y[0]], 10.0, groove_y[1] - groove_y[0])
    select(session, [{"sketch": groove, "index": 1}])
    return body_id, groove


def test_a_through_groove_warns_that_the_body_now_holds_two_lumps(session: Session) -> None:
    """落ちたら、棒が2つの塊に割れても何も知らされない。"""
    body_id, _ = _bar_with_groove_sketch(session, (-1.0, 11.0))
    result = session.apply("extrude", {"distance": 10.0, "mode": "cut", "body_id": body_id})
    assert "2個" in result["warning"]
    assert any("2個" in warning for warning in session.take_warnings())
    assert len(brep_topology.split_solids(session.document.get_body(body_id).shape)) == 2
    assert [body.id for body in session.document.bodies] == [body_id]


def test_a_groove_that_leaves_the_body_whole_does_not_warn(session: Session) -> None:
    body_id, _ = _bar_with_groove_sketch(session, (2.0, 8.0))
    result = session.apply("extrude", {"distance": 10.0, "mode": "cut", "body_id": body_id})
    assert "warning" not in result
    assert session.take_warnings() == []


@pytest.mark.parametrize("mode", ["new", "join"])
def test_two_separate_regions_pushed_out_together_do_not_warn_about_a_cut(session: Session, mode: str) -> None:
    """落ちたら、切り取りでない押し出し(離れた領域2つ)に「切り取りで塊が増えた」の警告が出る。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_rectangle(sketch_id, [0.0, 0.0], 5.0, 5.0)
    session.add_rectangle(sketch_id, [20.0, 0.0], 5.0, 5.0)
    select(session, [{"sketch": sketch_id, "index": 1}, {"sketch": sketch_id, "index": 2}])
    result = session.apply("extrude", {"distance": 5.0, "mode": mode})
    assert len(brep_topology.split_solids(session.document.get_body(result["body_id"]).shape)) == 2
    assert "warning" not in result
    assert session.take_warnings() == []


def test_a_face_offset_is_refused_on_such_a_body_but_not_on_the_whole_bar(session: Session) -> None:
    """警告の文が言う「面のオフセットは掛けられない」が本当であることを固定する(割れる前は掛かる)。"""
    body_id, groove = _bar_with_groove_sketch(session, (-1.0, 11.0))
    select(session, [{"body": body_id, "kind": "face", "index": 1}])
    session.preview("offset_faces", {"distance": 1.0})
    select(session, [{"sketch": groove, "index": 1}])
    session.apply("extrude", {"distance": 10.0, "mode": "cut", "body_id": body_id})
    select(session, [{"body": body_id, "kind": "face", "index": 1}])
    with pytest.raises(ValueError):
        session.apply("offset_faces", {"distance": 1.0})
