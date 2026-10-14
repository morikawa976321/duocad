"""エージェントループ — プロバイダ非依存の tool-use 往復。

`Session` の文書に対して、モデルに道具(`DocumentTools`)を渡して往復させる。UIは持たない。

1. 道具の定義を第二の場所に書かない: ツール一覧は `DocumentTools.descriptors()`、実行は `.call()`、失敗の封筒は
   `.error_envelope()`。MCP経路(`duocad/mcp_server.py`)と顔ぶれ・挙動・失敗の形が同一になる。
2. 暴走を必ず打ち切る: `max_turns` / `max_tokens` / 1リクエストのタイムアウト / 外から立てる中断フラグの4つ。
   打ち切りは理由を構造化して返す(実測: 矛盾した指示を渡すと思考が膨らみ、1リクエストが600秒返らなかった。
   伸びるのは文脈長ではなく思考の側なので、上限と短いタイムアウトが要る)。
3. ツールの実行時エラーでループを落とさない: 構造化封筒をそのままモデルへ返して自己修復させる(実測で機能した)。
4. UIの知識を持たない: 進捗は `progress(name, arguments, result, ok)` のコールバックへ流すだけ。

    tools = DocumentTools(session)
    loop = AgentLoop(tools, build_provider("openai_compat"))
    run = loop.run("この文書のボディを教えて")
    run.status   # "completed" / "max_turns" / "cancelled" / "provider_error" / ...

会話は `AgentLoop` に積み上がる(`run()` を続けて呼べば続きになる)。`reset()` で捨てる。
済んだ依頼は「人の発話 + AIの最終返答」だけを残す(ツールの往復は次の `run()` の頭で捨てる)。
相手の文脈長に近づいたら、古い依頼を半分まとめて捨てる。

OCP / build123d をimportしない。
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
    ProviderContextOverflow,
    ProviderError,
    ProviderTimeout,
    ToolCall,
    ToolResult,
    ToolResultTurn,
    Turn,
    UserTurn,
)

#: `logging.basicConfig()` は呼ばない(設定はアプリ側の責務)。stdoutへは出さない — MCPサーバ(`duocad/mcp_server.py`)がstdoutをJSON-RPC専用にしている。
_logger = logging.getLogger(__name__)

#: 1回の `run()` で許すモデルとの往復の上限。
DEFAULT_MAX_TURNS = 12

#: 1リクエストの出力上限(思考+本文)。待ち時間の上限はここだけが決める。
#: 思考付きモデルは上限が小さいと思考の途中で切れて本文が空で返るが、大きく取ると最悪値を捨てる(実際に1リクエストで7,468トークン(120秒)生成して待ち上限に当たった)。
#: 小さくできるのは思考の打ち切りをサーバ側が担うから: llama-server の `--reasoning-budget N` が思考をNトークンで切り、
#: `--reasoning-budget-message` が思考終了タグの直前へ文を注入するので、思考が尽きても本文は返る。
#: サーバ側の `--n-predict` は上限ではなく既定値で、ここで `max_tokens` を明示するとそちらが勝つ
#: (実測: `--n-predict 1024` のサーバへ16384を送ると3,520トークン返った)ので、保証はこの定数にしか置けない。
#: 1024の根拠: `--reasoning-budget 512` のサーバで実タスク20試行、打ち切り0/20・誤り0/20・最悪4.7秒。
#: 上限に当たっても `run()` は切れた引数のツールを実行せず `max_tokens` を構造化して返すので、小さすぎたときの害は「止まる」だけ。
DEFAULT_MAX_TOKENS = 1024

#: 待ち上限の算定に使う、**いちばん遅い実行環境**のスループット(tok/s)。
#: dGPU無しのラップトップ(Core Ultra 255H・DDR5-5600)を想定した値。
MIN_TOKENS_PER_SEC = 10.0
#: 接続とプリフィルに見込む固定分(秒)。
REQUEST_OVERHEAD_SEC = 15.0

#: 1リクエストの待ち上限(秒)。`DEFAULT_MAX_TOKENS` から導出する(独立させると片方だけ動かして事故る: 出力上限だけを16384へ上げて
#: 据え置いたとき、実効は約7,450トークンで頭打ちになり切断された)。待ち時間を決めるのは `DEFAULT_MAX_TOKENS` で、ここはネットワークや
#: プロセスのハングを検知する保険。いちばん遅い環境でも誤発火しない長さを取る。
DEFAULT_REQUEST_TIMEOUT = DEFAULT_MAX_TOKENS / MIN_TOKENS_PER_SEC + REQUEST_OVERHEAD_SEC

#: 1回の `run()` 全体の待ち上限(秒)。人が待てる時間はここだけが決める(他の3つは部分しか縛らず、総時間は往復数との掛け算になる。
#: 実測: 1リクエストが上限内(最悪16.5秒)のまま12往復を空回りして合計88秒)。
#: 30の根拠: 正常な複合多段が12.5〜16.1秒、単純な依頼が7.2〜7.4秒で、正常系には当たらず堂々巡り(88秒)は確実に切れる位置。
#: 足りなければ延ばさず何に時間を使ったかを見る(往復数の問題。`describe_document` のようにアプリが既に知っている情報の往復を消す)。
DEFAULT_MAX_TOTAL_SECONDS = 30.0

#: ウォームアップ(`AgentLoop.prepare`)の待ち上限(秒)。cold の読み込みは遅い機種で分単位になるので通常の往復とは別に取る。
#: ハングの保険として自分で置いた値で、実測から導いた待ち時間ではない。
PREPARE_TIMEOUT = 1800.0

#: 依頼のたびに、モデルの代わりに先に呼んでおく問い合わせ(`AgentLoop(first_look=FIRST_LOOK)`)。
#: 実測で、モデルはこの2つを依頼のたびに最初に呼ぶ。
FIRST_LOOK = ("describe_document", "get_selection")

#: 先に呼んだ結果がこれより大きいときは、一覧を件数に畳む(文字)。**自分で置いた値**
#: (実測: 2ボディ+2スケッチで約1,300文字、40ボディ+8スケッチで約7,800文字)。
FIRST_LOOK_MAX_CHARS = 4000

#: 進捗の口。`(ツール名, 引数, 結果, 成否)`。Gがこれを購読して画面に出す。
ProgressCallback = Callable[[str, dict, Any, bool], None]


@runtime_checkable
class ToolSurface(Protocol):
    """ループが道具に要求する面(`DocumentTools` がこれを満たす)。公開APIの5つだけに依存する(内部には触らない)。

    `current_seq` は `!=` で比較するだけの不透明な値(`DocumentTools.current_seq()` は `(文書の世代, ログの本数, 選択の指紋, 可視性の指紋)`。
    可視性は操作ログに載らないので、指紋が無いと「AIが考えている間に人がボディを隠した」が版に出ない)。
    `call_and_seq` は実行と直後の版を `(結果, 例外, 版)` で返す(例外は上げない)。本番の覆い(`ToolsOnLoop`)はこれを
    イベントループへの1回の投げ返しで走らせるので、実行の完了と版の読み取りの間に人の操作が挟まらない。
    """

    def descriptors(self) -> list: ...

    def call(self, name: str, arguments: dict) -> dict: ...

    def call_and_seq(self, name: str, arguments: dict) -> tuple: ...

    def error_envelope(self, tool: str, error: Exception) -> dict: ...

    def current_seq(self) -> Any: ...


class Cancellation:
    """外から立てる中断フラグ(スレッドをまたいで立ててよい)。

    ループはモデルへ投げる前とツールを1つ実行するたびに見る。走っているHTTPリクエストは、`on_cancel` で繋いだ
    プロバイダの `abort` が接続を閉じて止める(閉じないと、サーバは誰も読まない応答を最後まで生成し続ける)。
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._listeners: list[Callable[[], None]] = []

    def on_cancel(self, listener: Callable[[], None]) -> None:
        """中断が立った瞬間に呼ぶものを足す(`cancel()` を呼んだスレッドで走る)。"""
        self._listeners.append(listener)

    def cancel(self) -> None:
        self._event.set()
        for listener in list(self._listeners):
            try:
                listener()
            except Exception:
                _logger.warning("cancel listener failed", exc_info=True)

    def clear(self) -> None:
        self._event.clear()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()


