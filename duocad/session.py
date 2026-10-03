"""1つの文書を持つサーバ側の状態(`Session`)。WebSocketの接続とは独立(接続が切れても文書は残る)。

verbの呼び出し・選択・パラメータ・操作ログ・保存/開くを、1つの文書の上でまとめる。
**イベントループのスレッドだけが触る**前提の上に立つ(別スレッドから入ってくる経路は
`duocad/net/tools_on_loop.py` が畳む)。誰へどう配るかは `duocad/net/ws.py` の仕事で、
ここが持つのは変更を購読者へ知らせるところまで。

**配りもの(状態・版・キャッシュ)はここに無い** —— `duocad/view.py` の `DocumentView` が持ち、
`Session` は1行の委譲だけを残す(`session.view`)。向きは `Session` -> `view` の一方向。

**`duocad/net/` を参照しない**(向きは `net/` -> ここの一方向)。
**OCPを直接importしない**(境界はkernel.pyだけ)。
"""
from __future__ import annotations

import functools
import inspect
import os
import threading
from pathlib import Path
from typing import Any, Optional  # Any: Session verb wrappers / listeners

from . import (
    catalog,
    io_step,
    kernel,
    kernel_sketch,
    optimize_run,
    query,
    selection,
    sketch,
)
from .document import Document
from .entry import entries, entry_for
from .measure import measure_selection
from .oplog import OpLog
from .paths import DEFAULT_EXPORTS_DIR, DEFAULT_MODELS_DIR
from .speculative import speculative
from .verbs import import_step as import_step_verb
from .verbs.envelope_regions import envelope_pieces, selected_regions
from .view import DocumentView

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
class DocumentBusy(RuntimeError):
    """形状最適化の実行中に、文書を変える操作が来た(`Session._edits_document`)。

    最適化は別スレッドで**開始時の文書を持ったまま**数分〜数時間走り、終わりにその文書へ結果を足して
    保持・除外の形を読み直す。その間に文書を変える・差し替えると、結果が別の文書へ入る・古い条件の形が
    足される・Undo した条件のまま結果が入る(issue #26)。選択・表示の切り替え・保存・書き出しは通す。
    """


def _edits_document(method: Any) -> Any:
    """文書を変える `Session` のメソッドに付ける。**形状最適化の実行中は `DocumentBusy` で断る**。

    ブラウザ・MCP・アプリ内AIのどの経路も `Session` のメソッドを通るので、ここ1箇所で全部が止まる
    (画面の灰色化だけでは別タブ・AI・キーボードを止められない)。
    """

    @functools.wraps(method)
    def guarded(self: "Session", *args: Any, **kwargs: Any) -> Any:
        if self._optimizing:
            raise DocumentBusy(
                f"形状最適化の実行中は文書を変えられない({method.__name__})。"
                "終わるか中断してから操作すること"
            )
        return method(self, *args, **kwargs)

    guarded.edits_document = True  # type: ignore[attr-defined]
    return guarded


#: プレビューできるverb -> (`Session` のメソッド名, 受け取ってよい引数名)。
#: `Session.preview` の唯一の入口で、**ここに無いverbはプレビューできない**
#: (載せてよいverbの条件は `catalog.Command.previewable`)。
PREVIEWABLE_VERBS: dict = catalog.previewable_verbs()

