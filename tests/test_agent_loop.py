"""F(アプリ内エージェント)契約: **UIなしで回るtool-useループ**とプロバイダアダプタ。"""
from __future__ import annotations

import json

import pytest

from duocad import catalog
from duocad.agent import (
    AgentLoop,
    Cancellation,
    Limits,
    OpenAICompatProvider,
    ProviderError,
    ProviderTimeout,
)
from duocad.agent.providers import (
    AssistantTurn,
    ProviderReply,
    ToolCall,
    ToolResult,
    ToolResultTurn,
    UserTurn,
)
from duocad.mcp_server import DocumentTools, McpServer
from duocad.server import Session
from agent_support import FAKE_KEY, RecordedProvider, spoke, tool_use

#: 本物ではない。**形だけ本物に似せた文字列**(このリポジトリのどこにも実キーは置かない)。


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

    def call_and_seq(self, name, arguments, expected_seq=None):
        self.calls.append((name, dict(arguments)))
        return self.inner.call_and_seq(name, arguments, expected_seq)

    def error_envelope(self, tool, error):
        return self.inner.error_envelope(tool, error)

    def current_seq(self):
        return self.inner.current_seq()


def tools() -> DocumentTools:
    return DocumentTools(Session(None, 0.2))


def envelope_code(result) -> str:
    return result["error"]["code"]


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


def test_the_system_prompt_says_how_close_is_close_enough():
    """落ちたら、誤差の範囲のずれを直そうとして作り直しを繰り返す・迷って何もせず終わる失敗が戻る(#134。
    文言と幅は docs/LOCAL_LLM_QWEN38.md の測定で使ったもの)。"""
    shared = catalog.agent_instructions()

    assert "寸法の差が0.01mm以内、体積や面積の差が0.1%以内なら指示どおりにできているとみなし" in shared


#: 固定の停止・進行の規則を置く言い回し。AI が依頼の文面から判断するので、プロンプトに置かない。
FIXED_AUTONOMY_RULES = ("必ず止", "必ず進", "曖昧なら", "迷ったら", "承認を待", "許可を得", "(1) ", "(2) ")


def test_the_system_prompt_leaves_stop_or_proceed_to_the_ai():
    """**選択で止めるか verb まで進めるかは、人の指示から AI がその都度判断する**(本人決定)。

    落ちたら、固定の規則(曖昧なら止める等)がプロンプトへ戻っている、または
    「選択だけで止めたときは何を選んだかを述べる」「選択は人もAIも同じ操作で触る」が消えている。
    """
    shared = catalog.agent_instructions()

    assert "その都度判断" in shared
    assert "何を選んだかを述べる" in shared
    for tool in ("set_selection", "modify_selection", "toggle_selection", "clear_selection"):
        assert tool in shared, f"{tool} がプロンプトに無い(人と同じ選択操作を AI に伝えていない)"
    for phrase in FIXED_AUTONOMY_RULES:
        assert phrase not in shared, f"固定の規則『{phrase}』がプロンプトにある"
    assert "commit_selection" not in shared


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
    # ストリーミングで受ける(打ち切りで接続を閉じれば生成が止まる)。usage は明示しないと載らない
    assert payload["stream"] is True
    assert payload["stream_options"] == {"include_usage": True}
    function = payload["tools"][0]["function"]
    assert payload["tools"][0]["type"] == "function"
    assert function["name"] == surface.descriptors()[0]["name"]
    assert function["parameters"] == surface.descriptors()[0]["inputSchema"]


def _sent_payload(thinking=None) -> dict:
    transport = FakeTransport([{"choices": [{"message": {"content": "はい"}, "finish_reason": "stop"}]}])
    provider = (
        OpenAICompatProvider(transport=transport)
        if thinking is None
        else OpenAICompatProvider(transport=transport, thinking=thinking)
    )
    provider.complete(system="s", turns=[UserTurn("やって")], tools=[], max_tokens=16, timeout=5.0)
    return transport.sent[0]["payload"]


