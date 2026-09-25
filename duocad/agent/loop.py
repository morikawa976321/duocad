"""エージェントループ —— **プロバイダ非依存**の tool-use 往復。

`Session` の文書に対して、モデルに道具(`DocumentTools`)を渡して往復させる。UIは持たない。

1. **道具の定義を第二の場所に書かない。** ツール一覧は `DocumentTools.descriptors()`、実行は
   `.call()`、失敗の封筒は `.error_envelope()`。MCP経路(`duocad/mcp_server.py`)と顔ぶれ・
   挙動・失敗の形が同一になる。
2. **暴走を必ず打ち切る。** `max_turns` / `max_tokens` / 1リクエストのタイムアウト /
   外から立てる中断フラグの4つ。打ち切ったときは理由を構造化して返す(実測: 矛盾した指示を
   渡すと思考が膨らみ、1リクエストが600秒返らなかった。伸びるのは文脈長ではなく思考の側
   なので、**上限と短いタイムアウトが要る**)。
3. **ツールの実行時エラーでループを落とさない。** 構造化封筒(`not_found` /
   `invalid_argument` / `selection_not_committed` / `selection_empty` /
   `selection_kind_mismatch` / `invalid_predicate` / `internal`)をそのままモデルへ返して
   自己修復させる(実測でこれは機能した)。
4. **UIの知識を持たない。** 進捗は `progress(name, arguments, result, ok)` のコールバックへ
   流すだけ。

    tools = DocumentTools(session)
    loop = AgentLoop(tools, build_provider("openai_compat"))
    run = loop.run("この文書のボディを教えて")
    run.status   # "completed" / "max_turns" / "cancelled" / "provider_error" / ...

会話は `AgentLoop` に積み上がる(`run()` を続けて呼べば続きになる)。`reset()` で捨てる。

**OCP / build123d をimportしない**。
"""
from __future__ import annotations

import json
import logging
import re
import threading
import unicodedata
from time import monotonic
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol, Sequence, runtime_checkable

from .. import catalog
from .providers import (
    AssistantTurn,
    Provider,
    ProviderError,
    ProviderTimeout,
    ToolCall,
    ToolResult,
    ToolResultTurn,
    Turn,
    UserTurn,
)

#: **`logging.basicConfig()` は呼ばない**(設定はアプリ側の責務。ライブラリ側のコードなので
#: ハンドラを自分で足さない)。ハンドラ未設定時は `logging` の `lastResort` がstderrへ出す。
#: **stdoutへは出さない** —— MCPサーバ(`duocad/mcp_server.py`)がstdoutをJSON-RPC専用にしている。
_logger = logging.getLogger(__name__)

#: 1回の `run()` で許すモデルとの往復の上限。
DEFAULT_MAX_TURNS = 12

#: 1リクエストの出力上限(思考+本文)。**待ち時間の上限はここだけが決める**。
#:
#: 思考付きモデルは上限が小さいと思考の途中で切れて本文が空で返る。空返しを避けようと
#: 上限を大きく取ると**最悪値を捨てる**ことになり、実際に1リクエストで7,468トークン
#: (120秒)生成して下の待ち上限に当たった。
#:
#: いま小さくできるのは、**思考の打ち切りをサーバ側が担うようになった**からである ——
#: llama-server の `--reasoning-budget N` は思考をNトークンで切り、
#: `--reasoning-budget-message` が思考終了タグの直前へ文を注入するので、
#: 思考が尽きても本文は返る(空返しの原因が消えた)。
#:
#: **サーバ側の `--n-predict` は上限ではなく既定値**であり、ここで `max_tokens` を明示すると
#: そちらが勝つ(実測: `--n-predict 1024` のサーバへ16384を送ると3,520トークン返った)。
#: したがって**保証はクライアント側のこの定数にしか置けない**。
#:
#: 1024の根拠: `--reasoning-budget 512` のサーバで実タスクを20試行し、
#: 打ち切り0/20・誤り0/20・最悪4.7秒だった。上限に当たっても
#: `run()` は切れた引数のツールを実行せず `max_tokens` を構造化して返す(下記)ので、
#: 小さすぎたときの害は「止まる」だけで、壊れた形が残ることはない。
DEFAULT_MAX_TOKENS = 1024

