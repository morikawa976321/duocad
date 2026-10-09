"""WebSocketサーバ(「コマンドは下り、イベントは上り」)。静的配信は `duocad/net/http.py`。

アプリ層(ブラウザ)とコアの唯一の接点。**コアはこのサーバが無くても機能的に完全**であり、
ここには「文書を持つ」以外の機能を置かない。

プロトコル(JSON文字列):

    下り(client -> server)
        {"cmd": <コマンド名>, <引数>..., "client_op": int}

**下りコマンドの一覧と引数は `duocad/catalog.py` の `COMMANDS` が正**(ここに写さない)。
ソケットが受け取れる名前は、そこから導出した表(`SKETCH_COMMANDS` / `FEATURE_COMMANDS` /
`FILE_COMMANDS`)と `_dispatch` が個別に捌く分(`catalog` の `ws="special"`)で全部である。
`client_op` はどのクライアントの操作かを返すための添え物で、verbの引数ではない。

    上り(server -> client)
        {"type": "state", "state": {...}, "delta": {"bodies": [...]}, "applied": {...}|None}
        {"type": "preview", "preview_id": int|None, "verb": str, "bodies": [...]}
        {"type": "edges", "body_id": str, "revision": int, "edges": [...]}
        {"type": "file", "action": "save"|"export_step"|"export_stl"|"list", "path"}
        {"type": "error", "message": str, "where": str}
        {"type": "warning", "message": str}
        {"type": "ai_status", "provider", "model", "base_url", "api_key_configured", "api_key_source",
         "api_key_hint", "running", "providers"}
        {"type": "ai_tool_call", "name": str, "arguments": {...}, "result": {...}, "ok": bool}
        {"type": "ai_result", "status", "text", "detail", "turns", "tool_calls": [...], "usage": {...}}
        {"type": "ai_error", "message": str}

**`ai_*` はverbではない**。`ai_get_status` / `ai_configure` / `ai_set_api_key` / `ai_prepare` /
`ai_send` / `ai_cancel` は文書を
直接は変えない(操作ログに載らず、undoの単位でもない) —— 文書を変えるのはエージェントが呼ぶ
既存ツールの側で、それは通常のverb配信としてこの後に届く。したがって `catalog.COMMANDS` には
**入れない**し、対応するMCPツールも作らない(AIがAIを起動する口は要らない)。

**verbコマンドは対象集合(面・辺・領域)を引数で受けない**。数値・名前・パスだけを送り、押す面も
丸める辺も押し出す領域もverbの入口(`duocad/entry.py`)がコアの選択から取る
(`selection.require_selected` / `require_edges`)。`body_id` が残っているのは対象集合ではない
ものだけ(`set_visible` / `get_edges` の対象、`extrude` の行き先)。

選択の口は3つ(`modify_selection` / `toggle_selection` / `clear_selection`)。`set_selection`
(述語を取る置き換え)は開けていない(人はクリックで選ぶ)。

`preview` だけは**状態を配信しない**。verbを投機的に実行して形だけ測り、文書は
`duocad.speculative` が元へ戻す(確定した1回だけが操作ログに積まれる)。したがって
応答は要求した接続にだけ返り、他のクライアントには何も起きない。

    `state.bodies` は `geometry` を含まない(name/kind/visible/face_count/volume/revisionのみ、
    27部品でも数KB)。形状は `delta.bodies`(接続ごとに版が変わったBodyだけ、`geometry` 付き)で
    届く。毎回27部品ぶんのテセレーション結果(実測6MB超)を送り直すと、パーツ分解でサーバ側を
    速くした意味がクライアント側の描画で消える。接続直後は「知っている版」が空なので
    `delta.bodies` に全Bodyが入り、初期描画が揃う。

**状態変更は接続している全クライアントへ配信する**(これが無いと交互操作が成立しない)。
操作を出したクライアントにだけ `applied` を付け、それ以外には同じ `state` だけを送る。

    state() -> {
      "units", "active_body_id",
      "bodies": [{"body_id","name","kind","visible","revision",
                  "face_count","volume","geometry": <tessellate結果|None>}, ...],
      "log", "can_undo", "can_redo", "applied_count",
      "selection": {"items": [...], "count": int},
    }
    delta(known_revisions) -> {"bodies": [<上と同じ形・版が変わった分だけ>],
                               "unchanged": [BodyId, ...]}

**選択が `state` に載っていないとAIが書き換えた選択はブラウザへ届かない**(`_on_core_change` は
MCP起点の変更から `applied` を落として配るため。`set_visible` の効果が `bodies[].visible` で
届いているのと同じ場所)。選択の変更イベントは
`{"change": "selection", "selection": {...}, "client_op": ...}` の形で、verbの `applied` とは
キーで区別する(選択はverbではなく、操作ログにも載らない)。**可視性も同じ `change` 系**
(`{"change": "visibility", ...}`)—— こちらもverbではない。

**OCPを直接importしない**(境界はkernel.pyだけ)。
"""
from __future__ import annotations

import asyncio
import functools
import json
import threading
from typing import Any, Optional  # Any: listeners / 接続

from .. import catalog, optimize_run, selection
from ..agent import AgentLoop, Cancellation, Limits, ProviderError
from ..agent.loop import FIRST_LOOK
from ..agent.providers import (
    ANTHROPIC_MAX_TOKENS,
    DEFAULT_OPENAI_COMPAT_BASE_URL,
    api_key_configured,
    api_key_status,
    default_model,
    delete_api_key,
    save_api_key,
)
from ..session import Session
from .tools_on_loop import ToolsOnLoop

