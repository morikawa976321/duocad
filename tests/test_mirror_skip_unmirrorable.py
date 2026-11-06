"""鏡映は、軸の上に乗って鏡映できない図形だけを外して続け、外した数を警告で知らせる。
落ちたとき何を意味するか: 軸の上の図形が1つ混ざるだけで鏡映全体が失敗する、または外れた図形が黙って消える。
"""
from __future__ import annotations

import pytest

from duocad import datum, sketch
from document_support import replay_through_json
from session_support import select as _select


def _mixed(session):
    sketch_id = session.sketch_create("XY")["sketch_id"]
    axis_id = session.add_line(sketch_id, [0.0, 0.0], [10.0, 0.0])["entity_id"]
    on_axis_id = session.add_line(sketch_id, [1.0, 0.0], [5.0, 0.0])["entity_id"]
    off_axis_id = session.add_line(sketch_id, [1.0, 2.0], [3.0, 4.0])["entity_id"]
    return sketch_id, axis_id, on_axis_id, off_axis_id


def _pick(session, sketch_id, *entity_ids):
    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": None} for entity_id in entity_ids])


def test_a_target_on_the_axis_is_left_out_and_the_rest_is_mirrored(session) -> None:
    sketch_id, axis_id, on_axis_id, off_axis_id = _mixed(session)
    _pick(session, sketch_id, on_axis_id, off_axis_id)

    applied = session.mirror_sketch_entities({"sketch": sketch_id, "entity": axis_id})

    assert len(applied["created"]) == 1
    assert len(sketch.get_sketch(session.document, sketch_id).entities) == 4
    assert session.take_warnings() == ["鏡映軸の上に乗っている図形 1 個は、鏡映できないので外した"]
    args = session.document.log.entries[-1].args
    assert args["targets"] == [{"sketch": sketch_id, "entity": off_axis_id}]


def test_nothing_is_reported_when_nothing_was_left_out(session) -> None:
    sketch_id, axis_id, _, off_axis_id = _mixed(session)
    _pick(session, sketch_id, off_axis_id)
    session.mirror_sketch_entities({"sketch": sketch_id, "entity": axis_id})
    assert session.take_warnings() == []


def test_when_every_target_is_on_the_axis_the_mirror_is_still_refused(session) -> None:
    sketch_id, axis_id, on_axis_id, _ = _mixed(session)
    _pick(session, sketch_id, on_axis_id)
    before = len(session.document.log.entries)
    with pytest.raises(ValueError, match="重なる"):
        session.mirror_sketch_entities({"sketch": sketch_id, "entity": axis_id})
    assert len(session.document.log.entries) == before
    assert session.take_warnings() == []


def test_a_target_on_an_origin_axis_is_left_out_too(session) -> None:
    """原点の軸を鏡映軸にしたとき、軸自身を外す処理は効かないので、軸の上の図形で止まっていた。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]
    on_axis_id = session.add_line(sketch_id, [1.0, 0.0], [5.0, 0.0])["entity_id"]
    off_axis_id = session.add_line(sketch_id, [1.0, 2.0], [3.0, 4.0])["entity_id"]
    _pick(session, sketch_id, on_axis_id, off_axis_id)
    applied = session.mirror_sketch_entities(datum.make_ref("X"))
    assert len(applied["created"]) == 1


def test_the_saved_document_replays_with_only_the_mirrored_targets(session) -> None:
    sketch_id, axis_id, on_axis_id, off_axis_id = _mixed(session)
    _pick(session, sketch_id, on_axis_id, off_axis_id)
    session.mirror_sketch_entities({"sketch": sketch_id, "entity": axis_id})
    restored = replay_through_json(session.document)
    assert len(sketch.get_sketch(restored, sketch_id).entities) == 4
