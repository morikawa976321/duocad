"""1つの文書を持つサーバ側の状態(`Session`)。WebSocketの接続とは独立(接続が切れても文書は残る)。

verbの呼び出し・選択・パラメータ・操作ログ・保存/開くを、1つの文書の上でまとめる。
**イベントループのスレッドだけが触る**前提(別スレッドから入る経路は `duocad/net/tools_on_loop.py` が畳む)。
配信は `duocad/net/ws.py` の仕事で、ここは変更を購読者へ知らせるところまで。配りもの(状態・版・キャッシュ)は
`duocad/view.py` の `DocumentView` が持つ(`session.view`)。

**`duocad/net/` を参照しない**(向きは `net/` -> ここの一方向)。**OCPを直接importしない**(境界はkernel.pyだけ)。
"""
from __future__ import annotations

import functools
import inspect
import base64
import binascii
import json
import os
import shutil
import threading
import time
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
from .measure import inspect_selection, measure_selection
from .oplog import OpLog
from .paths import DEFAULT_EXPORTS_DIR, DEFAULT_MODELS_DIR
from .speculative import speculative
from .verbs import import_step as import_step_verb
from .verbs.envelope_regions import envelope_pieces, selected_regions
from .view import DocumentView

#: Windowsの予約デバイス名(拡張子の有無に関わらず特別扱い。大文字小文字を区別しない)。
_RESERVED_WINDOWS_NAMES = frozenset({
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
})

class DocumentBusy(RuntimeError):
    """形状最適化を走らせている文書に、文書を変える操作が来た(`Session._edits_document`)。

    最適化は別スレッドで開始時の文書を持ったまま走り、終わりにその文書へ結果を足すので、その間に変えると
    古い条件の形が足される・Undo した条件のまま結果が入る(issue #26)。選択・表示の切り替え・保存・書き出し、
    別のファイルを開いて編集することは通す(結果は走らせた文書のファイルへ届く。`deliver_optimization_result`)。
    """


class NoDocument(ValueError):
    """文書を開いていない(トップ画面)のに、文書を変える操作が来た(`Session._edits_document`)。
    素通しすると最初の変更で自動保存が `untitled-<n>.json` を作り、「何も開いていない」が黙って「無題の文書」になる。
    """


THUMBNAIL_DIR = ".thumbnails"
#: 受け取るPNGの上限[バイト]。画面が撮るのは128px四方で、RGBAを圧縮せず持っても64KiB。
THUMBNAIL_MAX_BYTES = 256 * 1024
_PNG_SIGNATURE = bytes([0x89]) + b"PNG" + bytes([0x0D, 0x0A, 0x1A, 0x0A])


def _mtime_or_none(path: Path) -> Optional[float]:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _edits_document(method: Any) -> Any:
    """文書を変える `Session` のメソッドに付ける。形状最適化を走らせている文書なら `DocumentBusy` で断る。
    ブラウザ・MCP・アプリ内AIのどの経路も `Session` のメソッドを通るので、ここ1箇所で全部が止まる。
    """

    @functools.wraps(method)
    def guarded(self: "Session", *args: Any, **kwargs: Any) -> Any:
        self._require_open()
        if self._document_locked():
            raise DocumentBusy(
                f"形状最適化の実行中は文書を変えられない({method.__name__})。"
                "終わるか中断してから操作すること"
            )
        return method(self, *args, **kwargs)

    guarded.edits_document = True  # type: ignore[attr-defined]
    return guarded


#: プレビューできるverb -> (`Session` のメソッド名, 受け取ってよい引数名)。ここに無いverbはプレビューできない
#: (載せてよい条件は `catalog.Command.previewable`)。
PREVIEWABLE_VERBS: dict = catalog.previewable_verbs()

