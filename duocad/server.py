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

# --------------------------------------------------------------------------------------
# 再輸出(入口の本体)
# --------------------------------------------------------------------------------------
# **入口は名前を減らさない。** 分割前の `duocad.server` はここに並ぶ名前をすべて
# モジュールスコープに持っていて、`duocad.server.<名前>` で引けた。実体を別ファイルへ
# 移したあとも同じ名前が同じように引けることが、この入口を残す理由そのものである
# (`import` で引く側だけでなく、モジュール属性を差し替える検査もここを見る)。
# 素通しのimport(下の `# noqa: F401`)は使われていないように見えるが**消さない**。
import asyncio  # noqa: F401  分割前から `duocad.server.asyncio` で引けた
import json  # noqa: F401
import os  # noqa: F401
import threading  # noqa: F401
from functools import partial  # noqa: F401
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer  # noqa: F401
from pathlib import Path  # noqa: F401
from typing import Any, Optional  # noqa: F401

from . import catalog, io_step, kernel, parameters, query, region, selection, sketch  # noqa: F401
# `build_provider` の再輸出は**差し替え点**である。`Server._ensure_ai_loop` はこの入口越しに
# 引く(`tests/test_agent_ws.py` が `duocad.server.build_provider` を monkeypatch する)。
from .agent import AgentLoop, Cancellation, ProviderError, build_provider  # noqa: F401
from .agent.providers import DEFAULT_OPENAI_COMPAT_BASE_URL  # noqa: F401
from .document import Document  # noqa: F401
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
    _send,  # noqa: F401
)
from .oplog import OpLog  # noqa: F401
from .paths import DEFAULT_MODELS_DIR, DEFAULT_WEB_DIST, REPO_ROOT
from .session import (
    PREVIEWABLE_VERBS,
    Session,
    _log_args_for_state,  # noqa: F401
    _REDACTED_LOG_ARGS,  # noqa: F401
    _RESERVED_WINDOWS_NAMES,  # noqa: F401
)
from .speculative import speculative  # noqa: F401
from .verbs import (  # noqa: F401
    chamfer as chamfer_verb,
    extrude as extrude_verb,
    extrude_face as extrude_face_verb,
    fillet as fillet_verb,
    import_step as import_step_verb,
    import_svg as import_svg_verb,
    pattern_circular as pattern_circular_verb,
    pattern_rectangular as pattern_rectangular_verb,
)

# **`__all__` は置かない。** 置くと `from duocad.server import *` で出る名前が分割前より
# 狭くなる(属性アクセスには効かないが、星importの意味は変わる)。入口が提供するものは
# 上のdocstringの地図が示す。


if __name__ == "__main__":
    # **黙って何もしないで終わらない**(実装計画書§6予防6)。`main()` を持たないので、
    # この番人が無いと `python -m duocad.server` が終了コード0で素通りする。
    print(
        "duocad.server は起動コマンドではない。サーバはこちら:\n"
        r"  .venv\Scripts\python.exe -m duocad.mcp_server --open",
        file=sys.stderr,
    )
    raise SystemExit(2)
