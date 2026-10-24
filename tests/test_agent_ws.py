"""アプリ内AIエージェントをWebSocketへ配線する契約(`duocad/server.py` の `ai_*`)。"""
from __future__ import annotations

import asyncio
import json
import threading
import time

from duocad.mcp_server import DocumentTools
from duocad.server import Server, Session
from duocad.net.ws import AI_LIMITS
from duocad.agent.providers import (
    ANTHROPIC_API_KEY_ENV,
    ANTHROPIC_MAX_TOKENS,
    DEFAULT_ANTHROPIC_MODEL,
    load_api_key,
    save_api_key,
)
from agent_support import FAKE_KEY, RecordedProvider, spoke, tool_use
from session_support import FakeConnection

#: 本物ではない(`tests/test_agent_loop.py` と同じ形。このリポジトリに実キーは置かない)。


def build_server(session: Session, loop: asyncio.AbstractEventLoop) -> Server:
    server = Server(session)
    server.bind_loop(loop)
    server.bind_agent_tools(DocumentTools(session))
    return server


async def _wait_until(predicate, *, timeout: float = 3.0, interval: float = 0.01) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("待っていた条件が時間内に成立しなかった")
        await asyncio.sleep(interval)


async def _settle(server: Server) -> None:
    """走っているAIの実行と、その知らせを運ぶ配信タスクが終わるのを待つ。"""
    async with asyncio.timeout(10):
        while server._pending_ai_runs or server._pending_pushes:
            await asyncio.gather(*server._pending_ai_runs, *server._pending_pushes, return_exceptions=True)
            await asyncio.sleep(0)


def test_ai_status_carries_no_key_of_any_kind(box_session: Session):
    """キーが無ければ llama-server から始まり、置いてあるかの真偽だけが載る。"""

    async def scenario():
        server = build_server(box_session, asyncio.get_running_loop())
        connection = FakeConnection()
        await server._dispatch(connection, json.dumps({"cmd": "ai_get_status"}))
        return connection.payloads

    payloads = asyncio.run(scenario())
    assert len(payloads) == 1
    status = payloads[0]
    assert status["type"] == "ai_status"
    assert status["api_key_configured"] is False
    assert status["provider"] == "openai_compat"
    assert status["running"] is False
    assert set(status["providers"]) == {"openai_compat", "anthropic"}


def test_ai_configure_switches_provider_and_model(box_session: Session):
    async def scenario():
        server = build_server(box_session, asyncio.get_running_loop())
        a, b = FakeConnection(), FakeConnection()
        server.clients.add(a)
        server.clients.add(b)
        await server._dispatch(
            a,
            json.dumps(
                {"cmd": "ai_configure", "provider": "anthropic", "model": "claude-sonnet-5-5"}
            ),
        )
        return a, b

    a, b = asyncio.run(scenario())
    # **設定は全接続へ配る**(共有の1会話なので、他の画面でも今の設定が見える)。既定(openai_compat)と別のプロバイダへ切り替える。
    for connection in (a, b):
        status = connection.of_type("ai_status")[-1]
        assert status["provider"] == "anthropic"
        assert status["model"] == "claude-sonnet-5-5"


def test_ai_configure_rejects_an_unknown_provider(box_session: Session):
    async def scenario():
        server = build_server(box_session, asyncio.get_running_loop())
        connection = FakeConnection()
        server.clients.add(connection)
        before = server._ai_provider_kind
        await server._dispatch(
            connection, json.dumps({"cmd": "ai_configure", "provider": "azure"})
        )
        return connection, before

    connection, before = asyncio.run(scenario())
    status = connection.of_type("ai_status")[-1]
    assert "error" in status
    assert status["provider"] == before, "拒否したのに設定が変わっている"


def test_ai_configure_is_rejected_while_running(box_session: Session):
    """実行中は設定を変えさせない(理由: 中断フラグを作り直すと中断ボタンが効かなくなる。
    `Server._ai_configure` のdocstring)。
    """

    async def scenario():
        server = build_server(box_session, asyncio.get_running_loop())
        server._ai_running = True
        status = server._ai_configure(None, "qwen3-30b", "http://127.0.0.1:9999/v1")
        return server, status

    server, status = asyncio.run(scenario())
    assert "error" in status
    assert server._ai_model is None, "実行中なのにモデルが変わっている"
    assert server._ai_base_url is None, "実行中なのにエンドポイントが変わっている"


