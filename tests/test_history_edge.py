"""履歴の端の取り消し・やり直しと、`error` の封筒の `kind`。落ちたら、画面が端の断りを赤い札にする・断りの種別を取り違える。"""
from __future__ import annotations

import asyncio
import json

from duocad.server import Server, Session
from session_support import FakeConnection, session_with_box


def errors_after(session: Session, *commands: dict) -> list[dict]:
    async def scenario() -> list[dict]:
        server = Server(session)
        server.bind_loop(asyncio.get_running_loop())
        client = FakeConnection()
        server.clients.add(client)
        await server._push(client, applied=None, only=client)
        client.payloads.clear()
        for command in commands:
            await server._dispatch(client, json.dumps(command))
        return [payload for payload in client.payloads if payload["type"] == "error"]

    return asyncio.run(scenario())


def test_undo_and_redo_at_the_ends_of_history_are_marked_history_edge():
    session = Session(0.2)
    errors = errors_after(session, {"cmd": "undo", "client_op": 1}, {"cmd": "redo", "client_op": 2})
    assert [(e["where"], e["kind"], e["client_op"]) for e in errors] == [("undo", "history_edge", 1), ("redo", "history_edge", 2)]
    assert errors[0]["message"] == "ValueError: 取り消せる操作が無い"
    assert errors[1]["message"] == "ValueError: やり直せる操作が無い"


def test_other_refusals_are_marked_refusal():
    errors = errors_after(session_with_box(), {"cmd": "set_visible", "body_id": "box", "visible": "yes", "client_op": 3})
    assert [e["kind"] for e in errors] == ["refusal"]


def test_a_selection_refusal_is_a_refusal_not_the_kind_of_figure_it_needs():
    """落ちたら、選択の例外が持つ「要る図形の種別」(`"edge"`)が封筒の `kind` に流れ、画面が想定した断りの例外名を札に出す。"""
    errors = errors_after(session_with_box(), {"cmd": "fillet", "radius": 1.0, "client_op": 4})
    assert [(e["kind"], e["message"].split(":")[0]) for e in errors] == [("refusal", "SelectionEmpty")]


def test_an_unexpected_exception_is_marked_error(monkeypatch):
    """落ちたら、想定外の失敗が断りの扱いになる、または `kind` が 3 つの外に出る。"""
    session = session_with_box()

    def explode(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(session, "apply", explode)
    errors = errors_after(session, {"cmd": "fillet", "radius": 1.0, "client_op": 5})
    assert [e["kind"] for e in errors] == ["error"]