def test_llama_server_gets_the_thinking_setting_with_the_sampling_measured_for_it():
    """落ちたら、Qwen3.8 がテンプレートの既定(思考あり・xhigh)で動く、または測定と違うサンプリングで動く(#135)。"""
    default = _sent_payload()
    assert default["chat_template_kwargs"] == {"enable_thinking": True, "reasoning_effort": "low"}
    assert (default["temperature"], default["top_p"], default["top_k"], default["presence_penalty"]) == (1.0, 0.95, 20, 0.0)
    assert "seed" not in default

    medium = _sent_payload("medium")
    assert medium["chat_template_kwargs"] == {"enable_thinking": True, "reasoning_effort": "medium"}

    off = _sent_payload("off")
    assert off["chat_template_kwargs"] == {"enable_thinking": False}
    assert (off["temperature"], off["top_p"], off["top_k"], off["presence_penalty"]) == (0.7, 0.8, 20, 1.5)


def test_leaving_thinking_to_the_server_sends_nothing_extra():
    """落ちたら、「サーバ任せ」を選んでも Qwen 向けの値が他のモデルへ届く。"""
    payload = _sent_payload("server")
    for key in ("chat_template_kwargs", "temperature", "top_p", "top_k", "min_p", "presence_penalty"):
        assert key not in payload


def test_an_unknown_thinking_setting_is_refused():
    with pytest.raises(ValueError):
        OpenAICompatProvider(transport=FakeTransport([]), thinking="xhigh")


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


def test_the_servers_timings_reach_the_run_summed_over_its_requests():
    """落ちたら、読む側と書く側のどちらが遅いかを画面から読めなくなっている。"""
    timed = {
        **OPENAI_TOOL_CALL,
        "timings": {"cache_n": 6000, "prompt_n": 40, "prompt_ms": 120.5, "predicted_n": 9,
                    "predicted_ms": 300.0, "predicted_per_second": 30.0},
    }
    reply = OpenAICompatProvider(transport=FakeTransport([])).parse_reply(timed)
    assert reply.usage["prompt_n"] == 40 and reply.usage["cache_n"] == 6000
    assert "predicted_per_second" not in reply.usage, "率は往復をまたいで足せない"

    final = ProviderReply(text="終わり", stop_reason="stop", stop="end_turn",
                          usage={"prompt_n": 10, "predicted_n": 5, "predicted_ms": 100.0})
    run = AgentLoop(tools(), RecordedProvider([
        ProviderReply(tool_calls=reply.tool_calls, stop_reason="tool_calls", stop="tool_use", usage=reply.usage),
        final,
    ])).run("調べて")
    assert run.usage["prompt_n"] == 50
    assert run.usage["predicted_n"] == 14
    assert run.usage["predicted_ms"] == 400.0


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


def test_a_raising_progress_callback_is_logged_with_a_warning_but_does_not_break_the_loop(caplog):
    """B(1.2, CODE_SURVEY_ACTIONS): `_notify` は購読者の例外を握りつぶさず、痕跡を残す。"""
    surface = SpyTools(tools())
    reply = ProviderReply(
        text="1つ調べる",
        tool_calls=(ToolCall(id="c1", name="describe_document", arguments={}),),
        stop_reason="tool_use",
        stop="tool_use",
    )
    provider = RecordedProvider([reply, spoke("調べました")])

    def broken_progress(*_args):
        raise RuntimeError("購読者側の事故")

    with caplog.at_level("WARNING", logger="duocad.agent.loop"):
        run = AgentLoop(surface, provider, progress=broken_progress).run("調べて")

    # 1: ループは最後まで走り、結果は正常に返る(例外は外へ伝播していない)。
    assert run.status == "completed"
    assert run.text == "調べました"

    # 2: caplog にWARNINGが1件出ており、投げた例外の型が分かる(exc_info付き)。
    warnings = [record for record in caplog.records if record.levelname == "WARNING"]
    assert len(warnings) == 1
    assert warnings[0].exc_info is not None
    assert warnings[0].exc_info[0] is RuntimeError
    assert "RuntimeError" in caplog.text


def test_a_failing_tool_returns_the_structured_envelope_and_the_loop_keeps_going():
    """**構造化封筒をそのままモデルへ返して自己修復させる**(MCP経路と同じ語彙)。"""
    surface = tools()
    provider = RecordedProvider(
        [tool_use("fillet", {"radius": 2.0}), tool_use("describe_document", {}), spoke("直しました")]
    )
    run = AgentLoop(surface, provider).run("R2でフィレットして")

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