def test_ai_send_returns_before_the_run_finishes(monkeypatch, box_session: Session):
    """`ai_send` の `_dispatch` 自体は**モデルの応答を待たずに**返る(スロー・プロバイダで確認)。"""
    monkeypatch.setattr(
        "duocad.server.build_provider",
        lambda *a, **k: RecordedProvider([spoke("done")], delay=0.3),
    )

    async def scenario():
        server = build_server(box_session, asyncio.get_running_loop())
        connection = FakeConnection()
        started = time.monotonic()
        await server._dispatch(connection, json.dumps({"cmd": "ai_send", "message": "hi"}))
        elapsed = time.monotonic() - started
        await _settle(server)
        return elapsed

    elapsed = asyncio.run(scenario())
    assert elapsed < 0.3, f"_dispatch がモデルの応答を待ってしまっている({elapsed:.3f}s)"


def test_ai_send_streams_tool_progress_and_a_final_result(monkeypatch, box_session: Session):
    monkeypatch.setattr(
        "duocad.server.build_provider",
        lambda *a, **k: RecordedProvider(
            [tool_use("describe_document", {}), spoke("ボディが1つあります")]
        ),
    )

    async def scenario():
        server = build_server(box_session, asyncio.get_running_loop())
        watcher = FakeConnection()
        server.clients.add(watcher)
        await server._dispatch(watcher, json.dumps({"cmd": "ai_send", "message": "教えて"}))
        # `ai_result` の直後にもう1つ `ai_status(running=False)` が飛ぶ
        # (`Server._ai_start` の `finished`)。**両方**が届くまで待つ —— 片方だけで
        # `scenario()` を抜けると、ループを閉じる瞬間にもう片方の配信タスクが
        # 「作ったのに走らせていない」まま捨てられる(実害は無いが警告がうるさい)。
        await _wait_until(
            lambda: watcher.of_type("ai_tool_call")
            and watcher.of_type("ai_result")
            and any(s["running"] is False for s in watcher.of_type("ai_status"))
        )
        await _settle(server)
        return watcher

    watcher = asyncio.run(scenario())
    tool_calls = watcher.of_type("ai_tool_call")
    assert len(tool_calls) == 1
    assert tool_calls[0]["name"] == "describe_document"
    assert tool_calls[0]["ok"] is True

    result = watcher.of_type("ai_result")[-1]
    assert result["status"] == "completed"
    assert result["text"] == "ボディが1つあります"
    assert [c["name"] for c in result["tool_calls"]] == ["describe_document"]

    # running=True → running=False の両方が届く(UIが中断ボタンの出し入れに使う)。
    statuses = [status["running"] for status in watcher.of_type("ai_status")]
    assert statuses[0] is True
    assert statuses[-1] is False


def test_ai_send_hands_the_model_the_document_it_is_about_to_work_on(monkeypatch, box_session: Session):
    """アプリは文書を既に知っている。先に渡さないと、依頼のたびに `describe_document` の1往復を払う。"""
    provider = RecordedProvider([spoke("ボディが1つあります")])
    monkeypatch.setattr("duocad.server.build_provider", lambda *a, **k: provider)

    async def scenario():
        server = build_server(box_session, asyncio.get_running_loop())
        watcher = FakeConnection()
        server.clients.add(watcher)
        await server._dispatch(watcher, json.dumps({"cmd": "ai_send", "message": "教えて"}))
        await _wait_until(
            lambda: watcher.of_type("ai_result")
            and any(s["running"] is False for s in watcher.of_type("ai_status"))
        )
        await _settle(server)

    asyncio.run(scenario())
    asked, looked, found = provider.requests[0]["turns"]
    assert asked.text == "教えて"
    assert [call.name for call in looked.tool_calls] == ["describe_document", "get_selection"]
    assert '"body_id": "box"' in found.results[0].content


