"""反復は隠したスケッチへ足せず、redo は表示状態に依存しない。"""
from __future__ import annotations

import pytest

from duocad.sketch import get_sketch


def _prepared(session):
    sketch_id = session.sketch_create("XY")["sketch_id"]
    entity_id = session.add_circle(sketch_id, [0.0, 0.0], 4.0)["entity_id"]
    return sketch_id, entity_id


@pytest.mark.parametrize("verb", ["pattern_rectangular", "pattern_circular"])
def test_a_pattern_into_a_hidden_sketch_is_refused(session, verb) -> None:
    """落ちたら、隠したスケッチへ反復の複製が足される。"""
    sketch_id, entity_id = _prepared(session)
    session.set_sketch_visible(sketch_id, False)
    before = len(get_sketch(session.document, sketch_id).entities)
    log_before = len(session.document.log.entries)
    with pytest.raises(ValueError, match="hidden"):
        if verb == "pattern_rectangular":
            session.pattern_rectangular(sketch_id, [entity_id], 10.0, 0.0, 3, 1)
        else:
            session.pattern_circular(sketch_id, [entity_id], [20.0, 0.0], 3)
    assert len(get_sketch(session.document, sketch_id).entities) == before
    assert len(session.document.log.entries) == log_before


def test_redo_of_a_pattern_survives_hiding_the_sketch(session) -> None:
    """落ちたら、undo の後にスケッチを隠しただけで反復の redo が通らなくなる。"""
    sketch_id, entity_id = _prepared(session)
    session.pattern_rectangular(sketch_id, [entity_id], 10.0, 0.0, 3, 1)
    session.undo()
    session.set_sketch_visible(sketch_id, False)
    session.redo()
    assert len(get_sketch(session.document, sketch_id).entities) == 3