#: 待ち上限の算定に使う、**いちばん遅い実行環境**のスループット(tok/s)。
#: dGPU無しのラップトップ(Core Ultra 255H・DDR5-5600)を想定した値。
MIN_TOKENS_PER_SEC = 10.0
#: 接続とプリフィルに見込む固定分(秒)。
REQUEST_OVERHEAD_SEC = 15.0

#: 1リクエストの待ち上限(秒)。**`DEFAULT_MAX_TOKENS` から導出する**。
#:
#: **上限を独立させると片方だけ動かして事故る** —— 実際に出力上限だけを16384へ上げて
#: この値を据え置いたとき、名目16384に対して実効は約7,450トークンで頭打ちになり、
#: そこに当たって切断された。
#:
#: 役割も変えてある。待ち時間を決めるのは `DEFAULT_MAX_TOKENS` であり、ここは
#: **ネットワークやプロセスのハングを検知するための保険**でしかない。だから
#: いちばん遅い環境でも誤発火しない長さを取る(速い環境では到達しない)。
DEFAULT_REQUEST_TIMEOUT = DEFAULT_MAX_TOKENS / MIN_TOKENS_PER_SEC + REQUEST_OVERHEAD_SEC

#: 1回の `run()` 全体の待ち上限(秒)。**人が待てる時間はここだけが決める**。
#:
#: `max_tokens` と `request_timeout` は1リクエストしか縛らず、`max_turns` は回数しか
#: 縛らない。**総時間は往復数との掛け算**なので、部分の上限をいくら締めても決まらない ——
#: 実測で、1リクエストが上限内(最悪16.5秒)のまま12往復を空回りして合計88秒かかった。
#: 待っている人には、1回の暴走か12回の空回りかは区別がつかない。
#:
#: 30の根拠: 正常な複合多段が12.5〜16.1秒、単純な依頼が7.2〜7.4秒だったので、
#: 正常系には当たらず、堂々巡り(88秒)は確実に切れる位置。**足りなければ延ばすのではなく、
#: 何に時間を使ったかを見ること** —— 正常系が30秒に当たるなら、それは上限ではなく
#: 往復数の問題である(`describe_document` のようにアプリが既に知っている情報の往復を消す)。
DEFAULT_MAX_TOTAL_SECONDS = 30.0

#: 進捗の口。`(ツール名, 引数, 結果, 成否)`。Gがこれを購読して画面に出す。
ProgressCallback = Callable[[str, dict, Any, bool], None]


