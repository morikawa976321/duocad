"""ボディとスケッチの通し番号(`duocad/naming.py`)。落ちたら、名前が undo/redo・保存・再生・プレビューの
どこかで振り直されるか、一度振った番号がもう一度振られている。"""
from __future__ import annotations

import json

from duocad.server import Session
from session_support import pad, select


def _body_names(session: Session) -> list:
    return [body.name for body in session.document.bodies]


def _sketch_names(session: Session) -> list:
    return [item.name for item in session.document.sketches]


def test_bodies_and_sketches_count_independently(session: Session) -> None:
    pad(session, 10.0, 10.0, 5.0)
    pad(session, 10.0, 10.0, 5.0, corner=(20.0, 0.0))
    session.sketch_create("XZ")

    assert _body_names(session) == ["ボディ1", "ボディ2"]
    assert _sketch_names(session) == ["スケッチ1", "スケッチ2", "スケッチ3"]


def test_undo_redo_keeps_the_name_and_an_undone_number_is_not_reused(session: Session) -> None:
    pad(session, 10.0, 10.0, 5.0)
    session.undo()  # 押し出しを取り消す(ボディ1が消える)
    session.redo()
    assert _body_names(session) == ["ボディ1"]

    session.undo()
    pad(session, 10.0, 10.0, 5.0)
    assert _body_names(session) == ["ボディ2"]


def test_the_high_water_mark_survives_save_and_reopen(session: Session) -> None:
    """取り消して捨てた操作の番号も、開き直した文書が振り直さない。"""
    pad(session, 10.0, 10.0, 5.0)
    pad(session, 10.0, 10.0, 5.0, corner=(20.0, 0.0))
    session.undo()  # ボディ2の押し出し
    session.undo()  # スケッチ2の長方形
    session.undo()  # スケッチ2
    session.save_document("numbered.json")

    session.open_document("numbered.json")
    assert _body_names(session) == ["ボディ1"]
    assert _sketch_names(session) == ["スケッチ1"]
    pad(session, 10.0, 10.0, 5.0, corner=(40.0, 0.0))
    assert _body_names(session) == ["ボディ1", "ボディ3"]
    assert _sketch_names(session) == ["スケッチ1", "スケッチ3"]


def test_copies_get_their_own_numbers_and_keep_them_on_replay(session: Session) -> None:
    body = pad(session, 10.0, 10.0, 5.0)
    select(session, [{"body": body.id, "kind": "body", "index": None}])
    session.move_body(0.0, 0.0, 30.0, copy=True)
    assert _body_names(session) == ["ボディ1", "ボディ2"]

    session.save_document("copied.json")
    session.open_document("copied.json")
    assert _body_names(session) == ["ボディ1", "ボディ2"]


def test_a_preview_does_not_consume_a_number(session: Session) -> None:
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_rectangle(sketch_id, [0.0, 0.0], 10.0, 10.0)
    select(session, [{"sketch": sketch_id, "index": 1}])
    session.preview("extrude", {"distance": 5.0, "mode": "new"})
    session.extrude(5.0, mode="new")
    assert _body_names(session) == ["ボディ1"]


def test_a_log_without_numbers_replays_with_the_old_names(session: Session, tmp_path) -> None:
    """この仕組みより前に保存された文書は、開いただけで名前が変わらない。"""
    pad(session, 10.0, 10.0, 5.0)
    session.save_document("legacy.json")
    path = session.models_dir / "legacy.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop("numbering", None)
    for operation in payload["operations"]:
        operation["args"].pop("number", None)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    session.open_document("legacy.json")
    assert _body_names(session) == ["solid"]
    assert _sketch_names(session) == [None]
    pad(session, 10.0, 10.0, 5.0, corner=(20.0, 0.0))
    assert _body_names(session) == ["solid", "ボディ1"]
