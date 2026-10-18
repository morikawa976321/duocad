"""ウォームアップ(`prepare`)の契約: system + tools を先に読ませ、スロットの保存・復元で cold を消す。

落ちたら、最初の依頼がツール定義の読み込み(遅い機種で分単位)を丸ごと待つ状態に戻っている。
スロットの口は llama-server 固有なので、口が無い相手でも黙って進むことも固定する。
"""
from __future__ import annotations

import functools
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from duocad import catalog
from duocad.agent import AgentLoop, OpenAICompatProvider, ProviderError
from duocad.agent.providers import SLOT_FILE, Provider
from duocad.mcp_server import DocumentTools
from duocad.server import Session


@contextmanager
def llama_server(*, restore_ok: bool, read: int, slot_endpoints: bool = True):
    """llama-server の3つの口(スロット復元・対話・スロット保存)だけを真似る。"""
    calls: list[str] = []
    bodies: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:
            pass

        def _reply(self, status: int, body: bytes, content_type: str = "application/json") -> None:
            self.send_response(status)
            self.send_header("content-type", content_type)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            calls.append(f"GET {self.path}")
            self._reply(200, b'{"default_generation_settings": {"n_ctx": 10240}, "total_slots": 1}')

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
            parts = urlsplit(self.path)
            if parts.path == "/slots/0":
                action = parse_qs(parts.query)["action"][0]
                calls.append(action)
                bodies.append(body)
                if not slot_endpoints:
                    self._reply(501, b'{"error": {"message": "no --slot-save-path"}}')
                elif action == "restore" and not restore_ok:
                    self._reply(400, b'{"error": {"message": "file not found"}}')
                else:
                    self._reply(200, b'{"id_slot": 0}')
                return
            calls.append("chat")
            bodies.append(body)
            timings = {"cache_n": 6676 - read, "prompt_n": read, "prompt_ms": 1.0, "predicted_n": 1, "predicted_ms": 1.0}
            chunks = [
                {"choices": [{"delta": {"content": "."}, "finish_reason": "length"}]},
                {"choices": [], "usage": {"prompt_tokens": 6676, "completion_tokens": 1}, "timings": timings},
            ]
            stream = b"".join(f"data: {json.dumps(chunk)}\n\n".encode() for chunk in chunks) + b"data: [DONE]\n\n"
            self._reply(200, stream, "text/event-stream")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=functools.partial(server.serve_forever, poll_interval=0.01), daemon=True).start()
    try:
        server.calls, server.bodies = calls, bodies
        yield server
    finally:
        server.shutdown()
        server.server_close()


def prepare(server) -> dict:
    provider = OpenAICompatProvider(base_url=f"http://127.0.0.1:{server.server_address[1]}/v1")
    return provider.prepare(system="つかいかた", tools=catalog.tool_descriptors(), timeout=5.0)


def test_a_restored_slot_that_still_matches_is_not_saved_again():
    """戻したスロットが今の定義と同じなら、読むのは1トークンで、保存し直さない。"""
    with llama_server(restore_ok=True, read=1) as server:
        result = prepare(server)

    assert server.calls == ["restore", "chat"]
    assert result["restored"] is True and result["saved"] is False and result["read"] == 1
    assert server.bodies[0] == {"filename": SLOT_FILE}


def test_a_prefix_that_had_to_be_read_is_saved_for_the_next_start():
    """復元できなかった(初回・定義を変えた)ら、読ませたあとで保存する。"""
    with llama_server(restore_ok=False, read=6676) as server:
        result = prepare(server)

    assert server.calls == ["restore", "chat", "save"]
    assert result["restored"] is False and result["saved"] is True


def test_the_warm_up_sends_the_real_prefix_and_generates_one_token():
    """本番と同じ system・ツール定義で投げる(違うとキャッシュが前方一致しない)。生成は1トークンだけ。"""
    with llama_server(restore_ok=True, read=1) as server:
        prepare(server)

    sent = server.bodies[1]
    assert sent["messages"][0] == {"role": "system", "content": "つかいかた"}
    assert sent["messages"][-1] == {"role": "user", "content": "."}
    assert [tool["function"]["name"] for tool in sent["tools"]] == list(catalog.tool_names())
    assert sent["max_tokens"] == 1


def test_a_server_without_slot_endpoints_is_still_warmed():
    """`--slot-save-path` 無しの llama-server や他のOpenAI互換でも、読ませるところまでは効く。"""
    with llama_server(restore_ok=False, read=6676, slot_endpoints=False) as server:
        result = prepare(server)

    assert "chat" in server.calls
    assert result["restored"] is False and result["saved"] is False


def test_the_context_size_is_read_from_the_server_once():
    """履歴を捨てる判定に使う文脈長。往復のたびに聞き直さない。"""
    with llama_server(restore_ok=True, read=1) as server:
        provider = OpenAICompatProvider(base_url=f"http://127.0.0.1:{server.server_address[1]}/v1")
        sizes = [provider.context_size(), provider.context_size()]

    assert sizes == [10240, 10240]
    assert server.calls == ["GET /props"]


def test_an_unknown_context_size_is_none_not_a_failure():
    """`/props` を持たない相手(他のOpenAI互換・居ないサーバ)では、分からないと答えるだけ。"""
    assert OpenAICompatProvider(base_url="http://127.0.0.1:9/v1").context_size() is None


class NotedProvider(Provider):
    name = "noted"

    def __init__(self, failure: Exception | None = None) -> None:
        self.model = "noted"
        self.failure = failure
        self.prepared: list[dict] = []

    def prepare(self, *, system, tools, timeout) -> dict:
        self.prepared.append({"system": system, "tools": list(tools)})
        if self.failure:
            raise self.failure
        return {"read": 1}


def test_the_loop_warms_with_exactly_what_run_will_send():
    surface = DocumentTools(Session(0.2))
    provider = NotedProvider()
    loop = AgentLoop(surface, provider)

    assert loop.prepare() == {"read": 1}
    assert provider.prepared == [{"system": catalog.agent_instructions(), "tools": surface.descriptors()}]
    assert loop.turns == [], "ウォームアップが会話に何かを足している"


def test_a_failed_warm_up_is_reported_not_raised():
    """相手がまだ居ないのは事故ではない(最初の依頼がそれを言う)。"""
    loop = AgentLoop(DocumentTools(Session(0.2)), NotedProvider(ProviderError("接続できない")))

    assert loop.prepare() == {"error": "接続できない"}
