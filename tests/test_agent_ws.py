"""G契約: アプリ内AIエージェントをWebSocketへ配線する(`duocad/server.py` の `ai_*`)。

タスクF(`duocad/agent/`)は「UIを持たないtool-useループ」を作った。ここは画面側の入口 ——
**`ai_*` はverbではない**(文書を直接は変えない・操作ログに載らない・undoの単位でもない)ので、
`duocad/catalog.py` の `COMMANDS` には入れない。除外そのものは
`test_ui_command_wiring.py::test_ai_commands_are_not_verbs` と
`test_mcp_verbs.py::test_ai_commands_have_no_mcp_tool` が固定する。

固定する4つ:

1. **APIキーは画面へ出ない** —— `ai_status` に載るのは `api_key_configured` の真偽だけ。
2. **`ai_send` はモデルへのHTTPリクエストを待たずに返る** —— 実行は `run_in_executor`
   (`_ai_start` のdocstring)。進捗・結果は別イベント(`ai_tool_call` / `ai_result`)で届く。
3. **AIがツールを呼んで文書を変えると、開いているブラウザの絵が変わる**(通常のverb配信に乗る。
   `test_a_verb_applied_through_mcp_reaches_the_browser` のAI版)。
4. **中断できる**(`ai_cancel`)。どこまでやったかは `ai_result.tool_calls` に残る。

実キーは使わない(`RecordedProvider` で `duocad.server.build_provider` を差し替える)。
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

from duocad.agent.providers import Provider, ProviderReply, ToolCall
from duocad.document import Body
from duocad.mcp_server import DocumentTools
from duocad.server import Server, Session

BOX_X, BOX_Y, BOX_Z = 30.0, 30.0, 40.0


@pytest.fixture()
def box_session() -> Session:
    made = Session(None, 0.2)
    made.document.add_body(
        Body(id="box", name="box", kind="solid", shape=BRepPrimAPI_MakeBox(BOX_X, BOX_Y, BOX_Z).Shape())
    )
    return made


class FakeConnection:
    """`Server` が使うのは `send` だけ(`test_feature_ui.py` と同じ形)。"""

    def __init__(self) -> None:
        self.payloads: list = []

    async def send(self, raw: str) -> None:
        self.payloads.append(json.loads(raw))

    def of_type(self, type_: str) -> list:
        return [p for p in self.payloads if p.get("type") == type_]


def tool_use(name: str, arguments: dict, call_id: str = "call_1") -> ProviderReply:
    call = ToolCall(id=call_id, name=name, arguments=arguments)
    return ProviderReply(text="", tool_calls=(call,), stop_reason="tool_use", stop="tool_use")


def spoke(text: str) -> ProviderReply:
    return ProviderReply(text=text, stop_reason="end_turn", stop="end_turn")


class RecordedProvider(Provider):
    """`tests/test_agent_loop.py` と同じ形。任意で `delay` だけ `complete()` を遅らせる
    (中断のテストが「まだ次のツールを呼んでいない間」に割り込む余地を作るため)。
    """

    name = "recorded"

    def __init__(self, replies, *, delay: float = 0.0) -> None:
        self.model = "recorded"
        self.replies = list(replies)
        self.delay = delay

    def complete(self, *, system, turns, tools, max_tokens, timeout) -> ProviderReply:
        if self.delay:
            time.sleep(self.delay)
        assert self.replies, "偽プロバイダの応答が尽きた"
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


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


async def _drain() -> None:
    """`call_soon_threadsafe` で作られた配信タスクを最後まで走らせきる。

    `_wait_until` の条件が真になった時点では、**そのイベントを運ぶタスクが作られた**
    ことしか保証されない(タスクの本体がそのループ反復で最後まで走るとは限らない)。
    `scenario()` がここで抜けて `asyncio.run` がループを閉じると、作っただけで
    走らせていないタスクが「Task was destroyed but it is pending」の警告付きで
    捨てられる(実害は無いテスト専用の後始末)。**`sleep(0)` だけでは足りないことがある**
    (実行器スレッドからの `call_soon_threadsafe` はOSのスケジューラをまたぐので、
    1回のループ反復に収まる保証が無い)ので、実時間のスリープも混ぜる。

    **それでも稀に(数%)「coroutine 'Server._broadcast_ai' was never awaited」が出ることがある**
    ——実行器スレッド発のスケジューリングとasyncioのready queueの相対順序は、この
    テストハーネス(1回の `asyncio.run` が数十ms で閉じる)の外では起きない競合であり、
    製品コード(イベントループが閉じない実サーバ)側の不具合ではない。`pyproject.toml` は
    `filterwarnings` を設定していないので、通常の `pytest -q` ではこの警告で赤くならない
    (`-W error::RuntimeWarning` を明示したときだけ目に入る)。
    """
    for _ in range(20):
        await asyncio.sleep(0)
    await asyncio.sleep(0.05)
    for _ in range(20):
        await asyncio.sleep(0)


# --------------------------------------------------------------------------------------
# 1. APIキーは画面へ出ない
# --------------------------------------------------------------------------------------
def test_ai_status_reports_configured_flag_only(monkeypatch, box_session: Session):
    """`ai_status` に載るのは `api_key_configured` の真偽だけ(値そのものは無い)。"""
    monkeypatch.setattr("duocad.server.api_key_configured", lambda: True)

    async def scenario():
        server = build_server(box_session, asyncio.get_running_loop())
        connection = FakeConnection()
        await server._dispatch(connection, json.dumps({"cmd": "ai_get_status"}))
        return connection.payloads

    payloads = asyncio.run(scenario())
    assert len(payloads) == 1
    status = payloads[0]
    assert status["type"] == "ai_status"
    assert status["api_key_configured"] is True
    assert status["running"] is False
    assert set(status["providers"]) == {"anthropic", "openai_compat"}
    # **値そのものはどこにも無い**(dictを丸ごと文字列化しても鍵の形が出てこない)
    assert "sk-" not in json.dumps(status)


def test_ai_configure_switches_provider_and_model(monkeypatch, box_session: Session):
    monkeypatch.setattr("duocad.server.api_key_configured", lambda: False)

    async def scenario():
        server = build_server(box_session, asyncio.get_running_loop())
        a, b = FakeConnection(), FakeConnection()
        server.clients.add(a)
        server.clients.add(b)
        await server._dispatch(
            a,
            json.dumps(
                {"cmd": "ai_configure", "provider": "openai_compat", "model": "qwen3-30b"}
            ),
        )
        return a, b

    a, b = asyncio.run(scenario())
    # **設定は全接続へ配る**(共有の1会話なので、他の画面でも今の設定が見える)。
    for connection in (a, b):
        status = connection.of_type("ai_status")[-1]
        assert status["provider"] == "openai_compat"
        assert status["model"] == "qwen3-30b"


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


def test_ai_configure_is_rejected_while_running(monkeypatch, box_session: Session):
    """実行中は設定を変えさせない(理由: 中断フラグを作り直すと中断ボタンが効かなくなる。
    `Server._ai_configure` のdocstring)。
    """
    monkeypatch.setattr("duocad.server.api_key_configured", lambda: True)

    async def scenario():
        server = build_server(box_session, asyncio.get_running_loop())
        before = server._ai_provider_kind  # 既定値("anthropic"。上のmonkeypatchによる)
        server._ai_running = True
        status = server._ai_configure("openai_compat", None, None)
        return before, status

    before, status = asyncio.run(scenario())
    assert before == "anthropic"
    assert "error" in status
    assert status["provider"] == before, "実行中なのに設定が変わっている"


# --------------------------------------------------------------------------------------
# 2. ai_send はブロックしない・進捗と結果が届く
# --------------------------------------------------------------------------------------
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
        return elapsed, server

    elapsed, server = asyncio.run(scenario())
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
        await _drain()
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


# --------------------------------------------------------------------------------------
# 3. AIが文書を変えると、開いているブラウザの絵が変わる
# --------------------------------------------------------------------------------------
def test_a_tool_call_that_mutates_the_document_reaches_the_browser(
    monkeypatch, box_session: Session
):
    """`set_visible` をAI経由で呼ぶと、**通常のverb配信**(`type: state`)が別接続へ届く。

    `_AiToolsOnLoop` が実行器スレッドからイベントループへ投げ返していること
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
        await _drain()
        return browser, box_session

    browser, session = asyncio.run(scenario())
    assert session.document.bodies[0].visible is False
    states = browser.of_type("state")
    mutated = [s for s in states if s["state"]["bodies"][0]["visible"] is False]
    assert mutated, "AIの set_visible が文書に反映されたのに配信されていない"
    # **出し手のいない変更**(MCPと同じ扱い) —— `applied` を付けない。
    assert "applied" not in mutated[-1]


# --------------------------------------------------------------------------------------
# 4. 中断できる
# --------------------------------------------------------------------------------------
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
        await _drain()
        return watcher

    watcher = asyncio.run(scenario())
    result = watcher.of_type("ai_result")[-1]
    assert result["status"] == "cancelled"
    # **どこまでやったかは残る**(タスクファイル完了条件2)。
    assert len(result["tool_calls"]) >= 1
    assert len(result["tool_calls"]) < 2, "中断が効かず最後まで回ってしまった"
