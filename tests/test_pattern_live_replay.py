"""反復を「元を参照して描くたびに複製を作る」形で持っていた頃の保存ログが、今も同じ形に再生される。"""
from __future__ import annotations

import math
from pathlib import Path

import pytest

from duocad import query
from duocad.document import Document
from duocad.oplog import OpLog
from duocad.parameters import parameter_of, set_parameter
from duocad.region import expanded_entities, regions
from duocad.sketch import add_circle, sketch_create
from document_support import install_live_pattern

from duocad import (
    brep_topology as _brep_topology,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "pattern_live_log.json"

#: 保存時の形: 80×60×10 の板から、r=2 の穴 3×2(set_parameter で横の間隔を 12 に変えた)と
#: 8×4 の長穴を中心 (50, 30) まわりに4つ抜いた
SAVED_VOLUME = 80 * 60 * 10 - 6 * math.pi * 2.0**2 * 10 - 4 * 8 * 4 * 10
SAVED_FACES = 28


def _replayed() -> Document:
    log = OpLog.from_json(FIXTURE.read_text(encoding="utf-8"))
    document = Document()
    document.log = log
    log.replay(document)
    return document


def test_a_saved_log_with_live_patterns_replays_to_the_saved_shape():
    document = _replayed()

    (body,) = document.bodies
    assert math.isclose(query.volume(body.shape), SAVED_VOLUME, rel_tol=1e-9)
    assert _brep_topology.face_count(body.shape) == SAVED_FACES


def test_replayed_live_patterns_still_draw_their_copies_without_entity_ids():
    document = _replayed()
    _, holes, ring = document.sketches

    assert [len(sketch.entities) for sketch in (holes, ring)] == [1, 1]
    assert [len(sketch.patterns) for sketch in (holes, ring)] == [1, 1]
    assert len(expanded_entities(document, holes.id)) == 6
    assert len(regions(document, holes.id)) == 6
    assert len(regions(document, ring.id)) == 4
    xs = sorted({round(item["params"]["cx"], 9) for item in expanded_entities(document, holes.id)})
    assert xs == [10.0, 22.0, 34.0]


def test_undo_and_redo_of_a_replayed_live_pattern_remove_and_restore_the_copies():
    document = _replayed()
    _, _, ring = document.sketches

    document.log.undo(document)  # 長穴の切り取り
    document.log.undo(document)  # 円形反復
    assert ring.patterns == []
    assert len(regions(document, ring.id)) == 1

    document.log.redo(document)
    assert len(regions(document, ring.id)) == 4


def _live_ring(document: Document, count: int = 6) -> tuple:
    sketch_id = sketch_create(document, "XY")
    circle = add_circle(document, sketch_id, (48.0, 30.0), 6.0)
    pattern_id = install_live_pattern(
        document, "pattern_circular", sketch_id, [circle],
        center=[30.0, 30.0], count=count, total_angle=360.0, rotate_copies=True,
    )
    return sketch_id, pattern_id


def test_the_count_of_a_live_circular_pattern_is_still_a_named_value():
    """旧形式の円形反復の個数・全角度は名前つきの値のまま: 書き換えれば複製の数が追随し、使えない値は断る。"""
    document = Document()
    sketch_id, pattern_id = _live_ring(document)

    set_parameter(document, parameter_of(document, sketch_id, pattern_id, "count"), 3.0)
    assert len(regions(document, sketch_id)) == 3

    with pytest.raises(ValueError):
        set_parameter(document, parameter_of(document, sketch_id, pattern_id, "count"), 0.0)
    for bad in (0.0, -90.0, 361.0):
        with pytest.raises(ValueError):
            set_parameter(document, parameter_of(document, sketch_id, pattern_id, "total_angle"), bad)
    assert len(regions(document, sketch_id)) == 3
