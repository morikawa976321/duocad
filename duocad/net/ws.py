"""WebSocketサーバ(「コマンドは下り、イベントは上り」)。静的配信は `duocad/net/http.py`。

アプリ層(ブラウザ)とコアの唯一の接点。コアはこのサーバが無くても機能的に完全で、ここには「文書を持つ」以外の機能を置かない。

プロトコル(JSON文字列):

    下り(client -> server)
        {"cmd": <コマンド名>, <引数>..., "client_op": int}

下りコマンドの一覧と引数は `duocad/catalog.py` の `COMMANDS` が正。ソケットが受け取れる名前は、そこから導出した表
(`SKETCH_COMMANDS` / `FEATURE_COMMANDS` / `FILE_COMMANDS`)と `_dispatch` が個別に捌く分(`catalog` の `ws="special"`)で全部。
**宣言に無い名前は `_dispatch` の入口で断る**(`WS_COMMAND_NAMES`。画面でできる操作は全部コマンドとして宣言する。issue #153)。
`client_op` はどのクライアントの操作かを返すための添え物で、verbの引数ではない。
`ack_id` も添え物で、付いていれば処理を終えた後(応答・配信を送り終えた後、失敗でも)に `done` を返す。
画面はこれで「ビューポートの変化を待たせている」間を数える(`web/src/serverWait.svelte.ts`)。

    上り(server -> client)
        {"type": "state", "state": {...}, "delta": {"bodies": [...]}, "applied": {...}|None}
        {"type": "preview", "preview_id": int|None, "verb": str, "bodies": [...]}
        {"type": "edges", "body_id": str, "revision": int, "edges": [...]}
        {"type": "edge_chains", "body_id": str, "revision": int, "chains": [[int, ...], ...]}
        {"type": "file", "action": "save"|"export_step"|"export_stl"|"list", "path"}
        {"type": "error", "message": str, "where": str, "client_op": int|None}
        {"type": "done", "ack_id": int}
        {"type": "warning", "message": str}
        {"type": "ai_status", "provider", "model", "base_url", "api_key_configured", "api_key_source",
         "api_key_hint", "running", "providers", "thinking", "thinking_choices"}
        {"type": "ai_tool_call", "name": str, "arguments": {...}, "result": {...}, "ok": bool}
        {"type": "ai_result", "status", "text", "detail", "turns", "tool_calls": [...], "usage": {...}}
        {"type": "ai_error", "message": str}
        {"type": "fem_status", "device", "cupy_installed", "gpu_name", "gpu_problem", "error"?}

`ai_*` はverbではない(`ai_get_status` / `ai_configure` / `ai_set_thinking` / `ai_set_api_key` / `ai_prepare` / `ai_send` /
`ai_cancel`)。
文書を直接は変えず(操作ログに載らず、undoの単位でもない)、文書を変えるのはエージェントが呼ぶ既存ツール側で、それは通常のverb配信として後から届く。
したがって `catalog.COMMANDS` には入れず、対応するMCPツールも作らない(AIがAIを起動する口は要らない)。

verbコマンドは対象集合(面・辺・領域)を引数で受けない。数値・名前・パスだけを送り、押す面も丸める辺も押し出す領域も
verbの入口(`duocad/entry.py`)がコアの選択から取る(`selection.require_selected` / `require_edges`)。
`body_id` が残っているのは対象集合ではないものだけ(`set_visible` / `get_edges` の対象、`extrude` の行き先)。
選択の口は `modify_selection` / `toggle_selection` / `clear_selection` の3つ。`set_selection`(述語を取る置き換え)は開けていない。

`preview` だけは状態を配信しない。verbを投機的に実行して形だけ測り、文書は `duocad.speculative` が元へ戻す。
応答は要求した接続にだけ返り、他のクライアントには何も起きない。

    `state.bodies` は `geometry` を含まない(name/kind/visible/face_count/volume/revisionのみ)。形状は `delta.bodies`
    (接続ごとに版が変わったBodyだけ、`geometry` 付き)で届く。毎回全部品のテセレーション結果(27部品で実測6MB超)を
    送り直すと、クライアント側の描画でサーバ側の高速化が消える。接続直後は「知っている版」が空なので全Bodyが入る。

状態変更は接続している全クライアントへ配信する。操作を出したクライアントにだけ `applied` を付け、それ以外には同じ `state` だけを送る。

    state() -> {
      "units", "active_body_id",
      "bodies": [{"body_id","name","kind","visible","revision",
                  "face_count","volume","geometry": <tessellate結果|None>}, ...],
      "log", "can_undo", "can_redo", "applied_count",
      "selection": {"items": [...], "count": int},
    }
    delta(known_revisions) -> {"bodies": [<上と同じ形・版が変わった分だけ>],
                               "unchanged": [BodyId, ...]}

選択が `state` に載っていないと、AIが書き換えた選択はブラウザへ届かない(`_on_core_change` はMCP起点の変更から `applied` を落として配る)。
選択の変更イベントは `{"change": "selection", "selection": {...}, "client_op": ...}`、可視性は `{"change": "visibility", ...}`
で、verbの `applied` とはキーで区別する(どちらもverbではなく、操作ログにも載らない)。

OCPを直接importしない(境界はkernel.pyだけ)。
"""
from __future__ import annotations

import asyncio
import functools
import json
import threading
from typing import Any, Optional  # Any: listeners / 接続

from .. import catalog, local_settings, optimize_run, selection
from ..agent import AgentLoop, Cancellation, Limits, ProviderError
from ..agent.loop import FIRST_LOOK
from ..agent.providers import (
    ANTHROPIC_MAX_TOKENS,
    DEFAULT_OPENAI_COMPAT_BASE_URL,
    DEFAULT_THINKING,
    THINKING_CHOICES,
    api_key_configured,
    api_key_status,
    check_thinking,
    default_model,
    delete_api_key,
    save_api_key,
)
from ..session import Session
from ..topopt import fem, gpu_cg
from .tools_on_loop import ToolsOnLoop

# ソケットから踏める口は `duocad/catalog.py` の宣言からの導出。登録制は変わらない: ここに現れない名前は
# ソケットから呼べない(届いた文字列で `Session` の任意のメソッドを呼べる作りにしない)。届いた引数は宣言と
# 突き合わせる(`catalog.Command.ws_arguments`。必須・未知・型)。既定値で補うと打ち忘れた寸法や `mode` が黙って入る。
#: ブラウザから踏めるスケッチの verb -> (`Session` のメソッド名, 受け取る引数名)。
SKETCH_COMMANDS: dict = catalog.ws_table("sketch")

#: ブラウザから踏めるフィーチャの verb -> (`Session` のメソッド名, 受け取る引数名)。
FEATURE_COMMANDS: dict = catalog.ws_table("feature")

#: ブラウザから踏めるファイル操作 -> (`Session` のメソッド名, 必須の引数名, 文書を変えるか)。
#: 文書を変えるもの(`import_step`)の応答は状態配信、変えないもの(`export_step` / `export_stl`)は書いた先のパスだけを
#: 要求した接続にだけ返す(`preview` と同じ扱い)。`open_document`(文書を差し替えるので全接続へ配る)・`save_document`・
#: `rename_document`(`file` の封筒 + 状態の配り直し。`save_document` は `name` が任意で、表の「引数は全部必須」の規律に
#: 合わない。`catalog.Param.ws` 参照)は表から外して `_dispatch` で個別に扱う。
FILE_COMMANDS: dict = catalog.file_table()

#: ソケットが受け取る名前の全体(宣言の `ws != "none"`)。`_dispatch` はこれに無い名前を分岐へ進めない。
#: 分岐があるのに宣言が無い口は `tests/test_ui_command_wiring.py` が落とす。
WS_COMMAND_NAMES: frozenset = frozenset(catalog.websocket_command_names())

#: アプリ内AIが繋げるプロバイダ(`ai_configure` が受け付ける綴り・`ai_status.providers`)。画面はこの長さで選択UIの要否を決める。
#: `anthropic` はキーが無くても並べる(選んで送ると「どこにキーを置くか」がエラーとして返る)。
AI_PROVIDERS: tuple = ("openai_compat", "anthropic")

