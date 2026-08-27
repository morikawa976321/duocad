"""`duocad.server` は**入口**。実体は2026-08-28にクラス境界で分けた:

    `duocad/paths.py`               `REPO_ROOT` / `DEFAULT_WEB_DIST` / `DEFAULT_MODELS_DIR`
    `duocad/session.py`             `Session`(1つの文書を持つサーバ側の状態)＋ `PREVIEWABLE_VERBS`
    `duocad/net/ws.py`              `Server`(WebSocketの配信と `_dispatch`)＋ソケットから踏める表
    `duocad/net/http.py`            `web/dist` の静的配信と `/mcp`
    `duocad/net/tools_on_loop.py`   `ToolsOnLoop`(別スレッドからの道具呼び出しの覆い)

ここに残すのは**上記の再輸出**と、末尾の起動の番人だけ。`duocad.mcp_server` とテストが
`duocad.server` から名前を取っているので、入口は動かさない。

**このモジュールは起動コマンドを持たない。** 提供するのは `Session` / `Server` / 静的配信で、
**これを起動するのは `duocad.mcp_server` だけ**(起動経路が2つあると「AIが変えた文書」と
「人が見ている文書」が別物になりうる):

    .venv\\Scripts\\python.exe -m duocad.mcp_server [--open] [--port 8765] [--http-port 8766]
"""
from __future__ import annotations

import sys

# `build_provider` の再輸出は**差し替え点**である。`Server._ensure_ai_loop` はこの入口越しに
# 引く(`tests/test_agent_ws.py` が `duocad.server.build_provider` を monkeypatch する)。
from .agent import build_provider
from .net.http import (
    MCP_HTTP_MAX_BODY,
    MCP_HTTP_PATH,
    WebHandler,
    mcp_http_hosts,
    start_http_server,
)
from .net.tools_on_loop import ToolsOnLoop
from .net.ws import (
    AI_PROVIDERS,
    FEATURE_COMMANDS,
    FILE_COMMANDS,
    SELECTION_CONSUMING_COMMANDS,
    SKETCH_COMMANDS,
    Server,
)
from .paths import DEFAULT_MODELS_DIR, DEFAULT_WEB_DIST, REPO_ROOT
from .session import PREVIEWABLE_VERBS, Session

__all__ = [
    "AI_PROVIDERS",
    "DEFAULT_MODELS_DIR",
    "DEFAULT_WEB_DIST",
    "FEATURE_COMMANDS",
    "FILE_COMMANDS",
    "MCP_HTTP_MAX_BODY",
    "MCP_HTTP_PATH",
    "PREVIEWABLE_VERBS",
    "REPO_ROOT",
    "SELECTION_CONSUMING_COMMANDS",
    "SKETCH_COMMANDS",
    "Server",
    "Session",
    "ToolsOnLoop",
    "WebHandler",
    "build_provider",
    "mcp_http_hosts",
    "start_http_server",
]


if __name__ == "__main__":
    # **黙って何もしないで終わらない**(実装計画書§6予防6)。`main()` を持たないので、
    # この番人が無いと `python -m duocad.server` が終了コード0で素通りする。
    print(
        "duocad.server は起動コマンドではない。サーバはこちら:\n"
        r"  .venv\Scripts\python.exe -m duocad.mcp_server --open",
        file=sys.stderr,
    )
    raise SystemExit(2)
