"""ストリーミングで受ける契約: 組み立てた応答は非streamingと同じ形になり、待ち時間切れと中断は
**接続を閉じる**。

落ちたら、打ち切ったあともサーバが誰も読まない応答を生成し続け、次の依頼がその後ろで待つ
(実測は docs/AI_INTEGRATION.md「llama-server の機構の実測」)。本物のHTTPで確かめる ——
接続が閉じたことは、相手側(ここで立てる小さなサーバ)からしか見えない。
"""
from __future__ import annotations

import functools
import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from duocad.agent import AgentLoop, Cancellation, OpenAICompatProvider, ProviderError, ProviderTimeout
from duocad.agent.providers import Provider, ProviderContextOverflow, UserTurn
from duocad.mcp_server import DocumentTools
from duocad.server import Session

#: 断片を送り終えたあと、接続が閉じられるまで送り続ける印(SSEのコメント行。クライアントは読み飛ばす)。
HANG = object()


def sse(chunk: dict) -> bytes:
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")


@contextmanager
def serving(script, *, status: int = 200, content_type: str = "text/event-stream"):
    """`script` の順に本文を返すサーバ。`received` に届いた本文、`gone` に切断を残す。"""
    received: list[dict] = []
    first_sent = threading.Event()
    gone = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:
            pass

        def do_POST(self) -> None:
            length = int(self.headers.get("content-length") or 0)
            received.append(json.loads(self.rfile.read(length) or b"{}"))
            self.send_response(status)
            self.send_header("content-type", content_type)
            self.end_headers()
            try:
                for item in script:
                    if item is HANG:
                        while True:
                            self.wfile.write(b": ping\n\n")
                            self.wfile.flush()
                            time.sleep(0.02)
                    self.wfile.write(item)
                    self.wfile.flush()
                    first_sent.set()
            except OSError:
                gone.set()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=functools.partial(server.serve_forever, poll_interval=0.01), daemon=True)
    thread.start()
    try:
        server.received, server.first_sent, server.gone = received, first_sent, gone
        yield server
    finally:
        server.shutdown()
        server.server_close()


def provider_for(server) -> OpenAICompatProvider:
    return OpenAICompatProvider(base_url=f"http://127.0.0.1:{server.server_address[1]}/v1")


def ask(provider: OpenAICompatProvider, timeout: float):
    return provider.complete(system="s", turns=[UserTurn("x")], tools=[], max_tokens=16, timeout=timeout)


TOOL_CALL_IN_PIECES = [
    sse({"choices": [{"delta": {"role": "assistant", "content": None}}]}),
    sse({"choices": [{"delta": {"reasoning_content": "考え中"}}]}),
    sse({"choices": [{"delta": {"content": "調べ"}}]}),
    sse({"choices": [{"delta": {"content": "る"}}]}),
    sse({"choices": [{"delta": {"tool_calls": [
        {"index": 0, "id": "call_a", "type": "function", "function": {"name": "describe_shape", "arguments": '{"body'}}
    ]}}]}),
    sse({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '_id": "b1"}'}}]}}]}),
    sse({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}),
    sse({"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 7},
         "timings": {"cache_n": 90, "prompt_n": 10, "prompt_ms": 5.0, "predicted_n": 7, "predicted_ms": 70.0}}),
    b"data: [DONE]\n\n",
]


def test_a_streamed_reply_is_assembled_into_the_shape_the_parser_already_reads():
    """断片で届く引数が1つのJSONへ繋がり、usage と timings が最後の断片から拾われること。"""
    with serving(TOOL_CALL_IN_PIECES) as server:
        reply = ask(provider_for(server), 5.0)

    assert server.received[0]["stream"] is True
    assert reply.text == "調べる"
    assert reply.stop == "tool_use"
    assert [(call.id, call.name, call.arguments) for call in reply.tool_calls] == [
        ("call_a", "describe_shape", {"body_id": "b1"})
    ]
    assert reply.usage["prompt_tokens"] == 100
    assert reply.usage["cache_n"] == 90 and reply.usage["predicted_n"] == 7