class Session:
    """1つの文書を持つサーバ側の状態。WebSocketの接続とは独立(接続が切れても文書は残る)。"""

    def __init__(
        self,
        step_path: Optional[Path],
        deviation: float,
        models_dir: Optional[Path] = None,
        exports_dir: Optional[Path] = None,
        opened: bool = True,
    ) -> None:
        self.document = Document()
        #: 文書を開いているか。False がトップ画面で、`self.document` は空のまま持ち、変更操作は `NoDocument` で断る(`_require_open`)。
        #: サーバは閉じて起動し(`mcp_server.main`)、Python から組み立てる側(テスト・スクリプト)は開いて始まる。
        #: 書き換えるのは `_replace_document` と `close_document` だけ。
        self._open = bool(opened)
        self.deviation = float(deviation)
        self.models_dir: Path = (
            DEFAULT_MODELS_DIR if models_dir is None else Path(models_dir)
        ).resolve()
        self.exports_dir: Path = (
            DEFAULT_EXPORTS_DIR if exports_dir is None else Path(exports_dir)
        ).resolve()
        #: `geometry()` や `_sketch_state` が積む警告。`view` と同じリストオブジェクトを共有する(吸い出す口は `take_warnings()` だけ)。
        self.warnings: list[str] = []
        #: いま編集しているファイルの名前(`models_dir` 配下のファイル名だけ。無ければ `None`)。これで `save_document` の契約を2つに割り、
        #: 「編集中でないファイルを上書きできる」穴をどのUIからも塞いでいる。
        #: 書き換えるのは `save_document(name)` の成功・`open_document` の成功・`_replace_document` の3箇所だけ。
        self._current_name: Optional[str] = None
        #: 編集中の文書のファイルを最初に書いた時刻(UNIX秒。`OpLog.to_json` の `created`)。`created` を持たない既存ファイルは
        #: None のまま上書きされ続ける(理由は docs/UI.md のファイル一覧の項)。
        self._created: Optional[float] = None
        self._applied_count = 0
        #: 自動保存で書けなかった変更があるか(issue #5)。通常は常に False で、書き込み失敗(ディスク・権限)のときだけ True。
        self._unsaved = False
        #: 保存の書き込みを直列にする。アプリ内AIの道具はワーカースレッドで動き(`net/ws.py` の `_ai_start`)、`apply` → `_emit` → 自動保存へ来るので、
        #: イベントループ側の保存と同じ一時ファイルへ同時に書く経路がある。
        self._save_lock = threading.Lock()
        #: 配りもの。文書を差し替えたら `view.reset()` を呼ぶ(`_replace_document`)。
        self.view = DocumentView(self.document, self.deviation, self.warnings)
        self._setup_floor_seq = 0  # load_step が積んだ最後のseq(これ以下はundoの対象外)
        self._listeners: list = []  # 文書が変わったときに呼ぶ購読者(下記 add_listener)
        self._suppress_events = 0  # >0 の間は `_emit` が配らない(プレビュー。`preview` 参照)
        #: 形状最適化の中断の旗。別スレッド(実行はワーカー、`cancel_optimization` はイベントループ)から触るので `Event`。
        self._optimize_cancel = threading.Event()
        #: 「探索を止めてこのステップを仕上げる」の要求(ステップの番号。負は最新。`None` は要求なし)。イベントループが書き、
        #: 実行のスレッドが読む(代入1回なので錠は要らない)。次の `begin_optimization` が畳む。
        self._finish_request: Optional[int] = None
        #: 直近の実行の各ステップ(`optimize_steps.OptimizationSteps`)と、その実行の文書・ファイル名。実行の後も残し、
        #: 画面が切り替えて見て、選んだステップを `finish_optimization_step` で仕上げる。
        self.optimization_steps: Any = None
        self._steps_document: Optional[Document] = None
        self._steps_name: Optional[str] = None
        #: 形状最適化が走っているか(`begin_optimization` 〜 `end_optimization`)。立っている間は走らせている文書だけを変える操作を断る
        #: (`_edits_document` / `_document_locked`)。イベントループのスレッドだけが書く。
        self._optimizing = False
        #: 走らせている文書(開始時の `self.document`)とそのファイル名・作成日時。別の文書へ切り替えても実行はこの `Document` を持ったまま
        #: 走り続け、結果は `deliver_optimization_result` がファイルへ届ける。
        self._optimize_document: Optional[Document] = None
        self._optimize_name: Optional[str] = None
        self._optimize_created: Optional[float] = None
        #: ファイルへ書けなかった結果(ファイル名 -> (文書, 作成日時))。次にその名前を開いたときディスクでなくこちらを開く。
        self._undelivered: dict[str, tuple[Document, Optional[float]]] = {}
        #: 別の文書を開いている間に終わった実行の、画面へまだ見せていない結果/失敗の封筒(ファイル名 -> 封筒。作って配るのは `net/ws.py`)。
        self.unseen_optimize_results: dict[str, dict] = {}
        if step_path is not None:
            self.load_step(step_path)

    # ---------------------------------------------------------------- 変更イベント(上り方向)
    def add_listener(self, callback: Any) -> None:
        """文書が変わったら `callback(applied: dict)` で知らせる(1操作につき1回)。
        コアが変更を発火しアプリ層が購読して描き直す。これが無いと、WebSocket以外の主体(MCPのAI)が文書を変えても
        ブラウザが古い絵を出し続ける。購読者は `Server`(WebSocket)。
        """
        self._listeners.append(callback)

    def remove_listener(self, callback: Any) -> None:
        """購読をやめる。登録されていなければ ValueError。
        `Server` を作り直す運用で古い購読者が残ると、閉じたループへ配信しようとして警告が溜まる。
        """
        self._listeners.remove(callback)

    @property
    def can_undo(self) -> bool:
        """次のundoが通るか。土台(部品の読み込み・編集用ボディ)は取り消せない。
        `state_meta()` と MCP の `describe_document` が同じ答えを返すための唯一の出どころ。
        """
        return self.document.log.can_undo and not self._at_setup_floor()

    @property
    def applied_count(self) -> int:
        """verbを適用した回数(undo / redo では動かない・単調増加)。

        `state_meta()` と MCP の `describe_document` の唯一の出どころ。操作ログの長さではない(undoで減るので、
        「undoで保存時点へ戻した文書」と「undoしてから別のverbを打った文書」が同じ値になる)。
        `web/src/App.svelte` の保存済み判定は、この「動かない」意味に依存している(そちらへ揃える)。
        `preview()` は投機実行のあいだ `_applied_count` を退避して戻すので、プレビューでこの値は動かない。
        """
        return self._applied_count

    @property
    def current_name(self) -> Optional[str]:
        """いま編集中のファイル名(`models_dir` 配下、無ければ `None`)。`state()["current_name"]` と同じ値。"""
        return self._current_name

    @property
    def document_open(self) -> bool:
        """文書を開いているか(False = トップ画面)。`state()["document_open"]` と同じ値。"""
        return self._open

    def _require_open(self) -> None:
        if not self._open:
            raise NoDocument(
                "文書を開いていない(トップ画面で「新規」か既存の文書を選んでから操作する)"
            )

    def _emit(self, applied: dict) -> dict:
        """購読者へ変更を知らせて `applied` をそのまま返す(呼び出し側は `return self._emit(...)`)。

        1人の購読者が落ちても他へ配り、操作は成功のままにする(確定した変更は巻き戻せない)。落ちたことは警告に残す。
        プレビュー中(`_suppress_events`)は誰にも配らない: 投機的に実行したverbも通るので、止めないと確定していない形が
        全クライアントへ配信され、MCPが実在しない中間状態を読む。呼び出し経路に関わらず止まるようここに置いてある
        (`Server._dispatching` の二重配信よけはWebSocket経由にしか効かない)。
        """
        if self._suppress_events:
            return applied
        # 文書が変わったら配る前にディスクへ書く(1操作ごとの自動保存。issue #5)。対象は verb の応答(`verb` キー)だけで、
        # 選択・可視性(`change` キー)は載らない。WebSocket・MCP・形状最適化のどの経路も通る唯一の合流点。
        # 配る前に書くのは、配りもの(`state.saved` / `current_name`)が保存の結果を含むため。
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
        """STEPを読み、ソリッドごとに参照ボディ+編集用コピーを作る(中身は verb `import_step`。pytestが直に呼ぶときだけ通る)。

        土台(undoの対象外)はverb名でなくseqの下限で区切る(`_setup_floor_seq`)。verb名で判定すると、後から
        ユーザー操作としても呼ばれうるverbが末尾に来た瞬間に以後の `undo` が誤って拒否される。
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
    # 実体は `duocad/view.py`。ここは1行の委譲だけ。`can_undo` / `applied_count` / 「プレビュー中か」の持ち主は
    # `Session` なので、委譲のときにこちらから渡す(`view` は `Session` を見ない)。
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
        """配りものに、いま編集しているファイル名・保存状態(`saved`)・開いているかを足す(`view` の持ち物ではない)。

        `state()` と `state_meta()` の両方を通すのは、片方だけに載って「`get_state` では見えるのに配信では見えない」を
        防ぐため。載せないとリロードで名前だけ失われ、ヘッダが「(無題)」のまま自動保存と Ctrl+S が現在のファイルへ書き続ける。
        """
        state["current_name"] = self._current_name
        state["saved"] = self.saved
        state["document_open"] = self._open
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
        """ビューポートのズームに合わせて目標のテセレーション精度を決め直す。文書は変わらない(ログにもundoにも載らない)。
        戻り値が True のときだけ配信し直せばよい(`duocad/net/ws.py`)。`self.deviation`(STL書き出しが使う)は動かさない。
        `focus` は face単位の高精度化(`{body_id: [面添字]}`)。省略すれば全面が細かい側(`DocumentView.set_view_resolution`)。
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
        """形を変えるverbの確定後、選択を全リセットする。カタログの `resets_selection` が正(`duocad/catalog.py`)。
        面・辺・領域の添字は形状変更で壊れるので、後継を当てにいかず全部空にする。呼び出し側は成功した後にだけ呼ぶ。
        プレビュー(`speculative()`)の中では抜けるときに選択ごと巻き戻るので効かない。
        """
        command = catalog.BY_NAME.get(verb)
        if command is not None and command.resets_selection:
            selection.clear_selection(self.document)

    # ---------------------------------------------------------------- 操作
    def set_visible(self, body_id: str, visible: bool, client_op: Optional[int] = None) -> dict:
        """パーツ一覧からの表示・非表示切り替え。コアの `selection.set_visible` を呼ぶだけ(docs/DESIGN_DECISIONS.md「層の分け方」)。

        verbではないので操作ログに積まれず、undo / redo では戻らない(理由は `selection.set_visible`)。
        選択と同じ `change` 系のイベントを発火する(verbの `applied` とはキーで区別):

            {"change": "visibility", "body_id": .., "visible": .., "previous": .., "changed": ..,
             "client_op": int | None}

        効果は配信される状態の `bodies[].visible` に載る。存在しない `body_id` は `KeyError`(documentは変更しない)。
        """
        changed = selection.set_visible(self.document, body_id, bool(visible))
        return self._emit({"change": "visibility", "client_op": client_op, **changed})

    def set_sketch_visible(
        self, sketch_id: str, visible: bool, client_op: Optional[int] = None
    ) -> dict:
        """ブラウザのスケッチ一覧からの表示・非表示切り替え(`selection.set_sketch_visible`。`set_visible` と同じ形)。
        イベントは `sketch_id` を持つ `{"change": "visibility", ...}`。存在しない `sketch_id` は `KeyError`。
        """
        changed = selection.set_sketch_visible(self.document, sketch_id, bool(visible))
        return self._emit({"change": "visibility", "client_op": client_op, **changed})

    # ---------------------------------------------------------------- 共有される選択集合
    # `set_visible` と同じ上り方向に乗せる: 変更 → `Session._emit` → `add_listener` の購読者(`Server._on_core_change`) → 全接続へ配信。
    # `state_meta()` に `"selection"` を載せない限りAIが書き換えた選択は画面へ届かない(MCP起点の変更では `applied` を落として配る)ので、
    # イベントと状態の両方を通す。
    # 発火するイベント(選択はverbではないので `applied` とはキーで区別): {"change": "selection",
    #   "selection": {"items": [...], "count": int}, "client_op": int | None}
    # 戻り値: 変更する4つは `self._emit(...)` の戻り(発火したイベントそのもの)、`get_selection` だけは発火せず
    # `Selection.to_dict()`(`{"items","count"}`)を返す。この2つは形が違うので `["selection"]` を挟むかどうかを取り違えないこと。
    def get_selection(self) -> dict:
        """現在の選択集合を素の値で返す(`Selection.to_dict()`)。発火はしない。"""
        return selection.get_selection(self.document).to_dict()

    def measure(self) -> dict:
        """いまの選択を測る(`measure.measure_selection`)。MCP の `measure` と WebSocket の `measure` が同じ計算を通る。"""
        return measure_selection(self)

    def inspect_selection(self) -> Optional[dict]:
        """選択がちょうど1項目ならその寸法、それ以外は None(`measure.inspect_selection`。画面の選択情報の表示)。"""
        return inspect_selection(self)

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
        """verbを投機的に実行して形だけ測り、文書を元へ戻す(巻き戻しは `duocad.speculative.speculative`)。操作ログにも
        イベントにも出ない。

        戻り値:
            {"verb": str,
             "bodies": [{"body_id","name","face_count","volume","geometry"}, ...]}

        `bodies` は形が変わった可視のBodyだけ。`geometry` は `state`/`delta` の同名フィールドと同じ形。
        プレビューできるverbは `PREVIEWABLE_VERBS` のものだけ(ソケットの文字列で任意のメソッドを呼べる作りにしない)。
        `_applied_count` は自分で退避する(`speculative` が戻すのは文書だけ)。テセレーションは `view` を通さず
        `kernel.tessellate` を直に呼ぶ(`view` のキャッシュを投機的な形で汚さないため)。剪定は `_view_inputs()` の `previewing` が止める。
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
                        # プレビューも画面の粗さで測る(本物のBodyと並べて出るので、土台の粗さのままだと確定の前後で滑らかさが跳ねる)。
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
        """直前の操作を取り消す。文書の土台を作った操作は取り消させない
        (1回多く押すと編集用ボディを作った操作まで取り消され、`active_body` が消えて以後の画面更新が全部落ちる)。
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
        """verb を1つ適用し、変更イベントを発火して、そのイベントを返す。verb を掛ける唯一の口。

        WebSocket・MCP とアプリ内 AI(`mcp_server.DocumentTools`)・`session.fillet(2.0)` の形の呼び出し(末尾の `_verb_method`)が
        全部ここを通るので、実行中の断り・選択のリセット・自動保存・配信はどの経路でも同じに効く。
        実体は verb のモジュールの入口(`duocad/entry.py`)。引数の検査はしない(必須は口ごとに違う。
        `catalog.Command.input_schema` / `ws_schema`)ので、呼ぶ側が済ませて来る。
        """
        function = entry_for(verb)
        self._require_open()
        if self._document_locked():
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
        """`envelope_regions` の確定前の姿。文書を変えない。分裂しても失敗にせず、全部の面の境界と面の数(`pieces`)を返す
        (確定時はトースト、プレビューは線の色で見せる。本人決定)。消えたときは `pieces=0`。
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
    # ---------------------------------------------------------------- 形状最適化を走らせる(`duocad/optimize_run.py` への委譲)
    def optimize_conditions(self) -> dict:
        """4条件がそろって走らせられるか(`optimize_run.conditions_report`)。"""
        return optimize_run.conditions_report(self.document)

    def optimize_resolution(self, rmin_mm: Any = None, element_size_mm: Any = None) -> dict:
        """要素サイズの上限・下限・既定。`element_size_mm` を渡すとその値の段も返す(`resolution_limits`)。
        `rmin_mm`(密度フィルタ半径)は扱えない(二値探索に密度フィルタが無い)ので、届いたら `OptimizeInputError`
        (`net/ws.py` の `optimize_resolution` が `ValueError` として拾い、`{"error": ...}` の非トースト表示へ運ぶ)。
        """
        if rmin_mm is not None:
            raise optimize_run.OptimizeInputError(
                "rmin_mm(最小部材幅/密度フィルタ半径)はこの版では扱えない。"
                "二値探索は密度フィルタを持たないので、要素サイズを直接指定すること"
            )
        return optimize_run.resolution_limits(self.document, element_size_mm)

    def cancel_optimization(self) -> dict:
        """走っている最適化に降りるよう伝える(旗を立てるだけ。実際に降りるのは次に進捗が出た時点)。
        旗は次の `begin_optimization` が畳む(ここで畳むと、中断の直後に立て直された旗を新しい実行が拾う)。
        """
        self._optimize_cancel.set()
        return {"cancelled": True}

    def request_finish(self, step: Optional[int] = None) -> dict:
        """走っている探索を止めて、`step` 番目(省くと最新)のステップを仕上げるよう伝える(降りるのは次の候補を作る前)。"""
        self._finish_request = -1 if step is None else int(step)
        return {"finishing": True}

    def steps_for_current(self) -> Any:
        """いま開いている文書の直近の実行のステップ(無ければ None)。同じオブジェクトか同じファイル名で判定する
        (`_document_locked` と同じ理由)。"""
        if self.optimization_steps is None:
            return None
        if self.document is self._steps_document:
            return self.optimization_steps
        if self._steps_name is not None and self._current_name == self._steps_name:
            return self.optimization_steps
        return None

    @property
    def optimizing(self) -> bool:
        """形状最適化が走っているか(`begin_optimization` 〜 `end_optimization`)。どの文書でかは問わない(同時に走るのは1本だけ。docs/TOPOPT.md)。"""
        return self._optimizing

    @property
    def optimizing_name(self) -> Optional[str]:
        """走らせている文書のファイル名(走っていない・無題なら None)。"""
        return self._optimize_name if self._optimizing else None

    @property
    def optimization_visible(self) -> bool:
        """**いま開いている文書が**最適化を走らせている文書か。進捗・中断・実行中の表示はこれが真のときだけ出す。"""
        return self._document_locked()

    def _document_locked(self) -> bool:
        """今の文書が、走っている最適化の対象か(=文書を変える操作を断るか)。同じオブジェクトか同じファイル名で判定する
        (実行中に同じファイルを開き直すとディスクから再生した別の `Document` になり、名前で見ないと編集できて結果がその編集を上書きする)。
        """
        if not self._optimizing:
            return False
        if self.document is self._optimize_document:
            return True
        return self._optimize_name is not None and self._current_name == self._optimize_name

    def optimization_status(self) -> dict:
        """走っているか・中断を頼まれているか(`net/ws.py` の `optimize_status` の中身)。
        `running` は「いま開いている文書で走っているか」(`optimization_visible`)。`cancelling` は走っている間だけ載せる
        (旗は次の開始まで立ったままなので、走っていないのに載せると次の実行が中断表示から始まって見える)。
        """
        visible = self.optimization_visible
        status: dict = {"running": visible}
        if visible and self._optimize_cancel.is_set():
            status["cancelling"] = True
        if visible and self._finish_request is not None:
            status["finishing"] = True
        return status

    def begin_optimization(self) -> None:
        """走らせる前にイベントループのスレッドで呼ぶ。中断の旗を畳み、今の文書を対象として控える。
        旗をワーカースレッドの中で畳むと、投げてから走り出すまでに届いた中断が消されて結果が足される(issue #35)。
        """
        if self._optimizing:
            raise ValueError("最適化が既に走っている(中断してから)")
        self._optimize_cancel.clear()
        self._finish_request = None
        self._optimize_document = self.document
        self._optimize_name = self._current_name
        self._optimize_created = self._created
        self._optimizing = True

    def end_optimization(self) -> None:
        """`begin_optimization` の対。文書を変える操作をまた受け付ける。"""
        self._optimizing = False
        self._optimize_document = None
        self._optimize_name = None
        self._optimize_created = None

    def deliver_optimization_result(self, result: dict) -> dict:
        """足し終えた結果を、走らせた文書のファイルへ届ける。`end_optimization` より前に、イベントループのスレッドで呼ぶ
        (ワーカースレッドで文書を差し替えると、同時に届いた「開く」と入れ違う)。

        結果は実行が持っていた `Document` に足されている(`optimize_run.run`)。届け先は3通り:

        - その文書を開いたまま … 変更イベントを発火し、自動保存する。
        - 同じファイルを開き直していた … 開き直した写し(編集を断られていたので捨てて失うものは無い)を実行の文書へ差し替える。
        - 別の文書を開いている … 実行の文書をそのファイルへ書くだけ。戻り値に `"elsewhere": True` を付け、画面へは配らず
          次に開いたときに見せる(`net/ws.py`)。

        結果の警告(`warning`)を `Session.warnings` へ積むのは、その文書を開いているときだけ。離れている間の分は結果の
        `warning` に載せ、戻ってきたときにパネルの通知で読ませる(`net/ws.py`)。`state != "done"` のときは警告の行き先を決めるだけ。
        """
        document = self._optimize_document
        name = self._optimize_name
        here = document is None or self.document is document or (
            name is not None and self._current_name == name
        )
        if here and result.get("warning"):
            self.warnings.append(result["warning"])
        if result.get("state") != "done":
            return result if here else dict(result, elsewhere=True)
        if here:
            if document is not None and self.document is not document:
                self._replace_document(document, current_name=name, created=self._optimize_created)
            applied = None
            if "body_id" in result:
                applied = self._after_logged_verb(
                    {
                        "body_id": result["body_id"],
                        "name": result["name"],
                        "triangle_count": result["triangle_count"],
                    }
                )
            # 配る前にディスクへ書く(2026-09-13実測: 数時間がかりの実行が、結果を見る前にサーバプロセスごと消えた)。
            # 変更イベントを発火しない経路(結果にボディが無い)でも書く。
            self.autosave()
            return result if applied is None else dict(result, applied=applied)
        label = name
        try:
            if label is None:
                label = self._unused_untitled_name()
            target = self._resolve_document_name(label)
            with self._save_lock:
                self._write_document_file(target, document.log.to_json(created=self._optimize_created))
            label = target.name
            # 一覧の絵は結果の無い形のまま。消しておけば次に開いたときに撮り直される。
            self._drop_thumbnail(target)
        except Exception as error:  # noqa: BLE001 - 書けない理由は何であれ知らせる(`autosave` と同じ)
            label = label or "untitled.json"
            self._undelivered[label] = (document, self._optimize_created)
            note = (
                f"最適化の結果を {label} へ書けなかった(次に {label} を開くと結果入りで開く): "
                f"{type(error).__name__}: {error}"
            )
            result = dict(result, warning=f"{result['warning']}\n{note}" if result.get("warning") else note)
        return dict(result, elsewhere=True, file=label)

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

        重い(数十秒〜数分)ので、呼び出し側は別スレッド(`asyncio.to_thread`)で回す。`progress(event: dict)` はそのスレッドから呼ばれる。
        `debug` / `debug_kinds` は開発用の可視化(`?debug=space` / `?debug=anchors` / `?voxel=1`)の材料を `optimize_run.run()` へ
        渡す口で、頼まなければ費用ゼロ・結果にも文書にも影響しない。
        `rmin_mm` / `max_iter` / `material_min_mm3` / `material_max_mm3` は旧SIMP方式の引数で扱えない。キーワードとして受け続けるのは、
        届いたときに `TypeError` でなく `OptimizeInputError`(意味を説明した文)で断るため(`net/ws.py` が転送すればここ1か所で断る)。
        `state != "done"` のときは文書を変えていない。

        呼び出し側が `begin_optimization` を済ませていなければ、ここで開始・届け・終了まで囲む(テスト・スクリプトの直呼び。
        戻り値は成功時に `"applied"` 付き)。`net/ws.py` は別スレッドへ投げる前に開始しておき(issue #35)、届け
        (`deliver_optimization_result`)はイベントループへ戻ってから自分で呼ぶ(ここは結果を実行の文書へ足すところまで)。
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
            from .optimize_steps import OptimizationSteps

            steps = OptimizationSteps()
            self.optimization_steps = steps
            self._steps_document, self._steps_name = self._optimize_document, self._optimize_name
            # **今の文書ではなく、開始時に控えた文書を解く**(途中で別の文書へ切り替えてもよい)。
            result = optimize_run.run(
                self._optimize_document,
                options,
                progress=progress,
                should_cancel=self._optimize_cancel.is_set,
                debug=debug,
                debug_kinds=debug_kinds,
                steps=steps,
                finish_request=lambda: self._finish_request,
            )
            if owned:
                result = self.deliver_optimization_result(result)
        finally:
            if owned:
                self.end_optimization()
        return result

    def finish_optimization_step(self, step: int, *, progress: Any = None) -> dict:
        """直近の実行の `step` 番目のステップを、探索をやり直さずに仕上げて結果を足す(`optimize_run.finish_step`)。

        `run_optimization` と同じく重いので別スレッドで回し、呼び出し側が `begin_optimization` を済ませていなければここで囲む。
        仕上げる先は開始時に控えた文書(いま開いている文書が直近の実行の文書であることは `net/ws.py` が確かめる)。
        """
        steps = self.optimization_steps
        owned = not self._optimizing
        if owned:
            self.begin_optimization()
        try:
            result = optimize_run.finish_step(self._optimize_document, steps, int(step), progress=progress,
                                              should_cancel=self._optimize_cancel.is_set)
            if owned:
                result = self.deliver_optimization_result(result)
        finally:
            if owned:
                self.end_optimization()
        return result

    # ---------------------------------------------------------------- ファイル操作
    # 保存形式は操作ログのJSON(docs/DESIGN_DECISIONS.md「技術選定の根拠」)。保存したものが開けないなら再生が壊れているので、隠さない。
    def _resolve_document_name(self, name: Any) -> Path:
        """ファイル名だけを受け取り、`models_dir` 配下に閉じたパスへ解決する(`models_dir` が無ければ作る)。

        保存先が起動場所で変わらないよう起点を `models_dir` に固定してある。文字列だけの拒否では脱出経路が残る(いずれも実測):

        - `Path("D:outside.json").is_absolute()` は False(Windowsのドライブ相対パス)。しかも `models_dir / "D:outside.json"` は
          pathlib が「ドライブ付きセグメント」を新しいアンカーとして扱い、`models_dir` を捨てて `D:\\outside.json` に解決される。
          コロン1文字が入口で、NTFSの代替データストリーム(`a.json:x`)も同じ。
        - 予約デバイス名(`CON`/`COM1`等、拡張子を付けても特別扱い)・末尾のドット/空白(Windowsが黙って落とすため
          `foo.json.` と `foo.json` が同じ実体になりうる)。
        - 既存のシンボリックリンク/ジャンクションを指すファイル名は、文字列検査では `models_dir` の外へ解決されるのを塞げない。

        判定は2段: ①明らかに拒まれる形を早く言う文字列検査、②解決後のパスが `models_dir` 配下か(`is_relative_to`。
        `resolve()` はリンクを辿る)。①だけに頼らない。
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
            # 一覧(`list_documents`)は `*.json` しか見ない。拡張子違いを受理すると、保存はできるのに一覧から二度と開けないファイルができる。
            # 黙って `.json` を補うより、打ち間違いをその場で返す。
            raise ValueError(f"拡張子は.json(一覧はモデルの*.jsonだけを見る): {text!r}")
        candidate = Path(text)
        if candidate.is_absolute():
            raise ValueError(f"ファイル名だけを受け取る(絶対パスは不可): {text!r}")
        self.models_dir.mkdir(parents=True, exist_ok=True)
        root = self.models_dir.resolve()
        resolved = (root / text).resolve()
        # 正の判定はここ(シンボリックリンク越しの脱出も含めて最終的にここで止まる)。
        if not resolved.is_relative_to(root):
            raise ValueError(f"models/の外を指している: {text!r} -> {resolved}")
        return resolved

    @staticmethod
    def _write_document_file(target: Path, payload: str) -> None:
        """一時ファイルへ書いてから差し替える(呼び手が `_save_lock` を持つ)。素の上書きは既存の中身を先に切り詰めるので、
        途中で落ちると唯一の永続形式である前回の保存が半端なJSONで潰れる。`os.replace` は同じディレクトリ内なら不可分。
        """
        staging = target.with_name(target.name + ".writing")
        staging.write_text(payload, encoding="utf-8")
        os.replace(staging, target)

    def save_document(self, name: Any = None) -> dict:
        """操作ログをJSONで書き出す。verbではない(ログに載らない)。

        契約は2つに割れている:

        - `save_document()`(引数なし) … いま編集しているファイルへ上書きする。編集中のファイルが無ければ拒否する。
        - `save_document(name)` … 新しい名前でのみ保存する。既に在る名前は拒否する(いま編集しているファイルの名前でも)。

        `name == self._current_name` なら通す、という例外条項を置かない(本人決定)。例外を残すと、後から別のUIを足したときに
        そこだけ穴が開き、しかもその穴は誰にも見えない。塞いでいるのは「編集中でないファイルを上書きできる」穴で、誤クリック1回で
        無関係なモデルが今の文書に置き換わり、操作ログが唯一の正である以上、失ったものは復旧できない。
        拒否の文はどうすればできるかまで言う(封筒 `net/ws.py` の `{"type":"error"}` が `{型名}: {文}` を前置するので、文だけで人に通じる形で)。

        `name` はファイル名だけ(`_resolve_document_name` が区切り文字・絶対パス・`..` を拒否する)。
        その検査を先に通す(models/ の外を指す名前は、存在の有無を問う前に断る)。
        呼び手は自動保存が主(issue #5、2026-09-16): 文書が変わるたび `autosave()` が引数なしで呼び、無題なら
        `untitled-<n>.json` を `name` に渡す。人が名前を決める口は `rename_document`。
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
            self._require_open()  # 閉じている間の空の文書を、新しい名前のファイルにしない
            if target.exists():
                # どちらの枝も拒否する(分かれるのは「次に何をすればよいか」の文言だけ)。
                raise ValueError(
                    f"{target.name} は既にある(いま編集中のファイルで、自動保存とCtrl+Sが書いている)"
                    if target.name == self._current_name
                    else f"{target.name} は編集中ではない(先に開けば上書きできる)"
                )
        if name is not None and self._document_locked():
            # 名前を変えて書くと、編集を断っている写しが別名で編集できる文書になり、結果は
            # 元の名前へ届く(写しには入らない)。
            raise ValueError(
                f"形状最適化の実行中は別名で保存できない({self._optimize_name} の実行が終わるか中断してから)"
            )
        created = self._created if name is None else time.time()
        payload = self.document.log.to_json(created=created)
        with self._save_lock:
            self._write_document_file(target, payload)
            if name is not None:
                # 同じ名前の古い文書(外で消された)の絵が残っていれば、それはこの文書の絵ではない。
                self._drop_thumbnail(target)
            # 書けてから現在ファイルにする。先に控えると、書き込みが落ちたとき「存在しないファイルを編集中」になり、
            # 次の引数なし保存が黙って新しいファイルを作る。
            self._current_name = target.name
            self._created = created
            self._unsaved = False
        return {
            "action": "save",
            "path": str(target),
            "name": target.name,
        }

    @property
    def saved(self) -> bool:
        """今の文書がそのままファイルに載っているか(`state()["saved"]`)。自動保存が書けなかったときだけ False
        (無題で何も変えていない文書は True)。
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
        """今の文書をディスクへ書く(1操作ごとの自動保存。issue #5、本人判断 2026-09-16)。

        `_emit` が verb の応答のたびに呼ぶ(undo / redo も含む。ファイルは常に今の文書の鏡)。無題の文書は最初の変更で
        `untitled-<n>.json` を自動で付ける(人が名前を決めるのは `rename_document`)。
        失敗しても例外にしない(文書の変更は確定済みで巻き戻せない)。書けなかったことは警告と `saved=False` で知らせ、
        戻り値は書けたか。捕まえるのは種類を問わない: Windows では別のプログラムがファイルを開いているだけで
        `os.replace` が `PermissionError` になり(実測)、漏れると最適化の経路で結果も終了の `optimize_status` も配られず画面が実行中のまま固まる。
        費用は操作ログ全体のJSON化と書き込み。実測は 4.8MB の文書で 0.02秒なので debounce も差分保存も入れていない。
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
        """いま編集しているファイルの名前を変える(ディスク上で移す)。verbではない。
        人が名前を付ける口(ファイルパネルの名前欄。issue #5)。`name` が既に在れば拒否(上書きの口は持たない。`copy_document` と同じ)。
        無題で何も変えていない文書(ファイルがまだ無い)は、その名前で空の文書を書く。
        """
        target = self._resolve_document_name(name)
        # 閉じている間に通すと、空の文書がその名前で書かれる(トップ画面のまま新規ファイルが増える)。
        self._require_open()
        if self._document_locked():
            # 移すと、終わった結果が元の名前で新しいファイルとして書かれる。
            raise ValueError(
                f"形状最適化の実行中は名前を変えられない({self._optimize_name} の実行が終わるか中断してから)"
            )
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
                target.write_text(self.document.log.to_json(created=self._created), encoding="utf-8")
            self._drop_thumbnail(target)
            self._carry_thumbnail(source, target, move=True)
            self._current_name = target.name
            self._unsaved = False
        return {
            "action": "rename",
            "path": str(target),
            "name": target.name,
            "previous": previous,
        }

    def open_document(self, name: Any) -> dict:
        """保存した操作ログを読み、再生して文書を差し替える(§3.1「これが正」)。`name` はファイル名だけ(`save_document` と同じ)。

        失敗しても今の文書を壊さない(新しい `Document` で再生し切ってから差し替える)。
        形状最適化の実行中も断らない(`new_document` も同じ)。実行は開始時の文書を持ったまま走り、結果はそのファイルへ届く
        (`deliver_optimization_result`)。走らせているファイル自身を開き直した写しは、終わるまで編集を断る(`_document_locked`)。
        差し替えの後始末は `_replace_document`(`new_document` と共通)。
        """
        source = self._resolve_document_name(name)
        if source.name in self._undelivered:
            return self._open_undelivered(source.name)
        if not source.exists():
            raise FileNotFoundError(f"開けるファイルが無い: {source}")
        payload = json.loads(source.read_text(encoding="utf-8"))
        log = OpLog.from_payload(payload)
        document = Document()
        # 束ねてから再生する(逆だと `set_parameter` を含むログで落ちる。パラメータの再生は `document.log` を頭から読むので、
        # 再生中の文書が自分のログを持っていないと成立しない。`parameters.rebuild` が理由つきで拒否する)。
        document.log = log
        log.replay(document)

        # 開けたものが編集中のファイルになる。再生が落ちたときはここまで来ないので現在名も差し替わらない。
        self._replace_document(
            document, current_name=source.name, created=OpLog.created_of(payload)
        )
        return self._emit({
            "change": "document",
            "action": "open",
            "path": str(source),
            "name": source.name,
        })

    def _open_undelivered(self, name: str) -> dict:
        """ファイルへ書けなかった最適化の結果入りの文書を開き、もう一度書く。開いた時点で今の文書になるので手放す。"""
        document, created = self._undelivered.pop(name)
        self._replace_document(document, current_name=name, created=created)
        self.autosave()
        return self._emit({
            "change": "document",
            "action": "open",
            "path": str(self._resolve_document_name(name)),
            "name": name,
        })

    def new_document(self) -> dict:
        """空の文書へ差し替える(「新規」)。verbではない(ログに載らない)。

        今の文書は捨てられ、undoでは戻らない(`open_document` と同じ)。確認は挟まない(一覧からのクリック1つで別の文書へ移れるのに、
        空の文書へ移るときだけ確認すると挙動が割れる。`web/src/FilesPanel.svelte`)。捨てる前の保存もしない(1操作ごとに自動保存済み)。
        編集中のファイルも無くなる(`current_name=None`)。次に文書を変えた瞬間に自動保存が `untitled-<n>.json` を作る
        (それまで引数なしの `save_document()` は拒否される)。
        """
        self._replace_document(Document(), current_name=None, created=None)
        return self._emit({
            "change": "document",
            "action": "new",
            # 開いたファイルが無いので `path` / `name` は `None`(`web/src/App.svelte` はこれで表示を(無題)・保存状態を「未保存」へ戻す)。
            "path": None,
            "name": None,
        })

    def close_document(self) -> dict:
        """文書を閉じてトップ画面(何も開いていない状態)へ戻す。verbではない。保存はしない(自動保存済み)。
        空の `Document` に差し替えてundoの山ごと捨てる: 中身を残したまま閉じた扱いにすると、開き直さずに編集できる経路が
        1つでも残った瞬間に、前の文書の続きが `untitled-<n>.json` として書かれる。
        """
        self._replace_document(Document(), current_name=None, created=None)
        self._open = False
        return self._emit({
            "change": "document",
            "action": "close",
            "path": None,
            "name": None,
        })

    def _replace_document(
        self, document: Document, *, current_name: Optional[str], created: Optional[float]
    ) -> None:
        """今の文書を `document` へ差し替える(`open_document` / `new_document` の共通部分)。

        `current_name` は既定値を持たない(キーワード必須): 既定で持ち越すと、`new_document` の後に前の文書のファイル名が残り、
        引数なしの `save_document()` が捨てたはずのファイルを空の文書で潰す。
        配りものは `view.reset()` で作り直す(Bodyのidは再生で作り直されるが同じidが再び現れうる。古い形状を指したキャッシュが
        残ると別の文書の形を配る)。選択集合は `Document` が持つので、差し替えた時点で空になっている。
        """
        self.document = document
        self._open = True  # 閉じる側(`close_document`)は差し替えの後で False に戻す
        self._current_name = current_name
        self._created = created
        self._unsaved = False  # 開いた/空にした文書に、書けなかった変更は無い
        # キャッシュの破棄は `view` の1呼び出し(とくに版の数え上げを消さない理由は `DocumentView.reset` の docstring)。
        self.view.reset(document)
        # 土台の下限は引き継がない(読み込んだログはすべてその文書の操作で、起動時の `load_step` の土台とは別物)。
        self._setup_floor_seq = 0
        self._applied_count += 1

    def list_documents(self) -> dict:
        """`models_dir` の中身を一覧にする(名前/更新日時/作成日時/サムネイルの版)。verbではない。

        読めるファイルだけを載せる判定は `OpLog.from_payload` のパースだけで行う(形状の再生はしない)。
        実測でファイルあたり約30ms(4.8MBのPCケースを3件で0.09s)。壊れたファイル(手で触られた・書き込み途中の `.writing` 等)は
        省く。旧形式(version 1)のファイルも `from_payload` が ValueError にするため黙って省かれる(次に版を上げるときは
        「一覧に出ないだけで理由が分からない」になる)。
        `created` は入れ物の作成日時(無いファイルは None)。`thumb` はサムネイルの更新日時で、画像そのものは載せない
        (一覧は自動保存のたびに取り直されるので、載せると全件の画像を毎回送る)。画像は `get_thumbnail` で1件ずつ取る。
        """
        self.models_dir.mkdir(parents=True, exist_ok=True)
        files = []
        for path in sorted(self.models_dir.glob("*.json")):
            try:
                mtime = path.stat().st_mtime
                payload = json.loads(path.read_text(encoding="utf-8"))
                OpLog.from_payload(payload)
            except (OSError, ValueError, KeyError, TypeError):
                continue
            files.append({
                "name": path.name,
                "mtime": mtime,
                "created": OpLog.created_of(payload),
                "thumb": _mtime_or_none(self._thumbnail_path(path)),
            })
        return {"action": "list", "path": str(self.models_dir), "files": files}

    def copy_document(self, source: Any, dest: Any) -> dict:
        """`models_dir` の `source` を `dest` へ複製する。verbではない。
        いま編集している文書も `current_name` も変わらない。`dest` が既に在れば拒否(上書きの口は持たない。
        `save_document(name)` と同じ。issue #17)。どちらの名前も `_resolve_document_name` を通す。
        複製は新しいファイルなので作成日時を今にする(バイト列を写さず、読んで書き直す)。サムネイルは写す。
        """
        origin = self._resolve_document_name(source)
        target = self._resolve_document_name(dest)
        if not origin.exists():
            raise FileNotFoundError(f"複製元が無い: {origin.name}")
        if target.exists():
            raise ValueError(f"{target.name} は既にある(別の名前にする)")
        log = OpLog.from_json(origin.read_text(encoding="utf-8"))
        # `save_document` と同じく一時ファイル経由(`list_documents` は `.writing` を省く)。
        staging = target.with_name(target.name + ".writing")
        staging.write_text(log.to_json(created=time.time()), encoding="utf-8")
        os.replace(staging, target)
        self._drop_thumbnail(target)
        self._carry_thumbnail(origin, target, move=False)
        return {
            "action": "copy",
            "path": str(target),
            "name": target.name,
            "source": origin.name,
        }

    def delete_document(self, name: Any) -> dict:
        """`models_dir` の `name` をディスクから削除する。verbではない。
        いま編集しているファイルなら、消してから文書を閉じる(応答に `closed: True`)。閉じずに残すと、次の操作の自動保存が
        消したはずの中身を `untitled-<n>.json` として黙って復活させる。ファイルを消せなかったら閉じない。
        無ければ FileNotFoundError。確認は挟まない(画面側が行内で聞く。`FilesPanel.svelte`)。
        `models/.trash` への退避はしない(issue #17)。サムネイルも消す。
        """
        target = self._resolve_document_name(name)
        if self._optimizing and target.name == self._optimize_name:
            # 消しても、終わった結果が同じ名前で書き戻されて黙って復活する。
            raise ValueError(
                f"{target.name} は形状最適化の実行中(終わるか、開いて中断してから削除する)"
            )
        if not target.exists():
            raise FileNotFoundError(f"削除するファイルが無い: {target.name}")
        target.unlink()
        # 書けなかった結果も一緒に捨てる(残すと、同じ名前で作り直したファイルを開いたときに出てくる)。
        self._undelivered.pop(target.name, None)
        self.unseen_optimize_results.pop(target.name, None)
        self._drop_thumbnail(target)
        deleted = {"action": "delete", "path": str(target), "name": target.name}
        if target.name == self._current_name:
            self.close_document()
            deleted["closed"] = True
        return deleted

    # ---------------------------------------------------------------- サムネイル
    # 一覧の絵は画面(three.js)が撮って預ける(`web/src/thumbnail.ts`)。置き場は `models/.thumbnails/<文書のファイル名>.png` の付随ファイルで、
    # 文書のJSONには入れない(描き直せる写し)。だから付随ファイルの失敗は元の操作を失敗にしない。
    def _thumbnail_path(self, document: Path) -> Path:
        """文書のファイル(`models_dir` 直下)に付くサムネイルのパス。"""
        return document.parent / THUMBNAIL_DIR / f"{document.name}.png"

    def _drop_thumbnail(self, document: Path) -> None:
        try:
            self._thumbnail_path(document).unlink(missing_ok=True)
        except OSError as error:
            self.warnings.append(f"サムネイルを消せなかった: {error}")

    def _carry_thumbnail(self, source: Path, target: Path, *, move: bool) -> None:
        """改名(`move`)・複製でサムネイルも付いていかせる。複製は更新日時ごと写す(画面は版で取り直しを決める)。"""
        origin = self._thumbnail_path(source)
        if not origin.exists():
            return
        try:
            if move:
                os.replace(origin, self._thumbnail_path(target))
            else:
                shutil.copy2(origin, self._thumbnail_path(target))
        except OSError as error:
            self.warnings.append(f"サムネイルを{'移せ' if move else '写せ'}なかった: {error}")

    def save_thumbnail(self, name: Any, png: Any) -> dict:
        """画面が撮ったPNG(base64)を `name` のサムネイルとして書く。verbではない。
        `png` が空文字なら消す(見えている立体が1つも無い文書)。文書のファイルが無い・書けない(OSError)ときも断らず
        `saved: False` を返す(撮ってから送るまでに改名が挟まりうるし、Windows では別のプログラムが開いているだけで
        `os.replace` が落ちる)。断るのは中身が壊れているとき(画面側の不具合)だけ。
        """
        target = self._resolve_document_name(name)
        thumbnail = self._thumbnail_path(target)
        reply = {"action": "save_thumbnail", "path": str(thumbnail), "name": target.name}
        text = str(png)
        if text == "":
            try:
                thumbnail.unlink(missing_ok=True)
            except OSError as error:
                self.warnings.append(f"サムネイルを消せなかった: {error}")
                return {**reply, "saved": False, "thumb": None}
            return {**reply, "saved": True, "thumb": None}
        try:
            data = base64.b64decode(text, validate=True)
        except binascii.Error as error:
            raise ValueError(f"サムネイルがbase64ではない: {error}") from None
        if not data.startswith(_PNG_SIGNATURE):
            raise ValueError("サムネイルがPNGではない")
        if len(data) > THUMBNAIL_MAX_BYTES:
            raise ValueError(f"サムネイルが大きすぎる({len(data)} > {THUMBNAIL_MAX_BYTES}バイト)")
        if not target.exists():
            return {**reply, "saved": False, "thumb": None}
        try:
            thumbnail.parent.mkdir(exist_ok=True)
            staging = thumbnail.with_name(thumbnail.name + ".writing")
            staging.write_bytes(data)
            os.replace(staging, thumbnail)
            thumb = thumbnail.stat().st_mtime
        except OSError as error:
            self.warnings.append(f"サムネイルを書けなかった: {error}")
            return {**reply, "saved": False, "thumb": None}
        return {**reply, "saved": True, "thumb": thumb}

    def get_thumbnail(self, name: Any) -> dict:
        """`name` のサムネイルをbase64で返す(無い・読めなければ `png: None`)。**verbではない**。"""
        target = self._resolve_document_name(name)
        thumbnail = self._thumbnail_path(target)
        reply = {"action": "thumbnail", "path": str(thumbnail), "name": target.name}
        try:
            thumb = thumbnail.stat().st_mtime
            data = thumbnail.read_bytes()
        except OSError:
            return {**reply, "png": None, "thumb": None}
        return {**reply, "png": base64.b64encode(data).decode("ascii"), "thumb": thumb}

    def export_step_file(self, path: Any) -> dict:
        """STEPを書き出す。ログに載らない。対象はいま表示中のBodyだけ。非表示で除外した件数は `skipped_hidden` として必ず応答に載る。"""
        return {"action": "export_step", **io_step.export(self.document, self._resolve_export_path(path), "step")}

    def _resolve_export_path(self, path: Any) -> Path:
        """書き出し先を絶対パスにする。相対は `exports_dir` 基準(その外へ逃げる経路は拒否)。絶対パスはそのまま(テスト・MCPの明示パス用)。"""
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
        対象はいま表示中のBodyだけ。非表示で除外した件数は `skipped_hidden` として必ず応答に載る。応答の `deviation` は書いた粗さの記録で入力ではない。
        """
        exported = io_step.export(self.document, self._resolve_export_path(path), "stl")
        return {
            "action": "export_stl",
            "path": exported["path"],
            "deviation": io_step.STL_DEVIATION,
            "body_ids": exported["body_ids"],
            "skipped_hidden": exported["skipped_hidden"],
        }

    def take_warnings(self) -> list[str]:
        """溜まった警告を取り出して空にする。リストを束ね直さず中身を空にする: `DocumentView` が同じリストオブジェクトへ積むので、
        別のリストへ差し替えると以後 `view` が積んだ警告が誰にも読まれず消える。
        """
        warnings = list(self.warnings)
        self.warnings.clear()
        return warnings


def _verb_method(verb: str, function: Any) -> Any:
    """`session.fillet(2.0)` の形で verb を呼べるメソッド。中身は `Session.apply` だけ。
    位置引数・既定値は入口(`duocad/entry.py`)の引数の並びに従う(Pythonから組み立てる側のため。WebSocket と MCP は `apply` を直接呼ぶ)。
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