def test_ai_send_rejects_a_second_message_while_running(monkeypatch, box_session: Session):
    monkeypatch.setattr(
        "duocad.server.build_provider",
        lambda *a, **k: RecordedProvider([spoke("done")], delay=0.2),
    )

    async def scenario():
        server = build_server(box_session, asyncio.get_running_loop())
        first, second = FakeConnection(), FakeConnection()
        await server._dispatch(first, json.dumps({"cmd": "ai_send", "message": "1"}))
        assert server._ai_running is True
        await server._dispatch(second, json.dumps({"cmd": "ai_send", "message": "2"}))
        await _settle(server)
        return second

    second = asyncio.run(scenario())
    assert second.of_type("ai_error"), "実行中の2通目が拒否されていない"


def test_ai_send_rejects_an_empty_message(box_session: Session):
    async def scenario():
        server = build_server(box_session, asyncio.get_running_loop())
        connection = FakeConnection()
        await server._dispatch(connection, json.dumps({"cmd": "ai_send", "message": "   "}))
        return server, connection

    server, connection = asyncio.run(scenario())
    assert connection.of_type("ai_error")
    assert server._ai_running is False, "空メッセージなのに実行中フラグが立っている"


def test_a_tool_call_that_mutates_the_document_reaches_the_browser(
    monkeypatch, box_session: Session
):
    """`set_visible` をAI経由で呼ぶと、**通常のverb配信**(`type: state`)が別接続へ届く。

    `ToolsOnLoop` が実行器スレッドからイベントループへ投げ返していること
    (=`Session` への書き込みが実際に起きていること)の実測でもある。
    """
    box = box_session.document.bodies[0]
    monkeypatch.setattr(
        "duocad.server.build_provider",
        lambda *a, **k: RecordedProvider(
            [tool_use("set_visible", {"body_id": box.id, "visible": False}), spoke("隠した")]
        ),
    )

    async def scenario():
        server = build_server(box_session, asyncio.get_running_loop())
        browser = FakeConnection()
        server.clients.add(browser)
        await server._push(browser, applied=None, only=browser)  # 接続直後の絵
        before = len(browser.payloads)

        await server._dispatch(browser, json.dumps({"cmd": "ai_send", "message": "隠して"}))
        await _wait_until(lambda: any(p.get("type") == "state" for p in browser.payloads[before:]))
        await _wait_until(
            lambda: browser.of_type("ai_tool_call")
            and browser.of_type("ai_result")
            and any(s["running"] is False for s in browser.of_type("ai_status"))
        )
        await _settle(server)
        return browser, box_session

    browser, session = asyncio.run(scenario())
    assert session.document.bodies[0].visible is False
    states = browser.of_type("state")
    mutated = [s for s in states if s["state"]["bodies"][0]["visible"] is False]
    assert mutated, "AIの set_visible が文書に反映されたのに配信されていない"
    # **出し手のいない変更**(MCPと同じ扱い) —— `applied` を付けない。
    assert "applied" not in mutated[-1]


def test_ai_cancel_stops_the_run_and_keeps_what_happened_so_far(monkeypatch, box_session: Session):
    monkeypatch.setattr(
        "duocad.server.build_provider",
        lambda *a, **k: RecordedProvider(
            [
                tool_use("describe_document", {}, call_id="call_1"),
                tool_use("describe_document", {}, call_id="call_2"),
                spoke("最後まで到達したら失敗"),
            ],
            delay=0.08,
        ),
    )

    async def scenario():
        server = build_server(box_session, asyncio.get_running_loop())
        watcher = FakeConnection()
        server.clients.add(watcher)
        await server._dispatch(watcher, json.dumps({"cmd": "ai_send", "message": "続けて"}))

        await _wait_until(lambda: watcher.of_type("ai_tool_call"))
        await server._dispatch(watcher, json.dumps({"cmd": "ai_cancel"}))
        await _wait_until(
            lambda: watcher.of_type("ai_result")
            and any(s["running"] is False for s in watcher.of_type("ai_status"))
        )
        await _settle(server)
        return watcher

    watcher = asyncio.run(scenario())
    result = watcher.of_type("ai_result")[-1]
    assert result["status"] == "cancelled"
    # **どこまでやったかは残る**。
    assert len(result["tool_calls"]) >= 1
    assert len(result["tool_calls"]) < 2, "中断が効かず最後まで回ってしまった"