def test_a_reply_cut_before_done_is_a_failure_not_a_short_answer():
    """[DONE] の前に切れた応答を、短い答えとして受け取らない。"""
    with serving(TOOL_CALL_IN_PIECES[:3]) as server:
        with pytest.raises(ProviderError) as caught:
            ask(provider_for(server), 5.0)
    assert "途中で切れた" in str(caught.value)


def test_running_out_of_time_closes_the_connection():
    """`timeout` は応答全体の上限。断片が届き続けていても切れ、**サーバ側から切断が見える**。"""
    with serving([TOOL_CALL_IN_PIECES[2], HANG]) as server:
        started = time.monotonic()
        with pytest.raises(ProviderTimeout):
            ask(provider_for(server), 0.3)
        elapsed = time.monotonic() - started
        assert server.gone.wait(3.0), "待ち時間切れのあとも接続が開いたまま(サーバは生成を続ける)"
    assert elapsed < 2.0, f"締切を過ぎても待っている({elapsed:.2f}s)"


def test_abort_from_another_thread_closes_the_connection():
    """中断は別スレッドから届く。読み取りを待っている最中でも、すぐ戻ること。"""
    with serving([TOOL_CALL_IN_PIECES[2], HANG]) as server:
        provider = provider_for(server)
        outcome: list = []

        def run() -> None:
            try:
                ask(provider, 30.0)
            except ProviderError as error:
                outcome.append(error)

        thread = threading.Thread(target=run)
        thread.start()
        assert server.first_sent.wait(3.0)
        provider.abort()
        thread.join(3.0)

        assert not thread.is_alive(), "abort しても読み取りが戻らない"
        assert server.gone.wait(3.0), "abort のあとも接続が開いたまま"
    assert outcome and not isinstance(outcome[0], ProviderTimeout)


def test_a_server_that_answers_in_one_body_still_works():
    """ストリーミングに応じないOpenAI互換サーバ(本文1つで返す)でも読めること。"""
    body = json.dumps({"choices": [{"message": {"content": "はい"}, "finish_reason": "stop"}]}).encode()
    with serving([body], content_type="application/json") as server:
        reply = ask(provider_for(server), 5.0)
    assert reply.text == "はい" and reply.stop == "end_turn"


def test_an_http_failure_keeps_its_status():
    with serving([b'{"error": {"message": "overloaded"}}'], status=503, content_type="application/json") as server:
        with pytest.raises(ProviderError) as caught:
            ask(provider_for(server), 5.0)
    assert caught.value.status == 503 and "overloaded" in str(caught.value)


def test_a_request_too_long_for_the_context_is_its_own_kind_of_failure():
    """llama-server が実際に返す本文(2026-10-04)。障害と区別できないと、履歴を捨ててやり直せない。"""
    body = (
        b'{"error":{"code":400,"message":"request (10389 tokens) exceeds the available context size '
        b'(10240 tokens), try increasing it","type":"exceed_context_size_error",'
        b'"n_prompt_tokens":10389,"n_ctx":10240}}'
    )
    with serving([body], status=400, content_type="application/json") as server:
        with pytest.raises(ProviderContextOverflow):
            ask(provider_for(server), 5.0)


class BlockingProvider(Provider):
    """`abort` されるまで返らないプロバイダ(往復の最中に中断が届く場面を作る)。"""

    name = "blocking"

    def __init__(self) -> None:
        self.model = "blocking"
        self.entered = threading.Event()
        self.aborted = threading.Event()

    def abort(self) -> None:
        self.aborted.set()

    def complete(self, *, system, turns, tools, max_tokens, timeout):
        self.entered.set()
        assert self.aborted.wait(5.0), "中断がプロバイダへ届かなかった"
        raise ProviderError("中断したので接続を閉じた")


def test_cancelling_in_the_middle_of_a_request_ends_the_run_as_cancelled():
    """往復の最中の中断が、接続を閉じたうえで `cancelled` として返ること(`provider_error` ではない)。"""
    provider = BlockingProvider()
    cancellation = Cancellation()
    loop = AgentLoop(DocumentTools(Session(None, 0.2)), provider, cancellation=cancellation)
    threading.Thread(target=lambda: (provider.entered.wait(5.0), cancellation.cancel())).start()

    run = loop.run("長い話をして")

    assert run.status == "cancelled"
    assert provider.aborted.is_set()
