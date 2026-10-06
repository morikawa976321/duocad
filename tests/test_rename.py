"""verb `rename`。落ちたら、名前が undo/redo・保存・パラメータの再生で失われるか、不正な名前がログに載る。"""
from __future__ import annotations

import pytest

from duocad.server import Session
from duocad.verbs import rename
from session_support import box, edit_parameter


def test_body_and_sketch_rename_undo_redo(session: Session) -> None:
    body, sketch_id = box(session)
    session.rename(body.id, "ブラケット")
    session.rename(sketch_id, "底面の輪郭")
    assert body.name == "ブラケット"
    assert session.document.sketches[0].name == "底面の輪郭"

    session.undo()
    assert session.document.sketches[0].name == "スケッチ1"
    session.undo()
    assert body.name == "ボディ1"
    session.redo()
    assert body.name == "ブラケット"


def test_the_name_survives_save_reopen_and_a_parameter_rebuild(session: Session) -> None:
    body, sketch_id = box(session)
    session.add_circle(sketch_id, [5.0, 5.0], 2.0)
    session.rename(body.id, "台座")
    session.save_document("renamed.json")
    session.open_document("renamed.json")
    assert session.document.bodies[0].name == "台座"

    radius = next(name for name, value in session.document.parameters.items() if value == 2.0)
    edit_parameter(session, radius, 3.0)
    assert session.document.bodies[0].name == "台座"


@pytest.mark.parametrize("name", ["", "   ", " 前に空白", "後ろに空白 ", "改\n行", "ボディ1"])
def test_bad_names_change_nothing(session: Session, name: str) -> None:
    body, _ = box(session)
    log_length = len(session.document.log.entries)
    with pytest.raises(ValueError):
        rename(session.document, body.id, name)
    assert body.name == "ボディ1"
    assert len(session.document.log.entries) == log_length


def test_unknown_target_is_a_key_error(session: Session) -> None:
    with pytest.raises(KeyError):
        rename(session.document, "body-missing", "x")


def test_duplicate_names_are_allowed(session: Session) -> None:
    body, sketch_id = box(session)
    session.rename(sketch_id, "ボディ1")
    assert session.document.sketches[0].name == body.name
