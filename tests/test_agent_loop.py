"""F(アプリ内エージェント)契約: **UIなしで回るtool-useループ**とプロバイダアダプタ。

ここが本体の検査である —— 実機のLLMには依存させない。**偽プロバイダ(記録済みの応答列を
返すスタブ)**でループの分岐を決定的に固定し、アダプタの整形はHTTPを差し替えた単体検査で見る
(llama-serverの実機は `tests/test_agent_llama_server.py` の1本だけ)。

**プロバイダは `openai_compat` の1つだけになった**(2026-08-27) —— ClaudeをAPIキーで叩く
`anthropic` アダプタとその鍵の読み口を廃止したため。Claudeへ繋ぐ道はMCP経路に残っている
(`tests/test_mcp.py` / `tests/test_mcp_http.py`)。

## このファイルが固定する5つ

1. **道具の定義を第二の場所に書かない** —— ツール一覧は `DocumentTools.descriptors()` そのまま、
   実行は `.call()`、失敗の封筒は `.error_envelope()`。MCP経路と顔ぶれ・挙動・失敗の形が同一。
2. **システムプロンプトを二重に持たない** —— MCPの `instructions` とエージェントは
   `catalog.agent_instructions()` の1本を読む。
3. **打ち切りが効く** —— `max_turns` / 中断フラグ / プロバイダの失敗。理由は構造化して返す。
4. **ツールの失敗でループが落ちない** —— 封筒をそのままモデルへ返して自己修復させる
   (未知のツール名・引数の食い違い・壊れたJSONも含む)。
5. **鍵がどこにも出ない** —— `repr`・例外メッセージ・進捗・結果のどれにも。
   併せて **`build_provider` が鍵を渡さない**(画面から鍵を受け取る経路を作らない)。
"""
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
    Provider,
    ProviderReply,
    ToolCall,
    ToolResult,
    ToolResultTurn,
    UserTurn,
)
from duocad.mcp_server import DocumentTools, McpServer
from duocad.server import Session

#: 本物ではない。**形だけ本物に似せた文字列**(このリポジトリのどこにも実キーは置かない)。
FAKE_KEY = "sk-test-DO-NOT-USE-0123456789"


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

    def current_seq(self):
        return self.inner.current_seq()


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


def _autonomy_rule_zones(text: str) -> tuple[str, str, str, str]:
    """プロンプトを「前置き / 第1段 / 第2段 / 後書き」の4区画に**漏れなく**割る。

    **終端句より後ろも検査対象に入れる** —— 入れないと、末尾に「ただし、第1段の操作も必ず
    提案で止める。」と1文足しても緑のままになる(実測)。連結が元の文に戻ることを確かめて、
    **検査から外れる区画が残らない**ようにする。

    切り出せないときは**緑にせず落とす**(空振り緑は、検査が効いていないことを「異常なし」と
    報告するので、穴が開いていることに気づけない)。
    """
    marks = ("(1) ", "(2) ", "形を変える verb が確定すると")
    for mark in marks:
        assert mark in text, (
            f"段の目印『{mark}』がプロンプトに無い。"
            "切り出せないなら検査は成立しないので、緑にせずここで落とす"
        )
    first, second, tail = (text.index(mark) for mark in marks)
    assert 0 < first < second < tail, f"2段の規則の並びが崩れている: {text}"
    zones = (text[:first], text[first:second], text[second:tail], text[tail:])
    assert "".join(zones) == text, "区画の割り方が壊れている(全文を覆えていない)"
    return zones


#: **止まれ**と読める指示。第2段の中にしか置いてはならない(下の検査の要)。
STOP_DIRECTIVES = (
    "止め",
    "止ま",
    "尋ね",
    "承認を待",
    "確認を待つ",
    "人に確認",
    "人へ確認",
    "許可を得",
    "指示を仰",
)

#: 言い回しを問わず**第1段の許可を全称で打ち消す**語。プロンプト全体で禁じる。
BLANKET_OVERRIDES = ("必ず", "第1段", "してはならない", "承認を得て", "許可を得")