def test_ai_configure_allows_a_base_url_for_openai_compat(box_session: Session):
    """`openai_compat`(キーを持たない)は今までどおりエンドポイントを差し替えられる。"""

    async def scenario():
        server = build_server(box_session, asyncio.get_running_loop())
        connection = FakeConnection()
        server.clients.add(connection)
        await server._dispatch(
            connection,
            json.dumps(
                {
                    "cmd": "ai_configure",
                    "provider": "openai_compat",
                    "base_url": "http://127.0.0.1:8080/v1",
                }
            ),
        )
        return connection

    connection = asyncio.run(scenario())
    status = connection.of_type("ai_status")[-1]
    assert "error" not in status
    assert status["base_url"] == "http://127.0.0.1:8080/v1"


def test_a_tampered_base_url_still_sends_no_key(monkeypatch, box_session: Session):
    """**深層防御**: 送り先を内部状態ごと差し替えても、**リクエストに鍵は乗らない**。"""
    monkeypatch.setenv(ANTHROPIC_API_KEY_ENV, FAKE_KEY)
    calls: list[dict] = []

    def fake_transport(url, payload, headers, timeout):
        calls.append({"url": url, "headers": dict(headers), "payload": payload})
        return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}

    async def scenario():
        server = build_server(box_session, asyncio.get_running_loop())
        server._ai_configure("openai_compat", None, None)  # 鍵があるとClaudeから始まるので選び直す
        server._ai_base_url = "http://attacker.example/v1"
        loop_obj = server._ensure_ai_loop()
        loop_obj.provider._transport = fake_transport  # HTTPだけ差し替える
        return loop_obj.run("ping")

    run = asyncio.run(scenario())
    assert run.status == "completed"
    assert len(calls) == 1, "HTTPが1回も(または複数回)呼ばれた"
    assert calls[0]["url"] == "http://attacker.example/v1/chat/completions"
    # **鍵はどこにも乗っていない**(ヘッダにも本文にも)。
    assert "authorization" not in {key.lower() for key in calls[0]["headers"]}
    assert FAKE_KEY not in json.dumps(calls[0], ensure_ascii=False)


def test_with_a_key_the_server_starts_on_claude_and_shows_the_model_but_not_the_key(
    monkeypatch, box_session: Session
):
    monkeypatch.setenv(ANTHROPIC_API_KEY_ENV, FAKE_KEY)

    async def scenario():
        server = build_server(box_session, asyncio.get_running_loop())
        connection = FakeConnection()
        await server._dispatch(connection, json.dumps({"cmd": "ai_get_status"}))
        return connection.payloads[0]

    status = asyncio.run(scenario())
    assert status["provider"] == "anthropic"
    assert status["model"] == DEFAULT_ANTHROPIC_MODEL
    assert status["base_url"] is None
    assert status["api_key_configured"] is True
    assert FAKE_KEY not in json.dumps(status)


def test_ai_configure_refuses_a_base_url_for_claude(box_session: Session):
    """`anthropic` の送り先は選ばせない(**黙って無視せずエラーで返す**)。"""

    async def scenario():
        server = build_server(box_session, asyncio.get_running_loop())
        connection = FakeConnection()
        server.clients.add(connection)
        await server._dispatch(
            connection,
            json.dumps(
                {"cmd": "ai_configure", "provider": "anthropic", "base_url": "http://attacker.example"}
            ),
        )
        return server, connection

    server, connection = asyncio.run(scenario())
    status = connection.of_type("ai_status")[-1]
    assert "error" in status
    assert status["provider"] == "openai_compat", "拒否したのに切り替わっている"
    assert server._ai_base_url is None


