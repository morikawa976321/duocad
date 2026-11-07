"""同じ対象 ID を2回渡す verb を断る。落ちたら、重なる複製が黙って作られる。"""
from __future__ import annotations

import pytest

from duocad.server import Session
from duocad.verbs import mirror_body, move_body, pattern_rectangular
from session_support import box


def test_move_and_mirror_refuse_a_body_listed_twice(session: Session) -> None:
    body, _ = box(session)
    log_length = len(session.document.log.entries)

    with pytest.raises(ValueError, match="duplicate"):
        move_body(session.document, [body.id, body.id], dx=5.0, dy=0.0, dz=0.0, copy=True)
    with pytest.raises(ValueError, match="duplicate"):
        mirror_body(session.document, [body.id, body.id], base_plane="YZ")

    assert len(session.document.bodies) == 1
    assert len(session.document.log.entries) == log_length


def test_pattern_refuses_an_entity_listed_twice(session: Session) -> None:
    sketch_id = session.sketch_create("XY")["sketch_id"]
    circle = session.add_circle(sketch_id, [0.0, 0.0], 4.0)["entity_id"]

    with pytest.raises(ValueError, match="duplicate"):
        pattern_rectangular(session.document, sketch_id, [circle, circle], 10.0, 0.0, 2, 1)

    assert len(session.document.sketches[0].entities) == 1
