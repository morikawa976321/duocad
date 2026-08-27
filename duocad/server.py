"""WebSocketサーバ + `web/dist` の静的配信(「コマンドは下り、イベントは上り」)。

アプリ層(ブラウザ)とコアの唯一の接点。**コアはこのサーバが無くても機能的に完全**であり、
ここには「文書を持つ」以外の機能を置かない。

**このモジュールは起動コマンドを持たない。** 提供するのは `Session` / `Server` / 静的配信で、
**これを起動するのは `duocad.mcp_server` だけ**(起動経路が2つあると「AIが変えた文書」と
「人が見ている文書」が別物になりうる):

    .venv\\Scripts\\python.exe -m duocad.mcp_server [--open] [--port 8765] [--http-port 8766]

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
        {"type": "file", "action": "save"|"export_step"|"export_stl"|"list", "path", "bytes"}
        {"type": "error", "message": str, "where": str}
        {"type": "warning", "message": str}
        {"type": "ai_status", "provider", "model", "base_url", "running", "providers"}
        {"type": "ai_tool_call", "name": str, "arguments": {...}, "result": {...}, "ok": bool}
        {"type": "ai_result", "status", "text", "detail", "turns", "tool_calls": [...], "usage": {...}}
        {"type": "ai_error", "message": str}

**`ai_*` はverbではない**。`ai_get_status` / `ai_configure` / `ai_send` / `ai_cancel` は文書を
直接は変えない(操作ログに載らず、undoの単位でもない) —— 文書を変えるのはエージェントが呼ぶ
既存ツールの側で、それは通常のverb配信としてこの後に届く。したがって `catalog.COMMANDS` には
**入れない**し、対応するMCPツールも作らない(AIがAIを起動する口は要らない)。

**verbコマンドは対象集合(面・辺・領域)を引数で受けない**。数値・名前・パスだけを送り、押す面も
丸める辺も押し出す領域も `Session` のverbラッパがコアの確定選択から取る
(`selection.require_confirmed` / `require_edges`)。`body_id` が残っているのは対象集合ではない
ものだけ(`set_visible` / `get_edges` の対象、`extrude` の行き先)。

選択の口は3つ(`modify_selection` / `commit_selection` / `clear_selection`)。`propose_selection`
は開けていない(AIが書く経路であり、人はポインティングで直す)。

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
      "selection": {"items": [...], "origin": "user"|"proposed", "count": int},
    }
    delta(known_revisions) -> {"bodies": [<上と同じ形・版が変わった分だけ>],
                               "unchanged": [BodyId, ...]}

**選択が `state` に載っていないとAIが書き換えた選択はブラウザへ届かない**(`_on_core_change` は
MCP起点の変更から `applied` を落として配るため。`set_visible` の効果が `bodies[].visible` で
届いているのと同じ場所)。選択の変更イベントは
`{"change": "selection", "selection": {...}, "client_op": ...}` の形で、verbの `applied` とは
キーで区別する(選択はverbではなく、操作ログにも載らない)。**可視性も同じ `change` 系**
(`{"change": "visibility", ...}`)—— こちらもverbではない(2026-08-26)。

**OCPを直接importしない**(境界はkernel.pyだけ)。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional  # Any: Session verb wrappers / listeners

from . import catalog, io_step, kernel, parameters, query, region, selection, sketch
from .agent import AgentLoop, Cancellation, ProviderError, build_provider
from .agent.providers import DEFAULT_OPENAI_COMPAT_BASE_URL
from .document import Document
from .oplog import OpLog
from .speculative import speculative
from .verbs import (
    chamfer as chamfer_verb,
    extrude as extrude_verb,
    extrude_face as extrude_face_verb,
    fillet as fillet_verb,
    import_step as import_step_verb,
    import_svg as import_svg_verb,
    pattern_circular as pattern_circular_verb,
    pattern_rectangular as pattern_rectangular_verb,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WEB_DIST = REPO_ROOT / "web" / "dist"
#: 保存/開くの既定の置き場。**リポジトリ直下・gitignore対象** —— 毎回パスを手打ちする
#: 代わりに、決まったフォルダの中身を一覧から選ぶ形にする。無ければ
#: `Session._resolve_document_name` が作る。テストは `Session(models_dir=tmp_path)` で
#: ここを差し替えて実フォルダを触らない。
DEFAULT_MODELS_DIR = REPO_ROOT / "models"

#: 状態配信で本文を伏せるログ引数。値の代わりに文字数だけを配る。
#:
#: `import_step` は読み込んだSTEPの**本文**を `args["step"]` に持ち、
#: `import_svg` は翻訳した**図形の全部**を `args["entities"]` に持つ(ロゴ1枚で数百件)。
#: PCケースのSTEPは4.6MBあり、`state_meta()` はWebSocketで**操作のたびに全部配られる**ので、
#: 素通しにすると1操作ごとに数MBが流れる(「27部品でも数KB」という前提が壊れる)。
#: 履歴パネルが使うのは `source_name` と件数だけなので、本文は配らない。
#: **ログそのもの(`document.log`)は無傷** —— 伏せるのは配信用の射影だけで、
#: 保存(`to_json`)は本文を書く。
_REDACTED_LOG_ARGS = ("step", "entities")


def _log_args_for_state(args: dict) -> dict:
    """状態配信用のargs。埋め込んだ本文・図形を「量」に置き換える(上記 `_REDACTED_LOG_ARGS`)。"""
    if not any(key in args for key in _REDACTED_LOG_ARGS):
        return args  # 大半の操作はここ。写しを作らない
    projected = dict(args)
    for key in _REDACTED_LOG_ARGS:
        value = projected.get(key)
        if isinstance(value, str):
            projected[key] = f"<{len(value)}文字は配信しない>"
        elif isinstance(value, list):
            projected[key] = f"<{len(value)}件は配信しない>"
    return projected


#: Windowsの予約デバイス名(拡張子の有無に関わらず特別扱いされる)。
#: 大文字小文字を区別しない。`_resolve_document_name` が拒否理由をまとめて言う。
_RESERVED_WINDOWS_NAMES = frozenset({
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
})

# サーバは常に空の文書で立ち上がり、STEPはアプリ内の「STEP読み込み」かMCPの `import_step`
# から読む(読み込みのverbは1本だけ)。読み込むSTEPのパスを既定として持つ場所は無いので、smokeスクリプトが使う
# PCケースのパスは `web/scripts/models.mjs` が持つ。

# --------------------------------------------------------------------------------------
# ソケットから踏める口(**すべて `duocad/catalog.py` の宣言からの導出**)
# --------------------------------------------------------------------------------------
# **ここに手で表を書かない。** verbのメタ情報(名前・引数・カテゴリ・プレビュー可否・
# MCPのスキーマ)は `catalog.COMMANDS` が唯一の宣言で、verbを1つ足すときに触るのは
# verb本体と宣言1箇所だけである。
#
# 導出しても**登録制であることは変わらない**: ここに現れない名前はソケットから呼べない
# (届いた文字列で `Session` の任意のメソッドを呼べる作りにしない)。表に載る引数は
# **1つでも欠けたら KeyError にする** —— 既定値で補うと、打ち忘れた寸法や `mode` が
# 黙って既定で入る。**並ぶのは数値・名前・パスだけで、面・辺・領域は1つも無い**。

#: プレビューできるverb -> (`Session` のメソッド名, 受け取ってよい引数名)。
#: `Session.preview` の唯一の入口で、**ここに無いverbはプレビューできない**
#: (載せてよいverbの条件は `catalog.Command.previewable`)。
PREVIEWABLE_VERBS: dict = catalog.previewable_verbs()

#: ブラウザから踏めるスケッチのコマンド -> (`Session` のメソッド名, 必須の引数名)。
SKETCH_COMMANDS: dict = catalog.ws_table("sketch")

#: ブラウザから踏めるフィーチャのコマンド -> (`Session` のメソッド名, 必須の引数名)。
FEATURE_COMMANDS: dict = catalog.ws_table("feature")

#: ブラウザから踏めるファイル操作 -> (`Session` のメソッド名, 必須の引数名, 文書を変えるか)。
#: **文書を変えるかどうかで応答が違う**のがこの表だけの事情:
#:
#: - `import_step` … 文書を変える(ログに載る)。応答は状態配信(`import_svg` はスケッチの
#:   コマンドへ移り、2026-08-28からこの表に無い —— `SKETCH_COMMANDS` 側にいる)
#: - `save_document` / `export_step` / `export_stl` … 文書を変えない。応答は書いた先の
#:   パスとバイト数だけ(要求した接続にだけ返る。`preview` と同じ扱い)
#: - `open_document` はどちらでもない —— 文書を**差し替える**ので全接続へ配る必要があり、
#:   表からは外して `_dispatch` で個別に扱う。
FILE_COMMANDS: dict = catalog.file_table()

#: **対象集合をコアの確定選択から取るコマンド**(`catalog.Command.uses_selection`)。
#: `_dispatch` はこれを受け取ったとき、選択が `origin="proposed"` のままなら
#: **自動で `commit_selection` してから**verbを走らせる(下記 `_commit_before_verb`)。
SELECTION_CONSUMING_COMMANDS: set = catalog.selection_consuming_commands()

#: アプリ内AIが繋げるプロバイダ(`ai_configure` が受け付ける綴り・`ai_status.providers`)。
#: **2026-08-27に `anthropic` を落として1つになった** —— ClaudeをAPIキーで叩く経路の廃止。
#: Claudeへ繋ぐ道はMCP(サブスク認証のClaude Codeが `/mcp` へ来る)側に残っている。
#: **列のまま残すのは次のプロバイダの差込口だから**で、画面はこの長さで選択UIの要否を決める。
AI_PROVIDERS: tuple = ("openai_compat",)


class ToolsOnLoop:
    """`ToolSurface`(`descriptors`/`descriptor`/`call`/`error_envelope`/`current_seq`)を
    **イベントループのスレッドへ限定する**覆い。

    `Session` は「イベントループのスレッドだけが触る」という前提の上に立っているが、
    そこへ**別のスレッドから入ってくる経路が2本ある**:

    - **アプリ内AI** —— `AgentLoop.run()` はモデルへのHTTPリクエストを含み数十秒かかりうるので、
      `Server._ai_start` が `run_in_executor` で実行器スレッドへ逃がす
    - **HTTPトランスポートのMCP** —— `/mcp` は `ThreadingHTTPServer` のワーカースレッドで
      走る(`WebHandler.do_POST`)

    どちらも直接 `DocumentTools` を叩くと2スレッドが同時に文書を書き換えうる。ここでは
    `.call()` / `.error_envelope()` / `.current_seq()` を `asyncio.run_coroutine_threadsafe`
    でイベントループへ1回ずつ投げ、その場で完了を待つ。往復するのは `Session` のverb呼び出し
    1回ぶん(通常は数ms)で、**プロバイダへのHTTPリクエスト(本当に長くかかる方)はこの覆いを
    経由しない**。
    """

    def __init__(self, tools: Any, loop: asyncio.AbstractEventLoop) -> None:
        self._tools = tools
        self._loop = loop

    def descriptors(self) -> list:
        # 起動時に1度組んだ静的なリストを読むだけ(`DocumentTools._build_descriptors`)。
        # 文書を触らないので、スレッドをまたいでも安全 —— 投げ返さない。
        return self._tools.descriptors()

    def descriptor(self, name: str) -> Any:
        # `descriptors()` と同じ静的な索引の引き当て(MCPの `tools/call` が引数検査に使う)。
        # 文書を触らないので投げ返さない。
        return self._tools.descriptor(name)

    def call(self, name: str, arguments: dict) -> dict:
        return self._run_on_loop(self._tools.call, name, arguments)

    def error_envelope(self, tool: str, error: Exception) -> dict:
        return self._run_on_loop(self._tools.error_envelope, tool, error)

    def current_seq(self) -> Any:
        # 読み取りだけでも `self.session.document` を触るので、`call`/`error_envelope` と
        # 同じくイベントループのスレッドへ投げ返す(このクラスの不変条件)。
        return self._run_on_loop(self._tools.current_seq)

    def _run_on_loop(self, fn: Any, *args: Any) -> Any:
        # **既にイベントループのスレッド上にいるなら、直接呼ぶ**。`run()` が実行器を挟まずに
        # 呼ばれる経路(`tests/test_agent_ws.py` が `asyncio.run(scenario())` の中から直に
        # 呼ぶ検査)では、`run_coroutine_threadsafe(...).result()` は**自分自身が塞いでいる
        # ループの完了を自分自身で待つ**ことになり、必ず固まる(2026-08-20実測)。
        # 同じスレッドなら投げ返す意味が無いので、そのまま呼んで済ませる。
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            return fn(*args)

        async def _once() -> Any:
            return fn(*args)

        return asyncio.run_coroutine_threadsafe(_once(), self._loop).result()


class Session:
    """1つの文書を持つサーバ側の状態。WebSocketの接続とは独立(接続が切れても文書は残る)。"""

    def __init__(
        self,
        step_path: Optional[Path],
        deviation: float,
        models_dir: Optional[Path] = None,
    ) -> None:
        self.document = Document()
        self.deviation = float(deviation)
        #: 保存/開く/一覧の唯一の置き場(2026-08-22)。既定は `DEFAULT_MODELS_DIR`
        #: (リポジトリ直下の `models/`)。テストが実フォルダを汚さないよう差し替えられる。
        self.models_dir: Path = (
            DEFAULT_MODELS_DIR if models_dir is None else Path(models_dir)
        ).resolve()
        self.warnings: list[str] = []
        self._applied_count = 0
        self._revision_state: dict = {}    # body_id -> (id(shape), visible) 直近の状態
        self._revision_counter: dict = {}  # body_id -> 現在の版(int)
        # sketch_id -> (署名, 領域つきペイロード)。領域の計算が重いので内容で引く(C)
        self._sketch_cache: dict = {}
        self._geometry_cache: dict = {}    # body_id -> (shape, tessellate結果)
        self._edge_cache: dict = {}        # body_id -> (shape, tessellate_edges結果)(D)
        self._vertex_cache: dict = {}      # body_id -> (shape, vertex_positions結果)
        self._volume_cache: dict = {}      # body_id -> (shape, query.volume結果)
        self._setup_floor_seq = 0  # load_step が積んだ最後のseq(これ以下はundoの対象外)
        self._listeners: list = []  # 文書が変わったときに呼ぶ購読者(下記 add_listener)
        self._suppress_events = 0  # >0 の間は `_emit` が配らない(プレビュー。`preview` 参照)
        if step_path is not None:
            self.load_step(step_path)

    # ---------------------------------------------------------------- 変更イベント(上り方向)
    def add_listener(self, callback: Any) -> None:
        """文書が変わったら `callback(applied: dict)` で知らせる。

        **コアが変更を発火し、アプリ層が購読して描き直す**。これが無いと、**WebSocket経由
        以外の主体**——MCPサーバからAIが文書を変えたとき——にブラウザが何も知らないまま
        古い絵を出し続け、交互操作が原理的に成立しない。購読者は `Server`(WebSocket)で、
        1つの操作につき1回だけ呼ばれる。
        """
        self._listeners.append(callback)

    def remove_listener(self, callback: Any) -> None:
        """購読をやめる。登録されていなければ ValueError(黙って空振りさせない)。

        文書は接続より長生きするので、`Server` を作り直す運用(テスト・再起動)で
        古い購読者が残ると、既に閉じたループへ配信しようとして警告が溜まる。
        """
        self._listeners.remove(callback)

    @property
    def can_undo(self) -> bool:
        """次のundoが通るか。**土台(部品の読み込み・編集用ボディ)は取り消せない**。

        `state_meta()` と MCP の `describe_document` が同じ答えを返すための唯一の出どころ
        (MCP側が `document.log.can_undo` を素で見ると、土台の直後に「undoできる」と
        答えてしまい、実際に呼ぶと拒否される食い違いが起きる)。
        """
        return self.document.log.can_undo and not self._at_setup_floor()

    def _emit(self, applied: dict) -> dict:
        """購読者へ変更を知らせて `applied` をそのまま返す(呼び出し側は `return self._emit(...)`)。

        **1人の購読者が落ちても他へ配り、操作自体は成功のままにする** — 通知の失敗で
        既に確定した文書の変更を巻き戻すことはできないため。落ちたことは警告として残す。

        **プレビュー中(`_suppress_events`)は誰にも配らない**。投機的に実行したverbも `_emit` を
        通るので、ここで止めないと「確定していない形」が全クライアントへ配信され、直後の
        巻き戻しで元へ戻る —— 画面がちらつくだけでなく、MCP側が読んだ状態が実在しない中間状態に
        なる。**呼び出し経路(WebSocket / MCP / 直呼び)に関わらず止まる**ようにここへ置いてある
        (`Server._dispatching` の二重配信よけはWebSocket経由にしか効かない)。
        """
        if self._suppress_events:
            return applied
        for callback in list(self._listeners):
            try:
                callback(applied)
            except Exception as error:
                self.warnings.append(f"変更イベントの配信に失敗: {type(error).__name__}: {error}")
        return applied

    # ---------------------------------------------------------------- 文書の組み立て
    def load_step(self, path: Path) -> list:
        """STEPを読み、ソリッドごとに参照ボディ+編集用コピーを作る(Python APIとして残す)。

        中身は verb `import_step` そのもの。

        **土台(undoの対象外にする範囲)はverb名ではなくseqの下限で区切る**(`_setup_floor_seq`)。
        verb名で判定すると、後からユーザー操作としても呼ばれうるverbが末尾に来た瞬間に
        以後の `undo` が誤って拒否される。

        通常の起動でここは通らない(pytestが直に呼ぶときだけ通る)。
        """
        editable_ids = import_step_verb(self.document, str(path))
        entries = self.document.log.entries
        self._setup_floor_seq = entries[-1].seq if entries else 0
        return editable_ids

    def _at_setup_floor(self) -> bool:
        """次のundoが文書の土台(部品の読み込み・編集用ボディ)に当たるか。"""
        entries = self.document.log.entries
        return bool(entries) and entries[-1].seq <= self._setup_floor_seq

    # ---------------------------------------------------------------- 状態
    def state(self) -> dict:
        """アプリ層へ渡す全状態。bodiesは配列(部品ごと)。**geometryを含む(重い)**。

        WebSocket配信(`Server._push`)はこれを毎回は使わない — `state_meta()` + `delta()`
        を組み合わせて、版が変わったBodyの `geometry` だけを送る(モジュールdocstring参照)。
        `state()` はテストと `get_state` の全量確認用に残す。
        """
        meta = self.state_meta()
        meta["bodies"] = [self.body_state(body) for body in self.document.bodies]
        return meta

    def state_meta(self) -> dict:
        """`state()` の軽量版。**bodiesの各要素にgeometryを含まない**(テセレーションを呼ばない)。

        パーツ一覧(名前・面数・可視性)のように形状そのものが要らない用途、および
        WebSocket配信のたびに送る「土台」の情報に使う。27部品でも数KB(state()は6MB超)。
        """
        active = self.document.active_body()
        return {
            "units": self.document.units,
            "active_body_id": active.id if active is not None else None,
            "bodies": [self.body_meta(body) for body in self.document.bodies],
            "log": [
                {
                    "seq": operation.seq,
                    "verb": operation.verb,
                    "args": _log_args_for_state(operation.args),
                }
                for operation in self.document.log.entries
            ],
            "can_undo": self.can_undo,
            "can_redo": self.document.log.can_redo,
            # **どこまで戻れるか**を画面に出すために位置を配る。
            # 真偽だけだと「3回undoした状態」と「1回undoした状態」が同じに見える。
            "redo_count": self.document.log.redo_count,
            "applied_count": self._applied_count,
            # 名前つきの寸法値(§3.4)。**打ち直しの対象そのもの**なので、どの名前が
            # 今いくつかを配らないと、パラメータ編集パネルは何も出せない。
            "parameters": dict(self.document.parameters),
            "selection": selection.get_selection(self.document).to_dict(),
            # スケッチは平面の枠・図形・領域まで含めて配る(下記)。
            "sketches": self.sketches_state(),
        }

    # ---------------------------------------------------------------- C: スケッチの配信
    def sketches_state(self) -> list:
        """全スケッチを**アプリ層が描ける形**で返す。

        1件ぶん:
            {"sketch_id", "plane": <PlaneRef>,
             "frame": {"origin","u","v","normal"},          # (u,v) と3Dの往復の規則
             "entities": [{"entity_id","kind","params","refs"}],   # 打ち直し用(名前つき)
             "patterns": [{"pattern_id","kind","sources","params","refs","options"}],  # 反復の打ち直し用
             "drawn":    [{"kind","params"}],                      # 反復を展開した描画用
             "regions":  [{"index","area","centroid","positions","indices"}]}

        **`entities` と `drawn` を分けている理由**: 前者は人が値を打ち直す対象(`refs` に
        パラメータ名が入る)で、後者は反復を展開した「実際に見えるもの」。反復した図形が
        画面に出ないのに領域だけ増える、を避けるため描画は必ず `drawn` を使う。**`patterns`**は
        `entities` と対になる打ち直し用の口(反復の間隔・個数)で、`SketchPanel.svelte` が図形の
        行に続けて出す(2026-08-27、`ParametersPanel.svelte` 廃止に伴い統合)。

        **領域の計算は重い**(`BRepAlgoAPI_Splitter`+テセレーション)。`state` は変更のたびに
        全接続へ配るので、**スケッチの内容の署名でキャッシュする**。署名には解決済みの数値が
        入るので、`set_parameter` で値が変われば作り直される。
        """
        # undoで消えたスケッチのキャッシュを落とす(セッション寿命で溜め込まない)
        alive = {item.id for item in self.document.sketches}
        for sketch_id in [key for key in self._sketch_cache if key not in alive]:
            self._sketch_cache.pop(sketch_id, None)
        return [self._sketch_state(item) for item in self.document.sketches]

    def _sketch_state(self, item) -> dict:
        try:
            drawn = region.expanded_entities(self.document, item.id)
        except Exception as error:  # 壊れたスケッチで画面全体を落とさない
            self.warnings.append(f"スケッチ {item.id} を描けない: {type(error).__name__}: {error}")
            drawn = []
        plane_shape = None
        if item.plane.get("kind") == "face":
            body = next(
                (b for b in self.document.bodies if b.id == item.plane.get("body")), None
            )
            plane_shape = body.shape if body is not None else None
        signature = json.dumps([item.plane, drawn, self.deviation], sort_keys=True, default=str)
        cached = self._sketch_cache.get(item.id)
        # 貼った面の形状は**参照そのもの**で比べる(`is`)。`id()` の整数だけを控えると、
        # 旧形状が解放された後に同じアドレスが再利用されたときに偽ヒットする
        # (`geometry()` のキャッシュが形状の同一性で引いているのと同じ判断)。
        if cached is not None and cached[0] == signature and cached[1] is plane_shape:
            payload = dict(cached[2])
        else:
            frame: Optional[dict] = None
            regions: list = []
            try:
                frame = region.plane_frame(self.document, item.id)
                if drawn:
                    regions = region.regions_with_geometry(
                        self.document, item.id, self.deviation
                    )
            except Exception as error:
                self.warnings.append(
                    f"スケッチ {item.id} の領域を計算できない: {type(error).__name__}: {error}"
                )
            payload = {"frame": frame, "drawn": drawn, "regions": regions}
            self._sketch_cache[item.id] = (signature, plane_shape, payload)
            payload = dict(payload)
        payload["sketch_id"] = item.id
        payload["plane"] = item.plane
        # 図形そのものは軽いのでキャッシュしない(打ち直しの反映が1手遅れるのを避ける)。
        # **ここも try で包む** —— 上の `drawn` だけを守っても、同じパラメータ解決を
        # するこちらが素通りすると `state_meta()` ごと落ち、以後の配信が全部止まる。
        try:
            payload["entities"] = [
                {
                    "entity_id": entity.id,
                    "kind": entity.kind,
                    "params": sketch.entity_params(self.document, item.id, entity.id),
                    # 値が名前つきパラメータなら**その名前**(`set_parameter` で打ち直せる)。
                    # 素の数値のままなら None(名前つきパラメータへの参照が無い図形)。
                    "refs": {
                        key: ref if isinstance(ref, str) else None
                        for key, ref in entity.params.items()
                    },
                }
                for entity in item.entities
            ]
        except Exception as error:
            self.warnings.append(
                f"スケッチ {item.id} の図形を読めない: {type(error).__name__}: {error}"
            )
            payload["entities"] = []
        # 反復も同じ理由でキャッシュしない・try で包む(上の `entities` と同じ規律)。
        # SketchPanelが反復の行を出すために要る(`pattern_id`/`kind`/`sources`/`params`/
        # `refs`/`options`。`entities` と同じ形にしておくと、フロント側の打ち直しの
        # コンポーネント(`commitParam`)をそのまま使い回せる)。
        try:
            payload["patterns"] = [
                {
                    "pattern_id": pattern.id,
                    "kind": pattern.kind,
                    "sources": list(pattern.sources),
                    "params": {
                        key: parameters.resolve(self.document, ref)
                        for key, ref in pattern.params.items()
                    },
                    "refs": {
                        key: ref if isinstance(ref, str) else None
                        for key, ref in pattern.params.items()
                    },
                    "options": dict(pattern.options),
                }
                for pattern in item.patterns
            ]
        except Exception as error:
            self.warnings.append(
                f"スケッチ {item.id} の反復を読めない: {type(error).__name__}: {error}"
            )
            payload["patterns"] = []
        return payload

    # ---------------------------------------------------------------- M3.5: 差分送信
    def body_revision(self, body_id: str) -> int:
        """そのBodyの版。**版は「経路」ではなく「状態」から導く** — 形状の同一性(id)とvisible
        のタプルが前回の記録と違ったら+1する。押し出し・undo・redo・可視性変更のどれでも
        自動的に効く(verbごとに数え上げない)。
        """
        body = self.document.get_body(body_id)
        key = (id(body.shape), body.visible)
        if self._revision_state.get(body_id) != key:
            self._revision_state[body_id] = key
            self._revision_counter[body_id] = self._revision_counter.get(body_id, 0) + 1
        return self._revision_counter[body_id]

    def geometry(self, body_id: str) -> dict:
        """1Bodyのテセレーション結果(キャッシュ付き)。不可視Bodyは ValueError。

        キャッシュは**形状の同一性**で判定する(版ではない)。可視性だけを変えたときに
        測り直すと、表示を切り替えるたびに高コストな再測定が走ってしまう。
        """
        body = self.document.get_body(body_id)
        if not body.visible:
            raise ValueError(f"body {body_id!r} is not visible: 不可視Bodyは測らない")
        cached = self._geometry_cache.get(body_id)
        if cached is not None and cached[0] is body.shape:
            return cached[1]
        result = kernel.tessellate(body.shape, self.deviation)
        if result["untriangulated"]:
            self.warnings.append(
                f"三角形分割が取れなかった面({body.name}): {result['untriangulated']}(画面に出ません)"
            )
        self._geometry_cache[body_id] = (body.shape, result)
        return result

    def edges(self, body_id: str) -> dict:
        """1Bodyの辺を折れ線で返す(キャッシュ付き)。

        戻り値: `{"body_id", "revision", "edges": [kernel.tessellate_edges の1件, ...]}`

        **`state` にも `delta` にも入れない**。辺が要るのはフィレット・面取りを起動している
        間だけで、常時配ると27部品(2835面)の初回配信にその数倍の折れ線が乗る ——
        差分配信にして稼いだぶんを、使わない辺の転送で丸ごと使い切ることになる。

        `revision` を一緒に返すのは、**クライアントが「今の形の辺か」を判定できるように**
        するため。押し出しで形が変われば版が上がるので、古い辺を指したままフィレットをかける
        (＝別の辺が丸まる)経路が閉じる。
        """
        body = self.document.get_body(body_id)
        if body.shape is None:
            raise ValueError(f"body {body_id!r} has no shape (辺を数えられない)")
        # **不可視Bodyの辺は配らない**(`geometry()` と同じ規律)。
        # 配ると、隠したはずの部品の辺だけが宙に浮いて見え、しかも指せる ——
        # 見えているものと指せるものを一致させる、という辺のピッキングの前提が崩れる。
        if not body.visible:
            raise ValueError(f"body {body_id!r} is not visible: 隠れているものの辺は指せない")
        revision = self.body_revision(body_id)
        cached = self._edge_cache.get(body_id)
        if cached is not None and cached[0] is body.shape:
            described = cached[1]
        else:
            described = kernel.tessellate_edges(body.shape, self.deviation)
            self._edge_cache[body_id] = (body.shape, described)
        return {"body_id": body_id, "revision": revision, "edges": described}

    def vertices(self, body_id: str) -> dict:
        """1Bodyの頂点を1始まりの添字つきの3D座標で返す(キャッシュ付き)。

        戻り値: `{"body_id", "revision", "vertices": [{"index", "point": [x,y,z]}, ...]}`

        **`edges()` と同じく配信しない**(`state` にも `delta` にも入れない)。違うのは
        画面側の使い方で、辺は「要求しているコマンドを起動している間だけ全部出す」のに対し、
        頂点は**選択に入っているものだけ**を出す —— 頂点を引数に取るverbが無いので候補を
        全部見せる意味が無い。ここが全頂点を返すのは、どれが選ばれるかを事前に知らない
        からで、画面に出るのは選択に入った添字だけである。

        `revision` を返す理由も辺と同じ —— 形が変われば版が上がるので、古い座標を
        描き続ける経路が閉じる。
        """
        body = self.document.get_body(body_id)
        if body.shape is None:
            raise ValueError(f"body {body_id!r} has no shape (頂点を数えられない)")
        # **不可視Bodyの頂点は配らない**(`geometry()` / `edges()` と同じ規律)。
        if not body.visible:
            raise ValueError(f"body {body_id!r} is not visible: 隠れているものの頂点は指せない")
        revision = self.body_revision(body_id)
        cached = self._vertex_cache.get(body_id)
        if cached is not None and cached[0] is body.shape:
            described = cached[1]
        else:
            described = query.describe_vertices(body.shape)
            self._vertex_cache[body_id] = (body.shape, described)
        return {"body_id": body_id, "revision": revision, "vertices": described}

    def delta(self, known_revisions: dict) -> dict:
        """クライアントが持っている版との差分だけを返す(未知のIDは必ずbodiesへ)。

        戻り値:
            {
              "bodies":    [<state()の bodies と同じ形。ただし版が変わったBodyだけ>],
              "unchanged": [BodyId, ...],   # 版が一致したBody
            }

        `known_revisions` に無いBodyId(＝クライアントが知らないBody)は必ず `bodies` に入る。
        逆に、クライアントが知っているのに `bodies` にも `unchanged` にも現れないIDは
        **文書から消えた**ことを意味する(undoでの消滅がこれ。専用の "removed" は持たない)。
        """
        bodies_out = []
        unchanged = []
        for body in self.document.bodies:
            current = self.body_revision(body.id)
            if known_revisions.get(body.id) == current:
                unchanged.append(body.id)
            else:
                bodies_out.append(self.body_state(body))
        return {"bodies": bodies_out, "unchanged": unchanged}

    def body_meta(self, body) -> dict:
        """1Bodyぶんの軽量な状態(geometryを含まない。state_metaとの共有部分)。

        **公開メソッド**: `DocumentTools._body_summary`(mcp_server.py)がMCP封筒の `bodies[]`
        要素を作るのにもこれを使う。MCP側は封筒の契約に合わせて `revision` キーを落とす。

        `state_meta()` を通じて**接続のたびに全Bodyぶん**呼ばれる(`_push`)。`volume` は
        キャッシュ付き(下記 `_body_volume`)——`face_count` は合計2msと軽いので毎回測る。
        """
        return {
            "body_id": body.id,
            "name": body.name,
            "kind": body.kind,
            "visible": body.visible,
            "revision": self.body_revision(body.id),
            "face_count": kernel.face_count(body.shape),
            "volume": self._body_volume(body),
        }

    def _body_volume(self, body) -> float:
        """bodyの体積(キャッシュ付き)。

        `geometry()` と同じ規律 —— キャッシュは**形状の同一性**(`is`)で判定する(版ではない)。
        `id()` の整数だけを控えると、旧shapeが解放された後に同じアドレスが再利用されて
        偽ヒットしうるので、shapeそのものを保持して比べる(`geometry()` のキャッシュと同じ代償)。
        `query.volume` は面ごとの数値積分でここにだけキャッシュが無く、可視性を切り替えた
        だけでも全ボディぶん測り直していた(PCケースで693ms/回)。
        """
        cached = self._volume_cache.get(body.id)
        if cached is not None and cached[0] is body.shape:
            return cached[1]
        result = query.volume(body.shape)
        self._volume_cache[body.id] = (body.shape, result)
        return result

    def body_state(self, body) -> dict:
        """1Bodyぶんの状態(stateとdeltaが共有する)。`body_meta` にgeometryを足したもの。"""
        meta = self.body_meta(body)
        meta["geometry"] = self.geometry(body.id) if body.visible else None
        return meta

    # ---------------------------------------------------------------- 選択の全リセット(2026-08-20)
    def _reset_selection_if_needed(self, verb: str) -> None:
        """形を変えるverbの確定後、選択を全リセットする(統一契約)。

        **カタログの `resets_selection` が正**(`duocad/catalog.py`)。面・辺・領域の添字は
        形状変更で壊れるので、後継を当てにいくより**全部空にする**ほうが単純で正しい。

        呼び出し側は**成功した後にだけ**呼ぶこと — 失敗した verb は操作ログに積まれず
        この関数にも到達しないので、「失敗しても選択は保持される」は自然に満たされる。
        """
        command = catalog.BY_NAME.get(verb)
        if command is not None and command.resets_selection:
            selection.clear_selection(self.document)

    # ---------------------------------------------------------------- 操作
    def extrude_face(self, distance: float, client_op: Optional[int] = None) -> dict:
        """**確定選択の面**(ちょうど1枚)を押し出す。`applied` を返す。

        押す面とそのBodyは引数ではなく選択から取る(`selection.require_confirmed`)——
        どのBodyの面かは参照が持っているので、27部品のうち1つを選ぶのに引数は要らない。
        `applied` は `"changed_bodies": [BodyId, ...]` を持つ(差分送信の対象)。
        """
        face = selection.require_confirmed(self.document, "face", exactly_one=True)[0]
        face_index = int(face["index"])
        operation = extrude_face_verb(
            self.document, face_index, float(distance), body_id=face["body"]
        )
        self._applied_count += 1
        changed_body_id = operation.args["body_id"]
        self._reset_selection_if_needed(operation.verb)
        return self._emit({
            "verb": operation.verb,
            "seq": operation.seq,
            "client_op": client_op,
            "body_id": changed_body_id,
            "face_index": int(face_index),
            "distance": float(distance),
            "changed_bodies": [changed_body_id],
        })

    def set_visible(self, body_id: str, visible: bool, client_op: Optional[int] = None) -> dict:
        """パーツ一覧からの表示・非表示切り替え。**コアの `selection.set_visible` を呼ぶだけ**
        (アプリ層に可視性の状態を持たせない。実装計画書§2)。

        **verbではない**ので操作ログには積まれず、undo / redo では戻らない
        (理由は `selection.set_visible`)。発火するのは選択と同じ `change` 系のイベントで、
        verbの `applied` とはキーで区別する:

            {"change": "visibility", "body_id": .., "visible": .., "previous": .., "changed": ..,
             "client_op": int | None}

        効果そのものは配信される状態の `bodies[].visible` に載る(`body_revision` が
        可視性を版に含めているので、`delta` でも必ず1つ上がる)。存在しない `body_id` は
        `KeyError`(コア側の契約どおり。documentは変更しない)。
        """
        changed = selection.set_visible(self.document, body_id, bool(visible))
        return self._emit({"change": "visibility", "client_op": client_op, **changed})

    # ---------------------------------------------------------------- A1: 共有される選択集合
    #
    # **`set_visible` と同じ上り方向に乗せる**(別系統を新設しない)。
    # 変更 → `Session._emit` → `add_listener` の購読者(`Server._on_core_change`) → 全接続へ配信。
    # **`state_meta()` に `"selection"` を載せない限りAIが書き換えた選択は画面へ届かない**
    # (MCP起点の変更では `applied` を落として配るため)。イベントと状態の両方を通す。
    #
    # 発火するイベントの形(verbの `applied` とはキーで区別する。選択はverbではない):
    #     {"change": "selection",
    #      "selection": {"items": [...], "origin": "user"|"proposed", "count": int},
    #      "client_op": int | None}
    #
    # **戻り値**: 変更する4つは `self._emit(...)` の戻り —— つまり**発火したイベントそのもの**
    # (`{"change","selection","client_op"}`)。`get_selection` だけは発火しないので
    # `Selection.to_dict()`(`{"items","origin","count"}`)を返す。**この2つは形が違う**ので、
    # 呼び出し側は `["selection"]` を挟むかどうかを取り違えないこと。
    def get_selection(self) -> dict:
        """現在の選択集合を素の値で返す(`Selection.to_dict()`)。発火はしない。"""
        return selection.get_selection(self.document).to_dict()

    def propose_selection(
        self,
        refs: Optional[list] = None,
        predicate: Optional[dict] = None,
        client_op: Optional[int] = None,
    ) -> dict:
        """AIの提案として選択を置き換え、変更イベントを発火して**そのイベント**を返す。"""
        selection.propose_selection(self.document, refs=refs, predicate=predicate)
        return self._emit(self._selection_event(client_op))

    def modify_selection(
        self,
        add: Optional[list] = None,
        remove: Optional[list] = None,
        client_op: Optional[int] = None,
    ) -> dict:
        """人の差分編集。足し引きして確定にし、変更イベントを発火して**そのイベント**を返す。"""
        selection.modify_selection(
            self.document,
            add=() if add is None else add,
            remove=() if remove is None else remove,
        )
        return self._emit(self._selection_event(client_op))

    def commit_selection(self, client_op: Optional[int] = None) -> dict:
        """提案を確定する(中身は変えない)。変更イベントを発火して**そのイベント**を返す。"""
        selection.commit_selection(self.document)
        return self._emit(self._selection_event(client_op))

    def clear_selection(self, client_op: Optional[int] = None) -> dict:
        """選択を空にする。変更イベントを発火して**そのイベント**を返す。"""
        selection.clear_selection(self.document)
        return self._emit(self._selection_event(client_op))

    def _selection_event(self, client_op: Optional[int]) -> dict:
        """選択変更イベントのペイロード(`set_visible` と同じく `_emit` へ渡す形)。"""
        return {
            "change": "selection",
            "selection": selection.get_selection(self.document).to_dict(),
            "client_op": client_op,
        }

    # ---------------------------------------------------------------- B: プレビュー(投機的実行)
    def preview(self, verb: str, args: Optional[dict] = None) -> dict:
        """verbを**投機的に実行して形だけ測り、文書を元へ戻す**。

        コマンド層の状態機械「起動 → 引数収集 → プレビュー → 確定(Enter) / 中断(Esc)」の
        プレビューの実体。**操作ログには何も積まれず、イベントも発火しない**。巻き戻しは
        `duocad.speculative.speculative` が行う。

        戻り値:
            {"verb": str,
             "bodies": [{"body_id","name","face_count","volume","geometry"}, ...]}

        `bodies` に入るのは**形が変わった可視のBodyだけ**。`geometry` は `state`/`delta` の
        同名フィールドと同じ形なので、アプリ層は本物の形状と同じ道具で描ける。

        **プレビューできるverbは `PREVIEWABLE_VERBS` に登録したものだけ**(ソケットから届いた
        文字列で `Session` の任意のメソッドを呼べる作りにしない)。

        **キャッシュと `_applied_count` は自分で退避する** —— `speculative` が戻すのは文書で
        あって `Session` の持ち物ではない。テセレーションは `self.geometry()` を使わず
        `kernel.tessellate` を直に呼ぶ(キャッシュを投機的な形で汚さないため)。
        """
        if verb not in PREVIEWABLE_VERBS:
            raise ValueError(
                f"verb {verb!r} はプレビューできない"
                f"(登録済み: {sorted(PREVIEWABLE_VERBS)})"
            )
        method_name, allowed = PREVIEWABLE_VERBS[verb]
        values = dict(args or {})
        unknown = sorted(set(values) - set(allowed))
        if unknown:
            raise ValueError(f"verb {verb!r} が受け取らない引数: {unknown}(受け取るのは {list(allowed)})")

        before = {id(body): body.shape for body in self.document.bodies}
        applied_count = self._applied_count
        bodies_out: list[dict] = []
        self._suppress_events += 1
        try:
            with speculative(self.document):
                getattr(self, method_name)(**values)
                for body in self.document.bodies:
                    if not body.visible or body.shape is None:
                        continue
                    if before.get(id(body)) is body.shape:
                        continue  # 形が変わっていないBodyは送らない(差分送信と同じ判断)
                    bodies_out.append({
                        "body_id": body.id,
                        "name": body.name,
                        "face_count": kernel.face_count(body.shape),
                        "volume": query.volume(body.shape),
                        "geometry": kernel.tessellate(body.shape, self.deviation),
                    })
        finally:
            self._suppress_events -= 1
            self._applied_count = applied_count
        return {"verb": verb, "bodies": bodies_out}

    def undo(self) -> dict:
        """直前の操作を取り消す。**文書の土台を作った操作は取り消させない**。

        計測の手順は「10回押して10回戻す」ので、1回多く押すと編集用ボディを作った操作まで
        取り消され、`active_body` が消えて以後の画面更新が全部落ちる。
        土台はundoの対象外にして、理由を画面へ返す。
        """
        if not self.document.log.entries:
            raise ValueError("取り消せる操作が無い")
        if self._at_setup_floor():
            raise ValueError(
                f"{self.document.log.entries[-1].verb} は取り消せない"
                "(部品の読み込みと編集用ボディは計測の土台)"
            )
        operation = self.document.log.undo(self.document)
        if operation is None:
            raise ValueError("取り消せる操作が無い")
        self._reset_selection_if_needed("undo")
        return self._emit({"verb": "undo", "seq": operation.seq, "undone": operation.verb})

    def redo(self) -> dict:
        operation = self.document.log.redo(self.document)
        if operation is None:
            raise ValueError("やり直せる操作が無い")
        self._reset_selection_if_needed("redo")
        return self._emit({"verb": "redo", "seq": operation.seq, "redone": operation.verb})

    # ---------------------------------------------------------------- F2: MCP / 全verbの口
    #
    # **新しいverbは作らない**。コアに既にあるものを `_emit` 経由で出すだけ。
    # MCPもブラウザも同じ `Session` を叩くので、ここを通らないと開いている画面へ届かない。
    def _after_logged_verb(self, extra: Optional[dict] = None) -> dict:
        """直前にログへ積まれたverbについて変更イベントを発火する。"""
        operation = self.document.log.entries[-1]
        self._applied_count += 1
        self._reset_selection_if_needed(operation.verb)
        payload: dict[str, Any] = {"verb": operation.verb, "seq": operation.seq}
        if extra:
            payload.update(extra)
        return self._emit(payload)

    def sketch_create(self, base_plane: Optional[str] = None) -> dict:
        """スケッチを作る。`base_plane`(XY/XZ/YZ)省略時は**確定選択の面**(ちょうど1枚)に貼る。

        **面は引数で受けない**。2箇所に同じ解決を持つと、片方だけ直したときに「AIと人で
        貼る先が違う」形で食い違う。

        平面も選択も無ければエラーにする —— 既定でXYに貼ると、図形が静かに別の場所に出る。
        2枚選んでいれば `SelectionAmbiguous`、AIの提案のままなら `SelectionNotCommitted` が
        `require_confirmed` から上がり、そのままWS/MCPのエラーになる。
        """
        if base_plane is not None:
            plane: Any = str(base_plane)
        else:
            face = selection.require_confirmed(
                self.document, "face", exactly_one=True
            )[0]
            plane = sketch.face_plane(face["body"], face["index"])
        sketch_id = sketch.sketch_create(self.document, plane)
        return self._after_logged_verb({"sketch_id": sketch_id})

    def add_line(self, sketch_id: str, p0: Any, p1: Any) -> dict:
        entity_id = sketch.add_line(self.document, sketch_id, p0, p1)
        return self._after_logged_verb({"entity_id": entity_id})

    def add_circle(self, sketch_id: str, center: Any, radius: float) -> dict:
        entity_id = sketch.add_circle(self.document, sketch_id, center, radius)
        return self._after_logged_verb({"entity_id": entity_id})

    def add_rectangle(self, sketch_id: str, corner: Any, width: float, height: float) -> dict:
        entity_id = sketch.add_rectangle(self.document, sketch_id, corner, width, height)
        return self._after_logged_verb({"entity_id": entity_id})

    def add_arc(
        self,
        sketch_id: str,
        center: Any,
        radius: float,
        start_deg: float,
        end_deg: float,
    ) -> dict:
        entity_id = sketch.add_arc(
            self.document, sketch_id, center, radius, start_deg, end_deg
        )
        return self._after_logged_verb({"entity_id": entity_id})

    def extrude(
        self,
        distance: float,
        mode: str = "join",
        body_id: Optional[str] = None,
    ) -> dict:
        """**確定選択の領域**を押し出す(`verbs.extrude`)。面を押す `extrude_face` とは別。

        押し出す領域は引数ではなく選択から取る。**`body_id` だけは引数のまま** ——
        これは「どのBodyへ足す/から引くか」であって対象集合ではない(Bodyは選択集合の
        項目ではない。MCPの `extrude` も同じ形)。`mode="new"` は行き先を持たないので
        `body_id` を渡すと ValueError(ブラウザからは元々送れない)。
        """
        refs = [
            region.RegionRef.from_dict(item)
            for item in selection.require_confirmed(self.document, "region")
        ]
        operation = extrude_verb(
            self.document, refs, float(distance), mode=mode, body_id=body_id
        )
        changed_body_id = operation.args["body_id"]
        return self._after_logged_verb({
            "body_id": changed_body_id,
            "distance": float(distance),
            "mode": mode,
            "changed_bodies": [changed_body_id],
        })

    def fillet(self, radius: Any) -> dict:
        """**確定選択の辺**を丸める。`radius` は数値か辺ごとのリスト。

        辺と対象Bodyは `selection.require_edges` が選択順のまま返す。順序が効くのは
        `edges` と `radius` を位置で対応させるため(並べ替えると半径の割り当てがずれる)。
        """
        edges, body_id = selection.require_edges(self.document)
        operation = fillet_verb(self.document, edges, radius, body_id=body_id)
        changed_body_id = operation.args["body_id"]
        return self._after_logged_verb({
            "body_id": changed_body_id,
            "changed_bodies": [changed_body_id],
        })

    def chamfer(self, distance: Any) -> dict:
        """**確定選択の辺**を面取りする(`fillet` と同じ規律)。"""
        edges, body_id = selection.require_edges(self.document)
        operation = chamfer_verb(self.document, edges, distance, body_id=body_id)
        changed_body_id = operation.args["body_id"]
        return self._after_logged_verb({
            "body_id": changed_body_id,
            "changed_bodies": [changed_body_id],
        })

    def pattern_rectangular(
        self,
        sketch_id: str,
        entities: Any,
        dx: float,
        dy: float,
        nx: int,
        ny: int,
    ) -> dict:
        pattern_id = pattern_rectangular_verb(
            self.document, sketch_id, entities, dx, dy, nx, ny
        )
        return self._after_logged_verb({"pattern_id": pattern_id})

    def pattern_circular(
        self,
        sketch_id: str,
        entities: Any,
        center: Any,
        count: int,
        total_angle: float = 360.0,
        rotate_copies: bool = True,
    ) -> dict:
        pattern_id = pattern_circular_verb(
            self.document,
            sketch_id,
            entities,
            center,
            count,
            total_angle=total_angle,
            rotate_copies=rotate_copies,
        )
        return self._after_logged_verb({"pattern_id": pattern_id})

    def set_parameter(self, name: str, value: float) -> dict:
        before_shapes = {body.id: id(body.shape) for body in self.document.bodies}
        operation = parameters.set_parameter(self.document, name, float(value))
        changed_bodies = [
            body.id
            for body in self.document.bodies
            if before_shapes.get(body.id) != id(body.shape)
        ]
        return self._after_logged_verb({
            "name": operation.args["name"],
            "value": operation.args["value"],
            "previous": operation.args["previous"],
            "changed_bodies": changed_bodies,
        })

    def import_step(self, path: Any) -> dict:
        """STEPを読み、**編集可能なボディまで作る**(verb `import_step`)。

        **STEPの読み込みはこれ1本**(2026-08-27に統合) —— ソリッドごとに参照ボディ(隠す)と
        編集用ボディを作る1回のverbで、何部品あってもログ1行・undo1回である。
        **メソッド名・WSのコマンド名・ログのverb名を同じ綴りにしてある**(1コマンドを引数で
        2つのverbに打ち分けると、コマンド名と積まれるverbが食い違う)。
        """
        body_ids = import_step_verb(self.document, str(self._resolve(path)))
        args = self.document.log.entries[-1].args
        return self._after_logged_verb({
            "body_ids": list(body_ids),
            "reference_ids": list(args["reference_ids"]),
        })

    def import_svg(self, sketch_id: str, path: Any, scale: Any = None) -> dict:
        """SVGの図形を**いま開いているスケッチ(`sketch_id`)へ**取り込む(verb `import_svg`)。

        **貼る先は引数で選ばない**(2026-08-28) —— スケッチのverbなので、貼る先は
        `sketch_id` が指すスケッチの平面で決まる(`add_line` と同じ形)。基準平面や面を
        指す経路は無い。スケッチの外から使いたければ、先に `sketch_create` で作ってから
        このverbを呼ぶ。

        `scale` は解決後のmmへさらに掛ける倍率(省略時1.0)。UIには出しておらず
        (`catalog` の `ws=False`)、単位を持たないSVGの寸法が合わないときだけAIが使う。
        """
        entity_ids = import_svg_verb(
            self.document,
            str(sketch_id),
            str(self._resolve(path)),
            scale=1.0 if scale is None else float(scale),
        )
        args = self.document.log.entries[-1].args
        return self._after_logged_verb(
            {
                "sketch_id": str(sketch_id),
                "entity_ids": list(entity_ids),
                "source_name": args["source_name"],
                "entity_count": len(args["entities"]),
            }
        )

    # ---------------------------------------------------------------- E: ファイル操作
    #
    # **保存形式は操作ログのJSON**(実装計画書§3.1)。形状はそこから導出されるので、
    # 開くたびに再生経路が検査されることになる —— 「保存したものが開けない」が
    # 起きるとしたら、それは再生が壊れているということであり、隠さない方がよい。
    def _resolve(self, path: Any) -> Path:
        """受け取ったパスを絶対パスにする(相対はサーバの作業ディレクトリ基準)。

        **どこへ書いたかを必ず応答へ返す**ため、ここで1度だけ解決する。ブラウザからは
        `model.json` のような相対パスが普通に来るので、解決を各所でやると
        「保存したと言われたファイルが見つからない」が起きる。
        """
        resolved = Path(str(path)).expanduser()
        return resolved if resolved.is_absolute() else (Path.cwd() / resolved).resolve()

    def _resolve_document_name(self, name: Any) -> Path:
        """**ファイル名だけ**を受け取り、`models_dir` 配下に閉じたパスへ解決する。

        保存先が「その日サーバをどこで起動したか」で変わらないよう、起点を `models_dir` に
        固定してある。**文字列だけの拒否では脱出経路が残る**(いずれも実測):

        - `Path("D:outside.json").is_absolute()` は **False**(Windowsのドライブ相対パス)。
          素の絶対パス判定をすり抜け、しかも `models_dir / "D:outside.json"` は pathlib が
          右辺の「ドライブ付きセグメント」を**新しいアンカー**として扱うため、`models_dir` 側が
          黙って捨てられて `D:\\outside.json` に解決される。**コロン1文字**が入口で、NTFSの
          代替データストリーム(`a.json:x` → `models\\a.json:x` へ書ける)も同じ入口を通る。
        - 予約デバイス名(`CON`/`COM1`等、拡張子を付けても特別扱い)・末尾のドット/空白
          (Windowsが黙って落とすため `foo.json.` と `foo.json` が同じ実体になりうる)。
        - **文字列の拒否だけでは、既存のシンボリックリンク/ジャンクションを指すファイル名が
          `models_dir` の外へ実際に解決されるケースを塞げない** —— 最終判定は必ず
          **解決後のパスが `models_dir` 配下か**で行う(`resolve()` はリンクを辿る)。

        したがって判定は2段: ①明らかに拒まれる形を早く言う文字列検査(区切り文字・コロン・
        `.`/`..`・予約名・前後の空白・末尾のドット/空白・拡張子)、②**解決後のパスが
        `models_dir` 配下であることの検査**(`is_relative_to`)。①だけに頼らない。

        `models_dir` が無ければここで作る。
        """
        text = str(name)
        stripped = text.strip()
        if stripped == "":
            raise ValueError("ファイル名が空")
        if text != stripped:
            raise ValueError(f"前後の空白は不可: {text!r}")
        if "/" in text or "\\" in text:
            raise ValueError(f"ファイル名だけを受け取る(区切り文字は不可): {text!r}")
        if ":" in text:
            # ドライブ指定("D:outside.json")もNTFS代替データストリーム("a.json:x")も
            # コロン1文字が入口(上のdocstring参照)。
            raise ValueError(f"ファイル名だけを受け取る(':'は不可): {text!r}")
        if text in (".", ".."):
            raise ValueError(f"ファイル名として使えない: {text!r}")
        if text.endswith(".") or text.endswith(" "):
            raise ValueError(f"末尾のドット/空白は不可(Windowsが黙って落とすため): {text!r}")
        base_name = text.split(".", 1)[0].upper()
        if base_name in _RESERVED_WINDOWS_NAMES:
            raise ValueError(f"予約されたデバイス名は使えない: {text!r}")
        if not text.lower().endswith(".json"):
            # **一覧(`list_documents`)は `*.json` しか見ない**。拡張子違いを受理すると、
            # 保存はできるのに一覧から二度と開けないファイルができる。黙って `.json` を
            # 補うより、打ち間違いをその場で本人に返す。
            raise ValueError(f"拡張子は.json(一覧はモデルの*.jsonだけを見る): {text!r}")
        candidate = Path(text)
        if candidate.is_absolute():
            raise ValueError(f"ファイル名だけを受け取る(絶対パスは不可): {text!r}")
        self.models_dir.mkdir(parents=True, exist_ok=True)
        root = self.models_dir.resolve()
        resolved = (root / text).resolve()
        # **正の判定はここ**(2026-08-23 QA指摘)。文字列検査をすり抜けるものが
        # あっても、シンボリックリンク越しの脱出も含めて最終的にここで止まる。
        if not resolved.is_relative_to(root):
            raise ValueError(f"models/の外を指している: {text!r} -> {resolved}")
        return resolved

    def save_document(self, name: Any) -> dict:
        """操作ログをJSONで書き出す。**verbではない**(文書を変えないのでログに載らない)。

        `name` は**ファイル名だけ**(`models_dir` 配下に固定)。区切り文字・絶対パス・
        `..` は `_resolve_document_name` が拒否する。
        """
        target = self._resolve_document_name(name)
        payload = self.document.log.to_json()
        # **一時ファイルへ書いてから差し替える**。素の上書きは既存の
        # 中身を先に切り詰めるので、途中で落ちると**唯一の永続形式である前回の保存が
        # 半端なJSONで潰れる**(形状は導出物なので、これを失うと復旧の手立てが無い)。
        # `os.replace` は同じディレクトリ内なら不可分に差し替わる。
        staging = target.with_name(target.name + ".writing")
        staging.write_text(payload, encoding="utf-8")
        os.replace(staging, target)
        return {
            "action": "save",
            "path": str(target),
            "name": target.name,
            "bytes": target.stat().st_size,
            "operations": len(self.document.log.entries),
        }

    def open_document(self, name: Any) -> dict:
        """保存した操作ログを読み、**再生して**文書を差し替える(§3.1「これが正」)。

        `name` は**ファイル名だけ**(`models_dir` 配下に固定。`save_document` と同じ)。

        **失敗しても今の文書を壊さない**。新しい `Document` の上で再生し切ってから
        差し替えるので、途中で落ちたら例外が上がるだけで画面はそのまま残る。

        差し替えの後始末(キャッシュ・版・土台の下限)は `_replace_document` が持つ
        (`new_document` と共通)。
        """
        source = self._resolve_document_name(name)
        if not source.exists():
            raise FileNotFoundError(f"開けるファイルが無い: {source}")
        log = OpLog.from_json(source.read_text(encoding="utf-8"))
        document = Document()
        # **束ねてから再生する**(順序が逆だと `set_parameter` を含むログで落ちる)。
        # パラメータの再生は「その操作までを作り直す」ために `document.log` を頭から
        # 読むので、再生中の文書が自分のログを持っていないと成立しない
        # (`parameters.rebuild` が理由つきで拒否する)。
        document.log = log
        log.replay(document)

        self._replace_document(document)
        return self._emit({
            "change": "document",
            "action": "open",
            "path": str(source),
            "name": source.name,
            "operations": len(log.entries),
        })

    def new_document(self) -> dict:
        """空の文書へ差し替える(「新規」)。**verbではない**(ログに載らない)。

        **今の文書は捨てられ、undoでは戻らない**(`open_document` と同じ)。確認は挟まない ——
        一覧からのクリック1つで別の文書へ移れるのに、空の文書へ移るときだけ確認を挟むと
        挙動が割れる(`web/src/FilesPanel.svelte` の「開くのに確認ダイアログを挟まない」)。

        **`save_document` は呼ばない**(自動保存はスコープ外)。保存していない編集は消える。
        """
        self._replace_document(Document())
        return self._emit({
            "change": "document",
            "action": "new",
            # **開いたファイルが無いので `path` / `name` は `None`**。受け取る側
            # (`web/src/App.svelte`)はこれで「今どれを触っているか」の表示を(無題)へ戻し、
            # 保存状態も「未保存」へ戻す(どこにも書かれていない文書になるため)。
            "path": None,
            "name": None,
            "operations": 0,
        })

    def _replace_document(self, document: Document) -> None:
        """今の文書を `document` へ差し替える(`open_document` / `new_document` の共通部分)。

        差し替えではキャッシュ(テセレーション・辺・頂点・体積・スケッチ・版)を**全部捨てる**
        —— Bodyのidは再生で作り直されるが、同じidが再び現れる可能性があり、古い形状を
        指したキャッシュが残っていると別の文書の形を配ることになる。

        **選択集合はここで触らない** —— 選択は `Document` が持つので、差し替えた時点で
        新しい文書の空の選択になっている。
        """
        self.document = document
        self._geometry_cache.clear()
        self._edge_cache.clear()
        self._vertex_cache.clear()
        self._volume_cache.clear()
        self._sketch_cache.clear()
        # **版の数え上げ(`_revision_counter`)は消さない**。消すと版が1へ戻り、同じidの
        # Bodyを知っているクライアントが「版が同じ=変わっていない」と判断して、
        # **前の文書の形を描き続ける**(idはログに記録されるので再生で同じidが現れうる)。
        # 状態のほう(`_revision_state`)だけ捨てれば、次の `body_revision` で必ず1つ上がる。
        self._revision_state.clear()
        # **土台の下限は引き継がない**。読み込んだログはすべてその文書の操作であり、
        # 起動時の `load_step` が積んだ「取り消させない土台」とは別物である。
        self._setup_floor_seq = 0
        self._applied_count += 1

    def list_documents(self) -> dict:
        """`models_dir` の中身を一覧にする(名前/更新日時/サイズ/操作数)。**verbではない**。

        操作数は `OpLog.from_json` で数える —— 形状の再生(`log.replay`)まではせず、パースだけ。
        STEP本文がログに入って1ファイルが4.8MB級になったが、実測でファイルあたり約30ms
        (PCケースの4.8MBを3件で0.09s)なので、この作りのままにしてある。

        壊れたファイル(手で触られた・書き込み途中の `.writing` 等)は一覧からは省く
        (一覧が原因不明の例外で丸ごと落ちるより、読める分だけ見せる方が実用に沿う)。
        **旧形式(version 1)のファイルもここでは黙って省かれる** —— `from_json` が ValueError
        にするため。次に版を上げるときは「一覧に出ないだけで理由が分からない」になることに注意。
        """
        self.models_dir.mkdir(parents=True, exist_ok=True)
        files = []
        for path in sorted(self.models_dir.glob("*.json")):
            try:
                stat = path.stat()
                operations = len(OpLog.from_json(path.read_text(encoding="utf-8")).entries)
            except (OSError, ValueError, KeyError, TypeError):
                continue
            files.append({
                "name": path.name,
                "mtime": stat.st_mtime,
                "size": stat.st_size,
                "operations": operations,
            })
        return {"action": "list", "path": str(self.models_dir), "bytes": 0, "files": files}

    def export_step_file(self, path: Any) -> dict:
        """STEPを書き出す。**ログに載らない**(文書を変えないのでverbではない)。

        対象は**いま表示中のBodyだけ**(引数で選べない)。非表示で除外した件数は
        `skipped_hidden` として必ず応答に載る(0件でも載る)。
        """
        target = self._resolve(path)
        if not target.parent.exists():
            raise ValueError(f"書き出し先のディレクトリが無い: {target.parent}")
        resolved = io_step.export_body_ids(self.document)
        skipped = io_step.export_skipped_hidden(self.document)
        io_step.export_step(self.document, target)
        return {
            "action": "export_step",
            "path": str(target),
            "bytes": target.stat().st_size,
            "body_ids": list(resolved),
            "skipped_hidden": list(skipped),
        }

    def export_stl_file(self, path: Any, deviation: Optional[float] = None) -> dict:
        """STLを書き出す。`deviation` は絶対の線形偏差[mm](省略時はセッションの値)。

        **偏差を指定できることが要件**(§7: 0.2mm→30.7KB / 0.01mm→170.6KB)。
        対象は**いま表示中のBodyだけ**(引数で選べない)。非表示で除外した件数は
        `skipped_hidden` として必ず応答に載る(0件でも載る)。
        """
        target = self._resolve(path)
        if not target.parent.exists():
            raise ValueError(f"書き出し先のディレクトリが無い: {target.parent}")
        used = float(self.deviation if deviation is None else deviation)
        resolved = io_step.export_body_ids(self.document)
        skipped = io_step.export_skipped_hidden(self.document)
        io_step.export_stl(self.document, target, used)
        return {
            "action": "export_stl",
            "path": str(target),
            "bytes": target.stat().st_size,
            "deviation": used,
            "body_ids": list(resolved),
            "skipped_hidden": list(skipped),
        }

    def take_warnings(self) -> list[str]:
        """溜まった警告を取り出して空にする(出したら消す)。"""
        warnings, self.warnings = self.warnings, []
        return warnings


# --------------------------------------------------------------------------------------
# 静的配信(web/dist)
# --------------------------------------------------------------------------------------
#: MCPのHTTPトランスポートの口(2026-08-26)。ブラウザが開くページと同じ8766に生やす
#: —— 人もClaude Codeも覚えるURLが1つで済む。
MCP_HTTP_PATH = "/mcp"

#: `/mcp` が受け取るリクエストボディの上限。WebSocketの `max_size` と揃える。
MCP_HTTP_MAX_BODY = 8 * 1024 * 1024


class WebHandler(SimpleHTTPRequestHandler):
    """`web/dist` を配る。加えて `/duocad-config.json` でWebSocketのURLを教え、
    `/mcp` でMCP(JSON-RPC 2.0 over HTTP)を喋る。

    ポートを別にしているのは、1つのポートでHTTPとWebSocketを同時に喋る実装が
    websocketsのバージョンに依存するため(壊れやすい側を避けた)。
    """

    config_payload: bytes = b"{}"
    #: `/mcp` を処理する `McpServer`(`duocad/mcp_server.py`)。`start_http_server` が挿す。
    #: None のままなら `/mcp` は 404 —— 静的配信だけ立てる検査のため。
    mcp_server: Any = None
    #: `Host` ヘッダとして許す綴り(DNSリバインディング対策。下記 `_mcp_forbidden`)。
    mcp_hosts: frozenset = frozenset()
    #: `McpServer` の状態(`initialized` 等)を2スレッドで同時に触らせない。
    #: `ThreadingHTTPServer` は接続ごとにスレッドを立てるので、直列化はここで作る。
    mcp_lock = threading.Lock()

    def do_POST(self) -> None:  # noqa: N802  (BaseHTTPRequestHandlerの命名に従う)
        """MCPのHTTPトランスポート。**`/mcp` 以外にPOSTの口は無い**。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.send_error(400)
            return
        if length > MCP_HTTP_MAX_BODY:
            # 大きすぎる本文は読み捨てない(読み捨てること自体が的になる)。接続ごと閉じる。
            self.send_error(413)
            return

        # **検査より先に本文を読み切る。** 読まずに 4xx を返して接続を閉じると、まだ書いている
        # クライアントの側で接続が中断される(Windowsでは `ConnectionAbortedError`)——
        # 拒否は届いているのにクライアントからは通信エラーに見える、という紛らわしい形になる。
        # 2026-08-26、`tests/test_mcp_http.py` の403の検査が**この競合で散発的に赤くなって**
        # 見つかった(1回目は緑・2回目で赤)。上限で頭打ちにしてあるので読み切って安全である。
        raw = self.rfile.read(length) if length > 0 else b""

        if self.path.split("?")[0] != MCP_HTTP_PATH or self.mcp_server is None:
            self.send_error(404)
            return
        if self._mcp_forbidden():
            return
        if not raw:
            self.send_error(400)
            return

        try:
            message = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            # JSON-RPCのパースエラー封筒(-32700)を返すのが仕様上は丁寧だが、そのために
            # `mcp_server.py` の定数をここへ写すと**同じ番号が2箇所**になる(server.py を
            # importしているのは mcp_server.py の側なので、逆向きのimportは循環する)。
            # 行儀のよいクライアントはここへ来ないので、素の400で足りると判断した。
            self.send_error(400)
            return

        with self.mcp_lock:
            response = self.mcp_server.handle(message)
        # `handle` の None は「返事を書いてはいけない」(通知)。HTTPでは202が対応する。
        self._send_json(202 if response is None else 200, response)

    def _mcp_forbidden(self) -> bool:
        """`/mcp` を**ブラウザから叩かれない**ようにする検査(通ったら False)。

        **なぜ要るか**: `127.0.0.1` へのbindは防御にならない —— 攻撃側のコードは利用者の
        ブラウザの中で動くからである(`allowed_origins` と同じ理屈)。しかもJSON-RPCの本文は
        `Content-Type: text/plain` にすれば**プリフライトの要らない「単純リクエスト」**として
        送れるので、悪意のあるページは応答を読めないまま `/mcp` に副作用だけ起こせる。

        防ぐのはこの2点で足りる:

        - **`Origin` があれば拒否する**。ブラウザは他オリジンへのPOSTに必ず `Origin` を付け、
          JavaScriptから偽装できない。duocad自身のページは `/mcp` を叩かないので、
          「`Origin` 付きのリクエストは全部よそ者」と言い切れる。
        - **`Host` を許可リストで見る**。`127.0.0.1:<port>` 以外の名前で解決して来たものを
          落とす(DNSリバインディング)。

        **CORSのヘッダは一切返さない**(だからプリフライトも通らない)。

        **トークンは置かない。** 守れる相手が増えないためである —— このPCでコードを実行できる
        相手は、トークンを書いたファイルも読める。`allowed_origins` が「`Origin` ヘッダ無しを
        意図的に許可する」と決めたのと同じ線引きで、その外側は防御対象にしない。おかげで
        `claude mcp add` はURL1つで済む(2026-08-26判断)。
        """
        if self.headers.get("Origin") is not None:
            self.send_error(403)
            return True
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in self.mcp_hosts:
            self.send_error(403)
            return True
        return False

    def _send_json(self, status: int, payload: Any) -> None:
        body = b"" if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        if body:
            self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802  (BaseHTTPRequestHandlerの命名に従う)
        path = self.path.split("?")[0]
        if path == MCP_HTTP_PATH:
            # **サーバ→クライアントのSSEストリームは提供しない**(duocadからMCPクライアントへ
            # pushするものが無い。ツールの顔ぶれは起動中変わらないので `listChanged` も出さない)。
            # 仕様はその場合405を返せと言っている。
            self.send_error(405)
            return
        if path == "/favicon.ico":
            # ブラウザが必ず取りに来る。404を返すとコンソールがエラーで汚れ、
            # 計測中に本物のエラーを見落とす(自動検査もこれで落ちた)。中身は要らない。
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/duocad-config.json":
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(self.config_payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(self.config_payload)
            return
        super().do_GET()

    def log_message(self, *args: Any) -> None:
        """アクセスログを出さない(計測中のコンソールを静かに保つ)。"""


def mcp_http_hosts(port: int) -> frozenset:
    """`/mcp` の `Host` ヘッダとして許す綴り(`WebHandler._mcp_forbidden`)。

    `localhost` と `127.0.0.1` は別の綴りなので両方を挙げる(WebSocketの `allowed_origins`
    が両方を挙げるのと同じ理由)。
    """
    return frozenset(f"{host}:{port}" for host in ("127.0.0.1", "localhost"))


def start_http_server(
    dist_dir: Path, port: int, ws_url: str, mcp: Any = None
) -> ThreadingHTTPServer:
    """静的配信をデーモンスレッドで立てる。`web/dist` が無ければ何も立てずに例外。

    `mcp` を渡すと同じポートで `/mcp`(MCPのHTTPトランスポート)も喋る。**`Session` を触るのは
    イベントループのスレッドだけ**という不変条件があるので、渡す `McpServer` の道具は
    `ToolsOnLoop` で包んであること —— 包まないと `ThreadingHTTPServer` のワーカースレッドが
    直に文書を書き換える。
    """
    if not (dist_dir / "index.html").exists():
        raise FileNotFoundError(
            f"{dist_dir} に index.html が無い。先に web をビルドすること:\n"
            f"    cd {dist_dir.parent}\n    npm install\n    npm run build"
        )
    WebHandler.config_payload = json.dumps({"ws_url": ws_url}).encode("utf-8")
    WebHandler.mcp_server = mcp
    WebHandler.mcp_hosts = mcp_http_hosts(port)
    handler = partial(WebHandler, directory=str(dist_dir))
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=httpd.serve_forever, name="duocad-http", daemon=True)
    thread.start()
    return httpd