def test_a_tampered_base_url_never_redirects_the_claude_key(monkeypatch, box_session: Session):
    """**深層防御**: 内部状態の `base_url` を書き換えても、Claudeへの要求は公式APIへしか飛ばない。"""
    monkeypatch.setenv(ANTHROPIC_API_KEY_ENV, FAKE_KEY)
    calls: list[dict] = []

    def fake_transport(url, payload, headers, timeout):
        calls.append({"url": url, "payload": payload})
        return {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}

    async def scenario():
        server = build_server(box_session, asyncio.get_running_loop())
        server._ai_base_url = "http://attacker.example/v1"
        loop_obj = server._ensure_ai_loop()
        loop_obj.provider._transport = fake_transport  # HTTPだけ差し替える
        return loop_obj.run("ping")

    run = asyncio.run(scenario())
    assert run.status == "completed"
    assert calls[0]["url"] == "https://api.anthropic.com/v1/messages"
    assert calls[0]["payload"]["max_tokens"] == ANTHROPIC_MAX_TOKENS


def _set_key(box_session: Session, key):
    """`ai_set_api_key` を1回送り、全接続へ配られた ai_status と、配られたもの全部を返す。"""

    async def scenario():
        server = build_server(box_session, asyncio.get_running_loop())
        sender, watcher = FakeConnection(), FakeConnection()
        server.clients.update({sender, watcher})
        await server._dispatch(sender, json.dumps({"cmd": "ai_set_api_key", "key": key}))
        return server, watcher

    server, watcher = asyncio.run(scenario())
    return server, watcher.of_type("ai_status")[-1], json.dumps(watcher.payloads, ensure_ascii=False)


def test_a_key_set_from_the_settings_is_saved_but_never_sent_back(box_session: Session):
    """設定ダイアログから置いたキーはファイルへ書かれ、画面へは末尾4文字しか戻らない。Claudeに切り替わる。"""
    server, status, everything = _set_key(box_session, f"  {FAKE_KEY}  ")

    assert "error" not in status
    assert load_api_key() == FAKE_KEY
    assert status["api_key_configured"] is True
    assert status["api_key_source"] == "file"
    assert status["api_key_hint"] == FAKE_KEY[-4:]
    assert status["provider"] == "anthropic"
    assert FAKE_KEY not in everything, "キーの値が画面へ配られた"


def test_saving_a_key_does_not_carry_the_local_model_name_over_to_claude(box_session: Session):
    """llama-server 向けに選んだモデル名を、キーを置いて切り替えた Claude へ渡さない。"""

    async def scenario():
        server = build_server(box_session, asyncio.get_running_loop())
        connection = FakeConnection()
        server.clients.add(connection)
        await server._dispatch(
            connection,
            json.dumps({"cmd": "ai_configure", "provider": "openai_compat", "model": "qwen3-30b"}),
        )
        await server._dispatch(connection, json.dumps({"cmd": "ai_set_api_key", "key": FAKE_KEY}))
        return connection.of_type("ai_status")[-1]

    status = asyncio.run(scenario())
    assert status["provider"] == "anthropic"
    assert status["model"] == DEFAULT_ANTHROPIC_MODEL


def test_a_broken_key_is_refused_without_echoing_it(box_session: Session):
    server, status, everything = _set_key(box_session, "sk-ant-abc" + chr(10) + "def")
    assert "改行" in status["error"]
    assert load_api_key() is None
    assert "abc" not in everything
    assert server._ai_provider_kind == "openai_compat", "断ったのに切り替わった"


def test_null_deletes_the_saved_key(box_session: Session):
    save_api_key(FAKE_KEY)
    server, status, _ = _set_key(box_session, None)
    assert "error" not in status
    assert status["api_key_configured"] is False
    assert load_api_key() is None


def test_an_environment_key_is_reported_as_taking_precedence(monkeypatch, box_session: Session):
    monkeypatch.setenv(ANTHROPIC_API_KEY_ENV, "sk-ant-from-env-wxyz")
    server, status, _ = _set_key(box_session, FAKE_KEY)
    assert status["api_key_source"] == "env"
    assert status["api_key_hint"] == "wxyz"