@runtime_checkable
class ToolSurface(Protocol):
    """ループが道具に要求する面(`DocumentTools` がこれを満たす)。

    **公開APIの5つだけ**に依存する —— 内部(`_build_descriptors` 等)には触らない。
    `current_seq` は `!=` で比較するだけの不透明な値(`DocumentTools.current_seq()` は
    `(ログの本数, 選択の指紋, 可視性の指紋)` のタプルを返す。可視性は操作ログに載らないので
    指紋を持たないと「AIが考えている間に人がボディを隠した」が版に出ない)。
    `call_and_seq` は実行と**直後の**版をひとまとまりで返す(`(結果, 例外, 版)`。例外は上げない) ——
    本番の覆い(`ToolsOnLoop`)はこれをイベントループへの1回の投げ返しで走らせるので、実行の完了と
    版の読み取りの間に人の操作が挟まらない。
    """

    def descriptors(self) -> list: ...

    def call(self, name: str, arguments: dict) -> dict: ...

    def call_and_seq(self, name: str, arguments: dict) -> tuple: ...

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
    """打ち切りの4つ(5つ目の中断フラグは `Cancellation`)。

    **総時間を決めるのは `max_total_seconds` だけ**である。他の3つは部分しか縛らない ——
    `max_tokens` は1リクエスト、`request_timeout` は1リクエストの待ち、`max_turns` は回数。
    掛け算(往復数 × 1往復)が人の待ち時間になるので、そこに直接の上限を置く。
    """

    max_turns: int = DEFAULT_MAX_TURNS
    max_tokens: int = DEFAULT_MAX_TOKENS
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT
    max_total_seconds: float = DEFAULT_MAX_TOTAL_SECONDS


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
    - `timeout`        … **合計時間**の上限で打ち切った(`max_turns` に届く前に時間が尽きた)。
                         往復の境目で切るので、実行済みのツールの結果は `tool_calls` に入る
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
        deadline = monotonic() + self.limits.max_total_seconds

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
            # **人が待てる時間の上限**。`max_tokens` は1リクエストしか縛らず、
            # `max_turns` は回数しか縛らないので、**どちらも総時間を決めない** ——
            # 実測で、曖昧な依頼のとき query ツールを堂々巡りして12往復を使い切り、
            # 1リクエストが上限内(最悪16.5秒)のまま**合計88秒**かかった。
            # 待っている人から見れば、それが1回の暴走か12回の空回りかは区別がつかない。
            # **次の往復を始める前**に見る(往復の途中で切ると、実行済みのツールの
            # 結果を積まないまま抜けることになる。中断と同じ扱いで境界を揃える)。
            if monotonic() >= deadline:
                return self._timeout_result(turn, outcomes, usage)

            turn += 1
            # **楽観ロックの版はここで控える** —— これから始まる
            # `provider.complete()` はモデルへのHTTPリクエストで数十秒〜数分かかりうる。
            # その間に人(またはMCP経由の別クライアント)が文書や選択を変えていたら、
            # モデルがいま組み立てているツール呼び出しは、自分が読んだ状態を前提にした
            # 古い決定になる。ツールを実行する**直前ごとに**(1件目だけでなく)これと
            # 比べる(下記「文書(または選択)が変わっていたら実行しない」)。
            seq_before_request = self.tools.current_seq()
            # **予算が縮めたのか、元から短かったのか**を覚えておく。
            # 「失敗した時刻が締切を過ぎているか」で判定すると取りこぼす —— 実測で、
            # 締切のわずか手前(29.2秒/上限30秒)で発火した回が `provider_error` を名乗った。
            # 縮めたのがこちらの予算なら、その事実がそのまま答えである。
            budget_left = deadline - monotonic()
            capped_by_budget = budget_left < self.limits.request_timeout
            try:
                reply = self.provider.complete(
                    system=self.system,
                    turns=self.turns,
                    tools=descriptors,
                    max_tokens=self.limits.max_tokens,
                    # **残り時間を超えて待たない**。上のdeadline判定は往復の
                    # 境目でしか見ないので、これが無いと実効上限は「合計時間 + 1リクエスト」に
                    # なる(実測: 上限30秒に対して35.2秒)。ここで頭を押さえると、
                    # はみ出しは接続を畳む分だけになる。
                    timeout=min(self.limits.request_timeout, budget_left),
                )
            except ProviderError as error:
                # **合計時間で頭を押さえた結果の失敗は `timeout` と名乗る**。
                # 上で `timeout=` を残り時間まで縮めているので、予算切れは接続の
                # タイムアウト = `ProviderError` として現れる。ここを分けないと、
                # 画面には「プロバイダの障害」と出る —— 原因はこちらの予算であって
                # 相手側ではないので、直す場所を見誤らせる。
                if capped_by_budget and isinstance(error, ProviderTimeout):
                    return self._timeout_result(turn, outcomes, usage)
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
            # ので `selection_empty` は発火しない)を含めて一律に守る網である。
            #
            # **チェックは呼び出し1件ごと**。1つの応答に複数のツール呼び出しが束ねて
            # 届くことがあり、1件目の実行そのものが版を動かす(=自分自身の行動)うえ、
            # 1件目を実行した後・2件目を実行する前にもイベントループが人の操作を処理
            # できる隙間がある(`ToolsOnLoop._run_on_loop` は呼び出しのたびに
            # イベントループへ制御を返す)。
            #
            # **run() を終了させない** —— 打ち切り(cancelled/max_turns/max_tokens)とは違い、
            # モデルは自己修復できる(実測)ので、次のターンで最新の状態を読み直させて続ける。
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
                payload, ok, seq_after = self._execute(call, expected_seq)
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
                # **基準は実行と同じ1手で読んだ版**(`call_and_seq`)。ここで改めて
                # `current_seq()` を読むと、実行の完了からこの読み取りまでの間に入った人の操作が
                # 基準に吸われ、次の直前チェックで見えなくなる(issue #48)。
                expected_seq = seq_after

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
    def _dimensions_nobody_asked_for(self, call: ToolCall) -> tuple[str, ...]:
        """人が量を言っていないのにモデルが埋めた寸法引数の名前(空なら問題なし)。

        **会話に出た全てのユーザー発話**を見る —— 「上面をR3で丸めて」のあとに
        「反対側も同じように」と続ける言い方があるので、直近の1発話に絞ると
        正しい依頼を止めてしまう。

        印が無いverb(`catalog.dimension_params` が空)は素通しする。
        `set_visible` の `visible` や `extrude` の `mode` のような**言葉で指定される
        how** はここでは見ない(印していない理由は `Param.dimension` に書いた)。
        """
        names = catalog.dimension_params(call.name)
        if not names:
            return ()
        filled = tuple(name for name in names if call.arguments.get(name) is not None)
        if not filled:
            return ()
        said = " ".join(
            turn.text for turn in self.turns if isinstance(turn, UserTurn)
        )
        return () if _mentions_a_quantity(said) else filled

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
                content=json.dumps(_failure(call.name, "internal", why, "NotExecuted"), ensure_ascii=False),
                ok=False,
            )
            for call in calls
        )

    def _execute(self, call: ToolCall, seq_before: Any) -> tuple[Any, bool, Any]:
        """ツールを1つ実行する。**失敗しても例外を上げない**(封筒にして返す)。

        戻りは `(結果, 成否, 実行直後の版)`。走らせなかったとき(引数が壊れている・寸法の捏造)の
        版は `seq_before` のまま(文書は触っていない)。
        """
        if call.parse_error:
            return _failure(call.name, "invalid_argument", call.parse_error, "ValueError"), False, seq_before
        invented = self._dimensions_nobody_asked_for(call)
        if invented:
            # **人が言っていない寸法で形を変えない**。
            # `agent_instructions()` は「曖昧・探索的な依頼なら propose_selection で止めて
            # 尋ねる」「迷ったら止める側に倒す」と書いてあるが、**モデルはこれを守れない**
            # (実測: 「角が尖ってて危ないから、いい感じに処理しといて」に対して、
            # 止まらず R1.5mm を自分で決めて fillet まで適用した)。
            # 規律を文章で頼むのをやめて、ここで機械的に止める。
            #
            # **失敗として返す**(例外にしない) —— モデルはこの封筒を読んで、
            # 提案を出して人に尋ねる側へ回れる。往復が1つ増えるだけで、
            # 適用してしまった形をundoで戻すより安い。
            # **`dimension_not_given` はアプリ内AIの経路だけの語**(MCP にはこのガードが無い。
            # 語彙の一覧は `MCP.md`「実装のメモ」、一致は `tests/test_doc_code_agreement.py`)。
            return (
                _failure(
                    call.name,
                    "dimension_not_given",
                    (
                        f"{', '.join(invented)} の値を人が指示していない。"
                        "寸法を自分で決めて形を変えてはいけない。"
                        "propose_selection で対象だけ提案し、"
                        "何を選んだかと、どの寸法が要るかを述べて尋ねること。"
                    ),
                    "DimensionNotGiven",
                ),
                False,
                seq_before,
            )
        payload, error, seq_after = self.tools.call_and_seq(call.name, call.arguments)
        if error is None:
            return payload, True, seq_after
        if isinstance(error, TypeError):
            # ハンドラの中での引数の食い違い(必須・未知キー・型はスキーマ検査が先に
            # `invalid_argument` の封筒にする)。`internal` に落とすとモデルが諦めるので
            # 「直せる誤り」として返す(封筒の語彙は `error_envelope` と同じ)。
            return _failure(call.name, "invalid_argument", str(error), type(error).__name__), False, seq_after
        return {"error": self.tools.error_envelope(call.name, error)}, False, seq_after

    def _notify(self, name: str, arguments: dict, result: Any, ok: bool) -> None:
        """進捗を購読者へ渡す。**購読者の失敗でループを落とさない**(モジュールdocstring規律3)。

        握りつぶしはしない —— 例外は外へ伝播させないが、型・メッセージ・トレースバックが
        追える形で警告ログを1件残す(`exc_info=True`)。ここが唯一の出どころなので、
        「進捗が更新されない」の原因追跡がこれで可能になる。
        """
        if self.progress is None:
            return
        try:
            self.progress(name, arguments, result, ok)
        except Exception:
            _logger.warning("progress callback failed for tool %r", name, exc_info=True)

    def _timeout_result(self, turns: int, outcomes: Sequence[ToolOutcome], usage: dict) -> AgentRun:
        return self._result(
            "timeout", f"合計時間が上限({self.limits.max_total_seconds:g}秒)に達した", turns, outcomes, usage
        )

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