# --------------------------------------------------------------------------------------
# ソケットから踏める口(**すべて `duocad/catalog.py` の宣言からの導出**)
# --------------------------------------------------------------------------------------
# **ここに手で表を書かない。** verbのメタ情報(名前・引数・カテゴリ・プレビュー可否・
# MCPのスキーマ)は `catalog.COMMANDS` が唯一の宣言で、verbを1つ足すときに触るのは
# verb本体と宣言1箇所だけである。
#
# 導出しても**登録制であることは変わらない**: ここに現れない名前はソケットから呼べない
# (届いた文字列で `Session` の任意のメソッドを呼べる作りにしない)。届いた引数は宣言と
# 突き合わせる(`catalog.Command.ws_arguments`。必須・未知・型) —— 既定値で補うと、
# 打ち忘れた寸法や `mode` が黙って既定で入る。**並ぶのは数値・名前・パスだけで、
# 面・辺・領域は1つも無い**。
#: ブラウザから踏めるスケッチの verb -> (`Session` のメソッド名, 受け取る引数名)。
SKETCH_COMMANDS: dict = catalog.ws_table("sketch")

#: ブラウザから踏めるフィーチャの verb -> (`Session` のメソッド名, 受け取る引数名)。
FEATURE_COMMANDS: dict = catalog.ws_table("feature")

#: ブラウザから踏めるファイル操作 -> (`Session` のメソッド名, 必須の引数名, 文書を変えるか)。
#: **文書を変えるかどうかで応答が違う**のがこの表だけの事情:
#:
#: - `import_step` … 文書を変える(ログに載る)。応答は状態配信(`import_svg` はスケッチの
#:   コマンドなので、この表には無い —— `SKETCH_COMMANDS` 側にいる)
#: - `export_step` / `export_stl` … 文書を変えない。応答は書いた先の
#:   パスだけ(要求した接続にだけ返る。`preview` と同じ扱い)
#: - `open_document` はどちらでもない —— 文書を**差し替える**ので全接続へ配る必要があり、
#:   表からは外して `_dispatch` で個別に扱う。
#: - **`save_document` も表から外れている** —— `name` が任意引数になり、
#:   「表に載る引数は全部必須」の規律に合わなくなったため(`catalog.Param.ws` 参照)。
#:   応答は `file` の封筒 + 状態の配り直し(現在ファイル名が変わるため)。
#: - **`rename_document` も表から外れている** —— 応答が `save_document` と同じ
#:   「`file` の封筒 + 状態の配り直し」で、表の「封筒だけ」に合わないため。
FILE_COMMANDS: dict = catalog.file_table()

#: アプリ内AIが繋げるプロバイダ(`ai_configure` が受け付ける綴り・`ai_status.providers`)。
#: 画面はこの長さで選択UIの要否を決める。`anthropic` はキーが無くても並べる —— 選んで送ると
#: 「どこにキーを置くか」がエラーとして返る(並べないと、その道があること自体が見えない)。
AI_PROVIDERS: tuple = ("openai_compat", "anthropic")

#: プロバイダごとの打ち切り(無ければ `Limits()`)。出力上限の理由は `ANTHROPIC_MAX_TOKENS`。
#: 合計時間(`max_total_seconds`)は人が待てる時間なので、プロバイダによらず同じにしてある。
AI_LIMITS: dict = {"anthropic": Limits(max_tokens=ANTHROPIC_MAX_TOKENS)}

async def _send(connection: Any, payload: dict) -> None:
    await connection.send(json.dumps(payload, ensure_ascii=False))

