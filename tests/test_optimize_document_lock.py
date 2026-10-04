"""形状最適化の実行中は**走らせている文書だけ**を変える操作を断る(issue #26)。別の文書は開いて編集できる。

落ちたら、結果が別の文書へ入る・古い条件の形が足される・別の文書へ移れない、のどれか。
"""
from __future__ import annotations

import json

import pytest

from duocad import catalog, optimize_run
from duocad.mcp_server import DocumentTools
from duocad.server import Session
from duocad.session import DocumentBusy
from duocad.verbs.add_mesh import add_mesh_body
from session_support import browser

#: 文書を変えるが、実行中も通してよいもの(操作ログに載らず、最適化が読む条件にも形にも触れない)。
_ALLOWED_WHILE_RUNNING = {"set_visible", "set_sketch_visible", "modify_selection", "toggle_selection",
                          "clear_selection",
                          "set_selection"}
#: 文書を差し替えるもの。実行中も通す —— 実行は開始時の文書を持ったまま走り続ける。
_SWITCHES_DOCUMENT = {"open_document", "new_document"}


def _box(session: Session) -> None:
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_rectangle(sketch_id, [0.0, 0.0], 10.0, 10.0)
    session.modify_selection([{"sketch": sketch_id, "index": 1}], [])
    session.extrude(5.0, mode="new")
    session.clear_selection()


@pytest.fixture()
def running(session: Session) -> Session:
    """`a.json`(箱が1つ)と `b.json`(空)を保存し、`a.json` を開いて最適化を走らせ始めた状態。"""
    session.save_document("b.json")
    session.new_document()
    _box(session)
    session.rename_document("a.json")
    session.begin_optimization()
    return session


def _fake_run_that_adds_a_mesh(monkeypatch) -> None:
    """探索を回さず、渡された文書に三角形1枚のボディを足して「終わった」と返す(`optimize_run.run` の代役)。"""

    def fake_run(document, options, **kwargs):
        body_id = add_mesh_body(document, [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0], [0, 1, 2], name="結果")
        return {"state": "done", "body_id": body_id, "name": "結果", "triangle_count": 1}

    monkeypatch.setattr(optimize_run, "run", fake_run)


def _verbs_on_disk(session: Session, name: str) -> list[str]:
    payload = json.loads((session.models_dir / name).read_text(encoding="utf-8"))
    return [entry["verb"] for entry in payload["operations"]]


def test_every_method_that_changes_the_document_is_guarded() -> None:
    """落ちたら、文書を変える verb を足したときに実行中の断りを付け忘れている(その verb だけ実行中に通る)。"""
    names = {command.session for command in catalog.COMMANDS
             if command.changes_document and command.session
             and command.session not in _ALLOWED_WHILE_RUNNING | _SWITCHES_DOCUMENT}
    names |= {"preview", "load_step"}
    unguarded = sorted(name for name in names if not getattr(getattr(Session, name), "edits_document", False))
    assert not unguarded, unguarded


@pytest.mark.parametrize("call", [
    lambda s: s.undo(),
    lambda s: s.sketch_create("XY"),
    lambda s: s.preview("sketch_create", {"base_plane": "XY"}),
], ids=["undo", "verb", "preview"])
def test_document_changes_are_refused_while_running(running: Session, call) -> None:
    """落ちたら、走らせている文書に Undo・verb・プレビューが通り、終わった結果が古い条件の上に足される。"""
    document, entries = running.document, len(running.document.log.entries)
    with pytest.raises(DocumentBusy):
        call(running)
    assert running.document is document and len(document.log.entries) == entries


def test_looking_around_is_still_allowed_while_running_and_edits_resume_afterwards(running: Session) -> None:
    """落ちたら、数分〜数時間の実行中に表示の切り替え・選択・保存まで止めている、または終わった後も断り続ける。"""
    body = running.document.bodies[-1]
    running.modify_selection([{"body": body.id, "kind": "body"}], [])
    running.clear_selection()
    running.set_visible(body.id, False)
    running.end_optimization()
    running.undo()


def test_another_document_can_be_opened_and_edited_while_running(running: Session) -> None:
    """落ちたら、最適化の実行中に別の文書へ移れない・移った先の編集まで断られる。"""
    running.open_document("b.json")
    running.sketch_create("XY")
    running.new_document()
    running.sketch_create("XY")
    assert running.optimizing and not running.optimization_visible