#: llama-server の出力上限。往復数・合計時間とともに docs/LOCAL_LLM_QWEN38.md の測定と同じ値(`Limits()` の既定では
#: dGPU の無い機械の 27B モデルがどの思考の設定でも完走しない。実測は同文書)。
LOCAL_MAX_TOKENS = 16384
#: 1リクエストの待ち上限 = 出力上限 ÷ 4 tok/s + 120秒。実測の生成 4.1 tok/s(思考ありの下限)とプレフィックスの読み込み
#: 115秒(上限)を、待ち側に倒して丸めた値(丸め幅は自分で置いた余裕で、実測ではない)。
LOCAL_REQUEST_TIMEOUT = LOCAL_MAX_TOKENS / 4.0 + 120.0
#: 古い依頼を捨てる判定に残す余地(`Limits.room_tokens`)。出力上限(16,384)のままだと、文脈長 16,384 のサーバではツールを
#: 呼ぶたびに履歴が捨てられる。実測(docs/LOCAL_LLM_QWEN38.md「思考時間のばらつき」): 373応答の出力の中央値 260、2,048 超は 8%。
#: 2,048 はその分布を見て選んだ値で、最適値は測っていない。超えて文脈の端で切れた応答は、`run()` の「切れたら捨ててやり直す」が拾う。
LOCAL_ROOM_TOKENS = 2048

#: プロバイダごとの打ち切り(無ければ `Limits()`)。出力上限の理由は `ANTHROPIC_MAX_TOKENS` と `LOCAL_MAX_TOKENS`。
AI_LIMITS: dict = {
    "anthropic": Limits(max_tokens=ANTHROPIC_MAX_TOKENS),
    "openai_compat": Limits(
        max_turns=60,
        max_tokens=LOCAL_MAX_TOKENS,
        request_timeout=LOCAL_REQUEST_TIMEOUT,
        max_total_seconds=2 * 60 * 60.0,
        room_tokens=LOCAL_ROOM_TOKENS,
    ),
}

def _args_object(message: dict, *, required: bool) -> dict:
    """作業空間の口の `args`(verb の引数の辞書)。**省略(無い・null)と型違いを分ける** —— `[]` / `false` / `0` を
    `or {}` で空に畳むと、型違いの確定要求がそのまま通って形が変わる(sol の指摘)。
    """
    args = message.get("args")
    if args is None:
        if required:
            raise ValueError("args(引数の辞書)が要る")
        return {}
    if not isinstance(args, dict):
        raise ValueError(f"args はオブジェクトで渡す(受け取った型: {type(args).__name__})")
    return args


async def _send(connection: Any, payload: dict) -> None:
    await connection.send(json.dumps(payload, ensure_ascii=False))