def _failure(tool: str, code: str, message: str, exception: str) -> dict:
    """ループ自身が作る失敗の封筒(`{"error": {...}}`)。形は `DocumentTools.error_envelope` と同じ。

    `error_envelope` を通さないのは、それが `ToolsOnLoop` 越しにイベントループへ往復するだけで
    ここには例外が無いため。`code` の語彙は `MCP.md`「実装のメモ」が正(`dimension_not_given` だけが
    アプリ内AIの経路の語)。
    """
    return {"error": {"code": code, "message": message, "tool": tool, "exception": exception}}


#: 識別子らしいかたまり(`body_1` / `sketch-1c72a3c7` / `entity-75aa`)。
#: **区切り(`-` か `_`)を含むものだけ**を識別子とみなす —— `[A-Za-z]\w*` で広く取ると
#: `R3` や `M8` まで消えてしまい、日本語CADでいちばん普通の寸法の言い方
#: (「上面をR3で丸めて」)が「数量を言っていない」と読まれる。
#: duocadのIDは `body-` / `sketch-` / `entity-` / `pattern-` の接頭辞を持つので、
#: 区切りの有無で足りる。
_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9]*[-_][A-Za-z0-9_.-]*")
_DIGIT = re.compile(r"\d")


def _mentions_a_quantity(text: str) -> bool:
    """人が数量を口にしたか。

    **識別子の中の数字は数えない** —— `body_1` を「寸法を言った」と読むとガードが
    素通りする。全角は NFKC で寄せてから見る(日本語入力で「Ｒ３」と打たれうる)。

    **厳密な照合(モデルが渡した値がそのまま本文にあるか)にはしていない。**
    「直径8mmの円」と言われて `radius: 4` を渡すのは正しい導出であり、そこを弾くと
    普通の依頼が通らない。見るのは**人が量を与えたかどうか**だけでよい ——
    量を与えていないなら、モデルが埋めた寸法は必ず捏造である。
    """
    plain = _IDENTIFIER.sub(" ", unicodedata.normalize("NFKC", text))
    return bool(_DIGIT.search(plain))


def _merge_usage(total: dict, latest: dict) -> dict:
    """トークン使用量を足し合わせる(数値の欄だけ)。"""
    merged = dict(total)
    for key, value in (latest or {}).items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            merged[key] = merged.get(key, 0) + value
    return merged
