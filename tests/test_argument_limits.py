"""個数・距離の上限と、座標の読み方の契約。
落ちたとき何を意味するか: 桁違いの個数・距離が入口を通ってイベントループを数十秒〜無限に止める、または OCCT が
プロセスごと落ちる。座標の配列の長さ違い・数でない要素が、黙って受理されるか internal の封筒になる。
"""
from __future__ import annotations

import json

import pytest

from duocad.document import Document
from duocad.oplog import OpLog
from duocad.sketch import add_arc, add_circle, add_ellipse, add_line, add_rectangle, get_sketch, point_2d, sketch_create
from duocad.region import region_ref, regions
from duocad.server import Session
from duocad.verbs import envelope_regions, pattern_circular, pattern_rectangular
from duocad.verbs.envelope_regions import MAX_DISTANCE
from duocad.verbs.pattern import MAX_PATTERN_COPIES
from document_support import replay_through_json


def _sketch_with_circle() -> tuple[Document, str, str]:
    doc = Document()
    sketch_id = sketch_create(doc, "XY")
    entity_id = add_circle(doc, sketch_id, (50.0, 0.0), 4.0)
    return doc, sketch_id, entity_id


def test_a_rectangular_pattern_may_add_up_to_the_limit_and_not_one_more():
    doc, sketch_id, circle = _sketch_with_circle()
    pattern_rectangular(doc, sketch_id, [circle], 1.0, 1.0, MAX_PATTERN_COPIES + 1, 1)  # 複製 5000 個ちょうど
    with pytest.raises(ValueError, match="多すぎる"):
        pattern_rectangular(doc, sketch_id, [circle], 1.0, 1.0, MAX_PATTERN_COPIES + 2, 1)


def test_the_limit_counts_copies_of_every_source_not_just_the_grid():
    doc = Document()
    sketch_id = sketch_create(doc, "XY")
    ids = [add_circle(doc, sketch_id, (10.0 * k, 0.0), 2.0) for k in range(3)]
    with pytest.raises(ValueError, match="多すぎる"):
        pattern_rectangular(doc, sketch_id, ids, 1.0, 1.0, 50, 50)  # 3 × 2499 = 7497 > 5000
    assert not get_sketch(doc, sketch_id).patterns


def test_a_circular_pattern_is_limited_the_same_way():
    doc, sketch_id, circle = _sketch_with_circle()
    with pytest.raises(ValueError, match="多すぎる"):
        pattern_circular(doc, sketch_id, [circle], (0.0, 0.0), MAX_PATTERN_COPIES + 2)
    pattern_circular(doc, sketch_id, [circle], (0.0, 0.0), MAX_PATTERN_COPIES + 1)


def test_a_refused_pattern_leaves_the_document_and_the_log_unchanged():
    doc, sketch_id, circle = _sketch_with_circle()
    before = len(doc.log.entries)
    with pytest.raises(ValueError):
        pattern_rectangular(doc, sketch_id, [circle], 1.0, 1.0, 10_000, 10_000)
    assert len(doc.log.entries) == before and not get_sketch(doc, sketch_id).patterns


def test_a_saved_pattern_above_the_limit_still_replays():
    """上限は新しく作る入口だけに掛け、保存済みの文書の再生は止めない(上限の前に作られた文書を開けなくしない)。"""
    doc, sketch_id, circle = _sketch_with_circle()
    pattern_rectangular(doc, sketch_id, [circle], 1.0, 1.0, 2, 1)
    raw = json.loads(doc.log.to_json())
    entry = next(e for e in raw["operations"] if e["verb"] == "pattern_rectangular")
    entry["args"]["nx"] = MAX_PATTERN_COPIES + 1000
    restored = Document()
    restored.log = OpLog.from_json(json.dumps(raw))
    restored.log.replay(restored)
    assert len(get_sketch(restored, sketch_id).patterns) == 1


