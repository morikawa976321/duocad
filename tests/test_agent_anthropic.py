"""Claude API(`anthropic`)のアダプタの契約: 整形・ストリーミングの組み立て・再試行・キーの読み口。

落ちたら、Claudeとの往復が壊れている(思考ブロックを送り返せず次の往復が400になる、`message_stop` の
あとも読み続けて「途中で切れた」を名乗る、一時的な混雑で依頼が落ちる、キーが外へ出る、のどれか)。
実ネットワークは叩かない(受け方の検査はここで立てる小さなサーバ、他は偽transport)。
"""
from __future__ import annotations

import http.client
import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from duocad.agent import AgentLoop, AnthropicProvider, ProviderError
from duocad.agent import providers
from duocad.agent.loop import FIRST_LOOK
from duocad.agent.providers import (
    MAX_RETRIES,
    AssistantTurn,
    ProviderContextOverflow,
    ToolCall,
    ToolResult,
    ToolResultTurn,
    UserTurn,
    api_key_status,
    build_provider,
    delete_api_key,
    load_api_key,
    save_api_key,
)
from duocad.mcp_server import DocumentTools
from duocad.server import Server, Session
from agent_support import FAKE_KEY
from test_agent_loop import FakeTransport
from test_agent_streaming import HANG, serving

TOOLS = [{"name": "describe_document", "description": "文書を見る", "inputSchema": {"type": "object"}}]

#: 思考ブロック(既定では本文が空で、署名だけが来る)。
THINKING = {"type": "thinking", "thinking": "", "signature": "sig-abc"}


def claude(transport) -> AnthropicProvider:
    return AnthropicProvider(FAKE_KEY, transport=transport)


def ask(provider: AnthropicProvider, turns=None, *, timeout: float = 5.0, tools=TOOLS):
    return provider.complete(
        system="sys", turns=turns or [UserTurn("x")], tools=tools, max_tokens=64, timeout=timeout
    )