class Server:
    """WebSocketの接続集合とコマンドの実行。状態変更は全接続へ配信する。

    接続ごとに「知っている版」を覚え(`_known_revisions`)、差分(`Session.delta`)はそれを基準に計算する。

    同時編集は前提にしない: 想定は「1人が1つのブラウザ」だけで、`Session` 全体を守る排他は無い。verbの適用は同期で
    形状は壊れないが、`_push` のawait中に別接続の操作が先に処理されうる(操作ログ・選択状態の反映順が意図と違いうる)。
    同時編集を仕様にするなら、まずここへ排他を入れる。
    """

    def __init__(self, session: Session, *, hidden_features: frozenset = frozenset()) -> None:
        self.session = session
        #: この版で隠す機能(`channel.hidden_features`)。属する口は `_dispatch` が断り、アプリ内AIの説明文の道具数からも除く。
        self.hidden_features = frozenset(hidden_features)
        self._hidden_commands = catalog.feature_command_names(self.hidden_features)
        #: この版で選べるプロバイダ(`ai_configure` が受け付ける綴り・`ai_status.providers`)。
        self.ai_providers: tuple = tuple(
            kind for kind in AI_PROVIDERS if not (kind == "openai_compat" and "local_ai" in self.hidden_features)
        )
        self.clients: set = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # WebSocket経由の操作を処理している最中か(`_on_core_change` の二重配信よけ)。
        self._dispatching = 0
        # 進行中の配信タスク(捨てるとGCされうる。`_spawn_push` 参照)
        self._pending_pushes: set = set()
        # WebSocket以外の主体(MCPサーバ)が文書を変えたとき、`_on_core_change` が全接続へ配信する。
        session.add_listener(self._on_core_change)
        self._known_revisions: dict = {}  # connection -> {body_id: revision}
        # 接続ごとの送信ロック。無いと2つの `_push` が同じ接続への配信で競合し、(a)古い版のペイロードが後から届く、
        # (b)送っていない版を「知っている」ことにされ以後の差分が欠落する。
        self._send_locks: dict = {}  # connection -> asyncio.Lock
        # -------------------------------------------------- G: アプリ内AIエージェント
        # 道具は起動時に一度だけ注入される(`bind_agent_tools`)。`mcp_server` が `server` をimportするので、このモジュールは
        # `mcp_server.DocumentTools` を作れない(循環)。`ToolSurface`(`descriptors`/`call`/`error_envelope`)を満たせば何でもよい。
        self._ai_tools: Optional[Any] = None
        # キーが置いてあれば Claude から始める(選び直しはサーバを立て直すたびに消える)。
        self._ai_provider_kind = (
            "anthropic" if "openai_compat" not in self.ai_providers or api_key_configured() else "openai_compat"
        )
        self._ai_model: Optional[str] = None
        self._ai_base_url: Optional[str] = None
        # llama-server へ送る思考の設定(`THINKING_CHOICES`)。プロバイダを切り替えても保つ(Claude には送らない)。
        self._ai_thinking = DEFAULT_THINKING
        self._ai_loop: Optional[AgentLoop] = None
        self._ai_cancellation = Cancellation()
        self._ai_running = False
        #: ウォームアップを始めた会話と、その終わりを待つための印(`_ai_prepare`)。
        self._ai_prepared_loop: Optional[AgentLoop] = None
        self._ai_prepare_done: Optional[threading.Event] = None
        #: 実行中のバックグラウンド往復(GCされないよう保持。`_pending_pushes` と同じ理由)。
        self._pending_ai_runs: set = set()
        # -------------------------------------------------- H: 形状最適化(重い=数十秒〜数分なので別スレッドで回す。走行中・中断の旗は `Session` が持つ)
        #: 実行中の最適化タスク(`_pending_ai_runs` と同じ理由で参照を保持する)。
        self._pending_optimize_runs: set = set()
        #: 進捗・結果は走らせている文書を開いている間だけ配る(`Session.optimization_visible`)。別の文書を開いている間の
        #: 最後の進捗はここに、終了時の結果/失敗は `Session.unseen_optimize_results` に控え、戻ってきたとき見せる(`_announce_optimization`)。
        self._optimize_last_progress: Optional[dict] = None
        # -------------------------------------------------- I: 計算の設定(この clone の設定ファイル)
        fem.FEM_DEVICE = local_settings.fem_device()

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """配信に使うイベントループを覚える(`run()` と `mcp_server` が起動時に呼ぶ)。

        コアの変更イベントは同期の呼び出しの中で飛んでくる(MCPのツールは `Session.set_visible` を素で呼ぶ)ので、
        そこから非同期の配信を始めるのにループの参照が要る。
        """
        self._loop = loop

    def bind_agent_tools(self, tools: Any) -> None:
        """アプリ内AIが使う道具を注入する(`mcp_server._run` が起動時に1回呼ぶ)。`ToolSurface` を満たす任意の値(型では縛らない)。"""
        self._ai_tools = tools

    def _on_core_change(self, applied: Optional[dict] = None) -> None:
        """コアが変わった。WebSocket以外の主体による変更だけを全接続へ配信する。

        WebSocket経由の操作は `_dispatch` が自分で `_push` するので、`_dispatching` の印で二重配信を避ける。
        `applied` を載せないのは、MCPからの変更は誰の操作でもなく、`applied` は操作元クライアントへの確認応答だから
        (ブラウザは `applied.client_op` で自分の操作と突き合わせる。他人の操作に付けると知らない `client_op` を自分のものとして拾う)。
        """
        if self._dispatching:
            return
        loop = self._loop
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                # 配信できないことを**黙って飲み込まない**(次の配信で画面へ出る)。
                self.session.warnings.append(
                    "文書が変わったがイベントループが無いため配信できなかった"
                    "(Server.bind_loop を呼ぶこと)"
                )
                return
        loop.call_soon_threadsafe(lambda: self._spawn_push())

    def _spawn(self, coro, failure: str) -> None:
        """配信タスクを起こし、参照を持ち続ける。

        `create_task` の戻りを捨てるとタスクがGCされうる。配信中の例外を誰も回収しないと、ブラウザは古いまま・警告も出ない形で
        静かに壊れるので、集合で保持し、落ちていたら `failure` を頭に警告として残す。
        """
        task = asyncio.get_running_loop().create_task(coro)
        self._pending_pushes.add(task)

        def finished(done: "asyncio.Task") -> None:
            self._pending_pushes.discard(done)
            if done.cancelled():
                return
            error = done.exception()
            if error is not None:
                self.session.warnings.append(f"{failure}: {type(error).__name__}: {error}")

        task.add_done_callback(finished)

    def _spawn_push(self) -> None:
        self._spawn(self._push(None, applied=None), "コア変更の配信に失敗")

    async def handle(self, connection: Any) -> None:
        self._loop = self._loop or asyncio.get_running_loop()
        self.clients.add(connection)
        try:
            try:
                await self._push(connection, applied=None, only=connection)
                # 最適化が走っているかも最初に知らせる(開始・終了・中断のときにしか配らないので、再読み込みした画面は中断ボタンを失い、終わる瞬間に切れていた画面は実行中のまま固まる)。
                await _send(connection, {"type": "optimize_status", **self._optimize_status()})
                if self.session.optimization_visible and self._optimize_last_progress is not None:
                    await _send(connection, self._optimize_last_progress)
            except Exception as error:
                # 文書が空(起動直後)等で状態が作れなくても、接続ごと落とさず理由を画面へ返す
                await _send(
                    connection,
                    {
                        "type": "error",
                        "where": "initial_state",
                        "message": f"{type(error).__name__}: {error}",
                    },
                )
            async for raw in connection:
                await self._dispatch(connection, raw)
        finally:
            self.clients.discard(connection)
            self._known_revisions.pop(connection, None)
            self._send_locks.pop(connection, None)
            self._on_disconnect(connection)

    def _on_disconnect(self, connection: Any) -> None:
        """1接続が切れた後の後始末(`handle()` の `finally` から呼ぶ)。

        最後の1接続が切れたときだけAIの実行(=文書の変更)を中断する(他の画面が見ている間は続ける。`ai_*` は共有の1会話)。
        形状最適化は中断しない: 1回で終わる有界な仕事で、画面のリロードで数分の実行が消える損失の方が大きい。
        再接続した画面へは結果が通常の状態配信で届き、実行中なら `handle()` が最初に `optimize_status` を送る。
        """
        if not self.clients and self._ai_running:
            self._ai_cancel()

    async def _push_applied(self, connection: Any, applied: dict, message: dict) -> None:
        """verb の応答に `client_op`(要求した画面が付けた通し番号)を載せて全接続へ配る。

        載せないもの: 状態を配り直すだけのもの(`get_state` / `set_view_resolution` / `save_document`)と `file` の封筒。
        `change` 系(`set_visible` / 選択の3つ)は `Session` が `_emit` の中で載せる。
        """
        applied["client_op"] = message.get("client_op")
        await self._push(connection, applied=applied)

    async def _dispatch(self, connection: Any, raw: Any) -> None:
        try:
            message = json.loads(raw)
            command = message.get("cmd")
        except (TypeError, ValueError) as error:
            await _send(connection, {"type": "error", "where": "parse", "message": str(error)})
            return

        try:
            if command not in WS_COMMAND_NAMES:
                raise ValueError(f"unknown command: {command!r}")
            if command in self._hidden_commands:
                raise ValueError(f"{command!r} はこの版では使えない(安定版では形状最適化を出さない。docs/STABLE.md)")
            if command == "get_state":
                await self._push(connection, applied=None, only=connection)
            elif command in ("undo", "redo"):
                applied = self._apply(
                    self.session.undo if command == "undo" else self.session.redo
                )
                await self._push_applied(connection, applied, message)
            elif command == "set_visible":
                applied = self._apply(
                    self.session.set_visible,
                    message["body_id"],
                    bool(message["visible"]),
                    message.get("client_op"),
                )
                await self._push(connection, applied=applied)
            elif command == "set_sketch_visible":
                applied = self._apply(
                    self.session.set_sketch_visible,
                    message["sketch_id"],
                    bool(message["visible"]),
                    message.get("client_op"),
                )
                await self._push(connection, applied=applied)
            # -------------------------------------------------- B: 選択(コアの選択集合をそのまま叩く。発火したイベントを `applied` として返し、状態は全接続へ配る)
            elif command == "modify_selection":
                applied = self._apply(
                    self.session.modify_selection,
                    message.get("add") or [],
                    message.get("remove") or [],
                    message.get("client_op"),
                )
                await self._push(connection, applied=applied)
            elif command == "toggle_selection":
                applied = self._apply(
                    self.session.toggle_selection,
                    message.get("refs") or [],
                    message.get("client_op"),
                )
                await self._push(connection, applied=applied)
            elif command == "clear_selection":
                applied = self._apply(self.session.clear_selection, message.get("client_op"))
                await self._push(connection, applied=applied)
            # -------------------------------------------------- B2: 作業空間(起動中のコマンド・モード。issue #153)。選択と同じ `change` 系
            elif command == "begin_command":
                verb = message.get("verb")
                if verb in self._hidden_commands:
                    raise ValueError(f"{verb!r} はこの版では使えない(docs/STABLE.md)")
                applied = self._apply(
                    self.session.begin_command, verb, _args_object(message, required=False), message.get("client_op")
                )
                await self._push(connection, applied=applied)
            elif command == "set_command_args":
                applied = self._apply(
                    self.session.set_command_args, _args_object(message, required=True), message.get("client_op")
                )
                await self._push(connection, applied=applied)
            elif command == "end_command":
                applied = self._apply(self.session.end_command, message.get("client_op"))
                await self._push(connection, applied=applied)
            elif command == "apply_command":
                # 応答は構えていた verb の封筒(`client_op` を載せて全接続へ)。
                applied = self._apply(self.session.apply_command, _args_object(message, required=False))
                await self._push_applied(connection, applied, message)
            elif command == "enter_sketch":
                applied = self._apply(self.session.enter_sketch, message["sketch_id"], message.get("client_op"))
                await self._push(connection, applied=applied)
            elif command in ("leave_sketch", "enter_optimize", "leave_optimize"):
                applied = self._apply(getattr(self.session, command), message.get("client_op"))
                await self._push(connection, applied=applied)
            # -------------------------------------------------- C/D: スケッチとフィーチャの verb
            # 引数は宣言(`catalog.Command.ws_arguments`)に従って取り出して検査し、`Session.apply` へ渡す(MCP・アプリ内AIと同じ1本)。
            # 省略された引数は渡さない: 既定値を埋めると、`move_body` のようにモードで要る引数が入れ替わる verb で、
            # モードが取らない引数が黙って効く。
            # 失敗は握りつぶさず画面へ返す(下の except)。フィレットは例外を投げず `IsDone()=False` で失敗する種類があり、コア側が例外へ変換している(§7)。
            elif command in SKETCH_COMMANDS or command in FEATURE_COMMANDS:
                applied = self._apply(
                    self.session.apply, command, catalog.BY_NAME[command].ws_arguments(message)
                )
                await self._push_applied(connection, applied, message)
            # -------------------------------------------------- D: 辺の折れ線(配信しない: 文書は変わらないので要求した接続にだけ返す。`Session.edges` のdocstringが「常時配らない」理由を持つ)
            elif command == "get_edges":
                result = self.session.edges(message["body_id"])
                await _send(connection, {"type": "edges", **result})
            elif command == "get_edge_chains":
                result = self.session.edge_chains(message["body_id"], list(message["chain_of"]))
                await _send(connection, {"type": "edge_chains", **result})
            # -------------------------------------------------- D: 頂点の座標(`get_edges` と同じく要求した接続にだけ返す)
            elif command == "get_vertices":
                result = self.session.vertices(message["body_id"])
                await _send(connection, {"type": "vertices", **result})
            # -------------------------------------------------- 測定(verbではない。配信しない)。`client_op` は画面が「確定が返ってきた」の突き合わせに使う
            elif command == "measure":
                result = self.session.measure()
                await _send(
                    connection,
                    {"type": "measure", "client_op": message.get("client_op"), **result},
                )
            elif command == "inspect_selection":
                payload: dict = {"request_id": message.get("request_id"), "item": None, "error": None}
                try:
                    payload["item"] = self.session.inspect_selection()
                except Exception as error:  # 契約は catalog の `inspect_selection` の note
                    payload["error"] = f"{type(error).__name__}: {error}"
                await _send(connection, {"type": "selection_info", **payload})
            # -------------------------------------------------- 可変解像度(ズーム連動)
            # 文書は変わらないが配る形の粗さが変わるので全接続へ配り直す(粗さは `DocumentView` が文書に1つ持つ設定で、
            # 単一ブラウザ前提。理由と同時編集のときの変更点は `DocumentView.set_view_resolution`)。
            # 変わらなかったときは何も送らない(ズームは連続で飛んでくるので、同値の再配信は27部品ぶんのメッシュを無駄に流す)。
            # `focus` は `{body_id: [1始まりの面添字]}` で、視錐台内/正面向きの面だけを細かく測らせる。判定はカメラを持つ
            # 画面側にしか作れないので受け取った事実を渡すだけ。省略・不正なら全面が細かい側(既定)。
            elif command == "set_view_resolution":
                changed = self.session.set_view_resolution(
                    message["world_per_pixel"],
                    message.get("size_hint") or 0.0,
                    message.get("focus"),
                )
                if changed:
                    await self._push(connection, applied=None)
            # -------------------------------------------------- E: ファイル操作(SVG読み込みは無い: `import_svg` はスケッチのverbなのでC節)
            # 応答は `FILE_COMMANDS` の3つ目の値で違う: 文書を変えるものは状態を全接続へ、書き出すだけのものは要求した接続へ結果だけ返す。
            elif command in FILE_COMMANDS:
                method, _keys, is_verb = FILE_COMMANDS[command]
                arguments = catalog.BY_NAME[command].ws_arguments(message)
                if is_verb:
                    applied = self._apply(self.session.apply, command, arguments)
                    await self._push_applied(connection, applied, message)
                else:
                    applied = self._apply(getattr(self.session, method), **arguments)
                    # AI の中断は送信より前(送信が切断で失敗しても、文書は既に閉じている)。
                    if applied.get("closed"):
                        self._stop_ai_on_switch()
                    await _send(connection, {"type": "file", **applied})
                    # 開いている文書を消すと閉じる(`Session.delete_document`)。文書が替わったので
                    # 全接続へ配る —— 封筒だけだと、他のタブは消えた文書を出したまま編集を断られ続ける。
                    if applied.get("closed"):
                        await self._push(connection, applied=None)
            # 保存は表に載らない(`name` が任意引数で、表の「引数は全部必須」の規律に合わない。`catalog.Param.ws`)。
            # 契約は `Session.save_document`: 引数なし=編集中のファイルへ上書き / `name` あり=新しい名前でのみ保存(既存名は拒否)。
            # 書いた後に状態も配り直す: `state.current_name` が変わるので、`file` の封筒だけだとヘッダが「(無題)」のまま
            # Ctrl+S と自動保存が現在のファイルへ書き続ける食い違いが残る。文書は変わらないので `applied` は付けない。
            elif command == "save_document":
                name = message.get("name")
                applied = self._apply(
                    self.session.save_document, *(() if name is None else (name,))
                )
                await _send(connection, {"type": "file", **applied})
                await self._push(connection, applied=None)
            # **改名も現在ファイル名を変える**ので、保存と同じ「封筒 + 状態の配り直し」。
            elif command == "rename_document":
                applied = self._apply(self.session.rename_document, message["name"])
                await _send(connection, {"type": "file", **applied})
                await self._push(connection, applied=None)
            # 開くは文書ごと差し替えるので全接続へ配る。`Session.open_document` の発火は `_on_core_change` も拾うが、
            # `_apply` の間は `_dispatching` が立つので二重には配らない。
            # 今の文書と同じ名前を2接続以上で開こうとしたら拒否する(単一ブラウザ前提の番人: ディスクから読み直して差し替えるため、
            # 素通しだと別タブの未保存の編集が黙って消える)。接続が1つなら開き直しは通す(保存前に戻す手立てとして使われている)。
            elif command == "open_document":
                name = message["name"]
                if name == self.session.current_name and len(self.clients) > 1:
                    raise ValueError(
                        f"{name} は既に別のタブで開いている"
                        "(そちらのタブを使うか、閉じてから開き直す)"
                    )
                applied = self._apply(self.session.open_document, name)
                self._stop_ai_on_switch()
                await self._push_applied(connection, applied, message)
                await self._announce_optimization()
            # **新規も文書ごと差し替える**ので開くと同じ扱い(全接続へ配る)。引数は無い。
            elif command == "new_document":
                applied = self._apply(self.session.new_document)
                self._stop_ai_on_switch()
                await self._push_applied(connection, applied, message)
                await self._announce_optimization()
            elif command == "close_document":
                applied = self._apply(self.session.close_document)
                self._stop_ai_on_switch()
                await self._push_applied(connection, applied, message)
                await self._announce_optimization()
            # -------------------------------------------------- B: プレビュー(投機的実行)
            # 配信しない(`Session.preview` が文書を元へ戻すので、要求した接続にだけ返す)。
            # `_apply` で囲まない: プレビュー中の発火は `Session._suppress_events` が止めており、ここで `_dispatching` まで立てると、
            # 計算中(押し出しなら約1秒)にMCPから届いた本物の変更まで誤判定で捨てる。
            elif command == "preview":
                result = self.session.preview(message["verb"], message.get("args") or {})
                await _send(
                    connection,
                    {
                        "type": "preview",
                        "preview_id": message.get("preview_id"),
                        **result,
                    },
                )
            # -------------------------------------------------- G: アプリ内AI(verbではない。応答は専用イベント `ai_status` / `ai_tool_call` / `ai_result` / `ai_error`。AIが文書を変えた分は通常のverb配信として `_on_core_change` 経由で別途届く)
            elif command == "ai_get_status":
                await _send(connection, {"type": "ai_status", **self._ai_status()})
            elif command == "ai_configure":
                status = self._ai_configure(
                    message.get("provider"), message.get("model"), message.get("base_url")
                )
                # ここは既にイベントループのスレッドなので `call_soon_threadsafe` を挟まず直接 `await` する
                # (待たずに抜けると、ここで `asyncio.run` が閉じるテストでは配信タスクが走る前に握りつぶされる)。
                await self._broadcast({"type": "ai_status", **status})
            elif command == "ai_set_thinking":
                await self._broadcast({"type": "ai_status", **self._ai_set_thinking(message.get("thinking"))})
            elif command == "ai_set_api_key":
                await self._broadcast({"type": "ai_status", **self._ai_set_api_key(message.get("key"))})
            elif command == "ai_prepare":
                self._ai_prepare()
            elif command == "ai_send":
                await self._ai_start(connection, str(message.get("message") or ""))
            elif command == "ai_cancel":
                self._ai_cancel()
                await self._broadcast({"type": "ai_status", **self._ai_status()})
            # -------------------------------------------------- H: 形状最適化(verbではない。`catalog` の `category="protocol"`)
            # 応答は専用イベント(`optimize_conditions` / `optimize_resolution` / `optimize_status` / `optimize_progress` /
            # `optimize_result` / `optimize_error` / 頼まれたときだけ `optimize_debug`)。結果が文書へ入ったことは
            # `_on_core_change` 経由で通常の状態配信として別途届く。
            elif command == "optimize_conditions":
                await _send(
                    connection,
                    {"type": "optimize_conditions", **self.session.optimize_conditions()},
                )
            # 失敗を `type: "error"` に落とさない: 条件面が1枚も無い状態は設定の途中で、画面はパネル内に出す(赤い札だと条件を置いている最中ずっと点滅する)。
            elif command == "optimize_resolution":
                try:
                    payload = self.session.optimize_resolution(message.get("rmin_mm"), message.get("element_size_mm"))
                except ValueError as error:
                    payload = {"error": str(error)}
                await _send(connection, {"type": "optimize_resolution", **payload})
            elif command == "run_optimization":
                await self._optimize_start(connection, message)
            elif command == "cancel_optimization":
                await self._optimize_cancel(connection)
            elif command == "finish_optimization":
                await self._optimize_finish(connection, message)
            elif command == "optimize_steps":
                steps = self.session.steps_for_current()
                await _send(connection, {
                    "type": "optimize_steps",
                    "steps": [] if steps is None else steps.meta(),
                    "finishable": steps is not None and steps.finisher is not None,
                })
            elif command == "optimize_step_voxels":
                await self._optimize_step_voxels(connection, message)
            # -------------------------------------------------- I: 計算の設定(verbではない。catalog にも MCP にも載せない。CuPy を読む(初回1秒前後)ので実行器で作る)
            elif command == "fem_get_status":
                status = await asyncio.get_running_loop().run_in_executor(None, self._fem_status)
                await _send(connection, {"type": "fem_status", **status})
            elif command == "fem_set_device":
                error = self._fem_set_device(message.get("device"))
                status = await asyncio.get_running_loop().run_in_executor(None, self._fem_status)
                await self._broadcast({"type": "fem_status", **status, **({"error": error} if error else {})})
            else:
                # 宣言はあるが分岐が無い(`tests/test_ui_command_wiring.py` が先に落とす)。
                raise ValueError(f"unknown command: {command!r}")
        except Exception as error:  # 1つの失敗でサーバを落とさない。必ず画面へ返す
            await _send(
                connection,
                {
                    "type": "error",
                    "where": str(command),
                    "message": f"{type(error).__name__}: {error}",
                    # 画面は自分が待っている操作の失敗かをこれで見分ける(別の要求の失敗で確定の待ちを解かない)。
                    "client_op": message.get("client_op"),
                },
            )
        finally:
            # 1接続は直列に捌くので、`done` が届いた時点でそれより前に送った分も済んでいる(画面は「この番号以下」を消す)。
            ack_id = message.get("ack_id")
            if ack_id is not None:
                await _send(connection, {"type": "done", "ack_id": ack_id})

    def _apply(self, call: Any, *args: Any, **kwargs: Any) -> dict:
        """WebSocket経由の変更を実行する。この間だけ `_dispatching` を立てる。

        同期の呼び出しだけを囲む: 印を `_push` の待ち時間まで立てると、その間に届いたMCPからの変更まで誤判定で捨てる。
        """
        self._dispatching += 1
        try:
            return call(*args, **kwargs)
        finally:
            self._dispatching -= 1

    # ------------------------------------------------------------------ G: アプリ内AIエージェント
    def _ai_status(self) -> dict:
        """画面に出してよい設定の全部。キーの値は載せない(載せるのは `api_key_status()` の3つだけ)。

        `model` / `base_url` は明示選択が無ければプロバイダの既定値を返す。`anthropic` の `base_url` は常に `null`。
        `providers` は増減しうる列として返し続ける(画面はこの長さで選択UIの要否を決める)。
        """
        anthropic = self._ai_provider_kind == "anthropic"
        key = api_key_status()
        return {
            "provider": self._ai_provider_kind,
            "model": self._ai_model or default_model(self._ai_provider_kind),
            "base_url": None if anthropic else self._ai_base_url or DEFAULT_OPENAI_COMPAT_BASE_URL,
            "api_key_configured": key["configured"],
            "api_key_source": key["source"],
            "api_key_hint": key["hint"],
            "running": self._ai_running,
            "providers": list(self.ai_providers),
            "thinking": self._ai_thinking,
            "thinking_choices": list(THINKING_CHOICES),
        }

    def _ai_set_thinking(self, thinking: Any) -> dict:
        """llama-server へ送る思考の設定を差し替える。**会話は作り直さない**(`_ai_configure` と違い、次の往復から効く)。

        実行中は受け付けない(走っている往復と次の往復で設定が混ざる)。ウォームアップはやり直さない: スロットを復元すると、
        温まっている会話の続き(medium ↔ off ならキャッシュが保たれる)を system + tools だけの状態で上書きする。
        """
        if self._ai_running:
            return {**self._ai_status(), "error": "AI応答中は思考の設定を変更できない(中断してから)"}
        try:
            self._ai_thinking = check_thinking(thinking)
        except ValueError as error:
            return {**self._ai_status(), "error": str(error)}
        provider = self._ai_loop.provider if self._ai_loop is not None else None
        if hasattr(provider, "thinking"):
            provider.thinking = self._ai_thinking
        return self._ai_status()

    def _ai_configure(
        self, provider: Optional[str], model: Optional[str], base_url: Optional[str]
    ) -> dict:
        """プロバイダ・モデル・エンドポイントを差し替える。キーは扱わない(値を受け取らない)。

        実行中は受け付けない(`_ai_cancellation` を作り直すと、走っている `AgentLoop.run()` が古い `Cancellation` を見続けて中断ボタンが効かなくなる)。

        `base_url` を自由に差し替えてよいのは鍵を持たないプロバイダ(`openai_compat`)だけ。`anthropic` は実キーを `x-api-key` に載せて
        `{base_url}/v1/messages` へ送るので、送り先を変えられると `ai_configure` → `ai_send` の2手でキーが外へ出る
        (WebSocketは同一オリジンポリシーで止まらず、127.0.0.1 bindは防御にならない)。`anthropic` 選択中の `base_url` は
        黙って無視せず明示的なエラーで拒否する。
        """
        if self._ai_running:
            return {**self._ai_status(), "error": "AI応答中は設定を変更できない(中断してから)"}
        if provider is not None and provider not in AI_PROVIDERS:
            return {**self._ai_status(), "error": f"未知のプロバイダ: {provider!r}"}
        if provider is not None and provider not in self.ai_providers:
            return {**self._ai_status(), "error": f"{provider!r} はこの版では使えない(安定版のAIはClaudeだけ。docs/STABLE.md)"}
        if (provider or self._ai_provider_kind) == "anthropic" and base_url:
            return {
                **self._ai_status(),
                "error": "Claude(anthropic)の送り先は公式APIに固定している(base_url は指定できない)",
            }
        if provider is not None:
            self._ai_provider_kind = provider
        self._ai_model = model or None
        self._ai_base_url = base_url or None
        # 古い設定で走っているウォームアップの接続を閉じる(捨てる会話のために相手を占有しない)。
        self._ai_cancellation.cancel()
        # 次に使うときに作り直す(古いプロバイダ・古い会話の履歴を持ち越さない)。
        self._ai_loop = None
        self._ai_cancellation = Cancellation()
        return self._ai_status()

    def _ai_set_api_key(self, key: Any) -> dict:
        """設定ダイアログから届いたClaudeのAPIキーを書く(`None` なら消す)。値は返さない。

        書いたら Claude へ切り替え、会話を作り直す(走っている `AgentLoop` のプロバイダは古いキーを持つ)。
        実行中は `_ai_configure` と同じ理由で受け付けない。
        `catalog.COMMANDS` にもMCPにも載せない(AIがキーを書き換える口を作らない)。拒否の理由にキーの値を入れない(`key_problem`)。
        """
        if self._ai_running:
            return {**self._ai_status(), "error": "AI応答中はAPIキーを変更できない(中断してから)"}
        try:
            if key is None:
                delete_api_key()
            elif not isinstance(key, str):
                raise ValueError("APIキーは文字列で送る")
            else:
                save_api_key(key)
                if self._ai_provider_kind != "anthropic":
                    # 前のプロバイダ向けに選んだモデル名(`qwen3-30b` など)をClaudeへ持ち越さない。
                    self._ai_model = None
                self._ai_provider_kind = "anthropic"
                self._ai_base_url = None
        except (ValueError, OSError) as error:
            reason = str(error) if isinstance(error, ValueError) else f"APIキーの保存に失敗: {error}"
            return {**self._ai_status(), "error": reason}
        self._ai_cancellation.cancel()
        self._ai_loop = None
        self._ai_cancellation = Cancellation()
        return self._ai_status()

    def _ensure_ai_loop(self) -> AgentLoop:
        """今の設定で `AgentLoop` を作る(無ければ)。失敗理由は `ProviderError`(画面に出せる文)。"""
        if self._ai_loop is not None:
            return self._ai_loop
        if self._ai_tools is None:
            raise ProviderError(
                "AIの道具が未初期化(サーバ起動側の問題: bind_agent_tools が呼ばれていない)"
            )
        if self._loop is None:
            raise ProviderError("イベントループが未起動")
        # `build_provider` は入口(`duocad.server`)越しに引く: `tests/test_agent_ws.py` がそこを monkeypatch するので、
        # ここで `from ..agent import build_provider` すると差し替えが素通りし(monkeypatch自体は成功するので黙って効かない)
        # 検査が本物のプロバイダを立てる。入口はこのモジュールをimportするので、逆向きの参照は呼び出し時まで遅らせる。
        from .. import server as entry

        # `anthropic` には `base_url` を渡さない(`_ai_configure` の拒否に対する二重の保証。検証を素通りする経路が生まれても、実キーが外部ホストへ渡る手前で止まる)。
        kind = self._ai_provider_kind
        provider = entry.build_provider(
            kind,
            model=self._ai_model,
            base_url=None if kind == "anthropic" else self._ai_base_url,
            thinking=self._ai_thinking,
        )
        marshaled = ToolsOnLoop(self._ai_tools, self._loop)
        self._ai_loop = AgentLoop(
            marshaled,
            provider,
            system=catalog.agent_instructions(self.hidden_features),
            limits=AI_LIMITS.get(kind, Limits()),
            cancellation=self._ai_cancellation,
            progress=self._ai_progress,
            first_look=FIRST_LOOK,
        )
        return self._ai_loop

    def _ai_progress(self, name: str, arguments: dict, result: Any, ok: bool) -> None:
        """`AgentLoop` の進捗コールバック。実行器スレッドから呼ばれる(`_ai_start` の `run_in_executor`)ので、`_emit_event` 経由でイベントループへ戻す。"""
        self._emit_event(
            {
                "type": "ai_tool_call",
                "name": name,
                "arguments": arguments,
                "result": result,
                "ok": bool(ok),
            }
        )

    def _ai_prepare(self) -> None:
        """固定プレフィックス(system + tools)を先に読ませておく。1つの会話につき1回。

        画面が繋がった時点と設定を変えた直後に届く。cold の読み込みは遅い機種で分単位かかるので、人が最初の依頼を打っている間に済ませる
        (実測: 最初の依頼の処理量が 6,686 → 16 トークン)。何も返さない(相手がまだ居ないのは事故ではなく、最初の依頼がそれを言う)。
        `ai_get_status` に相乗りさせないのは、読むだけの口から外へHTTPが飛ぶと検査や監視が相手のスロットを書き換えるため。
        """
        if self._ai_running:
            return
        try:
            loop_obj = self._ensure_ai_loop()
        except ProviderError:
            return
        if loop_obj is self._ai_prepared_loop:
            return
        self._ai_prepared_loop = loop_obj
        done = threading.Event()
        self._ai_prepare_done = done

        def work() -> dict:
            try:
                return loop_obj.prepare()
            finally:
                done.set()

        future = asyncio.get_running_loop().run_in_executor(None, work)
        self._pending_ai_runs.add(future)

        def finished(fut: "asyncio.Future") -> None:
            self._pending_ai_runs.discard(fut)
            if not fut.cancelled() and fut.exception() is not None:
                self.session.warnings.append(f"AIのウォームアップに失敗: {fut.exception()}")

        future.add_done_callback(finished)

    async def _ai_start(self, connection: Any, prompt: str) -> None:
        """1つの発話でエージェントを起こす。`AgentLoop.run()` を待たずに返る。

        モデルへのHTTPは数十秒かかりうり、ここで `await` すると同じ接続からの次のメッセージ(`ai_cancel` を含む)が読めなくなる
        (`handle()` は1接続を直列に処理する)。`run_in_executor` へ投げっぱなしにし、進捗・結果は `ai_tool_call` / `ai_result` として後から届ける。
        """
        prompt = prompt.strip()
        if not prompt:
            await _send(connection, {"type": "ai_error", "message": "空のメッセージは送れない"})
            return
        if self._ai_running:
            await _send(
                connection,
                {"type": "ai_error", "message": "既にAIが応答中(中断してから送ること)"},
            )
            return
        try:
            loop_obj = self._ensure_ai_loop()
        except ProviderError as error:
            await _send(connection, {"type": "ai_error", "message": str(error)})
            return

        self._ai_running = True
        self._ai_cancellation.clear()
        await self._broadcast({"type": "ai_status", **self._ai_status()})

        # ウォームアップが走っていれば終わってから始める(並べて投げても相手の側で後ろに並び、その待ちが `run()` の合計時間の予算を食って時間切れになる)。
        warming = self._ai_prepare_done if loop_obj is self._ai_prepared_loop else None

        def work():
            if warming is not None:
                warming.wait()
            return loop_obj.run(prompt)

        running_loop = asyncio.get_running_loop()
        future = running_loop.run_in_executor(None, work)
        self._pending_ai_runs.add(future)

        def finished(fut: "asyncio.Future") -> None:
            self._pending_ai_runs.discard(fut)
            self._ai_running = False
            if not fut.cancelled():
                error = fut.exception()
                if error is not None:
                    self._emit_event(
                        {"type": "ai_error", "message": f"{type(error).__name__}: {error}"}
                    )
                else:
                    self._emit_event({"type": "ai_result", **fut.result().to_dict()})
            self._emit_event({"type": "ai_status", **self._ai_status()})

        future.add_done_callback(finished)

    def _ai_cancel(self) -> None:
        """中断フラグを立てる。ループはモデルへ投げる前とツール1件ごとに見る(`duocad/agent/loop.py` の `Cancellation`)。走っているHTTPはプロバイダが接続を閉じて止める。"""
        self._ai_cancellation.cancel()

    def _stop_ai_on_switch(self) -> None:
        """人が文書を切り替えたら、走っているアプリ内AIを止める(issue #102)。

        版の照合(`current_seq` の世代)だけでは足りない: 照合はツールを差し替えるだけでループを続けるので、
        モデルは新しい文書を読み直して前の依頼をそこで続ける。人の操作をAIより優先する(切り替えを断らない)。
        """
        if self._ai_running:
            self._ai_cancel()

    # ------------------------------------------------------------------ I: 計算の設定
    def _fem_status(self) -> dict:
        """画面の「設定」に出す、形状最適化の CG を解く装置の状態。`gpu_name` / `gpu_problem` は gpu を選んでいて CuPy が入っているときだけ調べる(CuPy を読むと GPU にコンテキストを作り VRAM を取るため)。"""
        installed = gpu_cg.cupy_installed()
        described = gpu_cg.describe() if fem.FEM_DEVICE == "gpu" and installed else {"name": None, "problem": None}
        return {
            "device": fem.FEM_DEVICE,
            "cupy_installed": installed,
            "gpu_name": described["name"],
            "gpu_problem": described["problem"],
        }

    def _fem_set_device(self, device: Any) -> Optional[str]:
        """設定ダイアログから届いた装置を書き、次に準備する計算から効かせる。拒否の理由か None。

        走っている最適化の途中で替わってよい(GPU の解は CPU とビット一致し、変わるのは秒だけ。その回の記録の `linear-solve` に装置が残る)。
        gpu へ替えたときは `gpu_cg` が覚えている「GPU を使わない理由」を忘れて試し直す。
        """
        if device not in local_settings.FEM_DEVICES:
            return f"未知の装置: {device!r}"
        try:
            local_settings.update(fem_device=device)
        except OSError as error:
            return f"設定を保存できなかった: {error}"
        if device == "gpu":
            gpu_cg.reset()
        fem.FEM_DEVICE = device
        return None

    # ------------------------------------------------------------------ H: 形状最適化を走らせる
    def _optimize_status(self) -> dict:
        """画面が実行ボタンと中断ボタンの出し分けに使う最小限(`Session.optimization_status`)。`available` はこの版で形状最適化を
        使えるか(安定版は false)で、画面は入口とステップの取り直しをこれで決める(口を断るのと同じサーバが答える)。"""
        return {**self.session.optimization_status(),
                "available": "shape_optimization" not in self.hidden_features}

    async def _broadcast_optimize_status(self) -> None:
        """`optimize_status` を全接続へ配る。中身は接続ごとに送る直前に取り直す(`_broadcast` は接続を1つずつ await するので、作り置きの1通だと配る間に実行が終わったとき後ろの接続が「走っている」のまま固まる)。"""
        for target in list(self.clients):
            try:
                await _send(target, {"type": "optimize_status", **self._optimize_status()})
            except Exception:
                self.clients.discard(target)

    async def _optimize_start(self, connection: Any, message: dict) -> None:
        """最適化を1回走らせる。`run_optimization()` を待たずに返る(`_ai_start` と同じ形)。

        数十秒〜数分かかるので、ここで `await` すると `cancel_optimization` を含む次のメッセージが読めなくなる。
        別タスクへ投げ、進捗・結果は後からイベントで届ける。
        走らせる前に条件を点検する: 画面が出したいのは例外の文言ではなく「何が足りないか」の一覧(`conditions_report` の `problems`)。
        """
        if self.session.optimizing:
            running_name = self.session.optimizing_name
            await _send(
                connection,
                {
                    "type": "optimize_error",
                    "message": (
                        "最適化が既に走っている(中断してから)"
                        if self.session.optimization_visible
                        else f"{running_name or '別の文書'} で最適化が既に走っている"
                        "(終わるのを待つか、そのファイルを開いて中断してから)"
                    ),
                    # 画面はこの印を見て、走っている実行の進捗・通知を消さない。
                    "already_running": True,
                },
            )
            return
        report = self.session.optimize_conditions()
        if not report["ready"]:
            await _send(
                connection,
                {
                    "type": "optimize_error",
                    "message": "条件がそろっていない",
                    "problems": list(report["problems"]),
                },
            )
            return

        # 新引数に加えて旧引数(`rmin_mm`/`max_iter`/`material_min_mm3`/`material_max_mm3`)も転送する: 黙って落とすと
        # 「無視された」のと区別が付かない。`session.run_optimization` に断らせ、`OptimizeInputError` を `optimize_error` として画面へ返す。
        kwargs = {
            key: message[key]
            for key in (
                "element_size_mm", "target_safety_factor", "max_evaluations", "min_width_mm",
                "rmin_mm", "max_iter", "material_min_mm3", "material_max_mm3",
            )
            if message.get(key) is not None
        }
        # 開発用の可視化を頼まれたか(画面の `?debug=space` / `?debug=anchors` / `?voxel=1`)。頼まれた種類だけを `optimize_debug` で配る。
        # 既定は空(付けなければサーバは配列を1本も作らず、既定の画面は1画素も変わらない)。
        wanted = message.get("debug")
        if isinstance(wanted, (list, tuple)):
            kinds = [str(k) for k in wanted if str(k) in optimize_run.DEBUG_KINDS]
            if kinds:
                kwargs["debug_kinds"] = kinds
        # 中断の旗を畳むのも文書を閉じるのもここ(イベントループのスレッド)で済ませる。ワーカースレッドの中で畳むと、走り出すまでに届いた中断が消える(issue #35)。
        self.session.begin_optimization()
        # 前の実行のステップを、新しい実行が最初のステップを足すまで画面へ見せない(ステップは実行のスレッドが作り直す)。
        self.session.optimization_steps = None
        self._optimize_last_progress = None
        await self._broadcast_optimize_status()
        task = asyncio.get_running_loop().create_task(self._optimize_run(kwargs))
        self._pending_optimize_runs.add(task)

        def finished(done: "asyncio.Task") -> None:
            self._pending_optimize_runs.discard(done)
            if not done.cancelled() and done.exception() is not None:
                error = done.exception()
                self.session.warnings.append(f"最適化の後始末に失敗: {type(error).__name__}: {error}")

        task.add_done_callback(finished)

    async def _optimize_finish(self, connection: Any, message: dict) -> None:
        """「このステップで仕上げる」。走っている間は探索を止めてそのステップを仕上げるよう伝える(降りるのは次の候補を
        作る前)。走っていなければ、いま開いている文書の直近の実行のステップを、探索をやり直さずに仕上げる(別タスク)。
        `step` を省くと最新のステップ。"""
        step = message.get("step")
        step = None if step is None else int(step)
        if self.session.optimization_visible:
            if not self.session.request_finish(step)["finishing"]:
                # 探索はもう終わって仕上げている(実行が要求を読み終えた)。走っている実行の表示は消させない。
                await _send(connection, {
                    "type": "optimize_error",
                    "message": "探索はもう終わって仕上げている。終わった後にステップを選んで仕上げること",
                    "already_running": True,
                })
                return
            await self._broadcast_optimize_status()
            return
        if self.session.optimizing:
            await _send(connection, {
                "type": "optimize_error",
                "message": f"{self.session.optimizing_name or '別の文書'} で最適化が走っている(終わるのを待ってから)",
                "already_running": True,
            })
            return
        steps = self.session.steps_for_current()
        count = 0 if steps is None else len(steps)
        if steps is None or steps.finisher is None or count == 0:
            await _send(connection, {"type": "optimize_error",
                                     "message": "仕上げられるステップが無い(この文書で探索が合格した形を出していない)"})
            return
        index = count - 1 if step is None or step < 0 else step
        if index >= count:
            await _send(connection, {"type": "optimize_error", "message": f"ステップ {index} は無い(0〜{count - 1})"})
            return
        kwargs: dict = {}
        wanted = message.get("debug")
        if isinstance(wanted, (list, tuple)):
            kinds = [str(k) for k in wanted if str(k) in optimize_run.DEBUG_KINDS]
            if kinds:
                kwargs["debug_kinds"] = kinds
        self.session.begin_optimization()
        self.session.mark_finishing(index)
        self._optimize_last_progress = None
        await self._broadcast_optimize_status()
        task = asyncio.get_running_loop().create_task(self._optimize_run(kwargs, finish_step=index))
        self._pending_optimize_runs.add(task)

        def finished(done: "asyncio.Task") -> None:
            self._pending_optimize_runs.discard(done)
            if not done.cancelled() and done.exception() is not None:
                error = done.exception()
                self.session.warnings.append(f"仕上げの後始末に失敗: {type(error).__name__}: {error}")

        task.add_done_callback(finished)

    async def _optimize_step_voxels(self, connection: Any, message: dict) -> None:
        """ステップ1つの形(表面のセル)を返す。展開と表面の取り出しは数十ms〜かかるので実行器で作る。"""
        index = message.get("index")
        steps = self.session.steps_for_current()
        if steps is None or not isinstance(index, int):
            await _send(connection, {"type": "optimize_step_voxels", "index": index,
                                     "error": "この文書の実行のステップが無い"})
            return
        try:
            payload = await asyncio.to_thread(steps.voxels, index)
        except IndexError as error:
            await _send(connection, {"type": "optimize_step_voxels", "index": index, "error": str(error)})
            return
        await _send(connection, {"type": "optimize_step_voxels", **payload})

    async def _optimize_run(self, kwargs: dict, finish_step: Optional[int] = None) -> None:
        """別スレッドで端から端まで回し、結果を配る。例外を握り潰さない。

        `optimize_run.run()` の例外文は「次に何をすればよいか」を持っている(`docs/contracts/ui-optimize-integration.md`「最適化の配線」)ので、
        そのまま `optimize_error` として画面へ出す。

        最後に必ず状態を配り直す(`_push`): `state != "done"` では文書が変わらずコアのリスナが配らないが、`Session.warnings` に積まれた通知
        (条件面が空 / 保持・除外が表現できていない)を運ぶ経路がそこしか無い。

        結果を足したら(`state == "done"`)配る前にディスクへ書く(2026-09-13実測: 数時間がかりの実行が、結果を見る前にサーバプロセスごと消えた)。
        結果を足さなかった実行では書かない(1操作ごとの自動保存が既に書いてある。issue #36)。書くのは `Session.deliver_optimization_result` で、
        イベントループへ戻ってから呼ぶ(人の「開く」と同じスレッドで、届け先の判定と文書の差し替えを済ませるため)。

        別の文書を開いている間に終わったら、結果も失敗も配らずファイル名ごとに控え、走らせた文書へ戻ってきたときに見せる(`_announce_optimization`)。
        """
        payload: dict
        name = self.session.optimizing_name
        visible = True
        try:
            try:
                if "debug_kinds" in kwargs:
                    kwargs["debug"] = self._optimize_debug
                if finish_step is None:
                    work = functools.partial(self.session.run_optimization, progress=self._optimize_progress, **kwargs)
                else:
                    work = functools.partial(self.session.finish_optimization_step, finish_step,
                                             progress=self._optimize_progress, **kwargs)
                result = await asyncio.to_thread(work)
                result = self.session.deliver_optimization_result(result)
                # 無題の文書の結果を別の文書を開いている間に届けたら、届け先は新しく名前を付けたファイル。
                name = result.get("file", name)
                payload = {"type": "optimize_result", **result}
            except Exception as error:  # noqa: BLE001 - 文面をそのまま画面へ出す
                payload = {
                    "type": "optimize_error",
                    "message": f"{type(error).__name__}: {error}",
                    # 中断は失敗ではない: 画面はこの印で通知の色とトーストの有無を変える。
                    "cancelled": isinstance(error, optimize_run.OptimizeCancelled),
                }
            visible = self.session.optimization_visible
        finally:
            self.session.end_optimization()
            self._optimize_last_progress = None
        try:
            if visible:
                await self._broadcast(payload)
            elif name is not None:
                self.session.unseen_optimize_results[name] = payload
        finally:
            await self._broadcast_optimize_status()
            await self._push(None, applied=None)

    def _optimize_progress(self, event: dict) -> None:
        """`optimize_run.run()` の進捗。実行器スレッドから呼ばれるので `_emit_event` 経由。

        中身は `{"phase", "message", "evaluation"}` + phase ごとの追加(`docs/contracts/ui-optimize-integration.md`「最適化の配線」)。
        素の値だけなのでそのまま載せてよい。
        """
        self._emit_optimize_event({"type": "optimize_progress", **event})

    def _optimize_debug(self, event: dict) -> None:
        """開発用の可視化(塊・面アンカーの節点・平滑化前の密度場)。進捗とは別の口: 数十万要素の配列を積んだ大きなメッセージで、
        寿命も読み手も違う。頼んだときしか流れない(`run_optimization` の `debug`)。中身の形は `optimize_run._DebugSink`。
        """
        self._emit_optimize_event({"type": "optimize_debug", **event})

    def _emit_optimize_event(self, payload: dict) -> None:
        """最適化の進捗・可視化を、イベントループへ乗せてから配るか決める(どのスレッドからでも呼べる)。

        配るか否かはイベントループのスレッドで決める(今どの文書を開いているかを書き換えるのがそちらなので、ワーカーで判定すると切り替えと入れ違う)。
        """
        loop = self._loop
        if loop is None:
            return
        loop.call_soon_threadsafe(self._relay_optimize_event, payload)

    def _relay_optimize_event(self, payload: dict) -> None:
        """走らせている文書を開いている間だけ配る。最後の進捗は控えて、戻ってきた画面へ見せる。"""
        if payload["type"] == "optimize_progress":
            self._optimize_last_progress = payload
        if self.session.optimization_visible:
            self._spawn_broadcast(payload)

    async def _announce_optimization(self) -> None:
        """文書を差し替えた後、その文書から見た最適化の状態を配り直す。

        走らせている文書へ戻ったら「実行中」と最後の進捗を、離れたら「走っていない」を配る。
        別の文書を開いている間に終わった実行の結果/失敗は、その文書を開いたときに1度だけ配る。
        """
        if self.session.optimizing:
            # 走っていなければ、画面は終わったときの `optimize_status` で既に「走っていない」を知っている。
            await self._broadcast_optimize_status()
        if self.session.optimization_visible:
            if self._optimize_last_progress is not None:
                await self._broadcast(self._optimize_last_progress)
            return
        name = self.session.current_name
        if name is not None and name in self.session.unseen_optimize_results:
            await self._broadcast(self.session.unseen_optimize_results.pop(name))

    async def _optimize_cancel(self, connection: Any) -> None:
        """中断の旗を立てる。実際に降りるのは次に進捗が出た時点(進捗を出さない短い処理の途中では止まらない)。

        走っていないときは旗を立てない: 「中断中」と画面へ出すと、次の実行が中断表示から始まって見える。
        """
        if not self.session.optimization_visible:
            await _send(connection, {"type": "optimize_status", **self._optimize_status()})
            return
        self.session.cancel_optimization()
        await self._broadcast_optimize_status()

    def _emit_event(self, payload: dict) -> None:
        """状態配信ではないイベント(AI・形状最適化)を全接続へ配る配信を、イベントループへ確実に乗せる。
        どのスレッドからでも呼べる(`call_soon_threadsafe` は呼び出し元がイベントループのスレッドでも安全)。
        """
        loop = self._loop
        if loop is None:
            return
        loop.call_soon_threadsafe(self._spawn_broadcast, payload)

    def _spawn_broadcast(self, payload: dict) -> None:
        self._spawn(self._broadcast(payload), "イベントの配信に失敗")

    async def _broadcast(self, payload: dict) -> None:
        """イベントを全接続へ配る(進捗・結果はどの画面から見ても同じ1つの出来事)。"""
        for target in list(self.clients):
            try:
                await _send(target, payload)
            except Exception:
                self.clients.discard(target)

    async def _push(self, origin: Any, applied: Optional[dict], only: Any = None) -> None:
        """状態を配信する。`only` を指定するとその接続にだけ送る。

        `delta`(geometry付き、版が変わったBodyだけ)は接続ごとに知っている版が違いうるので接続ごとに計算する。
        接続ごとの「delta計算 → 送信 → 版の記録」は `_send_locks` で直列化する。ロック無しだと、新しい版の送信が先に終わって
        古い版が後から届く / 送信前に版を記録して届いていない版を知っている扱いになる、の2通りの事故が起きる。
        ロックは1接続ぶんなので、別々の接続への配信は並行のまま。

        `state` はロックの中(送る直前)で作る。先頭で1回作って使い回すと、ロック待ちだった先発の `_push` が古い `state` で
        後発を上書きする経路ができる。`delta` と同じロックの中なので `state` と `delta` の食い違いも消える。
        代償は接続数ぶん `state_meta()` を計算すること(27部品で数KB・ローカル1ユーザー)。
        """
        targets = [only] if only is not None else list(self.clients)

        async def deliver(target: Any) -> None:
            lock = self._send_locks.setdefault(target, asyncio.Lock())
            async with lock:
                meta = self.session.state_meta()
                known = self._known_revisions.get(target, {})
                delta = self.session.delta(known)
                payload = {
                    "type": "state",
                    "state": meta,
                    "delta": {"bodies": delta["bodies"]},
                }
                if applied is not None and target is origin:
                    payload["applied"] = applied
                try:
                    await _send(target, payload)
                    # **送信が成功してから**版を記録する(送信前に記録すると、失敗時や
                    # 割り込み時に「送っていない版を知っている」ことになってしまう)。
                    self._known_revisions[target] = {
                        body["body_id"]: body["revision"] for body in meta["bodies"]
                    }
                except Exception:  # 落ちた接続のために他へ配れないことを避ける
                    self.clients.discard(target)
                    self._known_revisions.pop(target, None)
                    self._send_locks.pop(target, None)

        # 警告は delta() が呼ぶ geometry() の中で積まれる(deliver() の内側)ので、全ターゲットの配信を終えてから取り出す(1回のpushで複数回積まれても束ねて1回出す)。
        await asyncio.gather(*(deliver(target) for target in targets))
        # 警告の宛先は操作を出したクライアント。MCPからの変更には出し手がいない(`origin is None`)ので配信先全員へ出す
        # (捨てると、AIが操作したときだけ「三角形分割が取れなかった」等が消える)。
        for warning in self.session.take_warnings():
            payload = {"type": "warning", "message": warning}
            for target in [origin] if origin is not None else targets:
                try:
                    await _send(target, payload)
                except Exception:
                    self.clients.discard(target)
