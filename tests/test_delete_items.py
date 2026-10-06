"""verb `delete_items`(IDで指したボディとスケッチを丸ごと消す)。落ちたら、消したものが undo で元の形・位置・
名前・名前つきの値に戻らないか、混ぜて消したときに undo が1回で済まない。"""
from __future__ import annotations

import pytest

from duocad import region, sketch
from duocad.server import Session
from duocad.verbs import delete_items
from session_support import box, pad


def _sketch_ids(session: Session) -> list:
    return [item.id for item in session.document.sketches]


def _body_ids(session: Session) -> list:
    return [body.id for body in session.document.bodies]


def _sketch_with_pattern(session: Session) -> str:
    sketch_id = session.sketch_create("XY")["sketch_id"]
    entity_id = session.add_circle(sketch_id, [0.0, 0.0], 2.0)["entity_id"]
    session.pattern_rectangular(sketch_id, [entity_id], 10.0, 10.0, 2, 2)
    return sketch_id


def test_a_whole_sketch_goes_and_comes_back_in_place(session: Session) -> None:
    first = session.sketch_create("XZ")["sketch_id"]
    target = _sketch_with_pattern(session)
    last = session.sketch_create("YZ")["sketch_id"]
    before_parameters = dict(session.document.parameters)
    before = sketch.get_sketch(session.document, target)
    entities = [(entity.id, entity.kind, dict(entity.params)) for entity in before.entities]
    patterns = [(pattern.id, pattern.kind) for pattern in before.patterns]

    session.delete_items(sketches=[target])
    assert _sketch_ids(session) == [first, last]
    # 名前つきの値は手放す(残すと `d1 d2 …` の連番が飛ぶ)
    assert session.document.parameters == {}

    session.undo()
    assert _sketch_ids(session) == [first, target, last]
    restored = sketch.get_sketch(session.document, target)
    assert restored.name == "スケッチ2"
    assert [(entity.id, entity.kind, dict(entity.params)) for entity in restored.entities] == entities
    assert [(pattern.id, pattern.kind) for pattern in restored.patterns] == patterns
    assert session.document.parameters == before_parameters
    assert len(region.regions(session.document, target)) == 4

    session.redo()
    assert _sketch_ids(session) == [first, last]


def test_bodies_and_sketches_mixed_are_one_undo(session: Session) -> None:
    body, sketch_id = box(session)
    other = pad(session, 10.0, 10.0, 5.0, corner=(40.0, 0.0))
    log_length = len(session.document.log.entries)

    session.delete_items(bodies=[body.id], sketches=[sketch_id])
    assert len(session.document.log.entries) == log_length + 1
    assert _body_ids(session) == [other.id]
    assert sketch_id not in _sketch_ids(session)

    session.undo()
    assert _body_ids(session) == [body.id, other.id]
    assert sketch_id in _sketch_ids(session)


def test_deleting_a_sketch_keeps_the_body_extruded_from_it(session: Session) -> None:
    body, sketch_id = box(session)
    session.delete_items(sketches=[sketch_id])
    assert _body_ids(session) == [body.id]


def test_save_reopen_then_undo(session: Session) -> None:
    body, sketch_id = box(session)
    session.delete_items(bodies=[body.id], sketches=[sketch_id])
    session.save_document("deleted-items.json")

    session.open_document("deleted-items.json")
    assert _body_ids(session) == [] and _sketch_ids(session) == []
    session.undo()
    assert _body_ids(session) == [body.id] and _sketch_ids(session) == [sketch_id]
    assert session.document.sketches[0].name == "スケッチ1"


@pytest.mark.parametrize(
    "bodies, sketches, error",
    [
        ([], [], ValueError),
        ([], ["sketch-missing"], KeyError),
        (["body-missing"], [], KeyError),
    ],
)
def test_bad_targets_change_nothing(session: Session, bodies, sketches, error) -> None:
    box(session)
    log_length = len(session.document.log.entries)
    with pytest.raises(error):
        delete_items(session.document, bodies, sketches)
    assert len(session.document.log.entries) == log_length


def test_duplicate_ids_are_refused(session: Session) -> None:
    sketch_id = session.sketch_create("XY")["sketch_id"]
    with pytest.raises(ValueError, match="duplicate"):
        delete_items(session.document, [], [sketch_id, sketch_id])
    assert _sketch_ids(session) == [sketch_id]
