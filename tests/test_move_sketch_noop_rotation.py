"""何も変えない回転(中心を省いた円・点の回転)は、新しい操作としては断り、ログに残さない。"""
from __future__ import annotations

import pytest

from duocad import sketch
from duocad.document import Document
from duocad.verbs import move_sketch_entities
from document_support import replay_through_json


def _setup():
    document = Document()
    sketch_id = sketch.sketch_create(document, "XY")
    circle = sketch.add_circle(document, sketch_id, (5.0, 5.0), 4.0)
    point = sketch.add_point(document, sketch_id, (2.0, 3.0))
    line = sketch.add_line(document, sketch_id, (0.0, 0.0), (10.0, 0.0))
    return document, sketch_id, circle, point, line


@pytest.mark.parametrize("which", ["circle", "point"])
def test_rotating_a_lone_circle_or_point_about_its_own_center_is_refused(which) -> None:
    """落ちたら、値が変わらない回転がログの行として増える。"""
    document, sketch_id, circle, point, _line = _setup()
    entity = circle if which == "circle" else point
    before = len(document.log.entries)
    with pytest.raises(ValueError, match="回転中心を指定する"):
        move_sketch_entities(
            document, [{"sketch": sketch_id, "entity": entity}], mode="rotate", angle=30.0
        )
    assert len(document.log.entries) == before


def test_rotating_about_an_explicit_center_that_equals_the_circle_center_is_refused() -> None:
    """落ちたら、中心を明示しても何も変わらない回転が通る。"""
    document, sketch_id, circle, _point, _line = _setup()
    with pytest.raises(ValueError, match="何も変わらない"):
        move_sketch_entities(
            document, [{"sketch": sketch_id, "entity": circle}], mode="rotate", angle=30.0, center=[5.0, 5.0]
        )


def test_rotating_things_that_move_still_works_and_replays() -> None:
    """落ちたら、本当に動く回転まで断られる、または再生で結果が変わる。"""
    document, sketch_id, circle, point, line = _setup()
    move_sketch_entities(
        document, [{"sketch": sketch_id, "entity": circle}, {"sketch": sketch_id, "entity": point}], mode="rotate", angle=90.0
    )
    move_sketch_entities(document, [{"sketch": sketch_id, "entity": line}], mode="rotate", angle=30.0)
    replayed = replay_through_json(document)
    assert [dict(e.params) for e in replayed.sketches[0].entities] == [dict(e.params) for e in document.sketches[0].entities]
