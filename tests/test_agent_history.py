"""履歴の契約: 済んだ依頼は「人の発話 + AIの最終返答」だけが残り、相手の文脈長に当たったら
古い依頼を半分まとめて捨ててやり直す。

落ちたら、(a) 会話が続くと文脈が尽きて以後の依頼がすべて失敗する、(b) 古いツール結果が残って
モデルが今は無い状態を信じる、(c) 前の依頼を指す言い方(「同じように」)が通じない、のどれかに
戻っている。
"""
from __future__ import annotations

from duocad.agent import AgentLoop, Limits, ProviderError
from duocad.agent.providers import ProviderContextOverflow, ProviderReply, ToolCall, UserTurn
from duocad.mcp_server import DocumentTools
from duocad.server import Session
from agent_support import RecordedProvider, spoke, tool_use


class SizedProvider(RecordedProvider):
    """文脈長を名乗るプロバイダ(llama-server の `/props` に当たる)。"""

    def __init__(self, replies, *, n_ctx: int) -> None:
        super().__init__(replies)
        self.n_ctx = n_ctx

    def context_size(self) -> int:
        return self.n_ctx


def tools() -> DocumentTools:
    return DocumentTools(Session(None, 0.2))


def shape(request: dict) -> list[str]:
    return [type(turn).__name__ for turn in request["turns"]]


def said(request: dict) -> list[str]:
    return [turn.text for turn in request["turns"] if isinstance(turn, UserTurn)]


def reply(*, text: str = "", calls: bool = False, stop: str, prompt: int, completion: int) -> ProviderReply:
    tool_calls = (ToolCall(id="c1", name="describe_document", arguments={}),) if calls else ()
    return ProviderReply(
        text=text,
        tool_calls=tool_calls,
        stop_reason=stop,
        stop=stop,
        usage={"prompt_tokens": prompt, "completion_tokens": completion},
    )


# --------------------------------------------------------------------------------------
# 済んだ依頼の畳み方
# --------------------------------------------------------------------------------------
def test_a_finished_request_keeps_only_what_was_said():
    provider = RecordedProvider([tool_use("describe_document", {}), spoke("ボディは無い"), spoke("はい")])
    loop = AgentLoop(tools(), provider)
    loop.run("文書を調べて")
    loop.run("ありがとう")

    assert shape(provider.requests[1]) == ["UserTurn", "AssistantTurn", "ToolResultTurn"]
    assert shape(provider.requests[2]) == ["UserTurn", "AssistantTurn", "UserTurn"]
    kept = provider.requests[2]["turns"][1]
    assert kept.text == "ボディは無い" and kept.tool_calls == ()


def test_a_request_that_did_not_finish_leaves_a_note_where_the_answer_would_be():
    """発話が2つ続く履歴を作らない(役割の交互を要求するテンプレートがある)。"""
    provider = RecordedProvider([tool_use("describe_document", {}), ProviderError("HTTP 500"), spoke("はい")])
    loop = AgentLoop(tools(), provider)
    assert loop.run("調べて").status == "provider_error"
    loop.run("もう一度")

    assert shape(provider.requests[2]) == ["UserTurn", "AssistantTurn", "UserTurn"]
    note = provider.requests[2]["turns"][1].text
    assert "最後まで進まなかった" in note and "describe_document" in note


def test_the_dimension_guard_still_hears_what_an_earlier_request_said():
    """「上面をR3で丸めて」のあとの「反対側も同じように」を、数量なしの依頼として止めない。"""
    provider = RecordedProvider([spoke("丸めた"), tool_use("fillet", {"radius": 3}), spoke("できなかった")])
    loop = AgentLoop(tools(), provider)
    loop.run("上面をR3で丸めて")
    run = loop.run("反対側も同じように")

    codes = [outcome.result["error"]["code"] for outcome in run.tool_calls if not outcome.ok]
    assert "dimension_not_given" not in codes


# --------------------------------------------------------------------------------------
# 文脈長に当たったとき
# --------------------------------------------------------------------------------------
def test_an_overflow_drops_the_older_half_and_retries_the_same_request():
    replies = [spoke(f"返事{index}") for index in range(4)]
    provider = RecordedProvider(replies + [ProviderContextOverflow("exceeds the available context size"), spoke("やり直せた")])
    loop = AgentLoop(tools(), provider)
    for index in range(4):
        loop.run(f"依頼{index}")
    run = loop.run("依頼4")

    assert run.status == "completed" and run.text == "やり直せた"
    assert said(provider.requests[4]) == ["依頼0", "依頼1", "依頼2", "依頼3", "依頼4"]
    assert said(provider.requests[5]) == ["依頼2", "依頼3", "依頼4"]
    assert run.turns == 1, "断られた往復を数えている"


def test_an_overflow_with_nothing_left_to_drop_says_so():
    """今の依頼だけで収まらないなら、障害ではなくそのことを返す。"""
    run = AgentLoop(tools(), RecordedProvider([ProviderContextOverflow("exceeds")])).run("巨大な依頼")

    assert run.status == "context_overflow"


def test_room_for_one_more_answer_is_made_before_the_next_request():
    """収まりはするが書く余地が無い、という往復を作らない(実測のトークン数で判定する)。"""
    crowded = reply(calls=True, stop="tool_use", prompt=880, completion=30)
    provider = SizedProvider([spoke("a"), spoke("b"), crowded, spoke("終わり")], n_ctx=1000)
    loop = AgentLoop(tools(), provider, limits=Limits(max_tokens=100))
    for index in range(3):
        loop.run(f"依頼{index}")

    assert said(provider.requests[2]) == ["依頼0", "依頼1", "依頼2"]
    assert said(provider.requests[3]) == ["依頼1", "依頼2"]


def test_an_answer_cut_by_the_context_running_out_is_retried_with_room():
    """文脈の残りが尽きて切れた応答を「出力の上限」として返さない。"""
    cut = reply(text="途中", stop="max_tokens", prompt=990, completion=10)
    provider = SizedProvider([spoke("a"), spoke("b"), cut, spoke("書けた")], n_ctx=1000)
    loop = AgentLoop(tools(), provider, limits=Limits(max_tokens=100))
    loop.run("依頼0")
    loop.run("依頼1")
    run = loop.run("依頼2")

    assert run.status == "completed" and run.text == "書けた"
    assert said(provider.requests[3]) == ["依頼1", "依頼2"]


def test_a_request_that_already_ended_does_not_cost_older_history():
    """終わった依頼のツール往復は次の `run()` が畳む。その前に古い依頼まで捨てるのは早すぎる。"""
    ended = reply(text="終わり", stop="end_turn", prompt=950, completion=5)
    provider = SizedProvider([spoke("a"), ended, spoke("c")], n_ctx=1000)
    loop = AgentLoop(tools(), provider, limits=Limits(max_tokens=100))
    for index in range(3):
        loop.run(f"依頼{index}")

    assert said(provider.requests[2]) == ["依頼0", "依頼1", "依頼2"]
