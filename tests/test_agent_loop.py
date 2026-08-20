"""F(アプリ内エージェント)契約: **UIなしで回るtool-useループ**と2つのプロバイダアダプタ。

ここが本体の検査である —— 実機のLLMには依存させない。**偽プロバイダ(記録済みの応答列を
返すスタブ)**でループの分岐を決定的に固定し、アダプタの整形はHTTPを差し替えた単体検査で見る
(llama-serverの実機は `tests/test_agent_llama_server.py` の1本だけ。Claude APIを叩く実テストは
課金が走るので**書かない**)。

## このファイルが固定する5つ

1. **道具の定義を第二の場所に書かない** —— ツール一覧は `DocumentTools.descriptors()` そのまま、
   実行は `.call()`、失敗の封筒は `.error_envelope()`。MCP経路と顔ぶれ・挙動・失敗の形が同一。
2. **システムプロンプトを二重に持たない** —— MCPの `instructions` とエージェントは
   `catalog.agent_instructions()` の1本を読む。
3. **打ち切りが効く** —— `max_turns` / 中断フラグ / プロバイダの失敗。理由は構造化して返す。
4. **ツールの失敗でループが落ちない** —— 封筒をそのままモデルへ返して自己修復させる
   (未知のツール名・引数の食い違い・壊れたJSONも含む)。
5. **APIキーがどこにも出ない** —— `repr`・例外メッセージ・進捗・結果のどれにも。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from duocad import catalog
from duocad.agent import (
    AgentLoop,
    AnthropicProvider,
    Cancellation,
    Limits,
    OpenAICompatProvider,
    ProviderError,
)
from duocad.agent.loop import ToolSurface
from duocad.agent.providers import (
    ANTHROPIC_API_KEY_ENV,
    SECRETS_FILENAME,
    AssistantTurn,
    Provider,
    ProviderReply,
    ToolCall,
    ToolResult,
    ToolResultTurn,
    UserTurn,
    api_key_configured,
    load_api_key,
)
from duocad.mcp_server import DocumentTools, McpServer
from duocad.server import Session

REPO_ROOT = Path(__file__).resolve().parent.parent

#: 本物ではない。**形だけ本物に似せた文字列**(このリポジトリのどこにも実キーは置かない)。
FAKE_KEY = "sk-ant-test-DO-NOT-USE-0123456789"


# --------------------------------------------------------------------------------------
# 偽プロバイダ(記録済みの応答列)と道具のスパイ
# --------------------------------------------------------------------------------------
class RecordedProvider(Provider):
    """渡した順に応答を返すだけのプロバイダ。例外を混ぜると、その往復で送出する。"""

    name = "recorded"

    def __init__(self, replies) -> None:
        self.model = "recorded"
        self.replies = list(replies)
        self.requests: list[dict] = []

    def complete(self, *, system, turns, tools, max_tokens, timeout):
        self.requests.append(
            {
                "system": system,
                "turns": list(turns),
                "tools": list(tools),
                "max_tokens": max_tokens,
                "timeout": timeout,
            }
        )
        assert self.replies, "偽プロバイダの応答が尽きた(ループが想定より多く回っている)"
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class SpyTools:
    """`ToolSurface` を満たす薄い覆い。**どのツールが実際に走ったか**を記録する。"""

    def __init__(self, inner: DocumentTools) -> None:
        self.inner = inner
        self.calls: list[tuple[str, dict]] = []

    def descriptors(self):
        return self.inner.descriptors()

    def call(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        return self.inner.call(name, arguments)

    def error_envelope(self, tool, error):
        return self.inner.error_envelope(tool, error)


def tools() -> DocumentTools:
    return DocumentTools(Session(None, 0.2))


def tool_use(name: str, arguments: dict, call_id: str = "call_1") -> ProviderReply:
    call = ToolCall(id=call_id, name=name, arguments=arguments)
    return ProviderReply(text="", tool_calls=(call,), stop_reason="tool_use", stop="tool_use")


def spoke(text: str) -> ProviderReply:
    return ProviderReply(text=text, stop_reason="end_turn", stop="end_turn")


def envelope_code(result) -> str:
    return result["error"]["code"]


# --------------------------------------------------------------------------------------
# 1. 道具の定義とシステムプロンプトの出所
# --------------------------------------------------------------------------------------
def test_the_loop_sends_document_tools_descriptors_verbatim():
    """ツール定義は `DocumentTools.descriptors()` そのもの(第二の表を作らない)。"""
    surface = tools()
    provider = RecordedProvider([spoke("はい")])
    AgentLoop(surface, provider).run("何もしないで")

    sent = provider.requests[0]["tools"]
    assert sent == surface.descriptors()
    assert [t["name"] for t in sent] == list(catalog.tool_names())


def test_the_system_prompt_is_the_same_text_the_mcp_server_hands_out():
    """**手順を2箇所に書かない** —— MCPの `instructions` とエージェントは同じ文を読む。"""
    shared = catalog.agent_instructions()
    server = McpServer(tools())
    initialize = server._handle_request("initialize", {})

    assert initialize["instructions"] == shared
    assert AgentLoop(tools(), RecordedProvider([])).system == shared
    # ツール数は宣言から数える(手書きの数字は verb を足した日に古くなる)
    assert str(len(catalog.tool_names())) in shared


# --------------------------------------------------------------------------------------
# 2. アダプタの整形(HTTPを差し替えた単体検査。実キーで外部を叩かない)
# --------------------------------------------------------------------------------------
class FakeTransport:
    """`post_json` と同じ形をした差し替え。送った内容を全部覚える。"""

    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.sent: list[dict] = []

    def __call__(self, url, payload, headers, timeout):
        self.sent.append(
            {"url": url, "payload": payload, "headers": headers, "timeout": timeout}
        )
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


ANTHROPIC_TOOL_USE = {
    "content": [
        {"type": "thinking", "thinking": "考えている", "signature": "sig"},
        {"type": "text", "text": "調べる"},
        {"type": "tool_use", "id": "toolu_1", "name": "describe_document", "input": {}},
    ],
    "stop_reason": "tool_use",
    "usage": {"input_tokens": 10, "output_tokens": 3},
}
ANTHROPIC_DONE = {
    "content": [{"type": "text", "text": "ボディはありません"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 20, "output_tokens": 5},
}


def test_anthropic_request_shape_and_forbidden_parameters():
    """`inputSchema` → `input_schema` のキー1つ違い。**現行モデルが400を返す欄は送らない**。"""
    transport = FakeTransport([ANTHROPIC_DONE])
    provider = AnthropicProvider(FAKE_KEY, transport=transport)
    surface = tools()

    provider.complete(
        system="つかいかた",
        turns=[UserTurn("やって")],
        tools=surface.descriptors(),
        max_tokens=1234,
        timeout=30.0,
    )

    sent = transport.sent[0]
    assert sent["url"].endswith("/v1/messages")
    assert sent["headers"]["anthropic-version"] == "2023-06-01"
    assert sent["headers"]["x-api-key"] == FAKE_KEY
    payload = sent["payload"]
    assert payload["model"] == "claude-opus-5"
    assert payload["max_tokens"] == 1234
    assert payload["system"] == "つかいかた"
    assert payload["messages"] == [{"role": "user", "content": "やって"}]
    assert payload["tools"][0]["input_schema"] == surface.descriptors()[0]["inputSchema"]
    assert [t["name"] for t in payload["tools"]] == list(catalog.tool_names())
    for forbidden in ("temperature", "top_p", "top_k", "thinking"):
        assert forbidden not in payload, f"{forbidden} は現行モデルが400を返す"


def test_anthropic_echoes_the_assistant_content_back_unchanged():
    """assistantの本文は**そのまま**返す(thinkingブロックを落とすと次のターンが壊れる)。"""
    provider = AnthropicProvider(FAKE_KEY, transport=FakeTransport([]))
    reply = provider.parse_reply(ANTHROPIC_TOOL_USE)
    turns = [
        UserTurn("やって"),
        AssistantTurn(text=reply.text, tool_calls=reply.tool_calls, raw=reply.raw_message),
        ToolResultTurn(
            (
                ToolResult(call_id="toolu_1", name="describe_document", content="{}", ok=True),
                ToolResult(call_id="toolu_2", name="measure", content="boom", ok=False),
            )
        ),
    ]

    messages = provider.render_messages(turns)
    assert messages[1] == {"role": "assistant", "content": ANTHROPIC_TOOL_USE["content"]}
    # 複数のツール結果は1つのuserメッセージにまとめる(分けると並列呼びが止む)
    assert messages[2]["role"] == "user"
    assert messages[2]["content"] == [
        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "{}", "is_error": False},
        {"type": "tool_result", "tool_use_id": "toolu_2", "content": "boom", "is_error": True},
    ]


def test_anthropic_reply_parsing():
    provider = AnthropicProvider(FAKE_KEY, transport=FakeTransport([]))
    reply = provider.parse_reply(ANTHROPIC_TOOL_USE)
    assert reply.text == "調べる"
    assert reply.stop == "tool_use"
    assert [(c.id, c.name, c.arguments) for c in reply.tool_calls] == [
        ("toolu_1", "describe_document", {})
    ]

    assert provider.parse_reply(ANTHROPIC_DONE).stop == "end_turn"
    assert provider.parse_reply({"content": [], "stop_reason": "max_tokens"}).stop == "max_tokens"
    assert provider.parse_reply({"content": [], "stop_reason": "refusal"}).stop == "refusal"


OPENAI_TOOL_CALL = {
    "choices": [
        {
            "message": {
                "role": "assistant",
                "content": "調べる",
                "reasoning_content": "サーバごとに増える欄",
                "tool_calls": [
                    {
                        "id": "call_a",
                        "type": "function",
                        "function": {
                            "name": "describe_shape",
                            "arguments": '{"body_id": "b1"}',
                        },
                    }
                ],
            },
            "finish_reason": "tool_calls",
        }
    ],
    "usage": {"prompt_tokens": 100, "completion_tokens": 7},
}


def test_openai_compat_request_shape():
    """`{type:"function", function:{...}}` で包むだけ。systemは先頭の1メッセージ。"""
    transport = FakeTransport([{"choices": [{"message": {"content": "はい"}, "finish_reason": "stop"}]}])
    provider = OpenAICompatProvider(transport=transport, base_url="http://127.0.0.1:8080/v1")
    surface = tools()

    provider.complete(
        system="つかいかた",
        turns=[UserTurn("やって")],
        tools=surface.descriptors(),
        max_tokens=256,
        timeout=5.0,
    )

    sent = transport.sent[0]
    assert sent["url"] == "http://127.0.0.1:8080/v1/chat/completions"
    assert "authorization" not in sent["headers"]  # ローカルはキー不要
    payload = sent["payload"]
    assert payload["messages"][0] == {"role": "system", "content": "つかいかた"}
    assert payload["messages"][1] == {"role": "user", "content": "やって"}
    assert payload["tool_choice"] == "auto"
    assert payload["stream"] is False
    function = payload["tools"][0]["function"]
    assert payload["tools"][0]["type"] == "function"
    assert function["name"] == surface.descriptors()[0]["name"]
    assert function["parameters"] == surface.descriptors()[0]["inputSchema"]


def test_openai_compat_reply_parsing_and_tool_result_messages():
    provider = OpenAICompatProvider(transport=FakeTransport([]))
    reply = provider.parse_reply(OPENAI_TOOL_CALL)
    assert reply.text == "調べる"
    assert reply.stop == "tool_use"
    assert reply.tool_calls[0].name == "describe_shape"
    assert reply.tool_calls[0].arguments == {"body_id": "b1"}

    messages = provider.render_messages(
        "",
        [
            AssistantTurn(text=reply.text, tool_calls=reply.tool_calls, raw=reply.raw_message),
            ToolResultTurn(
                (ToolResult(call_id="call_a", name="describe_shape", content="{}", ok=True),)
            ),
        ],
    )
    # 生のメッセージは戻さず組み直す(`reasoning_content` を送り返すと弾くサーバがある)
    assert "reasoning_content" not in messages[0]
    assert messages[0]["tool_calls"][0]["function"]["arguments"] == '{"body_id": "b1"}'
    # ツール結果は role:"tool" で1件ずつ
    assert messages[1] == {
        "role": "tool",
        "tool_call_id": "call_a",
        "name": "describe_shape",
        "content": "{}",
    }


def test_openai_compat_marks_broken_json_arguments_instead_of_guessing():
    provider = OpenAICompatProvider(transport=FakeTransport([]))
    reply = provider.parse_reply(
        {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "id": "call_x",
                                "function": {"name": "fillet", "arguments": "{radius: 2"},
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }
    )
    assert reply.tool_calls[0].parse_error
    assert reply.tool_calls[0].arguments == {}


# --------------------------------------------------------------------------------------
# 3. ループの分岐(ここが本体の検査)
# --------------------------------------------------------------------------------------
def test_multiple_tool_calls_in_one_reply_all_run_and_report_progress():
    surface = SpyTools(tools())
    reply = ProviderReply(
        text="2つ調べる",
        tool_calls=(
            ToolCall(id="c1", name="describe_document", arguments={}),
            ToolCall(id="c2", name="get_selection", arguments={}),
        ),
        stop_reason="tool_use",
        stop="tool_use",
    )
    provider = RecordedProvider([reply, spoke("どちらも空でした")])
    events = []
    run = AgentLoop(surface, provider, progress=lambda *args: events.append(args)).run("調べて")

    assert [name for name, _ in surface.calls] == ["describe_document", "get_selection"]
    assert [event[0] for event in events] == ["describe_document", "get_selection"]
    assert all(event[3] is True for event in events)
    assert run.status == "completed"
    assert run.text == "どちらも空でした"
    assert [outcome.name for outcome in run.tool_calls] == ["describe_document", "get_selection"]
    # 2件の結果は1つのターンにまとまってモデルへ返る
    last_turns = provider.requests[-1]["turns"]
    assert isinstance(last_turns[-1], ToolResultTurn)
    assert len(last_turns[-1].results) == 2


def test_a_failing_tool_returns_the_structured_envelope_and_the_loop_keeps_going():
    """**構造化封筒をそのままモデルへ返して自己修復させる**(MCP経路と同じ語彙)。"""
    surface = tools()
    provider = RecordedProvider(
        [tool_use("fillet", {"radius": 2.0}), tool_use("describe_document", {}), spoke("直しました")]
    )
    run = AgentLoop(surface, provider).run("フィレットして")

    first = run.tool_calls[0]
    assert first.ok is False
    assert envelope_code(first.result) == "selection_empty"
    assert first.result["error"]["required_kind"] == "edge"
    assert run.status == "completed"
    # モデルへ返った本文もJSONの封筒(自然文ではない)
    sent = provider.requests[1]["turns"][-1].results[0]
    assert sent.ok is False
    assert json.loads(sent.content)["error"]["code"] == "selection_empty"


def test_an_unknown_tool_name_comes_back_as_not_found():
    surface = tools()
    provider = RecordedProvider([tool_use("make_it_pretty", {}), spoke("ごめん")])
    run = AgentLoop(surface, provider).run("いい感じにして")

    assert envelope_code(run.tool_calls[0].result) == "not_found"
    assert run.status == "completed"


def test_wrong_argument_names_come_back_as_invalid_argument_not_internal():
    """引数の食い違いは**直せる誤り**として返す(`internal` に落とすとモデルが諦める)。"""
    surface = tools()
    provider = RecordedProvider([tool_use("describe_shape", {"body": "b1"}), spoke("直す")])
    run = AgentLoop(surface, provider).run("形を教えて")

    assert envelope_code(run.tool_calls[0].result) == "invalid_argument"


def test_broken_json_arguments_are_reported_without_running_the_tool():
    surface = SpyTools(tools())
    broken = ProviderReply(
        tool_calls=(ToolCall(id="c1", name="fillet", arguments={}, parse_error="壊れている"),),
        stop_reason="tool_calls",
        stop="tool_use",
    )
    provider = RecordedProvider([broken, spoke("書き直す")])
    run = AgentLoop(surface, provider).run("フィレットして")

    assert surface.calls == []  # 壊れた引数のまま実行しない
    assert envelope_code(run.tool_calls[0].result) == "invalid_argument"
    assert run.status == "completed"


def test_max_turns_cuts_the_loop_off_with_a_structured_reason():
    surface = tools()
    provider = RecordedProvider([tool_use("describe_document", {}, f"c{i}") for i in range(5)])
    run = AgentLoop(surface, provider, limits=Limits(max_turns=3)).run("ずっと調べて")

    assert run.status == "max_turns"
    assert run.turns == 3
    assert len(run.tool_calls) == 3
    assert "3" in run.detail
    assert run.to_dict()["status"] == "max_turns"


def test_the_cancel_flag_stops_the_loop_between_tool_calls():
    surface = tools()
    cancellation = Cancellation()
    reply = ProviderReply(
        tool_calls=(
            ToolCall(id="c1", name="describe_document", arguments={}),
            ToolCall(id="c2", name="get_selection", arguments={}),
        ),
        stop_reason="tool_use",
        stop="tool_use",
    )
    provider = RecordedProvider([reply, spoke("ここへは来ない")])

    def stop_after_first(*_args):
        cancellation.cancel()

    run = AgentLoop(
        surface, provider, progress=stop_after_first, cancellation=cancellation
    ).run("調べて")

    assert run.status == "cancelled"
    assert len(run.tool_calls) == 1
    assert len(provider.requests) == 1  # 2回目は投げない


def test_a_cancelled_turn_still_leaves_a_replayable_history():
    surface = tools()
    cancellation = Cancellation()
    reply = ProviderReply(
        tool_calls=(
            ToolCall(id="c1", name="describe_document", arguments={}),
            ToolCall(id="c2", name="get_selection", arguments={}),
        ),
        stop_reason="tool_use",
        stop="tool_use",
    )
    loop = AgentLoop(
        surface,
        RecordedProvider([reply]),
        progress=lambda *_args: cancellation.cancel(),
        cancellation=cancellation,
    )
    loop.run("調べて")

    results = loop.turns[-1].results
    assert [r.call_id for r in results] == ["c1", "c2"]
    assert [r.ok for r in results] == [True, False]
    # Claudeへ送り直せる形になっている(tool_use と tool_result が1対1)
    messages = AnthropicProvider(FAKE_KEY, transport=FakeTransport([])).render_messages(loop.turns)
    used = [b["id"] for b in messages[1]["content"] if b["type"] == "tool_use"]
    returned = [b["tool_use_id"] for b in messages[2]["content"]]
    assert used == returned == ["c1", "c2"]


def test_a_provider_failure_is_reported_not_raised():
    surface = tools()
    provider = RecordedProvider([ProviderError("HTTP 529 from https://api.anthropic.com: overloaded")])
    run = AgentLoop(surface, provider).run("やって")

    assert run.status == "provider_error"
    assert "529" in run.detail
    assert run.tool_calls == ()


def test_stop_reasons_that_are_not_end_turn_are_reported_as_such():
    surface = tools()
    truncated = ProviderReply(text="途中まで", stop_reason="max_tokens", stop="max_tokens")
    assert AgentLoop(surface, RecordedProvider([truncated])).run("長い話").status == "max_tokens"

    refused = ProviderReply(stop_reason="refusal", stop="refusal")
    assert AgentLoop(surface, RecordedProvider([refused])).run("だめな話").status == "refusal"


def test_the_conversation_continues_across_runs_until_reset():
    surface = tools()
    provider = RecordedProvider([spoke("1回目"), spoke("2回目")])
    loop = AgentLoop(surface, provider)
    loop.run("ひとつめ")
    loop.run("ふたつめ")

    assert [turn.text for turn in provider.requests[1]["turns"] if isinstance(turn, UserTurn)] == [
        "ひとつめ",
        "ふたつめ",
    ]
    loop.reset()
    assert loop.turns == []


def test_document_tools_satisfies_the_loop_surface():
    """ループが要求する面は公開APIの3つだけ(内部には触らない)。"""
    surface = tools()
    assert isinstance(surface, ToolSurface)
    for name in ("descriptors", "call", "error_envelope"):
        assert callable(getattr(surface, name))
        assert not name.startswith("_")


# --------------------------------------------------------------------------------------
# 4. APIキーが漏れない(セキュリティ要件)
# --------------------------------------------------------------------------------------
def test_the_api_key_is_read_from_the_environment_first_then_the_local_file(tmp_path):
    secrets = tmp_path / SECRETS_FILENAME
    secrets.write_text(json.dumps({ANTHROPIC_API_KEY_ENV: "from-file"}), encoding="utf-8")

    assert load_api_key(environ={ANTHROPIC_API_KEY_ENV: "from-env"}, path=secrets) == "from-env"
    assert load_api_key(environ={}, path=secrets) == "from-file"
    assert load_api_key(environ={ANTHROPIC_API_KEY_ENV: "   "}, path=secrets) == "from-file"
    assert load_api_key(environ={}, path=tmp_path / "missing.json") is None

    secrets.write_text("{壊れている", encoding="utf-8")
    assert load_api_key(environ={}, path=secrets) is None

    # 画面へ出してよいのは2値だけ(値は返さない)
    assert api_key_configured(environ={ANTHROPIC_API_KEY_ENV: "x"}, path=tmp_path / "none") is True
    assert api_key_configured(environ={}, path=tmp_path / "none") is False


def test_the_local_secrets_file_is_gitignored():
    ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert SECRETS_FILENAME in ignored


def test_the_api_key_never_shows_up_in_repr_or_error_messages():
    """HTTPライブラリはヘッダ値を例外文へ載せることがある。**外へ出る前に消す**。"""
    leaking = FakeTransport([ValueError(f"Invalid header value {FAKE_KEY!r}")])
    provider = AnthropicProvider(FAKE_KEY, transport=leaking)

    assert FAKE_KEY not in repr(provider)
    with pytest.raises(ProviderError) as caught:
        provider.complete(system="s", turns=[UserTurn("x")], tools=[], max_tokens=16, timeout=1.0)
    assert FAKE_KEY not in str(caught.value)
    assert "***" in str(caught.value)

    # ループの結果・進捗にも出ない
    run = AgentLoop(tools(), RecordedProvider([ProviderError(str(caught.value))])).run("やって")
    assert FAKE_KEY not in json.dumps(run.to_dict(), ensure_ascii=False)


def test_the_api_key_travels_only_in_the_header_not_in_the_body():
    transport = FakeTransport([ANTHROPIC_DONE])
    provider = AnthropicProvider(FAKE_KEY, transport=transport)
    provider.complete(
        system=catalog.agent_instructions(),
        turns=[UserTurn("やって")],
        tools=tools().descriptors(),
        max_tokens=64,
        timeout=1.0,
    )
    sent = transport.sent[0]
    assert FAKE_KEY not in json.dumps(sent["payload"], ensure_ascii=False)
    assert sent["headers"]["x-api-key"] == FAKE_KEY


def test_building_the_anthropic_provider_without_a_key_fails_with_a_useful_message(tmp_path):
    from duocad.agent.providers import build_provider

    with pytest.raises(ProviderError) as caught:
        build_provider("anthropic", environ={}, secrets=tmp_path / "none.json")
    message = str(caught.value)
    assert ANTHROPIC_API_KEY_ENV in message and SECRETS_FILENAME in message

    with pytest.raises(ValueError):
        build_provider("gemini")
