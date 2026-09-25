"""verb `delete_sketch` の契約。固定するのは5つ:

1. **入れ物ごと消える** —— 図形・反復・その名前つきの値が文書から無くなり、ログは1行、
   そのスケッチの図形/領域を指していた選択は空になる。押し出し済みのボディは残る。
2. **undo 1回で全部戻る** —— 図形・反復・**同じパラメータ名**・平面の姿勢・並びの位置。
   図形と反復の登録順が混ざっていても名前が変わらない(`delete` の「作った順に登録し直す」
   では足りない場合)。
3. **保存 -> 開く(再生) -> undo** が通る(args だけで完結する)。
4. **`set_parameter` の再生(`rebuild`)を跨いでも壊れない**(消したスケッチはログの再生で
   消え直す)。
5. 存在しない `sketch_id` は KeyError で**文書は何も変わらない**。
"""
from __future__ import annotations

import json

import pytest

from duocad import region, selection, sketch
from duocad.oplog import OpLog
from duocad.server import Session
from session_support import select as _select

DEVIATION = 0.2


def _sketch_with_pattern(session: Session) -> tuple:
    """図形 -> 反復 -> 図形 の順で作る(名前つきの値の登録順が図形と反復で混ざる)。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]
    circle = session.add_circle(sketch_id, [10.0, 10.0], 2.0)["entity_id"]
    pattern = session.pattern_rectangular(sketch_id, [circle], 15.0, 0.0, 3, 1)["pattern_id"]
    line = session.add_line(sketch_id, [0.0, 0.0], [50.0, 0.0])["entity_id"]
    return sketch_id, circle, pattern, line


def _fingerprint(session: Session, sketch_id: str) -> dict:
    """戻ったかどうかを比べるための、スケッチの中身の写し(名前つきの値は名前と値の両方)。"""
    item = sketch.get_sketch(session.document, sketch_id)
    return {
        "index": [s.id for s in session.document.sketches].index(sketch_id),
        "plane": json.dumps(item.plane, sort_keys=True, default=str),
        "entities": [(e.id, e.kind, dict(e.params)) for e in item.entities],
        "patterns": [
            (p.id, p.kind, list(p.sources), dict(p.params), dict(p.options)) for p in item.patterns
        ],
        "parameters": dict(session.document.parameters),
        "regions": len(region.regions(session.document, sketch_id)),
    }


# --------------------------------------------------------------------------------------
# 1. 入れ物ごと消える
# --------------------------------------------------------------------------------------
def test_deleting_a_sketch_removes_it_with_its_entities_patterns_and_parameter_names(session):
    """落ちたら: 消したはずの図形・反復・パラメータ名が文書に残っている(連番が飛ぶ)。"""
    other = session.sketch_create("XZ")["sketch_id"]
    session.add_rectangle(other, [0.0, 0.0], 5.0, 5.0)
    sketch_id, circle, _, _ = _sketch_with_pattern(session)
    other_params = {
        name for entity in sketch.get_sketch(session.document, other).entities
        for name in entity.params.values()
    }
    _select(session, [{"sketch": sketch_id, "entity": circle}])
    entries_before = len(session.document.log.entries)

    applied = session.delete_sketch(sketch_id)

    assert applied["verb"] == "delete_sketch" and applied["sketch_id"] == sketch_id
    assert [s.id for s in session.document.sketches] == [other]
    assert set(session.document.parameters) == other_params, "消したスケッチの名前が残った"
    assert len(session.document.log.entries) == entries_before + 1
    assert selection.get_selection(session.document).items == [], "消した図形を指す選択が残った"


def test_deleting_a_sketch_leaves_extruded_bodies_alone(session):
    """落ちたら: スケッチを消すとそこから押し出したボディまで消える(形は独立した実体)。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_rectangle(sketch_id, [0.0, 0.0], 20.0, 20.0)
    _select(session, [{"sketch": sketch_id, "index": 1}])
    session.extrude(20.0, mode="join")
    body_ids = [body.id for body in session.document.bodies]

    session.delete_sketch(sketch_id)

    assert [body.id for body in session.document.bodies] == body_ids
    assert session.state_meta()["bodies"][0]["volume"] == pytest.approx(8000.0)


