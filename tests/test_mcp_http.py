"""MCPのHTTPトランスポート(`/mcp`)の契約。"""
from __future__ import annotations

import asyncio
import http.client
import json
import socket
import threading

import pytest

from duocad import kernel
from duocad.mcp_server import DocumentTools, McpServer
from duocad.server import Session, ToolsOnLoop, WebHandler, start_http_server


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class _Http:
    """`/mcp` を素のHTTPで叩く(`http.client` は `Host` の差し替えまでさせてくれる)。"""

    def __init__(self, port: int) -> None:
        self.port = port

    def request(self, method: str, path: str, body: bytes = None, headers: dict = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def rpc(self, payload: dict, headers: dict = None):
        body = json.dumps(payload).encode("utf-8")
        sent = {"Content-Type": "application/json", **(headers or {})}
        status, raw = self.request("POST", "/mcp", body=body, headers=sent)
        return status, (json.loads(raw) if raw and status < 300 else None)


@pytest.fixture(scope="module")
def http_mcp(tmp_path_factory):
    """本番と同じ組み方で立てる —— イベントループは別スレッド、HTTPはさらに別スレッド。

    **モジュールで1つだけ立てる**: `httpd.shutdown()` は `serve_forever` の既定の
    `poll_interval`(0.5秒)まで待つので、テストごとに立て直すとその分だけ遅い(実測 0.50秒/本)。
    """
    tmp_path = tmp_path_factory.mktemp("http_mcp")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>duocad</title>", encoding="utf-8")

    loop = asyncio.new_event_loop()
    running = threading.Event()

    def _spin() -> None:
        asyncio.set_event_loop(loop)
        loop.call_soon(running.set)
        loop.run_forever()

    thread = threading.Thread(target=_spin, name="duocad-test-loop", daemon=True)
    thread.start()
    assert running.wait(10), "イベントループが立たなかった"

    session = Session(kernel.DEFAULT_DEVIATION, models_dir=tmp_path / "models")
    tools = DocumentTools(session)
    port = _free_port()
    httpd = start_http_server(
        dist, port, f"ws://127.0.0.1:{port}", mcp=McpServer(ToolsOnLoop(tools, loop))
    )
    try:
        yield _Http(port), session, tools
    finally:
        httpd.shutdown()
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=10)
        WebHandler.mcp_server = None


def _ok(response: dict) -> dict:
    assert "error" not in response, response
    return response["result"]


def test_handshake_and_tool_lineup_do_not_depend_on_the_transport(http_mcp):
    """1. HTTPで握手し、**stdioと同じ顔ぶれ**のツールが出る。"""
    http, _session, tools = http_mcp

    status, response = http.rpc(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "clientInfo": {"name": "t"}},
        }
    )
    assert status == 200
    result = _ok(response)
    assert result["protocolVersion"] == "2025-06-18"
    assert result["serverInfo"]["name"] == "duocad"
    assert result["instructions"]

    status, response = http.rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert status == 200
    over_http = {tool["name"] for tool in _ok(response)["tools"]}

    # 同じ `DocumentTools` を直に持たせた `McpServer`(= stdioが使うのと同じ組み方)と比べる。
    direct = McpServer(tools).handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    assert over_http == {tool["name"] for tool in direct["result"]["tools"]}


def test_tool_call_from_a_worker_thread_changes_the_same_document(http_mcp):
    """2. ワーカースレッドからの `tools/call` が、**イベントループ側の文書**を変える。"""
    http, session, _tools = http_mcp
    before = len(session.document.sketches)

    status, response = http.rpc(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "sketch_create", "arguments": {"base_plane": "XY"}},
        }
    )
    assert status == 200
    result = _ok(response)
    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload["verb"] == "sketch_create"

    # **HTTPのスレッドで呼んだのに、文書はこのプロセスの `Session` のもの**。
    assert len(session.document.sketches) == before + 1


@pytest.mark.parametrize(
    "headers, why",
    [
        ({"Origin": "https://evil.example"}, "Origin を名乗るPOSTは値を問わず断る(自分のページを名乗っても同じ)"),
        ({"Host": "evil.example"}, "DNSリバインディング"),
    ],
)
def test_browser_reachable_requests_are_refused(http_mcp, headers, why):
    """3. ブラウザから届きうる形のリクエストは403(`WebHandler._mcp_forbidden`)。"""
    http, session, _tools = http_mcp
    before = len(session.document.sketches)
    status, _ = http.rpc(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "sketch_create", "arguments": {"base_plane": "XY"}},
        },
        headers=headers,
    )
    assert status == 403, why
    # 副作用が起きていないことまで見る(応答を読めなくても書けてしまうのが怖い形なので)。
    assert len(session.document.sketches) == before


def test_transport_shape(http_mcp):
    """4-a. 通知は202(本文なし)、`/mcp` のGETは405、他のパスへのPOSTは404。"""
    http, _session, _tools = http_mcp

    status, raw = http.request(
        "POST",
        "/mcp",
        body=json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    assert (status, raw) == (202, b"")

    # サーバ→クライアントのSSEは提供しない(pushするものが無い)。仕様どおり405。
    assert http.request("GET", "/mcp")[0] == 405
    assert http.request("POST", "/duocad-config.json", body=b"{}")[0] == 404


def test_static_serving_still_works(http_mcp):
    """4-b. 同じポートに口を生やしても、ページとconfigは今までどおり配れる。"""
    http, _session, _tools = http_mcp

    status, raw = http.request("GET", "/")
    assert status == 200
    assert b"duocad" in raw

    status, raw = http.request("GET", "/duocad-config.json")
    assert status == 200
    assert json.loads(raw)["ws_url"].startswith("ws://127.0.0.1:")