def test_the_key_cannot_change_while_the_ai_is_running(monkeypatch, box_session: Session):
    """走っている会話のプロバイダは古いキーを持っているので、実行中は断る(`ai_configure` と同じ)。"""

    async def scenario():
        server = build_server(box_session, asyncio.get_running_loop())
        server._ai_running = True
        connection = FakeConnection()
        server.clients.add(connection)
        await server._dispatch(connection, json.dumps({"cmd": "ai_set_api_key", "key": FAKE_KEY}))
        return connection.of_type("ai_status")[-1]

    status = asyncio.run(scenario())
    assert "応答中" in status["error"]
    assert load_api_key() is None


def test_last_client_disconnecting_cancels_a_running_ai_run(monkeypatch, box_session: Session):
    monkeypatch.setattr(
        "duocad.server.build_provider",
        lambda *a, **k: RecordedProvider([spoke("done")], delay=0.2),
    )

    async def scenario():
        server = build_server(box_session, asyncio.get_running_loop())
        first, second = FakeConnection(), FakeConnection()
        server.clients.add(first)
        server.clients.add(second)
        await server._dispatch(first, json.dumps({"cmd": "ai_send", "message": "hi"}))
        assert server._ai_running is True

        # 1つ目が切れても、もう1つ見ているので中断しない。
        server.clients.discard(first)
        server._on_disconnect()
        assert server._ai_cancellation.cancelled is False

        # 最後の1つも切れたら中断する。
        server.clients.discard(second)
        server._on_disconnect()
        assert server._ai_cancellation.cancelled is True

        await _wait_until(lambda: not server._ai_running)
        return server

    server = asyncio.run(scenario())
    assert server._ai_running is False


class WarmingProvider(RecordedProvider):
    """`prepare` を数え、`gate` が開くまでその中で待つ(ウォームアップの最中を作る)。"""

    def __init__(self, replies, *, gate: threading.Event | None = None) -> None:
        super().__init__(replies)
        self.gate = gate
        self.order: list[str] = []

    def prepare(self, *, system, tools, timeout) -> dict:
        self.order.append("prepare-start")
        if self.gate is not None:
            assert self.gate.wait(5.0)
        self.order.append("prepare-end")
        return {}

    def complete(self, **request):
        self.order.append("complete")
        return super().complete(**request)


def test_switching_the_thinking_keeps_the_conversation_and_applies_from_the_next_request(box_session: Session):
    """落ちたら、思考の設定を変えるたびに会話が消える、または変えた設定が次の往復に届かない(#135)。"""
    sent: list[dict] = []

    def fake_transport(url, payload, headers, timeout):
        sent.append(payload)
        return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}

    async def scenario():
        server = build_server(box_session, asyncio.get_running_loop())
        connection = FakeConnection()
        server.clients.add(connection)
        server._ai_configure("openai_compat", None, None)
        loop_obj = server._ensure_ai_loop()
        loop_obj.provider._transport = fake_transport
        loop_obj.run("一つ目")
        await server._dispatch(connection, json.dumps({"cmd": "ai_set_thinking", "thinking": "medium"}))
        same_conversation = server._ensure_ai_loop() is loop_obj
        loop_obj.run("二つ目")
        await server._dispatch(connection, json.dumps({"cmd": "ai_set_thinking", "thinking": "xhigh"}))
        return server, connection, same_conversation

    server, connection, same_conversation = asyncio.run(scenario())
    statuses = connection.of_type("ai_status")
    assert statuses[0]["thinking"] == "medium" and "error" not in statuses[0]
    assert statuses[0]["thinking_choices"] == ["off", "low", "medium", "server"]
    assert same_conversation, "思考の設定を変えたら会話が作り直された"
    assert sent[0]["chat_template_kwargs"]["reasoning_effort"] == "low"
    assert sent[-1]["chat_template_kwargs"]["reasoning_effort"] == "medium"
    assert "一つ目" in json.dumps(sent[-1]["messages"], ensure_ascii=False)
    assert "error" in statuses[-1] and server._ai_thinking == "medium", "未知の設定で値が変わった"


def test_the_thinking_cannot_change_while_the_ai_is_running(box_session: Session):
    async def scenario():
        server = build_server(box_session, asyncio.get_running_loop())
        server._ai_running = True
        return server, server._ai_set_thinking("off")

    server, status = asyncio.run(scenario())
    assert "error" in status
    assert server._ai_thinking == "low"