def test_the_total_time_budget_cuts_the_loop_off_before_max_turns():
    """**総時間の上限は往復数と別に要る**。

    `max_tokens` は1リクエスト、`request_timeout` は1リクエストの待ち、`max_turns` は
    回数しか縛らない。人が待つのはその掛け算なので、部分の上限をいくら締めても決まらない
    —— 実測で、1リクエストが上限内のまま12往復を空回りして合計88秒かかった。
    """
    surface = tools()
    provider = RecordedProvider([tool_use("describe_document", {}, f"c{i}") for i in range(5)])

    run = AgentLoop(
        surface, provider, limits=Limits(max_turns=99, max_total_seconds=0.0)
    ).run("ずっと調べて")

    assert run.status == "timeout"
    assert run.turns == 0, "予算が尽きていれば1往復も始めない"
    assert run.tool_calls == (), "ツールを1つも実行しない"
    assert "30" not in run.detail and "0" in run.detail
    assert run.to_dict()["status"] == "timeout"


def test_a_dimension_nobody_asked_for_is_refused_before_the_verb_runs():
    """**規律を文章で頼まず、ここで機械的に止める**。

    モデルは「寸法を自分で決めない」を文章だけでは守れなかった —— 実測で
    「角が尖ってて危ないから、いい感じに処理しといて」に対し、止まらず R1.5mm を
    自分で決めて fillet まで適用した。適用してしまった形はundoでしか戻せない。
    """
    surface = tools()
    provider = RecordedProvider([tool_use("fillet", {"radius": 1.5}, "c0"), spoke("やめた")])

    run = AgentLoop(surface, provider).run("角が尖ってて危ないから、いい感じに処理しといて")

    assert envelope_code(run.tool_calls[0].result) == "dimension_not_given"
    assert run.tool_calls[0].ok is False
    assert "radius" in run.tool_calls[0].result["error"]["message"]


def test_a_string_where_a_number_is_expected_is_refused_before_the_handler_runs():
    """落ちたら、アプリ内AIが `radius: "12"` のような型違いを渡したとき、`normalize_values` が
    文字列を1文字ずつ読んで辺2本に半径1と2を付けるように**黙って別の値**で形が変わる(issue #64)。
    MCP は同じ検査を -32602 で先に弾いているので、封筒で返るのはこの経路だけ。"""
    surface = tools()
    provider = RecordedProvider([tool_use("fillet", {"radius": "12"}, "c0"), spoke("直します")])

    run = AgentLoop(surface, provider).run("R12でフィレットして")

    assert envelope_code(run.tool_calls[0].result) == "invalid_argument"
    assert run.tool_calls[0].ok is False
    assert "radius" in run.tool_calls[0].result["error"]["message"]
    assert surface.session.document.log.entries == [], "型違いの引数で形が変わっている"


def test_a_string_where_a_boolean_is_expected_is_refused(box_session: Session):
    """落ちたら、`visible: "false"` が真として通ってボディが隠れない(または隠れたまま戻らない)。"""
    surface = DocumentTools(box_session)
    provider = RecordedProvider(
        [tool_use("set_visible", {"body_id": "box", "visible": "false"}, "c0"), spoke("隠しました")]
    )

    run = AgentLoop(surface, provider).run("boxを隠して")

    assert envelope_code(run.tool_calls[0].result) == "invalid_argument"
    assert "visible" in run.tool_calls[0].result["error"]["message"]
    assert box_session.document.get_body("box").visible is True


def test_a_dimension_the_person_gave_passes_through():
    """人が量を言っていれば止めない(止めるのは捏造だけ)。"""
    surface = tools()
    provider = RecordedProvider([tool_use("fillet", {"radius": 3}, "c0"), spoke("done")])

    run = AgentLoop(surface, provider).run("上面の辺をR3で丸めて")

    assert envelope_code(run.tool_calls[0].result) != "dimension_not_given"


def test_a_dimension_derived_from_what_the_person_said_passes_through():
    """**厳密な照合にしない** —— 「半径4mm」から `diameter: 8` を導くのは正しい。"""
    surface = tools()
    provider = RecordedProvider(
        [tool_use("add_circle", {"sketch_id": "s", "center": [0, 0], "diameter": 8}, "c0"),
         spoke("done")]
    )

    run = AgentLoop(surface, provider).run("原点に半径4mmの円を描いて")

    assert envelope_code(run.tool_calls[0].result) != "dimension_not_given"