class Session:
    """1つの文書を持つサーバ側の状態。WebSocketの接続とは独立(接続が切れても文書は残る)。"""

    def __init__(
        self,
        step_path: Optional[Path],
        deviation: float,
        models_dir: Optional[Path] = None,
        exports_dir: Optional[Path] = None,
    ) -> None:
        self.document = Document()
        self.deviation = float(deviation)
        #: 保存/開く/一覧の唯一の置き場。既定は `DEFAULT_MODELS_DIR`
        #: (リポジトリ直下の `models/`)。テストが実フォルダを汚さないよう差し替えられる。
        self.models_dir: Path = (
            DEFAULT_MODELS_DIR if models_dir is None else Path(models_dir)
        ).resolve()
        #: STEP/STL書き出しの既定の置き場(`DEFAULT_EXPORTS_DIR` = リポジトリ直下の `export/`)。
        self.exports_dir: Path = (
            DEFAULT_EXPORTS_DIR if exports_dir is None else Path(exports_dir)
        ).resolve()
        #: `geometry()` や `_sketch_state` が積む警告。**`view` と同じリストオブジェクトを
        #: 共有する**(吸い出す口は `take_warnings()` だけ。2つの持ち主を作らない)。
        self.warnings: list[str] = []
        #: **いま編集しているファイルの名前**(`models_dir` 配下のファイル名だけ。無ければ `None`)。
        #: これを持たせることで `save_document` の契約を2つに割り、「編集中でないファイルを
        #: 上書きできる」穴を**どのUIからも**塞いでいる(契約の詳細は下記 `save_document`)。
        #:
        #: **書き換えるのは3箇所だけ**: `save_document(name)` の成功・`open_document` の成功・
        #: `_replace_document`(開く/新規の共通部分。引数で必ず決めさせる)。
        self._current_name: Optional[str] = None
        self._applied_count = 0
        #: **自動保存で書けなかった変更があるか**(issue #5、本人判断 2026-09-16)。文書が
        #: 変わるたび `_emit` が `autosave()` を走らせるので、通常は常に False。True になるのは
        #: 書き込みが失敗したとき(ディスク・権限)だけで、`state()["saved"]` が画面へ運ぶ。
        self._unsaved = False
        #: 保存の書き込みを直列にする。形状最適化の結果は**ワーカースレッドから**
        #: `_after_logged_verb` → `_emit` → 自動保存へ来るので、イベントループ側の操作の
        #: 保存と同じ一時ファイルへ同時に書く経路がある。
        self._save_lock = threading.Lock()
        #: 配りもの(状態・版・キャッシュ)。**文書を差し替えたら `view.reset()` を呼ぶ**
        #: (`_replace_document`)——古い文書について測ったものを握ったままにしないため。
        self.view = DocumentView(self.document, self.deviation, self.warnings)
        self._setup_floor_seq = 0  # load_step が積んだ最後のseq(これ以下はundoの対象外)
        self._listeners: list = []  # 文書が変わったときに呼ぶ購読者(下記 add_listener)
        self._suppress_events = 0  # >0 の間は `_emit` が配らない(プレビュー。`preview` 参照)
        #: 形状最適化の中断の旗。**別スレッドから立てられる**(実行はワーカースレッド、
        #: `cancel_optimization` はイベントループのスレッド)ので `Event` にしてある。
        self._optimize_cancel = threading.Event()
        #: 形状最適化が走っているか(`begin_optimization` 〜 `end_optimization`)。立っている間は
        #: 文書を変える操作を断る(`_edits_document`)。**イベントループのスレッドだけが書く**。
        self._optimizing = False
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

    @property
    def applied_count(self) -> int:
        """**verbを適用した回数**(undo / redo では動かない・単調増加)。

        `state_meta()` と MCP の `describe_document` が同じ答えを返すための唯一の出どころ。
        **操作ログの長さ(`len(document.log.entries)`)ではない** —— ログの長さはundoで減るので、
        「undoで保存時点へ戻した文書」と「undoしてから別のverbを打った文書」が同じ値になる。
        `web/src/App.svelte` の保存済み判定は、この2つを見分けるために**動かないほう**の意味に
        依存している(そちらへ揃える。逆向きに揃えない)。

        書き換える側(`extrude` 等の各verb / `_after_logged_verb` / 文書を開く)は
        従来どおり `_applied_count` を直に動かす。**`preview()` は投機実行のあいだ
        `_applied_count` を退避して戻す**ので、プレビューでこの値は動かない。
        """
        return self._applied_count

    @property
    def current_name(self) -> Optional[str]:
        """いま編集中のファイル名(`models_dir` 配下、無ければ `None`)。`state()["current_name"]` と同じ値。

        `net/ws.py` が「開こうとしている名前は今の文書そのものか」を判定するために読む
        (`_current_name` を外から直接触らせないための窓)。
        """
        return self._current_name

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
        # **文書が変わったら、配る前にディスクへ書く**(1操作ごとの自動保存。issue #5)。
        # verb の応答(`verb` キー。`_after_logged_verb` / undo / redo)だけが
        # 対象で、選択・可視性(`change` キー)はファイルに載らない。ここが WebSocket・MCP・
        # 形状最適化のどの経路も通る唯一の合流点である。配る前に書くのは、配りもの
        # (`state.saved` / `current_name`)が保存の結果を含むため。
        if "verb" in applied:
            self.autosave()
        for callback in list(self._listeners):
            try:
                callback(applied)
            except Exception as error:
                self.warnings.append(f"変更イベントの配信に失敗: {type(error).__name__}: {error}")
        return applied

    # ---------------------------------------------------------------- 文書の組み立て
    @_edits_document
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

    # ---------------------------------------------------------------- 配りもの(`DocumentView` への委譲)
    #
    # 状態・版・キャッシュの実体は `duocad/view.py` にある。ここに
    # 残すのは**1行の委譲だけ**で、呼び出し側(`net/ws.py` の `_push`、`mcp_server.py`、
    # テスト)が `session.state_meta()` の形で引き続き引けるようにするためのものである。
    #
    # `can_undo` / `applied_count` / 「プレビュー中か」の**持ち主は `Session`** なので、
    # 委譲のときにこちらから渡す(`view` は `Session` を見ない)。
    def _view_inputs(self) -> dict:
        """`view` が封筒に載せる値のうち、**持ち主が `Session` のもの**。"""
        return {
            "can_undo": self.can_undo,
            "applied_count": self.applied_count,
            "previewing": bool(self._suppress_events),
        }

    def state(self) -> dict:
        """アプリ層へ渡す全状態(`DocumentView.state`)。**geometryを含む(重い)**。"""
        return self._with_current_name(self.view.state(**self._view_inputs()))

    def state_meta(self) -> dict:
        """`state()` の軽量版(`DocumentView.state_meta`)。配信のたびに呼ばれる口。"""
        return self._with_current_name(self.view.state_meta(**self._view_inputs()))

    def _with_current_name(self, state: dict) -> dict:
        """配りものに**いま編集しているファイル名**と**保存状態**(`saved`)を足す(`view` の持ち物ではない)。

        **`view.state_meta()` へ足さない** —— `DocumentView` が持つのは文書から測れるもの
        (形・版・スケッチ)だけで、どのファイルを開いているかは `Session` の持ち物である。
        `state()` と `state_meta()` の両方をここへ通すのは、片方だけに載って
        「`get_state` では見えるのに配信では見えない」が起きないようにするため。

        **載せないと画面が説明のつかない状態になる**: リロードすると文書はサーバに残る
        のにブラウザ側の名前だけ失われ、ヘッダが「(無題)」のままなのに自動保存と Ctrl+S は
        現在のファイルへ書き続ける、という食い違いが出る。
        """
        state["current_name"] = self._current_name
        state["saved"] = self.saved
        return state

    def delta(self, known_revisions: dict) -> dict:
        """クライアントが持っている版との差分(`DocumentView.delta`)。"""
        return self.view.delta(known_revisions)

    def geometry(self, body_id: str) -> dict:
        """1Bodyのテセレーション結果(`DocumentView.geometry`)。"""
        return self.view.geometry(body_id)

    def edges(self, body_id: str) -> dict:
        """1Bodyの辺の折れ線(`DocumentView.edges`)。"""
        return self.view.edges(body_id)

    def vertices(self, body_id: str) -> dict:
        """1Bodyの頂点の座標(`DocumentView.vertices`)。"""
        return self.view.vertices(body_id)

    def set_view_resolution(
        self,
        world_per_pixel: float,
        size_hint: float = 0.0,
        focus=None,
    ) -> bool:
        """ビューポートのズームに合わせて目標のテセレーション精度を決め直す。

        **文書は一切変わらない**(操作ログにもundoにも載らず、`applied` も出ない)。
        変わるのは配りものの粗さだけなので、`DocumentView` へそのまま委譲する。
        戻り値が True のときだけ配信し直せばよい(`duocad/net/ws.py`)。

        `self.deviation`(セッションの土台。STL書き出しが使う)は**動かさない** ——
        書き出す形の粗さが画面のズームで変わってはいけない。

        `focus` は face単位の高精度化(`{body_id: [面添字]}`)。省略すれば全面が細かい側になる
        (`DocumentView.set_view_resolution`)。
        """
        return self.view.set_view_resolution(world_per_pixel, size_hint, focus)

    def body_meta(self, body) -> dict:
        """1Bodyぶんの軽量な状態(`DocumentView.body_meta`)。MCPの `bodies[]` もここを通る。"""
        return self.view.body_meta(body)

    def body_revision(self, body_id: str) -> int:
        """そのBodyの版(`DocumentView.body_revision`)。"""
        return self.view.body_revision(body_id)

    # ---------------------------------------------------------------- 選択の全リセット
    def _reset_selection_if_needed(self, verb: str) -> None:
        """形を変えるverbの確定後、選択を全リセットする(統一契約)。

        **カタログの `resets_selection` が正**(`duocad/catalog.py`)。面・辺・領域の添字は
        形状変更で壊れるので、後継を当てにいくより**全部空にする**ほうが単純で正しい。

        呼び出し側は**成功した後にだけ**呼ぶこと — 失敗した verb は操作ログに積まれず
        この関数にも到達しないので、「失敗しても選択は保持される」は自然に満たされる。

        **プレビュー(`preview` → `speculative()`)の中では見かけ上効かない** — ここが呼ばれても
        `with speculative(doc):` を抜ける時点で選択そのものが巻き戻り対象なので元へ戻る
        (イベントも `_suppress_events` で配らない)。確定(Enter)した本物の呼び出しだけが
        実際にリセットする。
        """
        command = catalog.BY_NAME.get(verb)
        if command is not None and command.resets_selection:
            selection.clear_selection(self.document)

    # ---------------------------------------------------------------- 操作
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

    def set_sketch_visible(
        self, sketch_id: str, visible: bool, client_op: Optional[int] = None
    ) -> dict:
        """ブラウザのスケッチ一覧からの表示・非表示切り替え。`set_visible`(ボディ)と同じ形で、
        実体は `selection.set_sketch_visible`(verbではない。ログに載らず undo でも戻らない)。
        イベントは `sketch_id` を持つ `{"change": "visibility", ...}` で、効果は配信される状態の
        `sketches[].visible` に載る。存在しない `sketch_id` は `KeyError`。
        """
        changed = selection.set_sketch_visible(self.document, sketch_id, bool(visible))
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
    #      "selection": {"items": [...], "count": int},
    #      "client_op": int | None}
    #
    # **戻り値**: 変更する4つは `self._emit(...)` の戻り —— つまり**発火したイベントそのもの**
    # (`{"change","selection","client_op"}`)。`get_selection` だけは発火しないので
    # `Selection.to_dict()`(`{"items","count"}`)を返す。**この2つは形が違う**ので、
    # 呼び出し側は `["selection"]` を挟むかどうかを取り違えないこと。
    def get_selection(self) -> dict:
        """現在の選択集合を素の値で返す(`Selection.to_dict()`)。発火はしない。"""
        return selection.get_selection(self.document).to_dict()

    def measure(self) -> dict:
        """いまの選択を測る(`measure.measure_selection`)。verb ではない —— 発火も記録もしない。

        MCP の `measure` と WebSocket の `measure` はどちらもここを通る(画面の「測定」と
        AI が読む値を同じ計算から出す)。
        """
        return measure_selection(self)

    def set_selection(
        self,
        refs: Optional[list] = None,
        predicate: Optional[dict] = None,
        client_op: Optional[int] = None,
    ) -> dict:
        """選択を置き換え、変更イベントを発火して**そのイベント**を返す。"""
        selection.set_selection(self.document, refs=refs, predicate=predicate)
        return self._emit(self._selection_event(client_op))

    def modify_selection(
        self,
        add: Optional[list] = None,
        remove: Optional[list] = None,
        client_op: Optional[int] = None,
    ) -> dict:
        """差分編集。足し引きして、変更イベントを発火して**そのイベント**を返す。"""
        selection.modify_selection(
            self.document,
            add=() if add is None else add,
            remove=() if remove is None else remove,
        )
        return self._emit(self._selection_event(client_op))

    def toggle_selection(self, refs: list, client_op: Optional[int] = None) -> dict:
        """参照ごとにあれば外し無ければ足す。変更イベントを発火して**そのイベント**を返す。"""
        selection.toggle_selection(self.document, refs)
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
    @_edits_document
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

        **`_applied_count` は自分で退避する** —— `speculative` が戻すのは文書であって
        `Session` の持ち物ではない。テセレーションは **`view` を通さず** `kernel.tessellate` を
        直に呼ぶ(`view` のキャッシュを投機的な形で汚さないため)。剪定のほうは
        `_view_inputs()` の `previewing` が止める。
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
        if catalog.BY_NAME[verb].preview_session is not None:
            # 文書を変えずに結果だけ計算するメソッドなので投機実行で包まない。
            return {"verb": verb, "bodies": [], **getattr(self, method_name)(**values)}

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
                        # **プレビューも画面の粗さで測る**(本物のBodyと並べて出るので、
                        # ここだけ土台の粗さのままだと確定の前後で滑らかさが跳ねる)。
                        "geometry": kernel.tessellate(
                            body.shape,
                            self.view.view_deviation,
                            self.view.view_angular_deflection,
                        ),
                    })
        finally:
            self._suppress_events -= 1
            self._applied_count = applied_count
        return {"verb": verb, "bodies": bodies_out}

    @_edits_document
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

    @_edits_document
    def redo(self) -> dict:
        operation = self.document.log.redo(self.document)
        if operation is None:
            raise ValueError("やり直せる操作が無い")
        self._reset_selection_if_needed("redo")
        return self._emit({"verb": "redo", "seq": operation.seq, "redone": operation.verb})

    # ---------------------------------------------------------------- verb の適用
    def apply(self, verb: str, args: Optional[dict] = None) -> dict:
        """verb を1つ適用し、変更イベントを発火して**そのイベント**を返す。

        **verb を掛ける唯一の口**である。WebSocket(`net/ws.py`)・MCP とアプリ内 AI
        (`mcp_server.DocumentTools`)・`session.fillet(2.0)` の形の呼び出し(末尾の `_verb_method`)が
        全部ここを通るので、実行中の断り・選択のリセット・自動保存・配信はどの経路でも同じに効く。

        実体は verb のモジュールに置いた入口(`duocad/entry.py`)。**引数の検査はしない** —— 何を必須と
        するかは口ごとに違う(`catalog.Command.input_schema` / `ws_schema`)ので、呼ぶ側が済ませて来る。
        """
        function = entry_for(verb)
        if self._optimizing:
            raise DocumentBusy(
                f"形状最適化の実行中は文書を変えられない({verb})。終わるか中断してから操作すること"
            )
        return self._after_logged_verb(function(self.document, **(args or {})))

    def _after_logged_verb(self, extra: Optional[dict] = None) -> dict:
        """直前にログへ積まれたverbについて変更イベントを発火する。"""
        operation = self.document.log.entries[-1]
        self._applied_count += 1
        self._reset_selection_if_needed(operation.verb)
        payload: dict[str, Any] = {"verb": operation.verb, "seq": operation.seq}
        if extra:
            payload.update(extra)
        return self._emit(payload)

    def preview_envelope_regions(self, radius: float = 0.0, offset: float = 0.0) -> dict:
        """`envelope_regions` の確定前の姿。**文書を変えない**(`Session.preview` の経路)。

        分裂しても失敗にせず、全部の面の境界と面の数(`pieces`)を返す —— 分裂を知らせるのは
        確定時のトーストで、プレビューは線の色で見せる(本人決定)。消えたときは `pieces=0`。
        """
        refs = selected_regions(self.document)
        sketch_id, pieces = envelope_pieces(self.document, refs, float(radius), float(offset))
        plane = sketch.get_sketch(self.document, sketch_id).plane
        entities = [
            {"kind": entity["kind"], "params": dict(entity["params"])}
            for piece in pieces
            for entity in kernel_sketch.face_boundary_entities(plane, piece)
        ]
        return {"sketch_ghost": {"sketch_id": sketch_id, "entities": entities, "pieces": len(pieces)}}
    # ---------------------------------------------------------------- 形状最適化を走らせる
    # 条件を置く4 verb の**続き**(`duocad/optimize_run.py`)。ここにあるのは委譲と、
    # 「中断の旗」と「結果を1つのボディとして足したことの通知」だけである。
    def optimize_conditions(self) -> dict:
        """4条件がそろって走らせられるか(`optimize_run.conditions_report`)。"""
        return optimize_run.conditions_report(self.document)

    def optimize_resolution(self, rmin_mm: Any = None) -> dict:
        """要素サイズの上限・下限・既定。

        `rmin_mm`(密度フィルタ半径)は**この版では扱えない**(二値探索は
        密度フィルタを持たない)。届いたら黙って無視せず `OptimizeInputError` にする
        (`net/ws.py` の `optimize_resolution` コマンドはこれを `ValueError` として拾い、
        `{"error": ...}` の非トースト表示へ運ぶ——既存の「設定の途中」表示と同じ経路)。
        """
        if rmin_mm is not None:
            raise optimize_run.OptimizeInputError(
                "rmin_mm(最小部材幅/密度フィルタ半径)はこの版では扱えない。"
                "二値探索は密度フィルタを持たないので、要素サイズを直接指定すること"
            )
        return optimize_run.resolution_limits(self.document)

    def cancel_optimization(self) -> dict:
        """走っている最適化に降りるよう伝える(**旗を立てるだけ**)。

        実際に降りるのは次に進捗が出た時点で、`run_optimization` が `OptimizeCancelled` を
        投げる。**旗は次の `begin_optimization` が畳む** —— ここで畳むと、中断の直後に
        立て直された旗を新しい実行が拾ってしまう。
        """
        self._optimize_cancel.set()
        return {"cancelled": True}

    @property
    def optimizing(self) -> bool:
        """形状最適化が走っているか(`begin_optimization` 〜 `end_optimization`)。"""
        return self._optimizing

    def optimization_status(self) -> dict:
        """走っているか・中断を頼まれているか(`net/ws.py` の `optimize_status` の中身)。

        `cancelling` は**走っている間だけ**載せる —— 旗は次の開始まで立ったままなので、
        走っていないのに「中断中」と出すと次の実行が中断表示から始まって見える。
        """
        status: dict = {"running": self._optimizing}
        if self._optimizing and self._optimize_cancel.is_set():
            status["cancelling"] = True
        return status

    def begin_optimization(self) -> None:
        """走らせる前に**イベントループのスレッドで**呼ぶ。中断の旗を畳み、文書を変える操作を断り始める。

        **旗はワーカースレッドの中で畳まない** —— 実行を別スレッドへ投げてから走り出すまでの間に届いた
        中断が、走り出した実行に消されて結果が足される(issue #35)。
        """
        if self._optimizing:
            raise ValueError("最適化が既に走っている(中断してから)")
        self._optimize_cancel.clear()
        self._optimizing = True

    def end_optimization(self) -> None:
        """`begin_optimization` の対。文書を変える操作をまた受け付ける。"""
        self._optimizing = False

    def run_optimization(
        self,
        *,
        target_safety_factor: Any = optimize_run.DEFAULT_TARGET_SAFETY_FACTOR,
        element_size_mm: Any = None,
        min_width_mm: Any = None,
        max_evaluations: Any = None,
        progress: Any = None,
        debug: Any = None,
        debug_kinds: Any = None,
        rmin_mm: Any = None,
        max_iter: Any = None,
        material_min_mm3: Any = None,
        material_max_mm3: Any = None,
    ) -> dict:
        """形状最適化を端から端まで走らせ、結果をメッシュボディとして足す。

        **重い(数十秒〜数分)。** 呼び出し側はイベントループを塞がないよう別スレッドで
        回すこと(`asyncio.to_thread`)。`progress(event: dict)` はそのスレッドから呼ばれる。

        `debug` / `debug_kinds` は開発用の可視化(画面の `?debug=space` / `?debug=anchors` /
        `?voxel=1`)の材料をそのまま `optimize_run.run()` へ渡す口。**頼まなければ費用ゼロ**で、
        結果にも文書にも1ビットも影響しない。

        `rmin_mm` / `max_iter` / `material_min_mm3` / `material_max_mm3` は**旧SIMP方式の
        引数で、この版では扱えない**(旧引数が外部から届いたら黙って無視せず、
        不支持と分かるエラーにする」)。キーワードとして受け続けているのは、届いたときに
        `TypeError`(引数名を知らない)ではなく`OptimizeInputError`(意味を説明した文)で
        断るため —— 呼び出し側(`net/ws.py`)が転送さえすれば、ここが一箇所で断る。

        戻り値は `optimize_run.run()` のものに、成功時だけ `"applied"`(変更イベントの封筒)を
        足したもの。**`state != "done"` のときは文書を1ビットも変えていない**。

        **呼び出し側が `begin_optimization` を済ませていなければ、ここで開始と終了を囲む**(テスト・
        スクリプトの直呼び)。`net/ws.py` は別スレッドへ投げる前に開始しておく(issue #35)。
        """
        unsupported = {
            "rmin_mm": rmin_mm,
            "max_iter": max_iter,
            "material_min_mm3": material_min_mm3,
            "material_max_mm3": material_max_mm3,
        }
        given = sorted(key for key, value in unsupported.items() if value is not None)
        if given:
            raise optimize_run.OptimizeInputError(
                f"{', '.join(given)} はこの版では扱えない(二値探索は密度フィルタ・SIMP反復・"
                "材料量の直接指定を持たない。目標安全率・要素サイズ・評価予算(max_evaluations)"
                "を使うこと)"
            )
        options = optimize_run.OptimizeOptions(
            target_safety_factor=float(target_safety_factor),
            element_size_mm=None if element_size_mm is None else float(element_size_mm),
            min_width_mm=None if min_width_mm is None else float(min_width_mm),
            max_evaluations=None if max_evaluations is None else int(max_evaluations),
        )
        owned = not self._optimizing
        if owned:
            self.begin_optimization()
        try:
            result = optimize_run.run(
                self.document,
                options,
                progress=progress,
                should_cancel=self._optimize_cancel.is_set,
                debug=debug,
                debug_kinds=debug_kinds,
            )
        finally:
            if owned:
                self.end_optimization()
        if result.get("warning"):
            # 既存の警告の口へ載せる(新しい運び方を作らない)。
            self.warnings.append(result["warning"])
        if result["state"] != "done":
            return result
        applied = self._after_logged_verb(
            {
                "body_id": result["body_id"],
                "name": result["name"],
                "triangle_count": result["triangle_count"],
            }
        )
        return dict(result, applied=applied)

    # ---------------------------------------------------------------- E: ファイル操作
    #
    # **保存形式は操作ログのJSON**(実装計画書§3.1)。形状はそこから導出されるので、
    # 開くたびに再生経路が検査されることになる —— 「保存したものが開けない」が
    # 起きるとしたら、それは再生が壊れているということであり、隠さない方がよい。
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
        # **正の判定はここ**。文字列検査をすり抜けるものが
        # あっても、シンボリックリンク越しの脱出も含めて最終的にここで止まる。
        if not resolved.is_relative_to(root):
            raise ValueError(f"models/の外を指している: {text!r} -> {resolved}")
        return resolved

    def save_document(self, name: Any = None) -> dict:
        """操作ログをJSONで書き出す。**verbではない**(文書を変えないのでログに載らない)。

        **契約は2つに割れている**:

        - `save_document()`(引数なし) … **いま編集しているファイルへ上書き**する。
          編集中のファイルが無ければ拒否する
        - `save_document(name)` … **新しい名前でのみ保存**する。**既に在る名前は拒否**する
          (いま編集しているファイルの名前であっても拒否する)

        **`name == self._current_name` なら通す、という例外条項を置かない**(本人決定)。
        一覧の行から上書きを送るUIが無くなった今、`name` の意味は「新しい名前」1つに
        固定できる。例外を残すと、後から別のUIを足したときに**そこだけ穴が開く**
        (しかも、その穴が開いたことは誰にも見えない)。

        塞いでいるのは「**編集中でないファイルを上書きできる**」穴である。誤クリック1回で
        無関係なモデルが今の文書に置き換わり、操作ログが唯一の正である以上、失ったものは
        復旧できない。**拒否の文はどうすればできるかまで言う** —— 呼び出し側の封筒
        (`net/ws.py` の `{"type":"error"}`)が `{型名}: {文}` を前置するので、
        文だけで人に通じる形で書くこと。

        `name` は**ファイル名だけ**(`models_dir` 配下に固定)。区切り文字・絶対パス・
        `..` は `_resolve_document_name` が拒否する。**その検査を先に通す** ——
        「既に在るか」を見るより前にパスの安全性を決着させる(models/ の外を指す名前は、
        存在の有無を問う前に断る)。

        **呼び手は自動保存が主**(issue #5、2026-09-16): 文書が変わるたび `autosave()` が
        引数なしで呼び、無題なら `untitled-<n>.json` を `name` に渡す。Ctrl+S は引数なしを
        手で呼ぶだけ(同じ内容を書き直す)。人が名前を決める口は `rename_document`。
        """
        if name is None:
            if self._current_name is None:
                raise ValueError(
                    "編集中のファイルが無い"
                    "(ファイルパネルで名前を付けて保存すれば、以後はそこへ上書きできる)"
                )
            target = self._resolve_document_name(self._current_name)
        else:
            target = self._resolve_document_name(name)
            if target.exists():
                # **どちらの枝も拒否する**。分かれるのは「次に何をすればよいか」の文言だけで、
                # 「現在のファイルなら通す」という抜け道はここに無い(上記docstring)。
                raise ValueError(
                    f"{target.name} は既にある(いま編集中のファイルで、自動保存とCtrl+Sが書いている)"
                    if target.name == self._current_name
                    else f"{target.name} は編集中ではない(先に開けば上書きできる)"
                )
        payload = self.document.log.to_json()
        with self._save_lock:
            # **一時ファイルへ書いてから差し替える**。素の上書きは既存の
            # 中身を先に切り詰めるので、途中で落ちると**唯一の永続形式である前回の保存が
            # 半端なJSONで潰れる**(形状は導出物なので、これを失うと復旧の手立てが無い)。
            # `os.replace` は同じディレクトリ内なら不可分に差し替わる。
            staging = target.with_name(target.name + ".writing")
            staging.write_text(payload, encoding="utf-8")
            os.replace(staging, target)
            # **書けてから現在ファイルにする**。先に控えると、書き込みが落ちたときに
            # 「存在しないファイルを編集中」という状態が残り、次の引数なし保存が
            # 黙って新しいファイルを作る(拒否されるべき経路が通ってしまう)。
            self._current_name = target.name
            self._unsaved = False
        return {
            "action": "save",
            "path": str(target),
            "name": target.name,
            "bytes": target.stat().st_size,
            "operations": len(self.document.log.entries),
        }

    @property
    def saved(self) -> bool:
        """今の文書がそのままファイルに載っているか(`state()["saved"]`)。

        **自動保存が書けなかったときだけ False**。無題で何も変えていない文書は True
        (書くものが無い —— 最初の変更で名前が付いて書かれる)。
        """
        return not self._unsaved

    def _unused_untitled_name(self) -> str:
        """`untitled-1.json` から順に、`models_dir` にまだ無い最初の名前。"""
        self.models_dir.mkdir(parents=True, exist_ok=True)
        number = 1
        while (self.models_dir / f"untitled-{number}.json").exists():
            number += 1
        return f"untitled-{number}.json"

    def autosave(self) -> bool:
        """今の文書をディスクへ書く(**1操作ごとの自動保存**。issue #5、本人判断 2026-09-16)。

        `_emit` が verb の応答のたびに呼ぶ(undo / redo も含む —— ファイルは常に今の文書の
        鏡で、redo の山は残さない)。**無題の文書は最初の変更で `untitled-<n>.json` の名前を
        自動で付ける**(以後はそこへ上書き。人が名前を決めるのは `rename_document`)。

        **失敗しても例外にしない** —— 文書の変更は既に確定しており、巻き戻せない。書けな
        かったことは警告と `saved=False`(画面の「未保存」札)で知らせる。戻り値は書けたか。
        **捕まえるのは種類を問わない** —— Windows では別のプログラムがファイルを開いている
        だけで `os.replace` が `PermissionError` になる(実測)。ここから例外が漏れると、
        最適化の経路では結果も終了の `optimize_status` も配られず画面が実行中のまま固まる。

        費用は操作ログ全体のJSON化と書き込み(差分ではない)。実測は 4.8MB の文書で 0.02秒
        なので、debounce も差分保存も入れていない。
        """
        try:
            if self._current_name is None:
                self.save_document(self._unused_untitled_name())
            else:
                self.save_document()
        except Exception as error:  # noqa: BLE001 - 書けない理由は何であれ画面へ出す
            self._unsaved = True
            self.warnings.append(f"自動保存できなかった: {type(error).__name__}: {error}")
            return False
        return True

    def rename_document(self, name: Any) -> dict:
        """いま編集しているファイルの名前を変える(ディスク上で移す)。**verbではない**。

        自動保存が名前を `untitled-<n>.json` で決めるので、人が名前を付ける口はこれになる
        (ファイルパネルの名前欄。issue #5)。`name` が既に在れば拒否 —— 上書きの口は持たない
        (`copy_document` と同じ線)。**無題で何も変えていない文書**(ファイルがまだ無い)は、
        その名前で空の文書を書く(「この文書をこの名前にする」の意味は同じで、書く先が
        無ければ作るだけ)。
        """
        target = self._resolve_document_name(name)
        if target.exists():
            raise ValueError(f"{target.name} は既にある(別の名前にする)")
        if self._current_name is None:
            return dict(self.save_document(target.name), action="rename", previous=None)
        with self._save_lock:
            previous = self._current_name
            source = self._resolve_document_name(previous)
            if source.exists():
                os.replace(source, target)
            else:
                # 編集中のファイルが外で消されていた。中身は文書が持っているので書き直す。
                target.write_text(self.document.log.to_json(), encoding="utf-8")
            self._current_name = target.name
            self._unsaved = False
        return {
            "action": "rename",
            "path": str(target),
            "name": target.name,
            "previous": previous,
            "bytes": target.stat().st_size,
            "operations": len(self.document.log.entries),
        }

    @_edits_document
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

        # **開けたものが編集中のファイルになる**(以後、引数なしの `save_document()` は
        # ここへ書く)。再生が落ちたときはこの行まで来ないので、現在名も差し替わらない。
        self._replace_document(document, current_name=source.name)
        return self._emit({
            "change": "document",
            "action": "open",
            "path": str(source),
            "name": source.name,
            "operations": len(log.entries),
        })

    @_edits_document
    def new_document(self) -> dict:
        """空の文書へ差し替える(「新規」)。**verbではない**(ログに載らない)。

        **今の文書は捨てられ、undoでは戻らない**(`open_document` と同じ)。確認は挟まない ——
        一覧からのクリック1つで別の文書へ移れるのに、空の文書へ移るときだけ確認を挟むと
        挙動が割れる(`web/src/FilesPanel.svelte` の「開くのに確認ダイアログを挟まない」)。

        **捨てる前に保存はしない** —— 名前つきの文書は1操作ごとに自動保存されていて
        (`autosave`)、失うものが無い。

        **編集中のファイルも無くなる**(`current_name=None`)。空の文書はどこにも書かれて
        おらず、次に文書を変えた瞬間に自動保存が `untitled-<n>.json` を作る(それまで
        引数なしの `save_document()` は拒否される)。
        """
        self._replace_document(Document(), current_name=None)
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

    def _replace_document(self, document: Document, *, current_name: Optional[str]) -> None:
        """今の文書を `document` へ差し替える(`open_document` / `new_document` の共通部分)。

        **`current_name` は既定値を持たない**(キーワード必須)。文書を差し替える経路は
        必ず「この後どのファイルを編集していることになるか」を1つ決める —— 既定で
        持ち越すと、`new_document` の後に前の文書のファイル名が残って、引数なしの
        `save_document()` が**捨てたはずのファイルを空の文書で潰す**。

        差し替えでは配りものを `view.reset()` で作り直す —— Bodyのidは再生で作り直されるが、
        同じidが再び現れる可能性があり、古い形状を指したキャッシュが残っていると別の文書の
        形を配ることになる。

        **選択集合はここで触らない** —— 選択は `Document` が持つので、差し替えた時点で
        新しい文書の空の選択になっている。
        """
        self.document = document
        self._current_name = current_name
        self._unsaved = False  # 開いた/空にした文書に、書けなかった変更は無い
        # キャッシュの破棄は**`view` の1呼び出し**(何を捨てて何を残すかは `DocumentView.reset`
        # が持つ。とくに版の数え上げを消さない理由はそちらのdocstring)。
        self.view.reset(document)
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

    def copy_document(self, source: Any, dest: Any) -> dict:
        """`models_dir` の `source` を `dest` へ複製する。**verbではない**(文書を変えない)。

        ディスク上のJSONを写すだけで、**いま編集している文書も `current_name` も変わらない**
        (`open_document` とは別の操作。複製元を開いていなくてもよい)。`dest` が既に在れば
        拒否する —— 上書きの口は持たない(`save_document(name)` と同じ線。issue #17)。
        どちらの名前も `_resolve_document_name` を通す(`models_dir` の外は指せない)。
        """
        origin = self._resolve_document_name(source)
        target = self._resolve_document_name(dest)
        if not origin.exists():
            raise FileNotFoundError(f"複製元が無い: {origin.name}")
        if target.exists():
            raise ValueError(f"{target.name} は既にある(別の名前にする)")
        # `save_document` と同じく一時ファイル経由で差し替える(途中で落ちても半端な
        # ファイルが一覧に出ない。`list_documents` は `.writing` を省く)。
        staging = target.with_name(target.name + ".writing")
        staging.write_bytes(origin.read_bytes())
        os.replace(staging, target)
        return {
            "action": "copy",
            "path": str(target),
            "name": target.name,
            "source": origin.name,
            "bytes": target.stat().st_size,
        }

    def delete_document(self, name: Any) -> dict:
        """`models_dir` の `name` をディスクから削除する。**verbではない**(文書を変えない)。

        **いま編集しているファイルは拒否する**(拒否の文はどうすればできるかまで言う) ——
        消した直後の自動保存/上書きが「無いファイルへ書く」になり、消したはずのものが
        黙って復活する。無ければ FileNotFoundError。確認は挟まない(画面側が行内で聞く。
        `FilesPanel.svelte`)。`models/.trash` への退避はしない(issue #17)。
        """
        target = self._resolve_document_name(name)
        if target.name == self._current_name:
            raise ValueError(
                f"{target.name} は編集中(別のファイルを開くか「新規」にしてから削除する)"
            )
        if not target.exists():
            raise FileNotFoundError(f"削除するファイルが無い: {target.name}")
        target.unlink()
        return {"action": "delete", "path": str(target), "name": target.name, "bytes": 0}

    def export_step_file(self, path: Any) -> dict:
        """STEPを書き出す。**ログに載らない**(文書を変えないのでverbではない)。

        対象は**いま表示中のBodyだけ**(引数で選べない)。非表示で除外した件数は
        `skipped_hidden` として必ず応答に載る(0件でも載る)。
        """
        return {"action": "export_step", **io_step.export(self.document, self._resolve_export_path(path), "step")}

    def _resolve_export_path(self, path: Any) -> Path:
        """書き出し先を絶対パスにする。

        **相対は `exports_dir` 基準**(起動ディレクトリに依存しない)。絶対パスはそのまま
        (テスト・MCPの明示パス用)。相対で `exports_dir` の外へ逃げる経路は拒否する。
        """
        text = str(path)
        stripped = text.strip()
        if stripped == "":
            raise ValueError("書き出し先が空")
        if text != stripped:
            raise ValueError(f"前後の空白は不可: {text!r}")
        candidate = Path(text).expanduser()
        if candidate.is_absolute():
            target = candidate.resolve()
            if not target.parent.exists():
                raise ValueError(f"書き出し先のディレクトリが無い: {target.parent}")
            return target
        self.exports_dir.mkdir(parents=True, exist_ok=True)
        root = self.exports_dir.resolve()
        resolved = (root / text).resolve()
        if not resolved.is_relative_to(root):
            raise ValueError(f"export/の外を指している: {text!r} -> {resolved}")
        resolved.parent.mkdir(parents=True, exist_ok=True)
        return resolved

    def export_stl_file(self, path: Any) -> dict:
        """STLを書き出す。粗さは `io_step.STL_DEVIATION` 固定で、人にもAIにも選ばせない。

        対象は**いま表示中のBodyだけ**(引数で選べない)。非表示で除外した件数は
        `skipped_hidden` として必ず応答に載る(0件でも載る)。応答の `deviation` は
        **書いた粗さの記録**であって入力ではない。
        """
        exported = io_step.export(self.document, self._resolve_export_path(path), "stl")
        # `deviation` は `bytes` の次(末尾に回すと画面やログの見た目が変わる)。
        return {
            "action": "export_stl",
            "path": exported["path"],
            "bytes": exported["bytes"],
            "deviation": io_step.STL_DEVIATION,
            "body_ids": exported["body_ids"],
            "skipped_hidden": exported["skipped_hidden"],
        }

    def take_warnings(self) -> list[str]:
        """溜まった警告を取り出して空にする(出したら消す)。

        **リストを束ね直さず中身を空にする**(`self.warnings = []` にしない)。`DocumentView` が
        同じリストオブジェクトへ積むので、ここで別のリストへ差し替えると、以後 `view` が積んだ
        警告が誰にも読まれないまま消える。
        """
        warnings = list(self.warnings)
        self.warnings.clear()
        return warnings


def _verb_method(verb: str, function: Any) -> Any:
    """`session.fillet(2.0)` の形で verb を呼べるメソッド。**中身は `Session.apply` だけ**。

    位置引数・既定値は入口(`duocad/entry.py`)の引数の並びに従う。Python から文書を組み立てる側
    (テスト・スクリプト)のための書き方で、WebSocket と MCP は `apply` を直接呼ぶ。
    """
    signature = inspect.signature(function)

    def method(self: "Session", *args: Any, **kwargs: Any) -> dict:
        bound = signature.bind(self.document, *args, **kwargs)
        return self.apply(verb, dict(list(bound.arguments.items())[1:]))

    method.__name__ = verb
    method.__qualname__ = f"Session.{verb}"
    method.__doc__ = function.__doc__
    method.edits_document = True  # type: ignore[attr-defined]
    return method


for _verb, _function in entries().items():
    if hasattr(Session, _verb):
        raise RuntimeError(f"`Session.{_verb}` が verb の入口と同じ名前で既に定義されている")
    setattr(Session, _verb, _verb_method(_verb, _function))