# --------------------------------------------------------------------------------------
# WebSocket
# --------------------------------------------------------------------------------------
async def _send(connection: Any, payload: dict) -> None:
    await connection.send(json.dumps(payload, ensure_ascii=False))


class Server:
    """WebSocketの接続集合とコマンドの実行。状態変更は全接続へ配信する。

    **接続ごとに「知っている版」を覚える**(`_known_revisions`)。文書は部品ごとに複数の
    Bodyを持つぶん、接続によって最後に送った版が違いうる(例: 遅れて `get_state` した接続は
    まだ何も知らない)。差分(`Session.delta`)はこの接続ごとの版を基準に計算する。

    **同時編集は前提にしない**(2026-08-27決定)。複数接続へ配信する作りだが、想定するのは
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
        # **プロバイダは1つしか無い**(2026-08-27。ClaudeをAPIキーで叩く経路を廃止した)。
        # 選ぶ仕組み自体は残してある —— 次のプロバイダの差込口である。
        self._ai_provider_kind = "openai_compat"
        self._ai_model: Optional[str] = None
        self._ai_base_url: Optional[str] = None
        self._ai_loop: Optional[AgentLoop] = None
        self._ai_cancellation = Cancellation()
        self._ai_running = False
        #: 実行中のバックグラウンド往復(GCされないよう保持。`_pending_pushes` と同じ理由)。
        self._pending_ai_runs: set = set()

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

    def _spawn_push(self) -> None:
        """配信タスクを起こし、**参照を持ち続ける**。

        `create_task` の戻りを捨てるとタスクは弱参照でしか繋がらず、途中でGCされうる
        (公式ドキュメントの警告)。加えて、配信中の例外を誰も回収しないと
        「Task exception was never retrieved」が出るだけで**ブラウザは古いまま・
        警告も出ない**という、完了条件3が静かに壊れる形になる。集合で保持し、
        終わったら外し、落ちていたら警告として残す。
        """
        task = asyncio.get_running_loop().create_task(self._push(None, applied=None))
        self._pending_pushes.add(task)

        def finished(done: "asyncio.Task") -> None:
            self._pending_pushes.discard(done)
            if done.cancelled():
                return
            error = done.exception()
            if error is not None:
                self.session.warnings.append(
                    f"コア変更の配信に失敗: {type(error).__name__}: {error}"
                )

        task.add_done_callback(finished)

    async def handle(self, connection: Any) -> None:
        self._loop = self._loop or asyncio.get_running_loop()
        self.clients.add(connection)
        try:
            try:
                await self._push(connection, applied=None, only=connection)
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
        """
        if not self.clients and self._ai_running:
            self._ai_cancel()

    async def _dispatch(self, connection: Any, raw: Any) -> None:
        try:
            message = json.loads(raw)
            command = message.get("cmd")
        except (TypeError, ValueError) as error:
            await _send(connection, {"type": "error", "where": "parse", "message": str(error)})
            return

        try:
            await self._commit_before_verb(connection, command, message)
            if command == "get_state":
                await self._push(connection, applied=None, only=connection)
            elif command == "extrude_face":
                applied = self._apply(
                    self.session.extrude_face,
                    message["distance"],
                    message.get("client_op"),
                )
                await self._push(connection, applied=applied)
            elif command in ("undo", "redo"):
                applied = self._apply(
                    self.session.undo if command == "undo" else self.session.redo
                )
                applied["client_op"] = message.get("client_op")
                await self._push(connection, applied=applied)
            elif command == "set_visible":
                applied = self._apply(
                    self.session.set_visible,
                    message["body_id"],
                    bool(message["visible"]),
                    message.get("client_op"),
                )
                await self._push(connection, applied=applied)
            # -------------------------------------------------- B: 選択(人が直す経路)
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
            elif command in ("commit_selection", "clear_selection"):
                applied = self._apply(
                    self.session.commit_selection
                    if command == "commit_selection"
                    else self.session.clear_selection,
                    message.get("client_op"),
                )
                await self._push(connection, applied=applied)
            # -------------------------------------------------- B: プレビュー(投機的実行)
            # **配信しない**。文書は変わらない(`Session.preview` が元へ戻す)ので、状態を配る
            # 理由が無く、他のクライアントに見せる中間状態も存在しない。要求した接続にだけ返す。
            #
            # **`_apply` で囲まない**(2つの止め方を重ねない)。プレビュー中の発火は
            # `Session._suppress_events` が経路によらず止めており、ここで `_dispatching` まで
            # 立てると、**プレビューの計算中(押し出しなら約1秒)にMCPから届いた本物の変更まで
            # 「WebSocket経由だから配信不要」と誤判定して黙って捨てる**。
            # -------------------------------------------------- C: スケッチ(手で描く経路)
            # **スケッチを作るのは表に載らない** —— `base_plane` が省略できる引数だからで
            # (表は載せた引数を全部必須にする)、省略は「コアの確定選択の面に貼る」を意味する。
            # `catalog` の `ws="special"` がその宣言で、ここがその実体である。
            elif command == "sketch_create":
                applied = self._apply(
                    self.session.sketch_create,
                    message.get("base_plane"),
                )
                applied["client_op"] = message.get("client_op")
                await self._push(connection, applied=applied)
            elif command in SKETCH_COMMANDS:
                method, keys = SKETCH_COMMANDS[command]
                applied = self._apply(
                    getattr(self.session, method),
                    **{key: message[key] for key in keys},
                )
                applied["client_op"] = message.get("client_op")
                await self._push(connection, applied=applied)
            # -------------------------------------------------- D: フィーチャ(手でかける経路)
            # スケッチの5コマンドと同じ形。**失敗は握りつぶさず必ず画面へ返す**(下の except)——
            # とくにフィレットは例外を投げずに `IsDone()=False` で失敗する種類の失敗が
            # あり、コア側が例外へ変換している(§7)。ここで握ると「押したのに何も
            # 起きない」になり、原因が半径にあることが人に伝わらない。
            elif command in FEATURE_COMMANDS:
                method, keys = FEATURE_COMMANDS[command]
                applied = self._apply(
                    getattr(self.session, method),
                    **{key: message[key] for key in keys},
                )
                applied["client_op"] = message.get("client_op")
                await self._push(connection, applied=applied)
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
            # -------------------------------------------------- E: ファイル操作
            # **SVG読み込みはもうここに無い**(2026-08-28) —— `import_svg` はスケッチのverbに
            # なり、`sketch_id` が必須引数になったので表(`SKETCH_COMMANDS`)に載る側へ移った
            # (上のC節で他のスケッチのverbと一緒に捌かれる)。
            # **verbかどうかで応答が違う**(`FILE_COMMANDS` の3つ目の値)。文書を変えるものは
            # 状態を全接続へ配り、書き出すだけのものは要求した接続へ結果だけ返す。
            elif command in FILE_COMMANDS:
                method, keys, is_verb = FILE_COMMANDS[command]
                applied = self._apply(
                    getattr(self.session, method),
                    **{key: message[key] for key in keys},
                )
                if is_verb:
                    applied["client_op"] = message.get("client_op")
                    await self._push(connection, applied=applied)
                else:
                    await _send(connection, {"type": "file", **applied})
            # **開くは文書ごと差し替える**ので全接続へ配る(自分だけ新しい文書を見ている、
            # という状態を作らない)。`Session.open_document` が発火したイベントは
            # `_on_core_change` 側でも拾われるが、`_apply` の間は `_dispatching` が
            # 立っているので二重には配らない。
            elif command == "open_document":
                applied = self._apply(self.session.open_document, message["name"])
                applied["client_op"] = message.get("client_op")
                await self._push(connection, applied=applied)
            # **新規も文書ごと差し替える**ので開くと同じ扱い(全接続へ配る)。引数は無い。
            elif command == "new_document":
                applied = self._apply(self.session.new_document)
                applied["client_op"] = message.get("client_op")
                await self._push(connection, applied=applied)
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
                # `_emit_ai_event` の `call_soon_threadsafe` を挟まず直接 `await` する
                # (待たずに関数を抜けると、テストのようにここで `asyncio.run` が閉じる
                # 呼び方では配信タスクが実行される前に握りつぶされる)。
                await self._broadcast_ai({"type": "ai_status", **status})
            elif command == "ai_send":
                await self._ai_start(connection, str(message.get("message") or ""))
            elif command == "ai_cancel":
                self._ai_cancel()
                await self._broadcast_ai({"type": "ai_status", **self._ai_status()})
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

    async def _commit_before_verb(self, connection: Any, command: Any, message: dict) -> None:
        """**人がツールバーからコマンドを実行する行為そのものを承認とみなす**(2026-08-23本人裁定)。

        対象集合を確定選択から取るverb(`SELECTION_CONSUMING_COMMANDS`)がこのソケットへ
        届いたとき、選択が `origin="proposed"`(AIの提案)のままなら、先に
        `commit_selection` してから走らせる。**中身は変えない** —— 変わるのは origin だけで、
        提案と確定の区別そのもの(`confirmed_items` の門)は残る。

        **ここはブラウザ側の経路だけ**である。MCPは `mcp_server.py` を通るのでこの覆いを
        受けず、AIは今までどおり自分の提案を `commit_selection` で明示的に確定する
        (`duocad/selection.py` のモジュールdocstring)。

        `preview` も対象にする —— 数値を打つと投機実行が飛ぶので、ここを外すと
        「提案のまま押し出そうとした瞬間に `SelectionNotCommitted` の札だけが出る」が残る。
        投機実行するverbは `message["verb"]` にある。

        **確定したら必ずここで配信する**(`_push`)。後続のverbの配信に相乗りさせてはいけない
        —— `preview` は「状態を配らない」コマンドなので、相乗りにすると
        **サーバの origin だけが `user` になり、画面は `proposed` のまま残る**。
        `SelectionPanel` を廃した今、origin を人へ伝えるのは3Dの色だけであり、
        AI側が `get_selection` で読む値でもあるので、この食い違いは
        「人が承認していないのに承認済みに見える」状態そのものになる(2026-08-23 QA指摘・実測で再現)。

        **`client_op` は載せない(`None`)**。これは人が出した選択編集ではなく、サーバが
        自動で行う確定である。verbの `client_op` を載せると、アプリ層がこれを
        「この画面が出した選択変更」と数え(`selfSelections` = 即適用の引き金)、
        さらに `pendingOp` を verb の完了前に解いてしまう。
        """
        name = message.get("verb") if command == "preview" else command
        if name not in SELECTION_CONSUMING_COMMANDS:
            return
        # **貼る先が引数で来ていたら選択は読まない** —— そのときは選択が使われない。
        # 使われない集合を承認扱いにすると、「基準平面を指しただけなのにAIの提案が
        # 確定していた」という静かな変化になる。**スケッチを作るverbは `sketch_create` だけ**
        # (2026-08-28: `import_svg` は `sketch_id` を取る側へ変わり、対象集合を確定選択から
        # 取らなくなったので `SELECTION_CONSUMING_COMMANDS` に入らず、この関数の対象からも外れた)。
        if name == "sketch_create" and message.get("base_plane") is not None:
            return
        if not selection.get_selection(self.session.document).is_proposed():
            return
        applied = self._apply(self.session.commit_selection, None)
        await self._push(connection, applied=applied)

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
        """画面に出してよい設定の全部。**鍵の類は一切載せない**(2026-08-27) ——
        `api_key_configured` も廃止した(APIキーで繋ぐプロバイダがもう無い)。

        `model` / `base_url` は明示的に選んでいなければ**プロバイダの既定値**を返す
        (`null` のままだと画面が「何と話しているか」を言えない)。

        **`providers` は増減しうる列として返し続ける** —— 画面はこの長さを見て
        選択UIを出すか決める(1つなら出さない)。
        """
        return {
            "provider": self._ai_provider_kind,
            "model": self._ai_model or "local",
            "base_url": self._ai_base_url or DEFAULT_OPENAI_COMPAT_BASE_URL,
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
        今ある `openai_compat` はローカルのllama-serverで鍵を持たないので、任意のホストを
        指しても飛ぶのはキーの無いリクエストだけになる(WebSocketは同一オリジンポリシーで
        止まらないので、127.0.0.1 bindは防御にならない —— 鍵を持つプロバイダを足すときは
        この節に拒否を書き戻すこと。2026-08-27にClaudeのAPIキー経路ごと消した)。
        """
        if self._ai_running:
            return {**self._ai_status(), "error": "AI応答中は設定を変更できない(中断してから)"}
        if provider is not None and provider not in AI_PROVIDERS:
            return {**self._ai_status(), "error": f"未知のプロバイダ: {provider!r}"}
        if provider is not None:
            self._ai_provider_kind = provider
        self._ai_model = model or None
        self._ai_base_url = base_url or None
        # 次に使うときに作り直す(古いプロバイダ・古い会話の履歴を持ち越さない)。
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
        provider = build_provider(
            self._ai_provider_kind, model=self._ai_model, base_url=self._ai_base_url
        )
        marshaled = ToolsOnLoop(self._ai_tools, self._loop)
        self._ai_loop = AgentLoop(
            marshaled, provider, cancellation=self._ai_cancellation, progress=self._ai_progress
        )
        return self._ai_loop

    def _ai_progress(self, name: str, arguments: dict, result: Any, ok: bool) -> None:
        """`AgentLoop` の進捗コールバック。**実行器スレッドから呼ばれる**
        (`_ai_start` の `run_in_executor`)ので、`_emit_ai_event` 経由でイベントループへ戻す。
        """
        self._emit_ai_event(
            {
                "type": "ai_tool_call",
                "name": name,
                "arguments": arguments,
                "result": result,
                "ok": bool(ok),
            }
        )

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
        await self._broadcast_ai({"type": "ai_status", **self._ai_status()})

        running_loop = asyncio.get_running_loop()
        future = running_loop.run_in_executor(None, loop_obj.run, prompt)
        self._pending_ai_runs.add(future)

        def finished(fut: "asyncio.Future") -> None:
            self._pending_ai_runs.discard(fut)
            self._ai_running = False
            if not fut.cancelled():
                error = fut.exception()
                if error is not None:
                    self._emit_ai_event(
                        {"type": "ai_error", "message": f"{type(error).__name__}: {error}"}
                    )
                else:
                    self._emit_ai_event({"type": "ai_result", **fut.result().to_dict()})
            self._emit_ai_event({"type": "ai_status", **self._ai_status()})

        future.add_done_callback(finished)

    def _ai_cancel(self) -> None:
        """中断フラグを立てる。**ループはモデルへ投げる前とツール1件ごとに見る**
        (`duocad/agent/loop.py` の `Cancellation` docstring) —— 走っているHTTP
        リクエスト自体は切れないので、反映は次のチェックポイントまで遅れうる。
        """
        self._ai_cancellation.cancel()

    def _emit_ai_event(self, payload: dict) -> None:
        """AI関連のイベントを全接続へ配る配信をイベントループへ確実に乗せる。
        **どのスレッドからでも呼べる**(`_ai_progress` は実行器スレッドから来る。
        `call_soon_threadsafe` は呼び出し元がイベントループのスレッドでも安全)。
        """
        loop = self._loop
        if loop is None:
            return
        loop.call_soon_threadsafe(self._spawn_ai_broadcast, payload)

    def _spawn_ai_broadcast(self, payload: dict) -> None:
        """`_spawn_push` と同じ形(タスクの参照を保持し、失敗を警告として拾う)。"""
        task = asyncio.get_running_loop().create_task(self._broadcast_ai(payload))
        self._pending_pushes.add(task)

        def finished(done: "asyncio.Task") -> None:
            self._pending_pushes.discard(done)
            if done.cancelled():
                return
            error = done.exception()
            if error is not None:
                self.session.warnings.append(
                    f"AIイベントの配信に失敗: {type(error).__name__}: {error}"
                )

        task.add_done_callback(finished)

    async def _broadcast_ai(self, payload: dict) -> None:
        """AI関連のイベントを全接続へ配る(進捗・結果はどの画面から見ても同じ会話の続き)。"""
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


if __name__ == "__main__":
    # **黙って何もしないで終わらない**(実装計画書§6予防6)。`main()` を持たないので、
    # この番人が無いと `python -m duocad.server` が終了コード0で素通りする。
    print(
        "duocad.server は起動コマンドではない。サーバはこちら:\n"
        r"  .venv\Scripts\python.exe -m duocad.mcp_server --open",
        file=sys.stderr,
    )
    raise SystemExit(2)
