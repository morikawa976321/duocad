"""`ack_id` を添えた下りに、処理を終えた後で `done` を返す口(`duocad/net/ws.py` の `_dispatch`)。

落ちたら、画面の上端の待ちバー(`web/src/serverWait.svelte.ts`)が消えない・応答より先に消える・失敗したまま出続ける、のどれかが起きる。
"""
from __future__ import annotations

from duocad.server import Session
from session_support import browser


def _types(client) -> list:
    return [payload["type"] for payload in client.payloads]


def test_done_follows_the_reply(session: Session) -> None:
    """成功した操作では、状態配信を送り終えた後に同じ番号の `done` が来る。"""

    async def script(send):
        return await send({"cmd": "sketch_create", "base_plane": "XY", "client_op": 1, "ack_id": 7})

    last, clients = browser(session, script)

    assert last == {"type": "done", "ack_id": 7}
    assert _types(clients[0])[-2:] == ["state", "done"]
    assert "done" not in _types(clients[1]), "`done` は頼んだ接続にだけ返す"


def test_done_follows_an_error(session: Session) -> None:
    """失敗しても `done` は来る(来ないと待ちバーが出たまま残る)。"""

    async def script(send):
        return await send({"cmd": "no_such_command", "ack_id": 3})

    last, clients = browser(session, script)

    assert last == {"type": "done", "ack_id": 3}
    assert _types(clients[0])[-2:] == ["error", "done"]


def test_no_done_without_ack_id(session: Session) -> None:
    """`ack_id` が無ければ `done` は返さない(待たせない下りに余計な封筒を流さない)。"""

    async def script(send):
        await send({"cmd": "sketch_create", "base_plane": "XY", "client_op": 1})

    _, clients = browser(session, script)

    assert "done" not in _types(clients[0])
