"""MCPのプロトコル層(JSON-RPC 2.0 と stdio トランスポート)の契約。

**依存を増やさず素で実装した**ので、プロトコルの正しさはここでしか担保されない。
ツールの中身は `tests/test_mcp_verbs.py` / `tests/test_mcp_selection.py`、HTTPトランスポートは
`tests/test_mcp_http.py`、実ブラウザでの確認は `web/scripts/smoke-mcp.mjs` が行う。
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from duocad.mcp_server import SUPPORTED_PROTOCOL_VERSIONS, DocumentTools, McpServer, serve_stdio

#: stdio越しの `tools/list` に入っているはずのツールの目印(顔ぶれ全体は
#: `tests/test_doc_code_agreement.py` が `MCP.md` の表と突き合わせる)。
M4_TOOLS = {"describe_document", "set_visible", "describe_shape"}
REPO_ROOT = Path(__file__).resolve().parent.parent


def _server() -> McpServer:
    """文書を持たないサーバ(プロトコル層しか叩かない)。"""
    return McpServer(DocumentTools(session=None))  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------
# JSON-RPC の誤りは標準コードで断る
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("message", "code"),
    [
        ({"jsonrpc": "2.0", "id": 1, "method": "resources/list"}, -32601),
        ({"jsonrpc": "1.0", "id": 1, "method": "ping"}, -32600),
        ([{"jsonrpc": "2.0", "id": 1, "method": "ping"}], -32600),
    ],
    ids=["未知のメソッド", "jsonrpcの版違い", "バッチ"],
)
def test_protocol_errors_come_back_with_the_standard_codes(message, code):
    """落ちたら、JSON-RPC の誤りを標準コード(クライアントが分岐に使う値)で返していない。"""
    response = _server().handle(message)
    assert response["error"]["code"] == code


def test_an_unknown_protocol_version_is_answered_with_the_server_default():
    """落ちたら、知らない版をそのまま返している(クライアントは返った版で続けるか切るかを決める)。"""
    response = _server().handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "1999-01-01"},
        }
    )
    assert response["result"]["protocolVersion"] == SUPPORTED_PROTOCOL_VERSIONS[0]


# --------------------------------------------------------------------------------------
# stdio トランスポート
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

    asyncio.run(serve_stdio(_server(), reader=incoming, writer=outgoing))

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