class Server:
    """WebSocketの接続集合とコマンドの実行。状態変更は全接続へ配信する。

    **接続ごとに「知っている版」を覚える**(`_known_revisions`)。文書は部品ごとに複数の
    Bodyを持つぶん、接続によって最後に送った版が違いうる(例: 遅れて `get_state` した接続は
    まだ何も知らない)。差分(`Session.delta`)はこの接続ごとの版を基準に計算する。

    **同時編集は前提にしない**。複数接続へ配信する作りだが、想定するのは
    「1人が1つのブラウザで開いている」ことだけで、**同じ文書を複数の接続が同時に編集する
    経路に `Session` 全体を守る排他は無い**。verbの適用自体は同期でawaitを挟まないので
    形状が壊れることはないが、ある接続が `_push` のawaitで待っている間に別の接続の操作が
    先に処理されうる(コマンド単位のインターリーブ)。操作ログや選択状態が意図と違う順序で
    反映されるのはこの前提の内側である。**同時編集を仕様にするなら、まずここへ排他を入れる。**
    """

    def __init__(self, session: Session) -> None:
        self.session = session
        self.clients: set = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # WebSocket経由の操作を処理している最中か(`_on_core_change` の二重配信よけ)。
        # `Session` の操作メソッドは同期なので、この印は「その1回の呼び出しの間」だけ立つ。
        self._dispatching = 0
        # 進行中の配信タスク(捨てるとGCされうる。`_spawn_push` 参照)
        self._pending_pushes: set = set()
        # **コアの変更を購読する**。WebSocket以外の主体(MCPサーバ)が
        # 文書を変えたとき、ここが全接続へ配信する。これが上り方向の実体。
        session.add_listener(self._on_core_change)
        self._known_revisions: dict = {}  # connection -> {body_id: revision}
        # 接続ごとの送信ロック。**2つの `_push` 呼び出し(例: 2クライアントがほぼ同時に
        # 操作したとき)が同じ接続への配信で競合するのを防ぐ**(ロック無しだと
        # 「known_revisionsの更新→await送信」の間に別の`_push`が割り込み、
        # (a) 古い版のペイロードが後から届く、(b) 実際には送っていない版を「知っている」ことに
        # されてしまい以後の差分が欠落する、の2通りの事故が起きうる)。
        self._send_locks: dict = {}  # connection -> asyncio.Lock
        # -------------------------------------------------- G: アプリ内AIエージェント
        # 道具は起動時に一度だけ注入される(`bind_agent_tools`。`bind_loop` と同じ形)。
        # このモジュールは `mcp_server.DocumentTools` を作れない(`mcp_server` が
        # `server` をimportしているので逆方向は循環になる)ので、実体は `mcp_server._run`
        # が渡す。`ToolSurface`(`descriptors`/`call`/`error_envelope`)を満たせば何でもよい。
        self._ai_tools: Optional[Any] = None
        # キーが置いてあれば Claude から始める(選び直しはサーバを立て直すたびに消える)。
        self._ai_provider_kind = "anthropic" if api_key_configured() else "openai_compat"
        self._ai_model: Optional[str] = None
        self._ai_base_url: Optional[str] = None
        self._ai_loop: Optional[AgentLoop] = None
        self._ai_cancellation = Cancellation()
        self._ai_running = False
        #: ウォームアップを始めた会話と、その終わりを待つための印(`_ai_prepare`)。
        self._ai_prepared_loop: Optional[AgentLoop] = None
        self._ai_prepare_done: Optional[threading.Event] = None
        #: 実行中のバックグラウンド往復(GCされないよう保持。`_pending_pushes` と同じ理由)。
        self._pending_ai_runs: set = set()
        # -------------------------------------------------- H: 形状最適化を走らせる
        # **重い(数十秒〜数分)ので別スレッドで回す**(`_optimize_start`)。「いま走っているか」と
        # 中断の旗は `Session` が持つ(`Session.begin_optimization` / `optimization_status`)。
        #: 実行中の最適化タスク(`_pending_ai_runs` と同じ理由で参照を保持する)。
        self._pending_optimize_runs: set = set()
        #: **進捗・結果は走らせている文書を開いている間だけ配る**(`Session.optimization_visible`)。
        #: 別の文書を開いている間に出た最後の進捗はここに、終わったときの結果/失敗は
        #: `Session.unseen_optimize_results` に控え、その文書へ戻ってきたときに見せる(`_announce_optimization`)。
        self._optimize_last_progress: Optional[dict] = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """配信に使うイベントループを覚える(`run()` と `mcp_server` が起動時に呼ぶ)。

        コアの変更イベントは**同期の呼び出しの中**で飛んでくる(MCPのツールは
        `Session.set_visible` を素で呼ぶ)ので、そこから非同期の配信を始めるために
        ループの参照が要る。
        """
        self._loop = loop

    def bind_agent_tools(self, tools: Any) -> None:
        """アプリ内AIが使う道具を注入する(`mcp_server._run` が起動時に1回呼ぶ)。

        `duocad.mcp_server.DocumentTools` を受け取る想定だが、型では縛らない
        (`ToolSurface` を満たす任意の値。テストは `SpyTools` のような薄い覆いを渡す)。
        """
        self._ai_tools = tools

    def _on_core_change(self, applied: Optional[dict] = None) -> None:
        """コアが変わった。**WebSocket以外の主体による変更だけ**を全接続へ配信する。

        WebSocket経由の操作は `_dispatch` が自分で `_push` するので、ここで配ると
        同じ状態を2回送ることになる(帯域だけでなく、`applied` の付いた配信と
        付かない配信が交錯して画面側の相関が壊れる)。`_dispatching` の印で区別する。

        `applied` を**載せずに**配るのは、MCPからの変更は「誰の操作でもない外からの
        変化」であり、`applied` は操作を出したクライアントへの確認応答だからである
        (ブラウザは `applied.client_op` で自分の操作と突き合わせる。他人の操作に
        付けて送ると、知らない `client_op` を自分のものとして拾う経路ができる)。
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
        """配信タスクを起こし、**参照を持ち続ける**。

        `create_task` の戻りを捨てるとタスクは弱参照でしか繋がらず、途中でGCされうる
        (公式ドキュメントの警告)。加えて、配信中の例外を誰も回収しないと
        「Task exception was never retrieved」が出るだけで**ブラウザは古いまま・
        警告も出ない**という、完了条件3が静かに壊れる形になる。集合で保持し、
        終わったら外し、落ちていたら `failure` を頭に警告として残す。
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
                # **最適化が走っているかも最初に知らせる。** 開始・終了・中断のときにしか配らないので、
                # 再読み込みした画面は中断ボタンを失い、終わる瞬間に切れていた画面は実行中のまま固まる。
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

        **誰も見ていない状態でAIの実行(=文書の変更)を放置しない**。**最後の1接続が切れた
        ときだけ中断する** —— 他の画面がまだ見ている間に片方が閉じても実行は続ける
        (`ai_*` は接続ごとではなく共有の1会話という設計と揃える)。

        **形状最適化(`Session.optimizing`)は同じ扱いにしない**。
        あちらは**1回で終わる有界な仕事**で、終われば `add_mesh_body` 1行が積まれるだけ
        (要らなければ選んで消せる)。対してAIのループは何回ツールを呼ぶか決まっていない。
        ここで切ると、**画面をリロードしただけで数分の実行が黙って消える** ——
        リロードは日常の操作であり、失うものの方が大きい。再接続した画面には結果が
        通常の状態配信として届き、実行中なら `handle()` が最初に `optimize_status` を送る。
        """
        if not self.clients and self._ai_running:
            self._ai_cancel()

    async def _push_applied(self, connection: Any, applied: dict, message: dict) -> None:
        """verb の応答に `client_op`(要求した画面が付けた通し番号)を載せて全接続へ配る。

        載せないものは載せない理由がそれぞれにある: 状態を配り直すだけのもの(`get_state` /
        `set_view_resolution` / `save_document`)、`file` の封筒で返す書き出し。
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
            # -------------------------------------------------- B: 選択
            # **コアの選択集合をそのまま叩く**(アプリ層に選択状態を持たせない)。
            # 発火したイベント(`{"change":"selection", ...}`)を `applied` として返し、
            # 状態は全接続へ配信する(`state.selection` に入っている)。
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
            # -------------------------------------------------- C/D: スケッチとフィーチャの verb
            # 引数は宣言(`catalog.Command.ws_arguments`)に従って取り出し、検査してから
            # `Session.apply` へ渡す —— MCP とアプリ内AIが通るのと同じ1本で、verb ごとの分岐は無い。
            # **省略された引数は渡さない**(ここで既定値を埋めると、`move_body` のようにモードで
            # 要る引数が入れ替わる verb で、モードが取らない引数が黙って効く側へ倒れる)。
            #
            # **失敗は握りつぶさず必ず画面へ返す**(下の except)—— とくにフィレットは例外を
            # 投げずに `IsDone()=False` で失敗する種類の失敗があり、コア側が例外へ変換している(§7)。
            # ここで握ると「押したのに何も起きない」になり、原因が半径にあることが人に伝わらない。
            elif command in SKETCH_COMMANDS or command in FEATURE_COMMANDS:
                applied = self._apply(
                    self.session.apply, command, catalog.BY_NAME[command].ws_arguments(message)
                )
                await self._push_applied(connection, applied, message)
            # -------------------------------------------------- D: 辺の折れ線(要るときだけ)
            # **配信しない**(`preview` と同じ扱い)。文書は変わらないので状態を配る理由が無く、
            # 要求した接続にだけ返す。`Session.edges` のdocstringが「常時配らない」理由を持つ。
            elif command == "get_edges":
                result = self.session.edges(message["body_id"])
                await _send(connection, {"type": "edges", **result})
            # -------------------------------------------------- D: 頂点の座標(選択に入った分だけ画面に出る)
            # **配信しない**(`get_edges` と同じ)。要求した接続にだけ返す。
            elif command == "get_vertices":
                result = self.session.vertices(message["body_id"])
                await _send(connection, {"type": "vertices", **result})
            # -------------------------------------------------- 測定(verbではない)
            # **配信しない**(`get_edges` と同じ)。文書は変わらない。`client_op` を返すのは、
            # 画面がコマンド層の「確定が返ってきた」の突き合わせに使うため(verb なら
            # 状態配信の `applied.client_op` が担う役)。
            elif command == "measure":
                result = self.session.measure()
                await _send(
                    connection,
                    {"type": "measure", "client_op": message.get("client_op"), **result},
                )
            # -------------------------------------------------- 可変解像度(ズーム連動)
            # **文書は変わらない**が、配る形の粗さが変わるので**全接続へ配り直す**
            # (`get_edges` と違って要求元だけに返す話ではない —— 粗さは
            # `DocumentView` が**文書に1つ**持つ画面の設定で、接続ごとには持たない。
            # これは上の `Server` docstring と同じ**単一ブラウザ前提**に乗った選択である
            # (理由と、同時編集を仕様に
            # するときに何を変えるかは `DocumentView.set_view_resolution` の docstring)。
            # 変わらなかったときは何も送らない —— ズーム操作は連続で飛んでくるので、
            # 同じ値での再配信を許すと27部品ぶんのメッシュが無駄に流れる。
            # `focus` は **face単位**の高精度化。`{body_id: [1始まりの面添字]}` で
            # 「視錐台に入っている/カメラに正面を向けている面」だけを細かく測らせる。
            # **判定は画面側にしか作れない**(カメラを持っているのがそちらだから)ので、
            # ここは受け取った事実を渡すだけにする。省略・不正な値なら全面が細かい側になり、
            # 可変解像度の既定と同じ挙動に戻る(旧クライアント互換)。
            elif command == "set_view_resolution":
                changed = self.session.set_view_resolution(
                    message["world_per_pixel"],
                    message.get("size_hint") or 0.0,
                    message.get("focus"),
                )
                if changed:
                    await self._push(connection, applied=None)
            # -------------------------------------------------- E: ファイル操作
            # **SVG読み込みはここには無い** —— `import_svg` はスケッチのverbで
            # `sketch_id` を必須引数に取るため表(`SKETCH_COMMANDS`)に載る側で捌く
            # (上のC節で他のスケッチのverbと一緒に扱う)。
            # **verbかどうかで応答が違う**(`FILE_COMMANDS` の3つ目の値)。文書を変えるものは
            # 状態を全接続へ配り、書き出すだけのものは要求した接続へ結果だけ返す。
            elif command in FILE_COMMANDS:
                method, _keys, is_verb = FILE_COMMANDS[command]
                arguments = catalog.BY_NAME[command].ws_arguments(message)
                if is_verb:
                    applied = self._apply(self.session.apply, command, arguments)
                    await self._push_applied(connection, applied, message)
                else:
                    applied = self._apply(getattr(self.session, method), **arguments)
                    await _send(connection, {"type": "file", **applied})
                    # 開いている文書を消すと閉じる(`Session.delete_document`)。文書が替わったので
                    # 全接続へ配る —— 封筒だけだと、他のタブは消えた文書を出したまま編集を断られ続ける。
                    if applied.get("closed"):
                        await self._push(connection, applied=None)
            # **保存は表に載らない** —— `name` が任意引数になったため。
            # 表(`FILE_COMMANDS`)は載せた引数を全部必須にする規律なので、省略を許す
            # コマンドはここで個別に捌く(`catalog.Param.ws` のdocstring)。
            #
            # 契約は `Session.save_document`: **引数なし=編集中のファイルへ上書き** /
            # **`name` あり=新しい名前でのみ保存**(既存名は拒否)。
            #
            # **書いた後に状態も配り直す**。「いま編集しているファイル」はサーバの状態で
            # (`state.current_name`)、名前を付けて保存すればそれが変わる —— `file` の封筒
            # だけを返すと、ヘッダが「(無題)」のままなのに Ctrl+S と自動保存は現在の
            # ファイルへ書き続ける、という食い違いが画面に残る。文書そのものは変わらないので
            # `applied` は付けない(操作ログに載らないのは従来どおり)。
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
            # **開くは文書ごと差し替える**ので全接続へ配る(自分だけ新しい文書を見ている、
            # という状態を作らない)。`Session.open_document` が発火したイベントは
            # `_on_core_change` 側でも拾われるが、`_apply` の間は `_dispatching` が
            # 立っているので二重には配らない。
            #
            # **今の文書と同じ名前を、2接続以上つながっている状態で開こうとしたら拒否する**。
            # 単一ブラウザ前提を守る番人で、
            # `open_document` はディスクから読み直して差し替えるため、素通しにすると
            # 別タブの未保存の編集が黙って消える。**接続が1つだけ**(別タブが無い)なら
            # 開き直しは今までどおり通す——保存前に戻す手立てとして使われている。
            elif command == "open_document":
                name = message["name"]
                if name == self.session.current_name and len(self.clients) > 1:
                    raise ValueError(
                        f"{name} は既に別のタブで開いている"
                        "(そちらのタブを使うか、閉じてから開き直す)"
                    )
                applied = self._apply(self.session.open_document, name)
                await self._push_applied(connection, applied, message)
                await self._announce_optimization()
            # **新規も文書ごと差し替える**ので開くと同じ扱い(全接続へ配る)。引数は無い。
            elif command == "new_document":
                applied = self._apply(self.session.new_document)
                await self._push_applied(connection, applied, message)
                await self._announce_optimization()
            elif command == "close_document":
                applied = self._apply(self.session.close_document)
                await self._push_applied(connection, applied, message)
                await self._announce_optimization()
            # -------------------------------------------------- B: プレビュー(投機的実行)
            # **配信しない**。文書は変わらない(`Session.preview` が元へ戻す)ので、状態を配る
            # 理由が無く、他のクライアントに見せる中間状態も存在しない。要求した接続にだけ返す。
            #
            # **`_apply` で囲まない**(2つの止め方を重ねない)。プレビュー中の発火は
            # `Session._suppress_events` が経路によらず止めており、ここで `_dispatching` まで
            # 立てると、**プレビューの計算中(押し出しなら約1秒)にMCPから届いた本物の変更まで
            # 「WebSocket経由だから配信不要」と誤判定して黙って捨てる**。
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
            # -------------------------------------------------- G: アプリ内AIエージェント
            # **verbではない**(モジュールdocstring参照) —— 応答は状態配信(`type: state`)
            # ではなく専用のイベント(`ai_status` / `ai_tool_call` / `ai_result` / `ai_error`)。
            # AIがツールを呼んで文書を変えたときは、その変更が通常のverb配信として
            # 別途(`applied=None`、`_on_core_change` 経由で)全接続へ届く。
            elif command == "ai_get_status":
                await _send(connection, {"type": "ai_status", **self._ai_status()})
            elif command == "ai_configure":
                status = self._ai_configure(
                    message.get("provider"), message.get("model"), message.get("base_url")
                )
                # **ここは既にイベントループのスレッド**(`_dispatch` の中)なので、
                # `_emit_event` の `call_soon_threadsafe` を挟まず直接 `await` する
                # (待たずに関数を抜けると、テストのようにここで `asyncio.run` が閉じる
                # 呼び方では配信タスクが実行される前に握りつぶされる)。
                await self._broadcast({"type": "ai_status", **status})
            elif command == "ai_set_api_key":
                await self._broadcast({"type": "ai_status", **self._ai_set_api_key(message.get("key"))})
            elif command == "ai_prepare":
                self._ai_prepare()
            elif command == "ai_send":
                await self._ai_start(connection, str(message.get("message") or ""))
            elif command == "ai_cancel":
                self._ai_cancel()
                await self._broadcast({"type": "ai_status", **self._ai_status()})
            # -------------------------------------------------- H: 形状最適化を走らせる
            # **verbではない**(`catalog` の `category="protocol"`) —— 応答は状態配信ではなく
            # 専用のイベント(`optimize_conditions` / `optimize_resolution` /
            # `optimize_status` / `optimize_progress` / `optimize_result` / `optimize_error` /
            # 頼まれたときだけ `optimize_debug`)。
            # 結果が文書へ入ったことは、`Session` のリスナ経由(`_on_core_change`)で
            # 通常の状態配信として別途届く。
            elif command == "optimize_conditions":
                await _send(
                    connection,
                    {"type": "optimize_conditions", **self.session.optimize_conditions()},
                )
            # **失敗を `type: "error"` に落とさない**(下の except へ行かせない)。
            # 条件面がまだ1枚も無い状態は「設定の途中」であって事故ではなく、画面は
            # このメッセージをパネルの中に出す —— 赤い札で流すと、条件を置いている
            # 最中ずっとエラーが点滅することになる。
            elif command == "optimize_resolution":
                try:
                    payload = self.session.optimize_resolution(message.get("rmin_mm"))
                except ValueError as error:
                    payload = {"error": str(error)}
                await _send(connection, {"type": "optimize_resolution", **payload})
            elif command == "run_optimization":
                await self._optimize_start(connection, message)
            elif command == "cancel_optimization":
                await self._optimize_cancel(connection)
            else:
                raise ValueError(f"unknown command: {command!r}")
        except Exception as error:  # 1つの失敗でサーバを落とさない。必ず画面へ返す
            await _send(
                connection,
                {
                    "type": "error",
                    "where": str(command),
                    "message": f"{type(error).__name__}: {error}",
                },
            )

    def _apply(self, call: Any, *args: Any, **kwargs: Any) -> dict:
        """WebSocket経由の変更を実行する。この間だけ `_dispatching` を立てる。

        **同期の呼び出しだけを囲む**(`await` を挟まない)。印を配信(`_push`)の間まで
        立てたままにすると、その待ち時間に届いたMCPからの変更まで
        「WebSocket経由だから配信不要」と誤判定して黙って捨ててしまう。
        """
        self._dispatching += 1
        try:
            return call(*args, **kwargs)
        finally:
            self._dispatching -= 1

    # ------------------------------------------------------------------ G: アプリ内AIエージェント
    def _ai_status(self) -> dict:
        """画面に出してよい設定の全部。**キーの値は載せない**(載せるのは `api_key_status()` の3つだけ)。

        `model` / `base_url` は明示的に選んでいなければ**プロバイダの既定値**を返す
        (`null` のままだと画面が「何と話しているか」を言えない)。`anthropic` の `base_url` は
        常に `null`(送り先は固定で、選べるものが無い)。

        **`providers` は増減しうる列として返し続ける** —— 画面はこの長さを見て
        選択UIを出すか決める(1つなら出さない)。
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
            "providers": list(AI_PROVIDERS),
        }

    def _ai_configure(
        self, provider: Optional[str], model: Optional[str], base_url: Optional[str]
    ) -> dict:
        """プロバイダ・モデル・エンドポイントを差し替える。**キーは扱わない**(値を受け取らない)。

        実行中は変更を受け付けない —— `self._ai_cancellation` を作り直すと、既に走っている
        `AgentLoop.run()` が古い `Cancellation` を見続けたままになり、中断ボタンが効かなくなる
        (`loop_obj.run` は `run_in_executor` に渡した時点でその回のオブジェクトを掴んでいる)。

        **`base_url` を自由に差し替えてよいのは、鍵を持たないプロバイダだけ**である。
        `anthropic` は実キーを `x-api-key` に載せて `{base_url}/v1/messages` へ送るので、送り先を
        変えられると `ai_configure` → `ai_send` の2手でキーが外へ出る(WebSocketは同一オリジン
        ポリシーで止まらないので、127.0.0.1 bindは防御にならない)。`anthropic` を選んでいる間の
        `base_url` は**明示的なエラーで拒否する**(黙って無視すると、効かない理由が画面から分からない)。
        `openai_compat` はキーを持たないので、どこを指しても飛ぶのはキーの無いリクエストだけになる。
        """
        if self._ai_running:
            return {**self._ai_status(), "error": "AI応答中は設定を変更できない(中断してから)"}
        if provider is not None and provider not in AI_PROVIDERS:
            return {**self._ai_status(), "error": f"未知のプロバイダ: {provider!r}"}
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
        """設定ダイアログから届いたClaudeのAPIキーを書く(`None` なら消す)。**値は返さない**。

        書いたら Claude へ切り替える(キーを置く理由はそれを使うことなので、起動時の
        「キーがあれば Claude から始める」と揃える)。会話は作り直す —— 走っている `AgentLoop` の
        プロバイダは古いキーを持っている。実行中は `_ai_configure` と同じ理由で受け付けない。

        **このコマンドは `catalog.COMMANDS` にもMCPにも載せない**(AIがキーを書き換える口を作らない)。
        拒否の理由にキーの値を入れない(`key_problem`)。
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
        # **`build_provider` は入口(`duocad.server`)越しに引く。** プロバイダの差し替え点は
        # `duocad.server.build_provider` で、`tests/test_agent_ws.py` の7箇所がそこを
        # monkeypatchする —— ここで `from ..agent import build_provider` すると差し替えが
        # 素通りし、検査が本物のプロバイダを立ててしまう(名前は入口にあるので
        # monkeypatch自体は成功し、**黙って効かない**)。入口はこのモジュールをimportするので、
        # 逆向きの参照は呼び出し時まで遅らせる(import時の循環は起きない)。
        from .. import server as entry

        # **`anthropic` には `base_url` を渡さない**(`_ai_configure` が拒否しているうえでの二重の保証。
        # 検証を素通りする経路がどこかに生まれても、実キーが外部ホストへ渡る手前で止まる)。
        kind = self._ai_provider_kind
        provider = entry.build_provider(
            kind, model=self._ai_model, base_url=None if kind == "anthropic" else self._ai_base_url
        )
        marshaled = ToolsOnLoop(self._ai_tools, self._loop)
        self._ai_loop = AgentLoop(
            marshaled,
            provider,
            limits=AI_LIMITS.get(kind, Limits()),
            cancellation=self._ai_cancellation,
            progress=self._ai_progress,
            first_look=FIRST_LOOK,
        )
        return self._ai_loop

    def _ai_progress(self, name: str, arguments: dict, result: Any, ok: bool) -> None:
        """`AgentLoop` の進捗コールバック。**実行器スレッドから呼ばれる**
        (`_ai_start` の `run_in_executor`)ので、`_emit_event` 経由でイベントループへ戻す。
        """
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
        """固定プレフィックス(system + tools)を先に読ませておく。**1つの会話につき1回**。

        画面が繋がった時点と設定を変えた直後に届く。cold の読み込みは遅い機種で分単位かかるので、
        人が最初の依頼を打っている間に済ませる(実測: 最初の依頼の処理量が 6,686 → 16 トークン)。

        **何も返さない** —— 相手がまだ居ないのは事故ではなく、最初の依頼がそれを言う。
        `ai_get_status` に相乗りさせないのは、あちらが状態を読むだけの口だからである
        (読んだだけで外へHTTPが飛ぶと、検査や監視が相手のスロットを書き換える)。
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
        """1つの発話でエージェントを起こす。**`AgentLoop.run()` を待たずに返る** ——

        モデルへのHTTPリクエストは数十秒かかりうる(モジュールdocstring・
        `duocad/agent/loop.py` の設計docstring)。ここで `await` すると、その間
        同じ接続からの次のメッセージ(`ai_cancel` を含む)が読めなくなる
        (`handle()` は `async for raw in connection: await self._dispatch(...)` で
        1接続を直列に処理するため)。したがって `run_in_executor` へ投げっぱなしにし、
        進捗・結果は別のイベント(`ai_tool_call` / `ai_result`)として後から届ける。
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

        # **ウォームアップが走っていれば、終わってから始める。** 並べて投げても相手の側で後ろに
        # 並ぶだけで、その待ちが `run()` の合計時間の予算を食って時間切れになる。
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
        """中断フラグを立てる。**ループはモデルへ投げる前とツール1件ごとに見る**
        (`duocad/agent/loop.py` の `Cancellation` docstring)。走っているHTTPリクエストは
        プロバイダが接続を閉じて止める。
        """
        self._ai_cancellation.cancel()

    # ------------------------------------------------------------------ H: 形状最適化を走らせる
    def _optimize_status(self) -> dict:
        """画面が実行ボタンと中断ボタンの出し分けに使う最小限(`Session.optimization_status`)。"""
        return self.session.optimization_status()

    async def _broadcast_optimize_status(self) -> None:
        """`optimize_status` を全接続へ配る。**中身は接続ごとに送る直前に取り直す** —— `_broadcast` は
        接続を1つずつ await するので、作り置きの1通を配る間に実行が終わると、後ろの接続には
        「走っている」が終了の通知より後に届き、実行中の表示のまま固まる。
        """
        for target in list(self.clients):
            try:
                await _send(target, {"type": "optimize_status", **self._optimize_status()})
            except Exception:
                self.clients.discard(target)

    async def _optimize_start(self, connection: Any, message: dict) -> None:
        """最適化を1回走らせる。**`run_optimization()` を待たずに返る**(`_ai_start` と同じ形)。

        既定の設定でも数十秒〜数分かかるので、ここで `await` すると同じ接続からの次の
        メッセージ(**`cancel_optimization` を含む**)が読めなくなる —— `handle()` は
        1接続を直列に処理する。したがって別タスクへ投げ、進捗・結果は後からイベントで届ける。

        **走らせる前に条件を点検する。** 条件が足りないまま `run_optimization` を呼ぶと
        例外の文言だけが返るが、画面が出したいのは「何が足りないか」の一覧
        (`conditions_report` の `problems`)である。
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

        # **新しい引数に加えて、旧引数(`rmin_mm`/`max_iter`/`material_min_mm3`/
        # `material_max_mm3`)も転送する。** ここで黙って落とすと、旧引数を送ってきた
        # クライアントには何も届かず「無視された」のと区別が付かない —— 転送して
        # `session.run_optimization` に断らせることで、`OptimizeInputError` が
        # `optimize_error` イベントとして画面へ返る(不支持と分かるエラーにする)。
        kwargs = {
            key: message[key]
            for key in (
                "element_size_mm", "target_safety_factor", "max_evaluations", "min_width_mm",
                "rmin_mm", "max_iter", "material_min_mm3", "material_max_mm3",
            )
            if message.get(key) is not None
        }
        # **開発用の可視化を頼まれたか**(画面の `?debug=space` / `?debug=anchors` /
        # `?voxel=1`)。頼まれた種類だけを `optimize_debug` イベントで配る。
        # **既定は空** —— 付けなければサーバは配列を1本も作らず、既定の画面は1画素も変わらない。
        wanted = message.get("debug")
        if isinstance(wanted, (list, tuple)):
            kinds = [str(k) for k in wanted if str(k) in optimize_run.DEBUG_KINDS]
            if kinds:
                kwargs["debug_kinds"] = kinds
        # **中断の旗を畳むのも文書を閉じるのもここ(イベントループのスレッド)で済ませる。** ワーカー
        # スレッドの中で畳むと、走り出すまでに届いた中断が消える(issue #35)。
        self.session.begin_optimization()
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

    async def _optimize_run(self, kwargs: dict) -> None:
        """別スレッドで端から端まで回し、結果を配る。**例外を握り潰さない**。

        `optimize_run.run()` の例外は「次に何をすればよいか」を文面に持っている
        (`20260904-1704` の契約①)ので、そのまま `optimize_error` として画面へ出す。

        **最後に必ず状態を配り直す**(`_push`)。文書が変わったぶんはコアのリスナ
        (`_on_core_change`)が配るが、`state != "done"` のときは文書が変わらないので
        その配信が起きない —— **`Session.warnings` に積まれた通知**(条件面が空 /
        保持・除外が表現できていない)を運ぶ経路がそこしか無いので、ここで流し切る。

        **結果を足したら(`state == "done"`)配る前にディスクへ書く**(2026-09-13実測: 数時間がかりの
        実行が、結果を見る前にサーバプロセスごと消えた)。結果は文書のRAMにしか無いので、
        ここを抜けたあと画面が0枚でもサーバごと落ちても、書けてさえいれば残る。**結果を足さなかった
        実行では書かない** —— 文書が変わっておらず、1操作ごとの自動保存が既に書いてある(issue #36)。
        書くのは `Session.deliver_optimization_result` で、**イベントループへ戻ってから**呼ぶ
        (人の「開く」と同じスレッドで、届け先の判定と文書の差し替えを済ませるため)。

        **別の文書を開いている間に終わったら、結果も失敗も配らずに控える**(ファイル名ごと)。
        その文書の画面には何も出さず、走らせた文書へ戻ってきたときに見せる(`_announce_optimization`)。
        """
        payload: dict
        name = self.session.optimizing_name
        visible = True
        try:
            try:
                if "debug_kinds" in kwargs:
                    kwargs["debug"] = self._optimize_debug
                result = await asyncio.to_thread(
                    functools.partial(
                        self.session.run_optimization, progress=self._optimize_progress, **kwargs
                    )
                )
                result = self.session.deliver_optimization_result(result)
                # 無題の文書の結果を別の文書を開いている間に届けたら、届け先は新しく名前を付けたファイル。
                name = result.get("file", name)
                payload = {"type": "optimize_result", **result}
            except Exception as error:  # noqa: BLE001 - 文面をそのまま画面へ出す
                payload = {
                    "type": "optimize_error",
                    "message": f"{type(error).__name__}: {error}",
                    # **中断は失敗ではない**(人がそう言ったのだから、赤い札で驚かせる理由が無い)。
                    # 画面はこの印を見て、通知の色とトーストの有無を変える。
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
        """`optimize_run.run()` の進捗。**実行器スレッドから呼ばれる**ので `_emit_event` 経由。

        中身は `{"phase", "message", "evaluation"}` + phase ごとの追加(`20260904-1704`
        の契約①)。**素の値だけでできている**のでそのまま載せてよい。
        """
        self._emit_optimize_event({"type": "optimize_progress", **event})

    def _optimize_debug(self, event: dict) -> None:
        """開発用の可視化(塊・面アンカーの節点・平滑化前の密度場)。**進捗とは別の口**。

        進捗に混ぜないのは、こちらが数十万要素の配列を積んだ大きなメッセージで、
        画面のパネル(いま何反復目か)とは寿命も読み手も違うため。**頼んだときしか流れない**
        (`run_optimization` の `debug` に種類を並べる)。中身の形は
        `optimize_run._DebugSink` を見ること。
        """
        self._emit_optimize_event({"type": "optimize_debug", **event})

    def _emit_optimize_event(self, payload: dict) -> None:
        """最適化の進捗・可視化を、イベントループへ乗せてから配るか決める(**どのスレッドからでも呼べる**)。

        配るか否かは**イベントループのスレッドで**決める —— 今どの文書を開いているかを書き換えるのは
        そちらのスレッドなので、ワーカースレッドで判定すると切り替えと入れ違う。
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

        走らせている文書へ戻ってきたら「実行中」と最後の進捗を、離れたら「走っていない」を配る
        (画面の実行中表示・中断ボタン・編集の封鎖はこれで切り替わる)。別の文書を開いている間に
        終わった実行の結果/失敗は、その文書を開いたときに1度だけ配る。
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
        """中断の旗を立てる。**実際に降りるのは次に進捗が出た時点**(進捗を出さない短い処理の途中では止まらない)。

        走っていないときに呼ばれても旗は立てない —— `Session.cancel_optimization` の
        旗を畳むのは次の開始(`Session.begin_optimization`)なので実害は無いが、走っていないのに
        「中断中」と画面へ出すと、次の実行が始まった瞬間に中断表示から始まって見える。
        """
        if not self.session.optimization_visible:
            await _send(connection, {"type": "optimize_status", **self._optimize_status()})
            return
        self.session.cancel_optimization()
        await self._broadcast_optimize_status()

    def _emit_event(self, payload: dict) -> None:
        """状態配信ではないイベント(AI・形状最適化)を全接続へ配る配信を、
        イベントループへ確実に乗せる。

        **どのスレッドからでも呼べる**(`_ai_progress` も `_optimize_progress` も
        実行器スレッドから来る。`call_soon_threadsafe` は呼び出し元がイベントループの
        スレッドでも安全)。
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

        `state`(軽量・geometryなし)は全接続共通で1回だけ作る。`delta`(geometry付き、
        版が変わったBodyだけ)は**接続ごとに知っている版が違いうる**ので、接続ごとに計算する
        (モジュールdocstring参照。ここが差分送信の出口)。

        **接続ごとの「delta計算 → 送信 → 版の記録」は `_send_locks` で直列化する**。
        2つの操作(2クライアントがほぼ同時に操作した場合や、
        1クライアントが応答を待たず連続でコマンドを送った場合)がそれぞれ別の `_push` を
        起こすと、ロック無しでは「新しい版の送信が先に終わり、後から古い版が上書きで届く」
        「送信前に版を記録してしまい、実際には届いていない版を`_known_revisions`が
        知っている扱いになる」の2通りの事故が起きる。ロックの範囲を1接続ぶんに絞って
        あるので、**別々の接続への配信は並行のまま**(1接続の遅延が他接続を止めない)。

        **`state` はロックの中で作る**。関数の先頭で1回だけ作って全ターゲットへ使い回すと、
        後発の `_push` が先にロックを取って新しい `state` を送った後、ロック待ちだった
        先発の `_push` が**古い** `state` で上書きする経路が生まれる(`_send_locks` は
        「同じ接続への配信」を直列化するだけで、`_push` 呼び出しをまたいだ新旧の順序は
        保証しない)。送る直前に読み直せば、送る値は常にその瞬間の状態であり、
        古い状態が後から上書きすることが原理的に起きない。`delta` と同じロックの中で作るので
        `state` と `delta` の食い違いも同時に消える。
        代償は接続数ぶん `state_meta()` を計算すること(27部品で数KB・ローカル1ユーザー)。
        """
        targets = [only] if only is not None else list(self.clients)

        async def deliver(target: Any) -> None:
            lock = self._send_locks.setdefault(target, asyncio.Lock())
            async with lock:
                # **送る直前に**状態を作る(上記docstring: 古い状態が後から上書きするのを防ぐ)
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

        # 警告は delta() が呼ぶ geometry() の中で積まれる(deliver() の内側)。
        # 全ターゲットぶんの配信(delta計算を含む)を終えてから取り出せば、
        # どのターゲットの分も取りこぼさない(1回のpushで複数回積まれても束ねて1回出す)。
        await asyncio.gather(*(deliver(target) for target in targets))
        # 警告の宛先は操作を出したクライアント。**MCPからの変更には出し手がいない**
        # (`origin is None`)ので、その場合は配信先全員へ出す — 出し手がいないことを理由に
        # 警告を捨てると、AIが操作したときだけ「三角形分割が取れなかった」等が消える。
        for warning in self.session.take_warnings():
            payload = {"type": "warning", "message": warning}
            for target in [origin] if origin is not None else targets:
                try:
                    await _send(target, payload)
                except Exception:
                    self.clients.discard(target)
