"""MCPサーバと「コアが発火し、アプリ層が購読する」上り方向の契約。

1. **プロトコル** — `initialize` / `tools/list` / `tools/call` が MCP(JSON-RPC 2.0)として
   正しく喋れること。**依存を増やさず素で実装した**ので、プロトコルの正しさはここでしか
   担保されない。
2. **`describe_document` / `set_visible` / `describe_shape` が名前も振る舞いも変わらず
   生き残っていること** — ツール全体の顔ぶれの契約は `tests/test_mcp_verbs.py` の
   `EXPECTED_TOOLS` にひとつだけ置く。
3. **`set_visible` はコアの verb を経由する** — 操作ログに載り、undoで戻る。
4. **上り方向** — MCPが文書を変えると `Session` が変更イベントを発火し、購読している
   WebSocketサーバが**接続している全クライアントへ配信する**。

**実ブラウザでの確認は `web/scripts/smoke-mcp.mjs`** が行う。
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from duocad.mcp_server import DocumentTools, McpServer, serve_stdio

#: M4の3つ。ツール全体の顔ぶれの正は `tests/test_mcp_verbs.py` の `EXPECTED_TOOLS`。
#: ここではstdio越しでもこの3つが見えることだけを確かめる。
M4_TOOLS = {"describe_document", "set_visible", "describe_shape"}
REPO_ROOT = Path(__file__).resolve().parent.parent

# --------------------------------------------------------------------------------------
# 6. stdio トランスポート(実際にプロセスを起動して喋る)
# --------------------------------------------------------------------------------------
def test_stdio_transport_speaks_line_delimited_json_rpc():
    """`serve_stdio` が1行1メッセージで読み書きし、EOFで戻ること(プロセスは起こさない)。"""
    import io

    class Pipe(io.BytesIO):
        pass

    incoming = Pipe(
        b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}\n'
        b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
        b"\n"  # 空行は読み飛ばす
        b"{ not json\n"
        b'{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n'
    )
    outgoing = Pipe()

    class Tools(DocumentTools):
        def __init__(self) -> None:  # 文書を持たないダミー(tools/listしか叩かない)
            super().__init__(session=None)  # type: ignore[arg-type]

    asyncio.run(serve_stdio(McpServer(Tools()), reader=incoming, writer=outgoing))

    lines = [json.loads(line) for line in outgoing.getvalue().decode("utf-8").splitlines()]
    assert [line.get("id") for line in lines] == [1, None, 2], "通知に返事を書いた/壊れた行を飛ばした"
    assert lines[0]["result"]["serverInfo"]["name"] == "duocad"
    assert lines[1]["error"]["code"] == -32700  # 壊れたJSONはパースエラー
    assert M4_TOOLS <= {tool["name"] for tool in lines[2]["result"]["tools"]}


def test_the_process_starts_and_answers_over_real_stdio():
    """`python -m duocad.mcp_server` を**実プロセスとして起動**して喋れること。

    MCPクライアント(Claude Code / Claude Desktop)はこの経路でしか繋がらない。
    あわせて **stdoutにJSON-RPC以外の行が出ていないこと**を確かめる — 起動メッセージを
    1行stdoutへ出すだけで接続は失敗する(だからログはstderrへ出す設計にしてある)。
    """
    handshake = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "duocad.mcp_server",
            # `--step` / `--no-step` は廃止(常に空の文書で起動する)
            # 既定のポート(8765/8766)は実機のサーバが使っている可能性があるので避ける
            "--port",
            "18765",
            "--http-port",
            "18766",
            "--web-dist",
            str(REPO_ROOT / "web" / "does-not-exist"),
        ],
        input="\n".join(json.dumps(message) for message in handshake) + "\n",
        capture_output=True,
        text=True,
        # **両側でUTF-8に揃える**。既定だとWindowsのパイプはcp932で読み書きされ、
        # 日本語のログを1行stderrへ出しただけで親側が UnicodeDecodeError で落ちる(実測)。
        # MCPの中身がUTF-8なので、子プロセスの側も PYTHONIOENCODING で明示する。
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        cwd=str(REPO_ROOT),
        timeout=180,
    )
    assert completed.returncode == 0, completed.stderr

    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    responses = []
    for line in lines:
        try:
            responses.append(json.loads(line))
        except ValueError:  # noqa: PERF203
            pytest.fail(f"stdoutにJSON-RPC以外の行が出ている(接続が壊れる): {line!r}")
    assert [response["id"] for response in responses] == [1, 2]
    assert responses[0]["result"]["protocolVersion"] == "2025-06-18"
    assert M4_TOOLS <= {tool["name"] for tool in responses[1]["result"]["tools"]}
    # 静的配信が立てられなくてもMCPは動く(理由はstderrに残る)
    assert "warning" in completed.stderr