def test_the_system_prompt_carries_the_two_step_autonomy_rule():
    """**AIが止まるか進むかの規則は、この文にしか無い**(本人決定)。

    `commit_selection` は最初からMCPに公開されているので、人の承認を強制していたのは
    プロンプトと文書だけだった。裏を返すと、**ここを旧規則(承認必須)へ戻してもコアは何も
    止めない** —— 赤にならずに振る舞いだけが変わる。だから文で固定する。

    即実行してよい側の条件は、手動UIのEnter要否(`web/src/commands/command.ts` の
    `needsEnter`)と**同じ2条件**である(対象を何個でも取る / how の入力がある)。
    **個々のverb名はこの文に書かない**(verbを足した日に古くなる)ので、ここでも見ない。

    落とすのは4つ: **句の削除 / 条件の否定・肯定の取り違え / 分岐の入れ替え / 第1段の許可を
    打ち消す停止指示の追記**。最後の1つが要点で、**矛盾の検出は区間の中ではなくプロンプト全文に
    対して行う**(止まれと読める語は第2段の中にしか置けない、という不変条件にしてある)。

    **落とせないもの**: 散文の意味は文字列検査では固定できないので、ここに挙げていない言い回しで
    の矛盾は常に書き足せる。そこは検査の領域ではなく、**この文を書き換える人が
    `agent_instructions()` のdocstringを読むことで守る**。
    """
    shared = catalog.agent_instructions()

    # 前提: 確定はAI自身が踏む(旧規則ならここが「人の承認を待つ」に戻る)
    assert "人の承認は要らない" in shared, "旧規則(承認必須)へ戻っている"

    head, first, second, tail = _autonomy_rule_zones(shared)

    # --- 第1段: 2条件が**どちらも無い**とき、確認を待たず適用してよい ---
    assert "対象を何個でも取る引数が無く" in first, "条件(1)が否定形で書かれていない"
    assert "how(寸法・選択肢・パス)の入力も無い" in first, "条件(2)が否定形で書かれていない"
    assert "確認を待たず" in first, "第1段の行動が『確認を待たない』でない"
    assert "実行してよい" in first, "第1段が実行の許可になっていない"

    # --- 第2段: 2条件の**いずれかがある**とき、AIが依頼から判断する ---
    assert "対象を何個でも取る、または how の入力がある" in second, "条件が肯定形で書かれていない"
    assert "自分で判断する" in second, "自律判断の規則が第2段に無い"
    # 分岐の向き —— 具体的なら適用、曖昧なら停止。入れ替わったら順序が壊れて落ちる
    concrete, applies = second.index("具体的な指示"), second.index("適用まで行き")
    vague, stops = second.index("曖昧"), second.index("止めて")
    assert concrete < applies < vague < stops, (
        f"具体/曖昧と適用/停止の対応が入れ替わっている: {second}"
    )
    # 止めると決めたら verb を呼ばない(呼べばコアが SelectionNotCommitted で断る)
    assert "止めたら verb は呼ばない" in second, "『止める』の中身が書かれていない"
    assert "止める側に倒す" in second, "迷ったときに倒す向きが書かれていない"

    # --- 全文: 第1段の許可を後段で打ち消していないこと ---
    # **区間の外を見ていなかったのが前の版の穴**(末尾への1文追記が素通りした)。
    # 止まれと読める語は第2段の中にしか置けない、という形にして全文を覆う。
    for label, zone in (("前置き", head), ("第1段", first), ("後書き", tail)):
        for word in STOP_DIRECTIVES:
            assert word not in zone, (
                f"{label}に停止指示『{word}』がある —— "
                "止まれと読める語を置いてよいのは第2段の中だけである"
                "(第1段の『確認を待たず適用してよい』を後段で打ち消す変更を落とすため)"
            )
    for word in BLANKET_OVERRIDES:
        assert word not in shared, (
            f"第1段の許可を全称で打ち消す言い回し『{word}』がプロンプトにある"
        )
    assert shared.count("(1) ") == 1, "段の見出し『(1) 』が増えている(後段で規則を上書きした疑い)"