def test_llama_server_runs_under_the_limits_it_was_measured_with():
    """落ちたら、dGPU の無い機械の 27B モデルが、どの思考の設定でも完走しない上限に戻っている(docs/LOCAL_LLM_QWEN38.md)。"""
    limits = AI_LIMITS["openai_compat"]
    assert (limits.max_turns, limits.max_tokens, limits.max_total_seconds) == (60, 16384, 7200.0)
    # 1リクエストで出力上限まで書き切れる待ちがある(実測の生成 4.1 tok/s)
    assert limits.request_timeout >= limits.max_tokens / 4.1


def test_ai_prepare_warms_each_conversation_once_and_only_when_asked(monkeypatch, box_session: Session):
    """読むだけの `ai_get_status` では走らず、`ai_prepare` を重ねても1回、設定を変えたらもう1回。"""
    provider = WarmingProvider([])
    monkeypatch.setattr("duocad.server.build_provider", lambda *a, **k: provider)

    async def scenario():
        server = build_server(box_session, asyncio.get_running_loop())
        connection = FakeConnection()
        server.clients.add(connection)
        await server._dispatch(connection, json.dumps({"cmd": "ai_get_status"}))
        await asyncio.sleep(0.05)
        after_status = list(provider.order)

        for _ in range(2):
            await server._dispatch(connection, json.dumps({"cmd": "ai_prepare"}))
        await _wait_until(lambda: provider.order[-1:] == ["prepare-end"])
        await asyncio.sleep(0.05)
        after_prepare = list(provider.order)

        await server._dispatch(connection, json.dumps({"cmd": "ai_configure", "model": "other"}))
        await server._dispatch(connection, json.dumps({"cmd": "ai_prepare"}))
        await _wait_until(lambda: provider.order.count("prepare-end") == 2)
        await _settle(server)
        return after_status, after_prepare, connection

    after_status, after_prepare, connection = asyncio.run(scenario())
    assert after_status == [], "状態を読んだだけでウォームアップが走っている"
    assert after_prepare == ["prepare-start", "prepare-end"]
    assert provider.order.count("prepare-start") == 2
    assert not connection.of_type("ai_error"), "ウォームアップは何も返さない"


def test_ai_send_waits_for_a_warm_up_that_is_still_running(monkeypatch, box_session: Session):
    """並べて投げると相手の側で後ろに並び、その待ちが合計時間の予算を食う。終わってから始める。"""
    gate = threading.Event()
    provider = WarmingProvider([spoke("done")], gate=gate)
    monkeypatch.setattr("duocad.server.build_provider", lambda *a, **k: provider)

    async def scenario():
        server = build_server(box_session, asyncio.get_running_loop())
        watcher = FakeConnection()
        server.clients.add(watcher)
        await server._dispatch(watcher, json.dumps({"cmd": "ai_prepare"}))
        await _wait_until(lambda: provider.order == ["prepare-start"])
        await server._dispatch(watcher, json.dumps({"cmd": "ai_send", "message": "hi"}))
        await asyncio.sleep(0.1)
        during = list(provider.order)
        gate.set()
        await _wait_until(
            lambda: watcher.of_type("ai_result")
            and any(s["running"] is False for s in watcher.of_type("ai_status"))
        )
        await _settle(server)
        return during, watcher

    during, watcher = asyncio.run(scenario())
    assert during == ["prepare-start"], "ウォームアップの最中に依頼を投げている"
    assert provider.order == ["prepare-start", "prepare-end", "complete"]
    assert watcher.of_type("ai_result")[-1]["status"] == "completed"


def test_ai_prepare_does_nothing_while_a_run_is_in_flight(monkeypatch, box_session: Session):
    provider = WarmingProvider([spoke("done")])
    monkeypatch.setattr("duocad.server.build_provider", lambda *a, **k: provider)

    async def scenario():
        server = build_server(box_session, asyncio.get_running_loop())
        server._ai_running = True
        await server._dispatch(FakeConnection(), json.dumps({"cmd": "ai_prepare"}))
        await asyncio.sleep(0.05)

    asyncio.run(scenario())
    assert provider.order == []


