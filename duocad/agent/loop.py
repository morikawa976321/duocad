"""エージェントループ —— **プロバイダ非依存**の tool-use 往復。

`Session` の文書に対して、モデルに道具(`DocumentTools`)を渡して往復させる。
UIは持たない(画面はタスクGが上に載せる)。

## 何を守っているか

1. **道具の定義を第二の場所に書かない。** ツール一覧は `DocumentTools.descriptors()`、
   実行は `.call()`、失敗の封筒は `.error_envelope()`。MCP経路(`duocad/mcp_server.py`)と
   顔ぶれ・挙動・失敗の形が同一になる。
2. **暴走を必ず打ち切る。** `max_turns` / `max_tokens` / 1リクエストのタイムアウト /
   外から立てる中断フラグの4つ。打ち切ったときは理由を構造化して返す
   (実測: 矛盾した指示を渡すと思考が膨らみ、1リクエストが600秒返らなかった。
   伸びるのは文脈長ではなく思考の側なので、**上限と短いタイムアウトが要る**)。
3. **ツールの実行時エラーでループを落とさない。** 構造化封筒
   (`not_found` / `invalid_argument` / `selection_not_committed` / `selection_empty` /
   `invalid_predicate` / `internal`)をそのままモデルへ返して自己修復させる
   (2026-08-20の実測でこれは機能した)。
4. **UIの知識を持たない。** 進捗は `progress(name, arguments, result, ok)` のコールバックへ
   流すだけ(購読側=Gが画面に出す)。

## 使い方

    tools = DocumentTools(session)
    loop = AgentLoop(tools, build_provider("openai_compat"))
    run = loop.run("この文書のボディを教えて")
    run.status   # "completed" / "max_turns" / "cancelled" / "provider_error" / ...

会話は `AgentLoop` に積み上がる(`run()` を続けて呼べば続きになる)。`reset()` で捨てる。

**OCP / build123d をimportしない**(`tests/test_boundaries.py` が検査する)。
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol, Sequence, runtime_checkable

from .. import catalog
from .providers import (
    AssistantTurn,
    Provider,
    ProviderError,
    ToolCall,
    ToolResult,
    ToolResultTurn,
    Turn,
    UserTurn,
)

#: 1回の `run()` で許すモデルとの往復の上限。
DEFAULT_MAX_TURNS = 12
#: 1リクエストの出力上限(思考+本文)。思考は既定で有効なので小さすぎると本文が切れる。
DEFAULT_MAX_TOKENS = 8192
#: 1リクエストの待ち上限(秒)。**短く保つ**(上記「暴走の打ち切り」)。
DEFAULT_REQUEST_TIMEOUT = 120.0

#: 進捗の口。`(ツール名, 引数, 結果, 成否)`。Gがこれを購読して画面に出す。
ProgressCallback = Callable[[str, dict, Any, bool], None]


@runtime_checkable
class ToolSurface(Protocol):
    """ループが道具に要求する面(`DocumentTools` がこれを満たす)。

    **公開APIの3つだけ**に依存する —— 内部(`_build_descriptors` 等)には触らない。
    """

    def descriptors(self) -> list: ...

    def call(self, name: str, arguments: dict) -> dict: ...

    def error_envelope(self, tool: str, error: Exception) -> dict: ...


class Cancellation:
    """外から立てる中断フラグ(スレッドをまたいで立ててよい)。

    ループは**モデルへ投げる前**と**ツールを1つ実行するたび**に見る。走っている
    HTTPリクエスト自体は切らない(切るのはタイムアウトの仕事)。
    """

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    def clear(self) -> None:
        self._event.clear()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()


@dataclass(frozen=True)
class Limits:
    """打ち切りの3つ(4つ目の中断フラグは `Cancellation`)。"""

    max_turns: int = DEFAULT_MAX_TURNS
    max_tokens: int = DEFAULT_MAX_TOKENS
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT


@dataclass(frozen=True)
class ToolOutcome:
    """実行したツール1件(進捗の購読者と、終わったあとの読み返し用)。"""

    name: str
    arguments: dict
    #: 成功なら `call()` が返した辞書、失敗なら `{"error": {...}}` の封筒。
    result: Any
    ok: bool


@dataclass(frozen=True)
class AgentRun:
    """1回の `run()` の結果。**打ち切りの理由は構造化して返す**(Gが画面に出す)。

    `status`:

    - `completed`      … モデルがツールを呼ばずに答えた(正常終了)
    - `max_turns`      … 往復の上限で打ち切った
    - `cancelled`      … 中断フラグが立った
    - `max_tokens`     … 出力上限で切れた(答えが途中)
    - `refusal`        … プロバイダが安全上の理由で断った
    - `provider_error` … HTTP・接続・応答の形(`detail` に理由。**APIキーは含まれない**)
    - `stopped`        … 上のどれでもない停止理由(`detail` に生値)
    """

    status: str
    text: str = ""
    detail: str = ""
    turns: int = 0
    tool_calls: tuple[ToolOutcome, ...] = ()
    usage: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "completed"

    def to_dict(self) -> dict:
        """そのままJSONにできる形(WebSocketでGへ流す想定)。"""
        return {
            "status": self.status,
            "text": self.text,
            "detail": self.detail,
            "turns": self.turns,
            "tool_calls": [
                {"name": outcome.name, "arguments": outcome.arguments, "ok": outcome.ok}
                for outcome in self.tool_calls
            ],
            "usage": self.usage,
        }


class AgentLoop:
    """道具を1つのプロバイダへ渡して回すループ。"""

    def __init__(
        self,
        tools: ToolSurface,
        provider: Provider,
        *,
        system: Optional[str] = None,
        limits: Limits = Limits(),
        progress: Optional[ProgressCallback] = None,
        cancellation: Optional[Cancellation] = None,
    ) -> None:
        self.tools = tools
        self.provider = provider
        #: **MCPの `instructions` と同じ文**(catalog.py の1本から読む)。
        self.system = catalog.agent_instructions() if system is None else system
        self.limits = limits
        self.progress = progress
        self.cancellation = cancellation or Cancellation()
        self.turns: list[Turn] = []

    # ------------------------------------------------------------------ 会話
    def reset(self) -> None:
        """会話を捨てる(文書は触らない)。"""
        self.turns = []

    def run(self, prompt: str) -> AgentRun:
        """1つの指示を投げて、モデルがツールを呼ばなくなるまで回す。"""
        self.turns.append(UserTurn(prompt))
        descriptors = self.tools.descriptors()
        outcomes: list[ToolOutcome] = []
        usage: dict = {}
        turn = 0

        while True:
            if self.cancellation.cancelled:
                return self._result("cancelled", "中断フラグが立った", turn, outcomes, usage)
            if turn >= self.limits.max_turns:
                return self._result(
                    "max_turns",
                    f"往復が上限({self.limits.max_turns})に達した",
                    turn,
                    outcomes,
                    usage,
                )

            turn += 1
            try:
                reply = self.provider.complete(
                    system=self.system,
                    turns=self.turns,
                    tools=descriptors,
                    max_tokens=self.limits.max_tokens,
                    timeout=self.limits.request_timeout,
                )
            except ProviderError as error:
                return self._result("provider_error", str(error), turn, outcomes, usage)

            usage = _merge_usage(usage, reply.usage)
            self.turns.append(
                AssistantTurn(
                    text=reply.text, tool_calls=reply.tool_calls, raw=reply.raw_message
                )
            )

            if not reply.tool_calls:
                status = {
                    "end_turn": "completed",
                    "max_tokens": "max_tokens",
                    "refusal": "refusal",
                }.get(reply.stop, "stopped")
                detail = "" if status == "completed" else f"stop_reason={reply.stop_reason!r}"
                return self._result(status, detail, turn, outcomes, usage, text=reply.text)

            results: list[ToolResult] = []
            cancelled_at: Optional[int] = None
            for index, call in enumerate(reply.tool_calls):
                if self.cancellation.cancelled:
                    cancelled_at = index
                    break
                payload, ok = self._execute(call)
                outcomes.append(
                    ToolOutcome(name=call.name, arguments=call.arguments, result=payload, ok=ok)
                )
                self._notify(call.name, call.arguments, payload, ok)
                results.append(
                    ToolResult(
                        call_id=call.id,
                        name=call.name,
                        content=json.dumps(payload, ensure_ascii=False),
                        ok=ok,
                    )
                )

            if cancelled_at is not None:
                # **呼ばれたツールには必ず結果を積む** —— 「呼んだのに結果が無い」履歴は
                # 次の `run()` でプロバイダに弾かれる(Claudeは tool_use と tool_result の
                # 対応を要求する)。走らせなかったぶんは中断した旨の封筒で埋める。
                for call in reply.tool_calls[cancelled_at:]:
                    results.append(
                        ToolResult(
                            call_id=call.id,
                            name=call.name,
                            content=json.dumps(
                                {
                                    "error": {
                                        "code": "internal",
                                        "message": "中断された(利用者が停止した)",
                                        "tool": call.name,
                                        "exception": "Cancelled",
                                    }
                                },
                                ensure_ascii=False,
                            ),
                            ok=False,
                        )
                    )
                self.turns.append(ToolResultTurn(tuple(results)))
                return self._result("cancelled", "中断フラグが立った", turn, outcomes, usage)

            self.turns.append(ToolResultTurn(tuple(results)))

    # ------------------------------------------------------------------ ツール
    def _execute(self, call: ToolCall) -> tuple[Any, bool]:
        """ツールを1つ実行する。**失敗しても例外を上げない**(封筒にして返す)。"""
        if call.parse_error:
            return (
                {
                    "error": {
                        "code": "invalid_argument",
                        "message": call.parse_error,
                        "tool": call.name,
                        "exception": "ValueError",
                    }
                },
                False,
            )
        try:
            return self.tools.call(call.name, call.arguments), True
        except TypeError as error:
            # 引数の名前・数の食い違い。`internal` に落とすとモデルが諦めるので
            # 「直せる誤り」として返す(封筒の語彙は `error_envelope` と同じ)。
            return (
                {
                    "error": {
                        "code": "invalid_argument",
                        "message": str(error),
                        "tool": call.name,
                        "exception": type(error).__name__,
                    }
                },
                False,
            )
        except Exception as error:
            return {"error": self.tools.error_envelope(call.name, error)}, False

    def _notify(self, name: str, arguments: dict, result: Any, ok: bool) -> None:
        """進捗を購読者へ渡す。**購読者の失敗でループを落とさない**。"""
        if self.progress is None:
            return
        try:
            self.progress(name, arguments, result, ok)
        except Exception:  # pragma: no cover - 購読者側の事故
            pass

    def _result(
        self,
        status: str,
        detail: str,
        turns: int,
        outcomes: Sequence[ToolOutcome],
        usage: dict,
        *,
        text: str = "",
    ) -> AgentRun:
        return AgentRun(
            status=status,
            text=text,
            detail=detail,
            turns=turns,
            tool_calls=tuple(outcomes),
            usage=usage,
        )


def _merge_usage(total: dict, latest: dict) -> dict:
    """トークン使用量を足し合わせる(数値の欄だけ)。"""
    merged = dict(total)
    for key, value in (latest or {}).items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            merged[key] = merged.get(key, 0) + value
    return merged