def test_an_id_in_the_prompt_is_not_mistaken_for_a_dimension():
    """`body_1` の 1 を「寸法を言った」と読むとガードが素通りする。"""
    surface = tools()
    provider = RecordedProvider([tool_use("fillet", {"radius": 2}, "c0"), spoke("やめた")])

    run = AgentLoop(surface, provider).run("body_1の角を丸めておいて")

    assert envelope_code(run.tool_calls[0].result) == "dimension_not_given"


def test_a_verb_without_a_dimension_is_never_guarded():
    """印の無いverbは素通し(`describe_document` は寸法を取らない)。"""
    surface = tools()
    provider = RecordedProvider([tool_use("describe_document", {}, "c0"), spoke("done")])

    run = AgentLoop(surface, provider).run("いい感じに教えて")

    assert run.tool_calls[0].ok is True


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
    # 送り直せる形になっている(呼び出しと結果が1対1で対応している)
    messages = OpenAICompatProvider(transport=FakeTransport([])).render_messages("", loop.turns)
    used = [c["id"] for c in messages[1]["tool_calls"]]
    returned = [m["tool_call_id"] for m in messages[2:]]
    assert used == returned == ["c1", "c2"]


def test_a_provider_failure_is_reported_not_raised():
    surface = tools()
    provider = RecordedProvider([ProviderError("HTTP 529 from http://127.0.0.1:8080/v1: overloaded")])
    run = AgentLoop(surface, provider).run("やって")

    assert run.status == "provider_error"
    assert "529" in run.detail
    assert run.tool_calls == ()


def test_a_real_provider_failure_is_not_renamed_to_timeout():
    """**本物の障害を `timeout` と名乗らせない**。"""
    surface = tools()
    provider = RecordedProvider([ProviderError("https://api.example へ接続できない: refused")])

    run = AgentLoop(surface, provider).run("やって")

    assert run.status == "provider_error"


def test_a_timeout_under_the_total_budget_is_reported_as_timeout():
    """予算が待ちを縮めた結果の待ち時間切れは `timeout`(相手のせいではない)。"""
    surface = tools()
    provider = RecordedProvider([ProviderTimeout("127.0.0.1:8080 が 5s で返らなかった")])

    run = AgentLoop(surface, provider, limits=Limits(max_total_seconds=5.0)).run("やって")

    assert run.status == "timeout"
    assert "5" in run.detail


def test_stop_reasons_that_are_not_end_turn_are_reported_as_such():
    surface = tools()
    truncated = ProviderReply(text="途中まで", stop_reason="max_tokens", stop="max_tokens")
    assert AgentLoop(surface, RecordedProvider([truncated])).run("長い話").status == "max_tokens"

    refused = ProviderReply(stop_reason="refusal", stop="refusal")
    assert AgentLoop(surface, RecordedProvider([refused])).run("だめな話").status == "refusal"


def test_an_unknown_stop_reason_falls_through_to_stopped_with_the_raw_value():
    """知らない停止理由を `completed` に化けさせない(生値を `detail` に残す)。"""
    unknown = ProviderReply(text="…", stop_reason="content_filter", stop="other")
    run = AgentLoop(tools(), RecordedProvider([unknown])).run("なにか")

    assert run.status == "stopped"
    assert "content_filter" in run.detail
    assert run.ok is False


def test_a_reply_with_neither_text_nor_tool_calls_is_not_completed():
    """落ちたら、思考だけで何も出さずに止まった応答が画面に「完了」と出る、または空になる前に実行した操作が結果から消える(#137)。"""
    empty = ProviderReply(text="", stop_reason="stop", stop="end_turn")
    run = AgentLoop(tools(), RecordedProvider([tool_use("describe_document", {}), empty])).run("作って")

    assert run.status == "empty_reply"
    assert run.ok is False
    assert "stop" in run.detail
    assert [outcome.name for outcome in run.tool_calls] == ["describe_document"]