def _rectangle_region(doc: Document) -> tuple[str, list]:
    sketch_id = sketch_create(doc, "XY")
    add_rectangle(doc, sketch_id, (0.0, 0.0), 10.0, 10.0)
    return sketch_id, [region_ref(sketch_id, regions(doc, sketch_id)[0].index)]


@pytest.mark.parametrize(
    "radius, offset",
    [(MAX_DISTANCE * 2, 0.0), (0.0, MAX_DISTANCE * 2), (0.0, -MAX_DISTANCE * 2), (2.0**49, 0.0), (1.0e12, 1.0e12)],
)
def test_an_envelope_distance_beyond_the_limit_is_refused_before_occt_is_called(radius: float, offset: float):
    """`radius` が 2 の冪の巨大値(2^49 など)だと、以前は OCCT が access violation でプロセスごと落ちた。"""
    doc = Document()
    _, refs = _rectangle_region(doc)
    before = len(doc.log.entries)
    with pytest.raises(ValueError, match="以下"):
        envelope_regions(doc, refs, radius, offset)
    assert len(doc.log.entries) == before


def test_an_envelope_just_inside_the_limit_is_still_computed():
    doc = Document()
    _, refs = _rectangle_region(doc)
    envelope_regions(doc, refs, 0.0, 5.0)


@pytest.mark.parametrize("value", [[1.0, 2.0], (1, 2)])
def test_point_2d_reads_two_numbers(value):
    assert point_2d(value, "p") == (float(value[0]), float(value[1]))


@pytest.mark.parametrize(
    "value",
    [[], [1.0], [1.0, 2.0, 3.0], ["1", "2"], [True, 2.0], [None, 1.0], "12", None, 5, [float("nan"), 0.0], [0.0, float("inf")], {"u": 1, "v": 2}],
)
def test_point_2d_refuses_anything_that_is_not_two_finite_numbers(value):
    with pytest.raises(ValueError, match="two"):
        point_2d(value, "p")


def test_every_sketch_entity_reader_refuses_a_point_of_the_wrong_shape_and_adds_nothing():
    doc = Document()
    sketch_id = sketch_create(doc, "XY")
    before = len(doc.log.entries)
    bad = [1.0, 2.0, 3.0]  # 3次元の座標の z を黙って捨てない
    for call in (
        lambda: add_line(doc, sketch_id, bad, (0.0, 0.0)),
        lambda: add_line(doc, sketch_id, (0.0, 0.0), [1.0]),
        lambda: add_circle(doc, sketch_id, bad, 5.0),
        lambda: add_ellipse(doc, sketch_id, bad, 5.0, 3.0),
        lambda: add_rectangle(doc, sketch_id, bad, 5.0, 3.0),
        lambda: add_arc(doc, sketch_id, bad, 5.0, 0.0, 90.0),
    ):
        with pytest.raises(ValueError, match="two"):
            call()
    assert len(doc.log.entries) == before and not get_sketch(doc, sketch_id).entities


def test_a_wrong_pivot_for_a_circular_pattern_is_refused():
    doc, sketch_id, circle = _sketch_with_circle()
    with pytest.raises(ValueError, match="two"):
        pattern_circular(doc, sketch_id, [circle], [0.0], 4)


def test_a_point_of_the_wrong_shape_over_the_session_is_a_value_error_not_an_internal_error():
    session = Session(0.2)
    sketch_id = session.sketch_create("XY")["sketch_id"]
    with pytest.raises(ValueError, match="two"):
        session.add_circle(sketch_id, [1.0], 5.0)
    assert not session.document.sketches[0].entities


def test_the_replayed_document_matches_after_a_refused_call():
    doc, sketch_id, circle = _sketch_with_circle()
    with pytest.raises(ValueError):
        pattern_rectangular(doc, sketch_id, [circle], 1.0, 1.0, 9999, 9999)
    pattern_rectangular(doc, sketch_id, [circle], 5.0, 5.0, 3, 2)
    restored = replay_through_json(doc)
    assert [p.kind for p in get_sketch(restored, sketch_id).patterns] == ["rectangular"]
