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