def test_a_truncated_reply_is_not_mistaken_for_tool_use():
    """**打ち切りの判定がツール呼び出しより先** —— 切れた引数を実行しないための前提。"""
    compat = OpenAICompatProvider(transport=FakeTransport([]))
    truncated = {
        "choices": [
            {**OPENAI_TOOL_CALL["choices"][0], "finish_reason": "length"},
        ]
    }
    reply = compat.parse_reply(truncated)
    assert reply.tool_calls
    assert reply.stop == "max_tokens"


def test_a_truncated_turn_stops_before_running_its_tool_calls():
    """引数のJSONが途中で切れている可能性があるので、**文書を変えるツールを走らせない**。"""
    surface = SpyTools(tools())
    truncated = ProviderReply(
        text="半径は",
        tool_calls=(
            ToolCall(id="c1", name="fillet", arguments={"radius": 1}),
            ToolCall(id="c2", name="describe_document", arguments={}),
        ),
        stop_reason="max_tokens",
        stop="max_tokens",
    )
    provider = RecordedProvider([truncated, spoke("ここへは来ない")])
    loop = AgentLoop(surface, provider)
    run = loop.run("フィレットして")

    assert surface.calls == []  # 1つも実行しない
    assert run.status == "max_tokens"
    assert run.tool_calls == ()
    assert len(provider.requests) == 1
    # 履歴は再送できる形に保つ(tool_use と tool_result が1対1)
    results = loop.turns[-1].results
    assert [r.call_id for r in results] == ["c1", "c2"]
    assert all(r.ok is False for r in results)
    assert "上限" in json.loads(results[0].content)["error"]["message"]


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


# 4. 鍵が漏れない(セキュリティ要件)
#
# キーの読み口は `load_api_key` の1つで、`build_provider` はそれを `anthropic` にだけ渡す。
# `openai_compat` が受け取れる `api_key`(鍵を要求するサーバのため)を `build_provider` は渡さない。
# Claude側の整形・受け方・再試行は `tests/test_agent_anthropic.py`。
def test_a_provider_key_never_shows_up_in_repr_or_error_messages():
    """HTTPライブラリはヘッダ値を例外文へ載せることがある。**外へ出る前に消す**。"""
    leaking = FakeTransport([ValueError(f"Invalid header value {FAKE_KEY!r}")])
    provider = OpenAICompatProvider(api_key=FAKE_KEY, transport=leaking)

    assert FAKE_KEY not in repr(provider)
    with pytest.raises(ProviderError) as caught:
        provider.complete(system="s", turns=[UserTurn("x")], tools=[], max_tokens=16, timeout=1.0)
    assert FAKE_KEY not in str(caught.value)
    assert "***" in str(caught.value)

    # ループの結果・進捗にも出ない
    run = AgentLoop(tools(), RecordedProvider([ProviderError(str(caught.value))])).run("やって")
    assert FAKE_KEY not in json.dumps(run.to_dict(), ensure_ascii=False)


def test_the_key_reaches_only_the_claude_provider(monkeypatch):
    """**キーは `anthropic` にだけ渡り、`openai_compat` には乗らない**。"""
    from duocad.agent import providers
    from duocad.agent.providers import build_provider

    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    with pytest.raises(ProviderError) as caught:
        build_provider("anthropic")
    assert providers.ANTHROPIC_API_KEY_ENV in str(caught.value), "どこに置けばよいかを言っていない"

    monkeypatch.setenv(providers.ANTHROPIC_API_KEY_ENV, FAKE_KEY)
    transport = FakeTransport(
        [{"choices": [{"message": {"content": "はい"}, "finish_reason": "stop"}]}]
    )
    provider = build_provider("openai_compat", transport=transport)
    provider.complete(system="s", turns=[UserTurn("x")], tools=[], max_tokens=16, timeout=1.0)

    sent = transport.sent[0]
    assert "authorization" not in {key.lower() for key in sent["headers"]}
    assert FAKE_KEY not in json.dumps(sent, ensure_ascii=False)

    claude = FakeTransport([{"content": [{"type": "text", "text": "はい"}], "stop_reason": "end_turn"}])
    build_provider("anthropic", transport=claude).complete(
        system="s", turns=[UserTurn("x")], tools=[], max_tokens=16, timeout=1.0
    )
    assert claude.sent[0]["headers"]["x-api-key"] == FAKE_KEY

    with pytest.raises(ValueError):
        build_provider("gemini")
