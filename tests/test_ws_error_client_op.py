"""WebSocket の `error` 封筒に、失敗した要求の `client_op` を載せる(`duocad/net/ws.py` の `_dispatch`)。

落ちたら、画面は別の要求(確定の直前に送ったプレビューなど)の失敗でも確定の応答待ち(`pendingOp`)を解き、
確定がまだサーバで走っている間に次の Enter が通る。
"""
from __future__ import annotations

from duocad.server import Session
from session_support import send_one


def test_error_carries_the_client_op(session: Session) -> None:
    answer = send_one(session, {"cmd": "no_such_command", "client_op": 42})

    assert answer["type"] == "error" and answer["client_op"] == 42


def test_error_without_client_op_carries_none(session: Session) -> None:
    """番号を付けない要求(プレビュー・辺の取り寄せなど)の失敗は None で、どの待ちとも一致しない。"""
    answer = send_one(session, {"cmd": "get_edges", "body_id": "no_such_body"})

    assert answer["type"] == "error" and answer["client_op"] is None
