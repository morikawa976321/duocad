"""`Server._push` の送信失敗の扱い。切れた接続だけを配信先から外す。落ちたら、生きている画面が1回の送信失敗で以後の更新を受け取れなくなる。"""
from __future__ import annotations

import asyncio

from websockets.exceptions import ConnectionClosed

from duocad.server import Server, Session
from session_support import FakeConnection


class _Failing(FakeConnection):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error
        self.failing = True

    async def send(self, raw) -> None:
        if self.failing and not (isinstance(raw, str) and '"type":"error"' in raw.replace(" ", "")):
            raise self.error
        await super().send(raw)


def _run(error: Exception):
    async def scenario():
        server = Server(Session(0.2))
        server.bind_loop(asyncio.get_running_loop())
        client = _Failing(error)
        server.clients.add(client)
        await server._push(client, applied=None, only=client)
        await server._push(None, applied=None)
        stays = client in server.clients
        client.failing = False
        await server._push(None, applied=None)
        return stays, client, client in server.clients

    return asyncio.run(scenario())


def test_an_unserializable_value_keeps_the_connection() -> None:
    stays, client, after = _run(TypeError("not serializable"))

    assert stays and after
    assert [p["where"] for p in client.of_type("error")] == ["send"], "知らせは間引かれて1回"
    assert client.of_type("state"), "直った後の配信は届く"


def test_a_closed_connection_is_dropped() -> None:
    stays, _, _ = _run(ConnectionClosed(None, None))

    assert not stays
