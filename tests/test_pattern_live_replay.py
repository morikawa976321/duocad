"""反復を「元を参照して描くたびに複製を作る」形で持っていた頃の保存ログが、今も同じ形に再生される。"""
from __future__ import annotations

import math
from pathlib import Path

from duocad import kernel, query
from duocad.document import Document
from duocad.oplog import OpLog
from duocad.region import expanded_entities, regions

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
    assert kernel.face_count(body.shape) == SAVED_FACES


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