# --------------------------------------------------------------------------------------
# 2. アダプタの整形(HTTPを差し替えた単体検査。外部を叩かない)
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
    """**構造化封筒をそのままモデルへ返して自己修復させる**(MCP経路と同じ語彙)。

    依頼に半径を書いてあるのは、**このテストが見たいのが選択の空**だからである
    (2026-08-23)。書かないと寸法のガードが先に立って `dimension_not_given` で
    止まり、`selection_empty` まで届かない —— ガード自体は下の専用テストが見る。
    """
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
    """**総時間の上限は往復数と別に要る**(2026-08-23)。

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


def test_the_total_time_budget_does_not_fire_on_a_normal_run():
    """正常系が上限に当たらないこと(既定30秒で普通の往復が切られては困る)。"""
    surface = tools()
    provider = RecordedProvider([tool_use("describe_document", {}, "c0"), spoke("できた")])

    run = AgentLoop(surface, provider).run("ボディを教えて")

    assert run.status == "completed"


# --------------------------------------------------------------------------------------
# 人が言っていない寸法で形を変えないこと(2026-08-23)
# --------------------------------------------------------------------------------------
def test_a_dimension_nobody_asked_for_is_refused_before_the_verb_runs():
    """**規律を文章で頼まず、ここで機械的に止める**。

    `agent_instructions()` は「曖昧な依頼なら propose_selection で止めて尋ねる」
    「迷ったら止める側に倒す」と書いてあるが、モデルはこれを守れなかった —— 実測で
    「角が尖ってて危ないから、いい感じに処理しといて」に対し、止まらず R1.5mm を
    自分で決めて fillet まで適用した。適用してしまった形はundoでしか戻せない。
    """
    surface = tools()
    provider = RecordedProvider([tool_use("fillet", {"radius": 1.5}, "c0"), spoke("やめた")])

    run = AgentLoop(surface, provider).run("角が尖ってて危ないから、いい感じに処理しといて")

    assert envelope_code(run.tool_calls[0].result) == "dimension_not_given"
    assert run.tool_calls[0].ok is False
    assert "radius" in run.tool_calls[0].result["error"]["message"]


def test_a_dimension_the_person_gave_passes_through():
    """人が量を言っていれば止めない(止めるのは捏造だけ)。"""
    surface = tools()
    provider = RecordedProvider([tool_use("fillet", {"radius": 3}, "c0"), spoke("done")])

    run = AgentLoop(surface, provider).run("上面の辺をR3で丸めて")

    assert envelope_code(run.tool_calls[0].result) != "dimension_not_given"


def test_a_dimension_derived_from_what_the_person_said_passes_through():
    """**厳密な照合にしない** —— 「直径8mm」から `radius: 4` を導くのは正しい。

    値がそのまま本文にあることを求めると、この普通の依頼が通らなくなる。
    見るのは「人が量を与えたか」だけでよい。
    """
    surface = tools()
    provider = RecordedProvider(
        [tool_use("add_circle", {"sketch_id": "s", "center": [0, 0], "radius": 4}, "c0"),
         spoke("done")]
    )

    run = AgentLoop(surface, provider).run("原点に直径8mmの円を描いて")

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
    """**本物の障害を `timeout` と名乗らせない**(2026-08-23)。

    合計時間の予算は1リクエストの待ちより常に短いので、「予算が縮めたか」だけで
    判定すると接続拒否やHTTP 5xxまで `timeout` になる。読み替えてよいのは
    `ProviderTimeout`(待ち時間切れ)のときだけである —— 原因がこちらの予算なのか
    相手の障害なのかで、直す場所が違う。
    """
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


# --------------------------------------------------------------------------------------
# 3b. 出力上限で切れたターンのツールは走らせない
# --------------------------------------------------------------------------------------
def test_a_truncated_reply_is_not_mistaken_for_tool_use():
    """**打ち切りの判定がツール呼び出しより先** —— 切れた引数を実行しないための前提。

    上限で切られた応答にも `tool_calls` は載る(**ブロック自体はある**)。それを
    `tool_use` に丸めると、切れた引数のままツールが走る。
    """
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


# --------------------------------------------------------------------------------------
# 4. 鍵が漏れない(セキュリティ要件)
#
# **ClaudeをAPIキーで叩く経路は2026-08-27に廃止した** —— 環境変数も `duocad_secrets.json` も
# 読まなくなり、`load_api_key` / `api_key_configured` ごと消えている。残っているのは
# `openai_compat` が鍵を要求するサーバのために受け取れる `api_key` だけで、**`build_provider`
# はそれを渡さない**。ここで固定するのはその2点である。
# --------------------------------------------------------------------------------------
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


def test_the_provider_the_app_builds_carries_no_key_at_all(monkeypatch):
    """**アプリが作るプロバイダには鍵が乗らない**。

    廃止した経路が生き残っていないことも同時に見る —— 環境に `ANTHROPIC_API_KEY` が
    居ても拾わないし、`anthropic` という綴りはもう作れない。
    """
    from duocad.agent.providers import build_provider

    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    transport = FakeTransport(
        [{"choices": [{"message": {"content": "はい"}, "finish_reason": "stop"}]}]
    )
    provider = build_provider("openai_compat", transport=transport)
    provider.complete(system="s", turns=[UserTurn("x")], tools=[], max_tokens=16, timeout=1.0)

    sent = transport.sent[0]
    assert "authorization" not in {key.lower() for key in sent["headers"]}
    assert FAKE_KEY not in json.dumps(sent, ensure_ascii=False)

    with pytest.raises(ValueError):
        build_provider("anthropic")
    with pytest.raises(ValueError):
        build_provider("gemini")