@dataclass(frozen=True)
class Limits:
    """打ち切りの4つ(5つ目の中断フラグは `Cancellation`)。総時間を決めるのは `max_total_seconds` だけ(他の3つは1リクエスト・待ち・回数しか縛らない)。"""

    max_turns: int = DEFAULT_MAX_TURNS
    max_tokens: int = DEFAULT_MAX_TOKENS
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT
    max_total_seconds: float = DEFAULT_MAX_TOTAL_SECONDS
    #: 次の往復のために文脈に残す余地(トークン)。足りなければ古い依頼を捨てる(`_short_of_room`)。`None` なら `max_tokens`。
    #: 出力上限が文脈長に近いと、ツールを呼ぶたびに履歴が捨てられる(捨てた依頼の寸法を寸法ガードが見失う)ので、そのときは分ける。
    room_tokens: Optional[int] = None


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
    """1回の `run()` の結果。打ち切りの理由は構造化して返す(Gが画面に出す)。

    `status`:

    - `completed`      … モデルがツールを呼ばずに答えた(正常終了)
    - `empty_reply`    … 本文もツール呼び出しも無いまま、正常な終了理由で返ってきた(結論を出さずに止まった)
    - `max_turns`      … 往復の上限で打ち切った
    - `timeout`        … 合計時間の上限で打ち切った。往復の境目で切るので、実行済みのツールの結果は `tool_calls` に入る
    - `cancelled`      … 中断フラグが立った
    - `max_tokens`     … 出力上限で切れた。そのターンのツールは実行していない(切れた応答の引数JSONは途中で切れている可能性がある。`run()` の該当箇所)
    - `refusal`        … プロバイダが安全上の理由で断った
    - `provider_error` … HTTP・接続・応答の形(`detail` に理由。APIキーは含まれない)
    - `context_overflow` … 今の依頼だけで相手の文脈長を超えた(古い依頼は捨て尽くした)
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
        first_look: Sequence[str] = (),
    ) -> None:
        self.tools = tools
        self.provider = provider
        #: 依頼のたびに先に呼んでおく、引数なしの問い合わせの名前(`_look_first`)。
        self.first_look = tuple(first_look)
        #: **MCPの `instructions` と同じ文**(catalog.py の1本から読む)。
        self.system = catalog.agent_instructions() if system is None else system
        self.limits = limits
        self.progress = progress
        self.cancellation = cancellation or Cancellation()
        abort = getattr(provider, "abort", None)
        if callable(abort):
            self.cancellation.on_cancel(abort)
        self.turns: list[Turn] = []

    # ------------------------------------------------------------------ 会話
    def reset(self) -> None:
        """会話を捨てる(文書は触らない)。"""
        self.turns = []

    def prepare(self) -> dict:
        """system + tools を先に読ませておく(会話には何も足さない)。

        `run()` と同じ文・同じツール定義を渡す(1文字違うと相手のキャッシュは食い違った所から先を使えない)。
        失敗は握って返す(サーバがまだ居ないのは事故ではなく、最初の依頼がそれを言う)。
        """
        prepare = getattr(self.provider, "prepare", None)
        if not callable(prepare):
            return {}
        try:
            return prepare(system=self.system, tools=self.tools.descriptors(), timeout=PREPARE_TIMEOUT)
        except ProviderError as error:
            return {"error": str(error)}

    def run(self, prompt: str) -> AgentRun:
        """1つの指示を投げて、モデルがツールを呼ばなくなるまで回す。"""
        # 前の依頼の中断で立ったプロバイダの印を下ろす。この後に届いた中断は下の判定が拾う。
        rearm = getattr(self.provider, "rearm", None)
        if callable(rearm):
            rearm()
        self._settle_history()
        self.turns.append(UserTurn(prompt))
        self._look_first()
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
            # 人が待てる時間の上限。`max_tokens` は1リクエスト、`max_turns` は回数しか縛らず、どちらも総時間を決めない
            # (実測: 曖昧な依頼で query ツールを堂々巡りして12往復を使い切り、1リクエストが上限内(最悪16.5秒)のまま合計88秒)。
            # 次の往復を始める前に見る(往復の途中で切ると、実行済みのツールの結果を積まないまま抜ける。中断と同じ扱いで境界を揃える)。
            if monotonic() >= deadline:
                return self._timeout_result(turn, outcomes, usage)

            turn += 1
            # 楽観ロックの版はここで控える: `provider.complete()` は数十秒〜数分かかりうり、その間に人(またはMCP経由の別クライアント)が
            # 文書や選択を変えると、モデルが組み立てているツール呼び出しは古い状態を前提にした決定になる。
            # ツールを実行する直前ごとに(1件目だけでなく)これと比べる(下記「文書(または選択)が変わっていたら実行しない」)。
            seq_before_request = self.tools.current_seq()
            # 予算が縮めたのか元から短かったのかを覚えておく。「失敗した時刻が締切を過ぎているか」で判定すると取りこぼす
            # (実測: 締切の手前(29.2秒/上限30秒)で発火した回が `provider_error` を名乗った)。
            budget_left = deadline - monotonic()
            capped_by_budget = budget_left < self.limits.request_timeout
            try:
                reply = self.provider.complete(
                    system=self.system,
                    turns=self.turns,
                    tools=descriptors,
                    max_tokens=self.limits.max_tokens,
                    # 残り時間を超えて待たない: deadline判定は往復の境目でしか見ないので、これが無いと実効上限は「合計時間 + 1リクエスト」になる
                    # (実測: 上限30秒に対して35.2秒)。
                    timeout=min(self.limits.request_timeout, budget_left),
                )
            except ProviderContextOverflow as error:
                # 相手は処理を始めずに断るので、やり直しは安い。捨てるものが尽きたら
                # (今の依頼だけで収まらない)そのことを返す。
                if self._drop_oldest_half():
                    turn -= 1
                    continue
                return self._result("context_overflow", str(error), turn, outcomes, usage)
            except ProviderError as error:
                # 中断が接続を閉じた結果の失敗は、障害ではなく中断である。
                if self.cancellation.cancelled:
                    return self._result("cancelled", "中断フラグが立った", turn, outcomes, usage)
                # 合計時間で頭を押さえた結果の失敗は `timeout` と名乗る(上で `timeout=` を残り時間まで縮めているので、予算切れは
                # 接続のタイムアウト = `ProviderError` として現れる。分けないと画面に「プロバイダの障害」と出て直す場所を見誤らせる)。
                if capped_by_budget and isinstance(error, ProviderTimeout):
                    return self._timeout_result(turn, outcomes, usage)
                return self._result("provider_error", str(error), turn, outcomes, usage)

            usage = _merge_usage(usage, reply.usage)
            # 次の往復に出力1回ぶんの余地が残らないなら先に古い依頼を捨てる(余地が無いまま続けると書く途中で文脈が尽きて切れる)。
            # 見るのは続きがあるとき(ツールを呼んだ)と既に切れたときだけ(終わった依頼のツール往復は次の `run()` が畳む)。
            if (reply.tool_calls or reply.stop == "max_tokens") and self._short_of_room(reply.usage):
                if self._drop_oldest_half() and reply.stop == "max_tokens":
                    # 切れた原因は出力上限ではなく文脈の残り。空けたのでこの往復をやり直す。
                    turn -= 1
                    continue
            self.turns.append(
                AssistantTurn(
                    text=reply.text, tool_calls=reply.tool_calls, raw=reply.raw_message
                )
            )

            if reply.stop == "max_tokens" and reply.tool_calls:
                # 上限で切られたターンのツールは走らせない: 引数のJSONが途中で切れている可能性があり(半径が "12" のはずが "1" で届く類)、
                # duocadのツールは文書を変える。止めて上限に当たったことを返すほうが安い。
                # 呼ばれたぶんには実行しなかった旨の結果を積む(履歴を再送できる形に保つ。中断と同じ扱い)。
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
                if status == "completed" and not reply.text:
                    # 思考が縮退ループした後に本文もツール呼び出しも出さず終わる(#137)。completed にすると、画面では
                    # 何もせずに「完了」と出る。再試行はしない(同じ文脈で同じ長考に入りやすく、1回で数十分かかりうる)。
                    status = "empty_reply"
                detail = "" if status == "completed" else f"stop_reason={reply.stop_reason!r}"
                return self._result(status, detail, turn, outcomes, usage, text=reply.text)

            # 文書(または選択)が変わっていたら実行しない(楽観ロック本体)。コア側の `_reset_selection_if_needed` が形状を変えるverbの後で
            # 選択を全リセットするので、面・辺・領域を消費するverbは `selection_empty` で既に守られている。ここはそれを経由しないverb
            # (`set_visible` / body_id で直接効くもの等)や、選択が空にならない入れ替え(辺Aを外して辺Bを選び直す。件数も種別も変わらない)を
            # 含めて一律に守る網。
            # チェックは呼び出し1件ごと: 1つの応答に複数のツール呼び出しが束ねて届くことがあり、1件目の実行そのものが版を動かすうえ、
            # 1件目の後・2件目の前にもイベントループが人の操作を処理できる隙間がある(`ToolsOnLoop._run_on_loop` は呼び出しのたびにイベントループへ制御を返す)。
            # run() を終了させない: 打ち切りと違いモデルは自己修復できる(実測)ので、次のターンで最新の状態を読み直させて続ける。
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
                # 自分がいま実行した結果を次の呼び出し前チェックの基準にする(自分の行動で版が動いたことを「よそからの変更」と誤検出しない)。
                # 基準は実行と同じ1手で読んだ版(`call_and_seq`): ここで改めて `current_seq()` を読むと、実行完了からの間に入った人の操作が
                # 基準に吸われて次の直前チェックで見えなくなる(issue #48)。
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

    # ------------------------------------------------------------------ 履歴
    def _look_first(self) -> None:
        """モデルが依頼のたびに最初に呼ぶ問い合わせを、代わりに呼んで履歴へ置く(その往復を消す)。

        「モデルが呼んで結果が返った」形で置く(発話に書き足さない): 実測(Gemma 4 12B)で、発話の末尾へ添えると往復は減るが最初の返事の思考が
        伸びて書く量が約1.5倍になり、全体では遅くなった。発話と分かれるので、寸法のガードが結果の数字を「人が言った数量」と読むこともない。
        進捗と `AgentRun.tool_calls` には載せない(モデルの判断ではないので画面に「AIが呼んだ」と出さない)。取れなければ置かずに進む(モデルは自分で呼べる)。
        """
        if not self.first_look:
            return
        calls: list[ToolCall] = []
        results: list[ToolResult] = []
        try:
            for index, name in enumerate(self.first_look):
                content = json.dumps(_condensed(self.tools.call(name, {})), ensure_ascii=False)
                calls.append(ToolCall(id=f"{FIRST_LOOK_ID}{index}", name=name, arguments={}))
                results.append(ToolResult(call_id=calls[-1].id, name=name, content=content, ok=True))
        except Exception:
            _logger.warning("first look failed", exc_info=True)
            return
        self.turns.append(AssistantTurn(text="", tool_calls=tuple(calls)))
        self.turns.append(ToolResultTurn(tuple(results)))

    def _settle_history(self) -> None:
        """済んだ依頼を「人の発話 + AIの最終返答」の2つへ畳む(ツールの往復は捨てる)。

        ツールの結果は量の大半を占め、文書が変わると古い(残すとモデルが今は無い状態を信じる)。人の発話は残す(「反対側も同じように」は
        前の依頼を指し、寸法のガード(`_dimensions_nobody_asked_for`)は過去の発話から数量を探す)。
        最後まで進まなかった依頼には返答の代わりに1行の注記を置く(発話が2つ続く履歴を作らない。役割の交互を要求するチャットテンプレートがある)。
        """
        settled: list[Turn] = []
        for request in _requests(self.turns):
            if not isinstance(request[0], UserTurn):
                continue
            last = request[-1]
            answered = isinstance(last, AssistantTurn) and not last.tool_calls and last.text
            settled.append(request[0])
            settled.append(AssistantTurn(text=last.text if answered else _unfinished_note(request)))
        self.turns = settled

    def _drop_oldest_half(self) -> bool:
        """済んだ依頼の古い側半分を捨てる。捨てるものが無ければ `False`。

        まとめて捨てる: 先頭を削ると相手のキャッシュはそこから後ろを全部読み直すので、1依頼ずつだと毎回読み直す
        (実測: 18依頼で総処理量が約2倍、半分ずつなら約1.2倍)。今の依頼には触らない。
        """
        current = max(
            (index for index, turn in enumerate(self.turns) if isinstance(turn, UserTurn)), default=0
        )
        settled = current // 2
        if settled == 0:
            return False
        del self.turns[: 2 * ((settled + 1) // 2)]
        return True

    def _short_of_room(self, usage: dict) -> bool:
        """いまの往復の続きに、出力1回ぶん(`Limits.room_tokens`)の余地が残っていないか。相手の文脈長とこの往復の実測トークン数が分かるときだけ判定する(推定はしない)。"""
        used = (usage.get("prompt_tokens"), usage.get("completion_tokens"))
        if not all(isinstance(value, (int, float)) for value in used):
            return False
        context_size = getattr(self.provider, "context_size", None)
        n_ctx = context_size() if callable(context_size) else None
        room = self.limits.max_tokens if self.limits.room_tokens is None else self.limits.room_tokens
        return bool(n_ctx) and used[0] + used[1] + room > n_ctx

    # ------------------------------------------------------------------ ツール
    def _dimensions_nobody_asked_for(self, call: ToolCall) -> tuple[str, ...]:
        """人が量を言っていないのにモデルが埋めた寸法引数の名前(空なら問題なし)。

        会話に出た全てのユーザー発話を見る(「上面をR3で丸めて」のあとの「反対側も同じように」があるので、直近の1発話に絞ると正しい依頼を止める)。
        印が無いverb(`catalog.dimension_params` が空)は素通しする。`set_visible` の `visible` や `extrude` の `mode` のような
        言葉で指定される how は見ない(理由は `Param.dimension`)。
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
        """走らせなかったツール呼び出しにも結果を積む(中断・出力上限で打ち切ったとき)。

        「呼んだのに結果が無い」履歴は次の `run()` でプロバイダに弾かれる(Claudeは `tool_use` と `tool_result` の1対1を要求する)。
        理由もモデルへ伝わり、次のターンで同じ手を打ち直せる。
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
        """ツールを1つ実行する。失敗しても例外を上げない(封筒にして返す)。

        戻りは `(結果, 成否, 実行直後の版)`。走らせなかったとき(引数が壊れている・寸法の捏造)の版は `seq_before` のまま。
        """
        if call.parse_error:
            return _failure(call.name, "invalid_argument", call.parse_error, "ValueError"), False, seq_before
        invented = self._dimensions_nobody_asked_for(call)
        if invented:
            # 人が言っていない寸法で形を変えない。モデルは「寸法を自分で決めない」を文章だけでは守れない(実測: 「角が尖ってて危ないから、
            # いい感じに処理しといて」に対して、止まらず R1.5mm を自分で決めて fillet まで適用した)ので、ここで機械的に止める。
            # 失敗として返す(例外にしない): モデルはこの封筒を読んで対象を選ぶところまでにして人に尋ねる側へ回れる。
            # `dimension_not_given` はアプリ内AIの経路だけの語(MCP にはこのガードが無い。語彙の一覧は `MCP.md`「実装のメモ」、一致は `tests/test_doc_code_agreement.py`)。
            return (
                _failure(
                    call.name,
                    "dimension_not_given",
                    (
                        f"{', '.join(invented)} の値を人が指示していない。"
                        "寸法を自分で決めて形を変えてはいけない。"
                        "set_selection で対象だけ選び、"
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
            # ハンドラの中での引数の食い違い(必須・未知キー・型はスキーマ検査が先に `invalid_argument` の封筒にする)。
            # `internal` に落とすとモデルが諦めるので「直せる誤り」として返す。
            return _failure(call.name, "invalid_argument", str(error), type(error).__name__), False, seq_after
        return {"error": self.tools.error_envelope(call.name, error)}, False, seq_after

    def _notify(self, name: str, arguments: dict, result: Any, ok: bool) -> None:
        """進捗を購読者へ渡す。購読者の失敗でループを落とさない(モジュールdocstring規律3)。例外は外へ伝播させず、型・メッセージ・トレースバックが追える警告ログを1件残す。"""
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


#: 先に呼んだ問い合わせの呼び出しIDの頭(モデルが呼んだものと見分けるため)。
FIRST_LOOK_ID = "first_look_"


def _condensed(payload: Any) -> Any:
    """先に呼んだ結果が大きすぎるとき、一覧を件数に畳む(要るときにモデルが同じツールを呼び直すほうが、依頼のたびに読ませるより安い)。"""
    if len(json.dumps(payload, ensure_ascii=False)) <= FIRST_LOOK_MAX_CHARS:
        return payload

    def counted(value: Any) -> Any:
        if isinstance(value, list):
            return f"({len(value)}件。大きいので省いた。要るときはこのツールを呼び直す)"
        if isinstance(value, dict):
            return {key: counted(item) for key, item in value.items()}
        return value

    return counted(payload)


def _requests(turns: Sequence[Turn]) -> list[list[Turn]]:
    """履歴を依頼ごとに切る(人の発話から次の発話の手前まで)。"""
    requests: list[list[Turn]] = []
    for turn in turns:
        if isinstance(turn, UserTurn) or not requests:
            requests.append([])
        requests[-1].append(turn)
    return requests


def _unfinished_note(request: Sequence[Turn]) -> str:
    """最後まで進まなかった依頼の代わりに残す1行(何を実行済みかだけを言う)。"""
    ran = [
        result.name
        for turn in request
        if isinstance(turn, ToolResultTurn)
        for result in turn.results
        if result.ok and not result.call_id.startswith(FIRST_LOOK_ID)
    ]
    done = f"実行したツール: {', '.join(ran)}" if ran else "ツールは実行していない"
    return f"(この依頼は最後まで進まなかった。{done})"


def _failure(tool: str, code: str, message: str, exception: str) -> dict:
    """ループ自身が作る失敗の封筒(`{"error": {...}}`)。形は `DocumentTools.error_envelope` と同じ。

    `error_envelope` を通さないのは、それが `ToolsOnLoop` 越しにイベントループへ往復するだけで、ここには例外が無いため。
    `code` の語彙は `MCP.md`「実装のメモ」が正(`dimension_not_given` だけがアプリ内AIの経路の語)。
    """
    return {"error": {"code": code, "message": message, "tool": tool, "exception": exception}}


#: 識別子らしいかたまり(`body_1` / `sketch-1c72a3c7` / `entity-75aa`)。区切り(`-` か `_`)を含むものだけを識別子とみなす:
#: `[A-Za-z]\w*` で広く取ると `R3` や `M8` まで消えて、「上面をR3で丸めて」が「数量を言っていない」と読まれる。
#: duocadのIDは `body-` / `sketch-` / `entity-` / `pattern-` の接頭辞を持つので区切りの有無で足りる。
_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9]*[-_][A-Za-z0-9_.-]*")
_DIGIT = re.compile(r"\d")


def _mentions_a_quantity(text: str) -> bool:
    """人が数量を口にしたか。識別子の中の数字は数えない(`body_1` を「寸法を言った」と読むとガードが素通りする)。全角は NFKC で寄せてから見る。

    厳密な照合(渡した値がそのまま本文にあるか)にはしない: 「直径8mmの円」に `radius: 4` は正しい導出で、弾くと普通の依頼が通らない。
    見るのは人が量を与えたかどうかだけ(与えていないなら、モデルが埋めた寸法は必ず捏造)。
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