def test_reopening_the_running_file_gives_a_copy_that_refuses_edits(running: Session) -> None:
    """落ちたら、実行中に同じファイルを開き直した写しが編集でき、終わった結果がその編集を上書きする。"""
    running.open_document("b.json")
    running.open_document("a.json")
    assert running.optimization_visible
    with pytest.raises(DocumentBusy):
        running.sketch_create("XY")


def test_the_running_file_cannot_be_renamed_saved_as_or_deleted(running: Session) -> None:
    """落ちたら、実行中に走らせているファイルを動かせ、結果が元の名前で書き戻されて消したものが復活する。"""
    with pytest.raises(ValueError, match="名前を変えられない"):
        running.rename_document("c.json")
    with pytest.raises(ValueError, match="別名で保存できない"):
        running.save_document("c.json")
    running.open_document("b.json")
    with pytest.raises(ValueError, match="形状最適化の実行中"):
        running.delete_document("a.json")


def test_a_result_finished_on_another_document_lands_in_its_own_file(running: Session, monkeypatch) -> None:
    """落ちたら、別の文書を開いている間に終わった結果が、今開いている文書に入る・どこにも残らない。"""
    _fake_run_that_adds_a_mesh(monkeypatch)
    running.open_document("b.json")
    result = running.deliver_optimization_result(running.run_optimization())
    running.end_optimization()

    assert result["elsewhere"] is True and result["file"] == "a.json"
    assert running.current_name == "b.json" and running.document.bodies == []
    assert _verbs_on_disk(running, "a.json")[-1] == "add_mesh_body"
    running.open_document("a.json")
    assert [body.name for body in running.document.bodies][-1] == "結果"


def test_a_result_finished_on_a_reopened_copy_replaces_the_copy(running: Session, monkeypatch) -> None:
    """落ちたら、実行中に開き直した写し(結果が無い)が残り、戻ってきても結果が見えない。"""
    _fake_run_that_adds_a_mesh(monkeypatch)
    running.open_document("b.json")
    running.open_document("a.json")
    result = running.deliver_optimization_result(running.run_optimization())
    running.end_optimization()

    assert "elsewhere" not in result and result["applied"]["verb"] == "add_mesh_body"
    assert [body.name for body in running.document.bodies][-1] == "結果"
    assert _verbs_on_disk(running, "a.json")[-1] == "add_mesh_body"
    running.sketch_create("XY")  # 終わったので編集できる


def test_a_result_that_could_not_be_written_opens_from_memory(running: Session, monkeypatch) -> None:
    """落ちたら、別の文書を開いている間に終わった結果をファイルへ書けなかったとき、数時間の結果が消える。"""
    _fake_run_that_adds_a_mesh(monkeypatch)
    running.open_document("b.json")
    real_write = Session._write_document_file

    def refuse(target, payload):
        raise PermissionError("別のプログラムが開いている")

    monkeypatch.setattr(Session, "_write_document_file", staticmethod(refuse))
    running.deliver_optimization_result(running.run_optimization())
    running.end_optimization()
    assert any("a.json へ書けなかった" in warning for warning in running.take_warnings())

    monkeypatch.setattr(Session, "_write_document_file", staticmethod(real_write))
    running.open_document("a.json")
    assert [body.name for body in running.document.bodies][-1] == "結果"
    assert _verbs_on_disk(running, "a.json")[-1] == "add_mesh_body"


def test_the_browser_gets_a_named_refusal(running: Session) -> None:
    """落ちたら、画面(別タブ・キーボードの Ctrl+Z を含む)から送った Undo が実行中に通る。"""
    _, [connection] = browser(running, lambda send: send({"cmd": "undo"}), connections=1, initial_push=False)
    [error] = connection.of_type("error")
    assert error["where"] == "undo" and "DocumentBusy" in error["message"]


def test_the_ai_gets_a_busy_code_instead_of_an_invalid_argument(running: Session) -> None:
    """落ちたら、MCP・アプリ内AIへの断りが `invalid_argument` になり、AI が値を変えて打ち直し続ける。"""
    tools = DocumentTools(running)
    with pytest.raises(DocumentBusy) as caught:
        tools.call("undo", {})
    assert tools.error_envelope("undo", caught.value)["code"] == "busy"