# --------------------------------------------------------------------------------------
# 2. undo 1回で全部戻る
# --------------------------------------------------------------------------------------
def test_undo_restores_the_sketch_with_the_same_parameter_names(session):
    """落ちたら: undo で戻ったスケッチの名前つきの値が別名になっている(以後の
    `set_parameter` が別の値を指す)か、平面・並び・反復が戻っていない。"""
    session.sketch_create("XZ")  # 先頭に別のスケッチを置く(並びの位置が 1 になる)
    sketch_id, *_ = _sketch_with_pattern(session)
    before = _fingerprint(session, sketch_id)
    assert before["regions"] == 3

    session.delete_sketch(sketch_id)
    session.undo()

    assert _fingerprint(session, sketch_id) == before
    assert session.document.log.can_redo
    session.redo()
    assert all(s.id != sketch_id for s in session.document.sketches)


def test_undo_after_a_new_verb_in_between_still_restores_the_names(session):
    """落ちたら: 間に別の図形を足して名前を消費しても、undo を2回戻せば元の名前で戻る
    (undo は積んだ順に戻るので、控えた名前は必ず空いている)。"""
    sketch_id, *_ = _sketch_with_pattern(session)
    other = session.sketch_create("YZ")["sketch_id"]
    before = _fingerprint(session, sketch_id)

    session.delete_sketch(sketch_id)
    session.add_circle(other, [0.0, 0.0], 1.0)  # 空いた d1.. を取る
    session.undo()  # 円
    session.undo()  # delete_sketch

    assert _fingerprint(session, sketch_id) == before


# --------------------------------------------------------------------------------------
# 3. 保存 -> 開く(再生) -> undo
# --------------------------------------------------------------------------------------
def test_delete_sketch_survives_save_open_and_undo(session):
    """落ちたら: 再生した文書で削除が効いていない、または再生後の undo で戻せない。"""
    sketch_id, *_ = _sketch_with_pattern(session)
    before = _fingerprint(session, sketch_id)
    session.delete_sketch(sketch_id)
    payload = session.document.log.to_json()

    reopened = Session(None, DEVIATION)
    log = OpLog.from_json(payload)
    document = reopened.document
    document.log = log
    log.replay(document)
    assert all(s.id != sketch_id for s in document.sketches)
    assert document.parameters == {}

    reopened.undo()
    assert _fingerprint(reopened, sketch_id) == before


# --------------------------------------------------------------------------------------
# 4. set_parameter の再生を跨ぐ
# --------------------------------------------------------------------------------------
def test_delete_sketch_replays_inside_a_parameter_rebuild(session):
    """落ちたら: 残ったスケッチの値を打ち直すと、消したはずのスケッチが再生で復活する。"""
    kept = session.sketch_create("XY")["sketch_id"]
    kept_entity = session.add_rectangle(kept, [0.0, 0.0], 10.0, 10.0)["entity_id"]
    doomed, *_ = _sketch_with_pattern(session)
    session.delete_sketch(doomed)
    width_name = sketch.get_sketch(session.document, kept).entities[0].params["width"]

    session.set_parameter(width_name, 12.0)

    assert [s.id for s in session.document.sketches] == [kept]
    assert sketch.entity_params(session.document, kept, kept_entity)["width"] == 12.0


# --------------------------------------------------------------------------------------
# 5. 存在しない sketch_id
# --------------------------------------------------------------------------------------
def test_an_unknown_sketch_id_changes_nothing(session):
    sketch_id, *_ = _sketch_with_pattern(session)
    before = _fingerprint(session, sketch_id)
    entries = len(session.document.log.entries)

    with pytest.raises(KeyError):
        session.delete_sketch("sketch-nope")

    assert _fingerprint(session, sketch_id) == before
    assert len(session.document.log.entries) == entries
