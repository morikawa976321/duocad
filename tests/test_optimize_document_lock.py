"""形状最適化の実行中は文書を変える操作を断る(issue #26)。落ちたら、結果が別の文書へ入る・古い条件の形が足される。"""
from __future__ import annotations

import pytest

from duocad import catalog
from duocad.mcp_server import DocumentTools
from duocad.server import Session
from duocad.session import DocumentBusy
from session_support import browser

#: 文書を変えるが、実行中も通してよいもの(操作ログに載らず、最適化が読む条件にも形にも触れない)。
_ALLOWED_WHILE_RUNNING = {"set_visible", "set_sketch_visible", "modify_selection", "commit_selection",
                          "clear_selection",
                          "propose_selection"}


def _running_session_with_a_body() -> Session:
    session = Session(None, 0.2)
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_rectangle(sketch_id, [0.0, 0.0], 10.0, 10.0)
    session.modify_selection([{"sketch": sketch_id, "index": 1}], [])
    session.extrude(5.0, mode="new")
    session.clear_selection()
    session.begin_optimization()
    return session


def test_every_method_that_changes_the_document_is_guarded() -> None:
    """落ちたら、文書を変える verb を足したときに実行中の断りを付け忘れている(その verb だけ実行中に通る)。"""
    names = {command.session for command in catalog.COMMANDS
             if command.changes_document and command.session and command.session not in _ALLOWED_WHILE_RUNNING}
    names |= {"preview", "load_step"}
    unguarded = sorted(name for name in names if not getattr(getattr(Session, name), "edits_document", False))
    assert not unguarded, unguarded


@pytest.mark.parametrize("call", [
    lambda s: s.undo(),
    lambda s: s.new_document(),
    lambda s: s.open_document("other.json"),
    lambda s: s.sketch_create("XY"),
    lambda s: s.set_parameter("w", 3.0),
    lambda s: s.preview("sketch_create", {"base_plane": "XY"}),
], ids=["undo", "new_document", "open_document", "verb", "set_parameter", "preview"])
def test_document_changes_are_refused_while_running(call) -> None:
    """落ちたら、実行中に Undo・新規・開く・verb・プレビューが通り、終わった結果が別の文書や古い条件の上に足される。"""
    session = _running_session_with_a_body()
    document, entries = session.document, len(session.document.log.entries)
    with pytest.raises(DocumentBusy):
        call(session)
    assert session.document is document and len(document.log.entries) == entries


def test_looking_around_is_still_allowed_while_running_and_edits_resume_afterwards() -> None:
    """落ちたら、数分〜数時間の実行中に表示の切り替え・選択・保存まで止めている、または終わった後も断り続ける。"""
    session = _running_session_with_a_body()
    body = session.document.bodies[-1]
    session.modify_selection([{"body": body.id, "kind": "body"}], [])
    session.clear_selection()
    session.set_visible(body.id, False)
    session.end_optimization()
    session.undo()


def test_the_browser_gets_a_named_refusal() -> None:
    """落ちたら、画面(別タブ・キーボードの Ctrl+Z を含む)から送った Undo が実行中に通る。"""
    session = _running_session_with_a_body()

    _, [connection] = browser(session, lambda send: send({"cmd": "undo"}), connections=1, initial_push=False)
    [error] = connection.of_type("error")
    assert error["where"] == "undo" and "DocumentBusy" in error["message"]


def test_the_ai_gets_a_busy_code_instead_of_an_invalid_argument() -> None:
    """落ちたら、MCP・アプリ内AIへの断りが `invalid_argument` になり、AI が値を変えて打ち直し続ける。"""
    session = _running_session_with_a_body()
    tools = DocumentTools(session)
    with pytest.raises(DocumentBusy) as caught:
        tools.call("undo", {})
    assert tools.error_envelope("undo", caught.value)["code"] == "busy"
