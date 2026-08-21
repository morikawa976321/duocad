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
   `selection_kind_mismatch` / `invalid_predicate` / `internal`)をそのままモデルへ返して自己修復させる
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

    **公開APIの4つだけ**に依存する —— 内部(`_build_descriptors` 等)には触らない。
    `current_seq` はH・2026-08-20追加(「同時編集の安全性」楽観ロック。`run()` の docstring)。
    `!=` で比較するだけの不透明な値(`DocumentTools.current_seq()` は
    `(ログの本数, 選択の指紋)` のタプルを返す)。
    """

    def descriptors(self) -> list: ...

    def call(self, name: str, arguments: dict) -> dict: ...

    def error_envelope(self, tool: str, error: Exception) -> dict: ...

    def current_seq(self) -> Any: ...


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
    - `max_tokens`     … 出力上限で切れた(答えが途中)。**そのターンのツールは実行していない**
                         —— 切れた応答のツール呼び出しは引数のJSONが途中で切れている
                         可能性があるため(`run()` の該当箇所に理由)
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
            # **楽観ロック(H・2026-08-20)の版はここで控える** —— これから始まる
            # `provider.complete()` はモデルへのHTTPリクエストで数十秒〜数分かかりうる。
            # その間に人(またはMCP経由の別クライアント)が文書や選択を変えていたら、
            # モデルがいま組み立てているツール呼び出しは、自分が読んだ状態を前提にした
            # 古い決定になる。ツールを実行する**直前ごとに**(1件目だけでなく)これと
            # 比べる(下記「文書(または選択)が変わっていたら実行しない」)。
            seq_before_request = self.tools.current_seq()
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

            if reply.stop == "max_tokens" and reply.tool_calls:
                # **上限で切られたターンのツールは走らせない。**
                # 打ち切られた応答のツール呼び出しは引数のJSONが途中で切れている
                # 可能性があり(半径が "12" のはずが "1" で届く類)、duocadのツールは
                # 文書を変える —— 「たぶん合っている引数」で形を変えるより、止めて
                # 上限に当たったことを返すほうが安い。呼ばれたぶんには実行しなかった旨の
                # 結果を積んでおく(履歴を再送できる形に保つ。中断と同じ扱い)。
                self.turns.append(
                    ToolResultTurn(
                        self._unrun_results(
                            reply.tool_calls, "出力上限で応答が切れたため実行しなかった"
                        )
                    )
                )
                return self._result(
                    "max_tokens",
                    f"stop_reason={reply.stop_reason!r}(切れた引数のままツールを実行しない)",
                    turn,
                    outcomes,
                    usage,
                    text=reply.text,
                )

            if not reply.tool_calls:
                status = {
                    "end_turn": "completed",
                    "max_tokens": "max_tokens",
                    "refusal": "refusal",
                }.get(reply.stop, "stopped")
                detail = "" if status == "completed" else f"stop_reason={reply.stop_reason!r}"
                return self._result(status, detail, turn, outcomes, usage, text=reply.text)

            # **文書(または選択)が変わっていたら実行しない(楽観ロック本体)。**
            # `_reset_selection_if_needed`(コア側)は形状を変えるverbの後で確定選択を
            # 全リセットするので、面・辺・領域を消費するverbは `selection_empty` で
            # 既に守られている —— ここは**それを経由しないverb**(`set_visible` /
            # `set_parameter` / body_id で直接効くもの等)や、**選択が空にならない
            # 入れ替え**(辺Aを外して辺Bを選び直して確定する。件数も種別も変わらない
            # ので `selection_empty` は発火しない)を含めて一律に守る網であり、
            # 「先回りして大きな仕組みを作らない」の要請どおり道具ごとの特別扱いはしない。
            #
            # **チェックは呼び出し1件ごと**(QA critical指摘・2026-08-20)。1つの応答に
            # 複数のツール呼び出しが束ねて届くことがあり、1件目の実行そのものが版を
            # 動かす(=自分自身の行動)うえ、1件目を実行した後・2件目を実行する前にも
            # イベントループが人の操作を処理できる隙間がある(`_AiToolsOnLoop._run_on_loop`
            # は呼び出しのたびにイベントループへ制御を返す)。応答が届いた直後の1回だけ
            # 見て残り全部を実行してしまうと、この隙間を見逃す。
            #
            # **run() を終了させない** —— 打ち切り(cancelled/max_turns/max_tokens)とは違い、
            # モデルは自己修復できる(2026-08-20実測)ので、次のターンで最新の状態を
            # 読み直させて続ける。
            expected_seq = seq_before_request
            results: list[ToolResult] = []
            cancelled_at: Optional[int] = None
            stale_at: Optional[int] = None
            stale_seq: Optional[Any] = None
            for index, call in enumerate(reply.tool_calls):
                if self.cancellation.cancelled:
                    cancelled_at = index
                    break
                observed_seq = self.tools.current_seq()
                if observed_seq != expected_seq:
                    stale_at = index
                    stale_seq = observed_seq
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
                # 自分がいま実行した呼び出しの結果を、次の呼び出し前チェックの基準にする
                # (=「自分の行動で版が動いた」ことを「よそからの変更」と誤検出しない)。
                expected_seq = self.tools.current_seq()

            if cancelled_at is not None:
                # 走らせなかったぶんは中断した旨で埋める(下の `_unrun_results` 参照)。
                results.extend(
                    self._unrun_results(
                        reply.tool_calls[cancelled_at:], "中断された(利用者が停止した)"
                    )
                )
                self.turns.append(ToolResultTurn(tuple(results)))
                return self._result("cancelled", "中断フラグが立った", turn, outcomes, usage)

            if stale_at is not None:
                results.extend(
                    self._unrun_results(
                        reply.tool_calls[stale_at:],
                        "文書または選択が変わったため実行しなかった"
                        f"(seq {expected_seq!r} -> {stale_seq!r})。"
                        "describe_document / get_selection で最新の状態を確認し、"
                        "選択をやり直してから再試行すること。",
                    )
                )
                self.turns.append(ToolResultTurn(tuple(results)))
                continue

            self.turns.append(ToolResultTurn(tuple(results)))

    # ------------------------------------------------------------------ ツール
    def _unrun_results(self, calls: Sequence[ToolCall], why: str) -> tuple[ToolResult, ...]:
        """**走らせなかったツール呼び出しにも結果を積む**(中断・出力上限で打ち切ったとき)。

        「呼んだのに結果が無い」履歴は次の `run()` でプロバイダに弾かれる
        (Claudeは `tool_use` と `tool_result` の1対1の対応を要求する)。理由も
        モデルへ伝わるので、次のターンで同じ手を打ち直せる。
        """
        return tuple(
            ToolResult(
                call_id=call.id,
                name=call.name,
                content=json.dumps(
                    {
                        "error": {
                            "code": "internal",
                            "message": why,
                            "tool": call.name,
                            "exception": "NotExecuted",
                        }
                    },
                    ensure_ascii=False,
                ),
                ok=False,
            )
            for call in calls
        )

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
