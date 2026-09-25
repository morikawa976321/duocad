"""アプリ内AIエージェントのテストが共有する、モデルの代わりのプロバイダと応答の作り方。"""
from __future__ import annotations

import time

from duocad.agent.providers import Provider, ProviderReply, ToolCall

#: 本物に見えるが無効な API キー。「キーが表示・記録・送信に漏れない」を見るテストが使う。
FAKE_KEY = "sk-test-DO-NOT-USE-0123456789"


class RecordedProvider(Provider):
    """渡した順に応答を返すだけのプロバイダ。例外を混ぜると、その往復で送出する。

    `delay` は `complete()` を遅らせる秒数(中断のテストが「まだ次のツールを呼んでいない間」に
    割り込む余地を作る)。受け取った要求は `requests` に残る。
    """

    name = "recorded"

    def __init__(self, replies, *, delay: float = 0.0) -> None:
        self.model = "recorded"
        self.replies = list(replies)
        self.delay = delay
        self.requests: list[dict] = []

    def complete(self, *, system, turns, tools, max_tokens, timeout) -> ProviderReply:
        self.requests.append(
            {"system": system, "turns": list(turns), "tools": list(tools), "max_tokens": max_tokens, "timeout": timeout}
        )
        if self.delay:
            time.sleep(self.delay)
        assert self.replies, "偽プロバイダの応答が尽きた(ループが想定より多く回っている)"
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def tool_use(name: str, arguments: dict, call_id: str = "call_1") -> ProviderReply:
    """ツールを1つ呼ぶ応答。"""
    call = ToolCall(id=call_id, name=name, arguments=arguments)
    return ProviderReply(text="", tool_calls=(call,), stop_reason="tool_use", stop="tool_use")


def spoke(text: str) -> ProviderReply:
    """ツールを呼ばずに言葉だけで終わる応答。"""
    return ProviderReply(text=text, stop_reason="end_turn", stop="end_turn")