def answer(text: str = "はい") -> dict:
    return {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn"}


def busy(status: int = 529, retry_after: float | None = 0.0) -> ProviderError:
    return ProviderError(f"HTTP {status}: overloaded", status=status, retry_after=retry_after)


# --------------------------------------------------------------------------------------
# 整形
# --------------------------------------------------------------------------------------
def test_the_request_is_in_claude_shape_and_caches_the_fixed_prefix():
    transport = FakeTransport([answer()])
    ask(claude(transport))
    sent = transport.sent[0]
    payload = sent["payload"]

    assert sent["url"] == "https://api.anthropic.com/v1/messages"
    assert payload["model"] == "claude-haiku-5-5"
    assert payload["stream"] is True
    assert payload["tools"] == [
        {"name": "describe_document", "description": "文書を見る", "input_schema": {"type": "object"}}
    ]
    # system の末尾と、伸びていく会話の末尾(自動)の2か所。
    assert payload["system"] == [{"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}]
    assert payload["cache_control"] == {"type": "ephemeral"}
    # 現行モデルが400を返すもの・既定のままにしておくもの。
    for absent in ("temperature", "top_p", "top_k", "thinking"):
        assert absent not in payload


def test_raw_assistant_content_goes_back_byte_for_byte_and_tool_results_share_one_message():
    """**思考ブロックは本文が空でもそのまま送り返す**。ツール結果は1つのuserメッセージにまとめる。"""
    raw = [THINKING, {"type": "tool_use", "id": "t1", "name": "describe_document", "input": {}}]
    turns = [
        UserTurn("見て"),
        AssistantTurn(text="", tool_calls=(ToolCall("t1", "describe_document"),), raw=raw),
        ToolResultTurn(
            (ToolResult("t1", "describe_document", "{}", True), ToolResult("t2", "x", "失敗", False))
        ),
    ]
    transport = FakeTransport([answer()])
    ask(claude(transport), turns)
    messages = transport.sent[0]["payload"]["messages"]

    assert messages[1] == {"role": "assistant", "content": raw}
    assert messages[2]["role"] == "user"
    assert [block["is_error"] for block in messages[2]["content"]] == [False, True]


def test_a_turn_without_raw_content_is_rebuilt_with_its_tool_calls():
    """ループが代わりに呼んだ問い合わせ(生の本文が無い)は、tool_useブロックを落とさずに組み直す。"""
    turns = [
        UserTurn("見て"),
        AssistantTurn(text="", tool_calls=(ToolCall("first0", "describe_document"),)),
        ToolResultTurn((ToolResult("first0", "describe_document", "{}", True),)),
    ]
    transport = FakeTransport([answer()])
    ask(claude(transport), turns)
    assert transport.sent[0]["payload"]["messages"][1]["content"] == [
        {"type": "tool_use", "id": "first0", "name": "describe_document", "input": {}}
    ]


def test_a_loop_round_trip_sends_the_thinking_back_after_the_tool_result(box_session: Session):
    """ループを1周回す: 思考+ツール呼び出し → 結果を返す → 答える。2回目の要求に1回目の生の本文が載る。"""
    first = {
        "content": [THINKING, {"type": "tool_use", "id": "t1", "name": "get_selection", "input": {}}],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    transport = FakeTransport([first, answer("選択は空")])
    loop = AgentLoop(DocumentTools(box_session), claude(transport), first_look=FIRST_LOOK)
    run = loop.run("選択を教えて")

    assert run.status == "completed", run.detail
    second = transport.sent[1]["payload"]["messages"]
    assert second[-2] == {"role": "assistant", "content": first["content"]}
    assert second[-1]["content"][0]["tool_use_id"] == "t1"


# --------------------------------------------------------------------------------------
# ストリーミングの組み立て(本物のHTTP)
# --------------------------------------------------------------------------------------
def event(payload: dict) -> bytes:
    return f"event: {payload['type']}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


def streamed_tool_call() -> list:
    return [
        event({"type": "message_start", "message": {"id": "m1", "role": "assistant", "content": [],
                                                       "usage": {"input_tokens": 120, "cache_read_input_tokens": 100,
                                                                 "output_tokens": 1}}}),
        event({"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}}),
        event({"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig-abc"}}),
        event({"type": "content_block_stop", "index": 0}),
        event({"type": "ping"}),
        event({"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}}),
        event({"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "選択を"}}),
        event({"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "見る"}}),
        event({"type": "content_block_stop", "index": 1}),
        event({"type": "content_block_start", "index": 2,
               "content_block": {"type": "tool_use", "id": "t1", "name": "fillet", "input": {}}}),
        event({"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": '{"radi'}}),
        event({"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": 'us": 3}'}}),
        event({"type": "content_block_stop", "index": 2}),
        event({"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 42}}),
        event({"type": "message_stop"}),
    ]


def provider_for(server) -> AnthropicProvider:
    return AnthropicProvider(FAKE_KEY, base_url=f"http://127.0.0.1:{server.server_address[1]}")


def test_the_stream_is_folded_and_reading_stops_at_message_stop():
    """`message_stop` のあとも相手が接続を開けたままでも、そこで返る(EOFを待たない)。"""
    with serving(streamed_tool_call() + [HANG]) as server:
        started = time.monotonic()
        reply = ask(provider_for(server), timeout=5.0)
        elapsed = time.monotonic() - started

    assert elapsed < 2.0, "message_stop で読むのをやめていない"
    assert reply.text == "選択を見る"
    assert reply.stop == "tool_use"
    assert reply.tool_calls == (ToolCall("t1", "fillet", {"radius": 3}),)
    assert reply.raw_message[0] == {"type": "thinking", "thinking": "", "signature": "sig-abc"}
    # 入力は message_start、出力は message_delta の値(後者で上書き)。
    assert reply.usage["input_tokens"] == 120
    assert reply.usage["cache_read_input_tokens"] == 100
    assert reply.usage["output_tokens"] == 42
    assert server.received[0]["stream"] is True


def test_an_error_event_mid_stream_fails_the_request_without_a_retry():
    script = [
        event({"type": "message_start", "message": {"id": "m1", "content": [], "usage": {}}}),
        event({"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}),
    ]
    with serving(script) as server:
        with pytest.raises(ProviderError) as caught:
            ask(provider_for(server))
    assert "overloaded_error" in str(caught.value)
    assert len(server.received) == 1


@contextmanager
def answering(responses):
    """`(status, headers, body)` を届いた順に1つずつ返すサーバ。`received` に届いた本文を残す。"""
    received: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:
            pass

        def do_POST(self) -> None:
            length = int(self.headers.get("content-length") or 0)
            received.append(json.loads(self.rfile.read(length) or b"{}"))
            status, headers, body = responses.pop(0)
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        server.received = received
        yield server
    finally:
        server.shutdown()
        server.server_close()


def test_a_429_is_retried_after_the_wait_the_server_asked_for():
    """本物のHTTPで1周: 429(`retry-after: 0`) → ストリーミングの200。"""
    responses = [
        (429, {"content-type": "application/json", "retry-after": "0"},
         b'{"type":"error","error":{"type":"rate_limit_error","message":"slow down"}}'),
        (200, {"content-type": "text/event-stream"}, b"".join(streamed_tool_call())),
    ]
    with answering(responses) as server:
        reply = ask(provider_for(server))
    assert reply.stop == "tool_use"
    assert len(server.received) == 2


def test_a_prompt_too_long_is_a_context_overflow():
    """Claudeの文脈あふれはループが古い履歴を捨ててやり直せる型で返る。"""
    error = providers._failure("HTTP 400", "prompt is too long: 1000001 tokens > 1000000 maximum", 400)
    assert isinstance(error, ProviderContextOverflow)


# --------------------------------------------------------------------------------------
# 再試行
# --------------------------------------------------------------------------------------
def test_an_overloaded_response_is_retried():
    transport = FakeTransport([busy(529), answer("通った")])
    assert ask(claude(transport)).text == "通った"
    assert len(transport.sent) == 2


def test_retries_stop_after_the_limit():
    transport = FakeTransport([busy(429)] * (MAX_RETRIES + 2))
    with pytest.raises(ProviderError):
        ask(claude(transport))
    assert len(transport.sent) == MAX_RETRIES + 1


def test_client_errors_and_timeouts_are_not_retried():
    transport = FakeTransport([ProviderError("HTTP 400", status=400)])
    with pytest.raises(ProviderError):
        ask(claude(transport))
    assert len(transport.sent) == 1


def test_a_wait_that_does_not_fit_the_time_left_is_not_taken():
    """合計時間の残りに収まらない待ちはしない(待つより、失敗をすぐ画面へ返す)。"""
    transport = FakeTransport([busy(529, retry_after=5.0), answer()])
    started = time.monotonic()
    with pytest.raises(ProviderError):
        ask(claude(transport), timeout=1.0)
    assert time.monotonic() - started < 0.5
    assert len(transport.sent) == 1


def test_an_abort_that_lands_before_sending_stops_the_request():
    """ループが中断を確かめた後・送る前に届いた中断を、往復の頭で消さない。"""
    transport = FakeTransport([answer()])
    provider = claude(transport)
    provider.abort()
    with pytest.raises(ProviderError):
        ask(provider)
    assert transport.sent == [], "中断したのに送った"


def test_an_abort_that_lands_while_connecting_is_never_sent(monkeypatch):
    """往復の頭の確認を過ぎてから、ソケットを控える前に届いた中断(本物のHTTP)。

    落ちたら、中断した依頼が相手へ送られて課金される(応答を捨てても請求は消えない)。
    """
    provider = None

    class AbortedWhileConnecting(http.client.HTTPConnection):
        def __init__(self, *args, **kwargs):
            provider.abort()
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(http.client, "HTTPConnection", AbortedWhileConnecting)
    with serving(streamed_tool_call()) as server:
        provider = provider_for(server)
        with pytest.raises(ProviderError):
            ask(provider)
    assert server.received == [], "中断したのに送った"


def test_an_abort_that_lands_while_connecting_discards_the_reply():
    """`StreamingTransport` が自分の印を下ろした直後の中断は接続を閉じられない。応答のほうを捨てる。"""
    provider = None

    def aborted_midway(url, payload, headers, timeout):
        provider.abort()
        return answer()

    provider = claude(aborted_midway)
    with pytest.raises(ProviderError):
        ask(provider)


def test_the_next_request_is_not_cancelled_by_the_previous_abort(box_session: Session):
    transport = FakeTransport([answer("次の依頼")])
    provider = claude(transport)
    provider.abort()  # 前の依頼の中断
    run = AgentLoop(DocumentTools(box_session), provider).run("続けて")
    assert run.status == "completed", run.detail


def test_abort_cuts_a_retry_wait_short():
    transport = FakeTransport([busy(529, retry_after=30.0), answer()])
    provider = claude(transport)
    threading.Timer(0.1, provider.abort).start()
    started = time.monotonic()
    with pytest.raises(ProviderError):
        ask(provider, timeout=60.0)
    assert time.monotonic() - started < 2.0
    assert len(transport.sent) == 1


# --------------------------------------------------------------------------------------
# キーとモデル
# --------------------------------------------------------------------------------------
def test_the_key_is_read_from_the_environment_first_then_the_secrets_file(monkeypatch, tmp_path):
    secrets = tmp_path / "duocad_secrets.json"
    monkeypatch.setattr(providers, "SECRETS_PATH", secrets)
    assert load_api_key() is None

    secrets.write_text(json.dumps({providers.ANTHROPIC_API_KEY_ENV: "from-file"}), encoding="utf-8")
    assert load_api_key() == "from-file"

    monkeypatch.setenv(providers.ANTHROPIC_API_KEY_ENV, "from-env")
    assert load_api_key() == "from-env"

    monkeypatch.delenv(providers.ANTHROPIC_API_KEY_ENV)
    secrets.write_text("{壊れている", encoding="utf-8")
    assert load_api_key() is None


def test_the_secrets_file_is_read_whatever_a_windows_editor_saved_it_as(monkeypatch, tmp_path):
    """PowerShell 5.1 の `>` は UTF-16LE、`Set-Content -Encoding utf8` はBOM付きUTF-8で書く。"""
    secrets = tmp_path / "duocad_secrets.json"
    monkeypatch.setattr(providers, "SECRETS_PATH", secrets)
    body = json.dumps({providers.ANTHROPIC_API_KEY_ENV: "sk-ant-x"})
    for encoding in ("utf-16", "utf-8-sig", "utf-8"):
        secrets.write_bytes(body.encode(encoding))
        assert load_api_key() == "sk-ant-x", encoding


def test_an_unreadable_secrets_file_does_not_stop_the_server(monkeypatch, tmp_path, box_session: Session):
    """読めないファイルは「未設定」扱い。例外にするとClaudeを使わない人のサーバまで立たなくなる。"""
    secrets = tmp_path / "duocad_secrets.json"
    monkeypatch.setattr(providers, "SECRETS_PATH", secrets)
    secrets.write_bytes(b"\x80\x81\x82 not utf-8")

    assert load_api_key() is None
    assert Server(box_session)._ai_provider_kind == "openai_compat"
    with pytest.raises(ProviderError) as caught:
        build_provider("anthropic")
    assert "読めなかった" in str(caught.value)


def test_saving_and_deleting_the_key_keeps_other_fields(monkeypatch, tmp_path):
    """画面から書くキーは同じファイルへ入り、他の欄は残す。消して空になればファイルごと消す。"""
    secrets = tmp_path / "duocad_secrets.json"
    monkeypatch.setattr(providers, "SECRETS_PATH", secrets)

    save_api_key("sk-ant-first")
    assert load_api_key() == "sk-ant-first"
    assert api_key_status() == {"configured": True, "source": "file", "hint": "irst"}

    secrets.write_text(json.dumps({"OTHER": "x", providers.ANTHROPIC_API_KEY_ENV: "sk-ant-old"}), encoding="utf-8")
    save_api_key("sk-ant-second")
    assert json.loads(secrets.read_text(encoding="utf-8")) == {
        "OTHER": "x", providers.ANTHROPIC_API_KEY_ENV: "sk-ant-second"
    }
    delete_api_key()
    assert json.loads(secrets.read_text(encoding="utf-8")) == {"OTHER": "x"}

    secrets.unlink()
    save_api_key("sk-ant-third")
    delete_api_key()
    assert not secrets.exists()
    assert api_key_status() == {"configured": False, "source": None, "hint": None}
    with pytest.raises(ValueError):
        save_api_key("   ")


def test_a_failed_replace_leaves_no_staging_file_with_the_key(monkeypatch, tmp_path):
    """置き換えに失敗しても、キーの入った `duocad_secrets.json.tmp` を残さない。"""
    secrets = tmp_path / "duocad_secrets.json"
    monkeypatch.setattr(providers, "SECRETS_PATH", secrets)

    def locked(*_args):
        raise PermissionError("locked")

    monkeypatch.setattr(providers.os, "replace", locked)
    with pytest.raises(PermissionError):
        save_api_key("sk-ant-staged")
    assert list(tmp_path.iterdir()) == []


def test_an_unreadable_secrets_file_is_not_overwritten(monkeypatch, tmp_path):
    """読めないファイルを空とみなして書き戻すと、読めなかった欄を消す。書き換えずに断る。"""
    secrets = tmp_path / "duocad_secrets.json"
    monkeypatch.setattr(providers, "SECRETS_PATH", secrets)
    secrets.write_text('{"OTHER": "x", 壊れている', encoding="utf-8")

    for change in (lambda: save_api_key("sk-ant-new"), delete_api_key):
        with pytest.raises(ValueError):
            change()
        assert secrets.read_text(encoding="utf-8") == '{"OTHER": "x", 壊れている'


def test_a_key_broken_by_a_newline_is_refused_and_never_echoed(monkeypatch):
    """HTTPライブラリは改行入りのヘッダ値を `b'...\\n...'` の形で例外文へ載せる。その形でも消す。"""
    broken = "sk-ant-abc\ndef"
    leaking = FakeTransport([ValueError(f"Invalid header value {broken.encode()!r}")])
    with pytest.raises(ProviderError) as caught:
        ask(AnthropicProvider(broken, transport=leaking))
    assert "abc\\ndef" not in str(caught.value)
    assert "***" in str(caught.value)

    monkeypatch.setenv(providers.ANTHROPIC_API_KEY_ENV, broken)
    with pytest.raises(ProviderError) as refused:
        build_provider("anthropic")
    assert "abc" not in str(refused.value)


def test_the_key_never_shows_up_in_errors_even_after_retries():
    leaking = FakeTransport([busy(529), ValueError(f"Invalid header value {FAKE_KEY!r}")])
    provider = claude(leaking)
    assert FAKE_KEY not in repr(provider)
    with pytest.raises(ProviderError) as caught:
        ask(provider)
    assert FAKE_KEY not in str(caught.value)
    assert "***" in str(caught.value)


def test_the_model_can_be_swapped_by_environment_variable(monkeypatch):
    monkeypatch.setenv(providers.ANTHROPIC_API_KEY_ENV, FAKE_KEY)
    assert build_provider("anthropic").model == "claude-haiku-5-5"
    monkeypatch.setenv(providers.ANTHROPIC_MODEL_ENV, "claude-sonnet-5-5")
    assert build_provider("anthropic").model == "claude-sonnet-5-5"
    assert build_provider("anthropic", model="claude-opus-5-5").model == "claude-opus-5-5"


def test_build_provider_never_points_claude_elsewhere(monkeypatch):
    monkeypatch.setenv(providers.ANTHROPIC_API_KEY_ENV, FAKE_KEY)
    provider = build_provider("anthropic", base_url="http://attacker.example")
    assert provider.base_url == "https://api.anthropic.com"
