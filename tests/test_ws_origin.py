"""WebSocketのOrigin検査。理由は `duocad/mcp_server.py` の `allowed_origins` が持つ。

**Originヘッダ無しは許可する**(本人決定)。**「無しを許可」を緩みと読み替えて塞がないこと**
—— 塞ぐと `tests/test_mcp_verbs.py` の実サーバ検査と `web/scripts/*.mjs` が全部落ちる。

ここは**握手だけ**を検査する(duocadのプロトコルは他のテストが見る)ので、ハンドラは接続を
受けるだけの空実装でよい。
"""
from __future__ import annotations

import asyncio

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import InvalidHandshake

from duocad.mcp_server import VITE_DEV_PORT, allowed_origins


# --------------------------------------------------------------------------------------
# 許可リストそのもの
# --------------------------------------------------------------------------------------
def test_the_allowlist_covers_both_host_spellings_and_the_dev_server():
    """`localhost` と `127.0.0.1` は**別オリジン**なので両方要る。

    片方だけにすると「なぜか繋がらない日がある」という最悪の壊れ方をする。
    """
    origins = allowed_origins(8766)
    for host in ("127.0.0.1", "localhost"):
        assert f"http://{host}:8766" in origins        # 静的配信
        assert f"http://{host}:{VITE_DEV_PORT}" in origins  # `npm run dev`(README §1.3)


def test_the_allowlist_follows_the_http_port_option():
    """`--http-port` を変えたら許可リストも動く(既定値を焼き込まない)。"""
    origins = allowed_origins(9999)
    assert "http://127.0.0.1:9999" in origins
    assert "http://127.0.0.1:8766" not in origins


def test_a_missing_origin_header_is_allowed_on_purpose():
    """`None`(ヘッダ無し)が入っていること。**これは意図した許可である**(モジュールdocstring)。"""
    assert None in allowed_origins(8766)


# --------------------------------------------------------------------------------------
# 実際の握手
# --------------------------------------------------------------------------------------
async def _accept(connection) -> None:
    """握手が通ったことだけ確かめるハンドラ(1通受けて閉じる)。"""
    try:
        await connection.recv()
    except Exception:
        pass


def _handshake(origin: str | None) -> bool:
    """`origin` を名乗って繋げるか。拒否されたら False。"""

    async def scenario() -> bool:
        async with serve(
            _accept, "127.0.0.1", 0, origins=allowed_origins(8766)
        ) as server:
            port = server.sockets[0].getsockname()[1]
            headers = {"Origin": origin} if origin is not None else None
            try:
                async with connect(
                    f"ws://127.0.0.1:{port}", additional_headers=headers
                ) as socket:
                    await socket.send("ping")
                return True
            except InvalidHandshake:
                return False

    return asyncio.run(scenario())


@pytest.mark.parametrize(
    "origin",
    [
        "http://evil.example.com",
        "http://127.0.0.1:8767",   # 隣のポート(別オリジン)
        "http://127.0.0.1",        # ポート無し(既定80。duocadは配っていない)
        "null",                    # sandboxed iframe / file:// が名乗る値
    ],
)
def test_a_page_from_somewhere_else_cannot_open_the_websocket(origin: str):
    """**これが塞ぎたかった穴**。duocadが配っていないページからは繋がらない。

    `"null"` を含めているのは、sandboxed iframe や `file://` のページが `Origin: null` を
    送るため —— `None`(ヘッダ無し)の許可と文字列 `"null"` の許可を取り違えると**穴が空いた
    まま緑になる**。
    """
    assert _handshake(origin) is False


@pytest.mark.parametrize(
    "origin",
    [
        "http://127.0.0.1:8766",
        "http://localhost:8766",
        f"http://127.0.0.1:{VITE_DEV_PORT}",
        f"http://localhost:{VITE_DEV_PORT}",
    ],
)
def test_the_pages_duocad_serves_can_connect(origin: str):
    """自分が配るページ(静的配信・dev server)は今までどおり繋がる。"""
    assert _handshake(origin) is True


def test_a_client_without_an_origin_header_can_connect():
    """**素のWSクライアントは今までどおり繋がる**(`smoke*.mjs` / `acceptance.mjs`)。

    ここが落ちたら、検査スクリプト一式が道連れになる。
    """
    assert _handshake(None) is True
