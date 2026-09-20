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

import os
import threading
from pathlib import Path
from typing import Any, Optional  # Any: Session verb wrappers / listeners

from . import (
    catalog,
    io_step,
    kernel,
    optimize,
    optimize_run,
    parameters,
    query,
    region,
    selection,
    sketch,
)
from .document import Document
from .oplog import OpLog
from .paths import DEFAULT_MODELS_DIR
from .speculative import speculative
from .verbs import (
    add_mesh_body as add_mesh_body_verb,
    boolean_body as boolean_body_verb,
    chamfer as chamfer_verb,
    delete as delete_verb,
    extrude as extrude_verb,
    extrude_face as extrude_face_verb,
    fillet as fillet_verb,
    import_step as import_step_verb,
    import_svg as import_svg_verb,
    mirror_body as mirror_body_verb,
    mirror_sketch_entities as mirror_sketch_entities_verb,
    move_body as move_body_verb,
    move_sketch_entities as move_sketch_entities_verb,
    pattern_circular as pattern_circular_verb,
    pattern_rectangular as pattern_rectangular_verb,
    clear_loads as clear_loads_verb,
    set_exclude_bodies as set_exclude_bodies_verb,
    set_fixtures as set_fixtures_verb,
    set_loads as set_loads_verb,
    set_preserve_bodies as set_preserve_bodies_verb,
)
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
    ) -> None:
        self.document = Document()
        self.deviation = float(deviation)
        #: 保存/開く/一覧の唯一の置き場。既定は `DEFAULT_MODELS_DIR`
        #: (リポジトリ直下の `models/`)。テストが実フォルダを汚さないよう差し替えられる。
        self.models_dir: Path = (
            DEFAULT_MODELS_DIR if models_dir is None else Path(models_dir)
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
        #: 配りもの(状態・版・キャッシュ)。**文書を差し替えたら `view.reset()` を呼ぶ**
        #: (`_replace_document`)——古い文書について測ったものを握ったままにしないため。
        self.view = DocumentView(self.document, self.deviation, self.warnings)
        self._setup_floor_seq = 0  # load_step が積んだ最後のseq(これ以下はundoの対象外)
        self._listeners: list = []  # 文書が変わったときに呼ぶ購読者(下記 add_listener)
        self._suppress_events = 0  # >0 の間は `_emit` が配らない(プレビュー。`preview` 参照)
        #: 形状最適化の中断の旗。**別スレッドから立てられる**(実行はワーカースレッド、
        #: `cancel_optimization` はイベントループのスレッド)ので `Event` にしてある。
        self._optimize_cancel = threading.Event()
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

        書き換える側(`extrude_face` 等の各verb / `_after_logged_verb` / 文書を開く)は
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
        """配りものに**いま編集しているファイル名**を1つ足す(`view` の持ち物ではない)。

        **`view.state_meta()` へ足さない** —— `DocumentView` が持つのは文書から測れるもの
        (形・版・スケッチ)だけで、どのファイルを開いているかは `Session` の持ち物である。
        `state()` と `state_meta()` の両方をここへ通すのは、片方だけに載って
        「`get_state` では見えるのに配信では見えない」が起きないようにするため。

        **載せないと画面が説明のつかない状態になる**: リロードすると文書はサーバに残る
        のにブラウザ側の名前だけ失われ、ヘッダが「(無題)」のままなのに保存ボタン
        (現在ファイルへの上書き)だけは通る、という食い違いが出る。
        """
        state["current_name"] = self._current_name
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

    def sketch_create_offset(self, distance: float, base_plane: Optional[str] = None) -> dict:
        """オフセット平面にスケッチを作る。基準は `sketch_create` と同じ
        `base_plane`(XY/XZ/YZ)省略時は確定選択の面(ちょうど1枚)で、そこから法線方向へ
        `distance` だけ離れた平面に貼る。

        **解決は `sketch.offset_plane` に投げるだけ**(基準の解決自体は `sketch_create` と
        同じ2行)。verb名だけ `sketch_create_offset` にする理由は `sketch.sketch_create` の
        docstring参照(Enter要否がverb名ごとに1つ固定されているため)。
        """
        if base_plane is not None:
            base: Any = str(base_plane)
        else:
            face = selection.require_confirmed(
                self.document, "face", exactly_one=True
            )[0]
            base = sketch.face_plane(face["body"], face["index"])
        plane = sketch.offset_plane(base, distance)
        sketch_id = sketch.sketch_create(
            self.document, plane, verb=sketch.VERB_SKETCH_CREATE_OFFSET
        )
        return self._after_logged_verb({"sketch_id": sketch_id})

    def add_line(self, sketch_id: str, p0: Any, p1: Any) -> dict:
        entity_id = sketch.add_line(self.document, sketch_id, p0, p1)
        return self._after_logged_verb({"entity_id": entity_id})

    def add_circle(self, sketch_id: str, center: Any, radius: float) -> dict:
        entity_id = sketch.add_circle(self.document, sketch_id, center, radius)
        return self._after_logged_verb({"entity_id": entity_id})

    def add_rectangle(
        self,
        sketch_id: str,
        corner: Any,
        width: float,
        height: float,
        angle: float = 0.0,
    ) -> dict:
        entity_id = sketch.add_rectangle(
            self.document, sketch_id, corner, width, height, angle
        )
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

    def boolean_body(self, target_ref: Any, mode: str) -> dict:
        """**ターゲット(1つ・面参照)に確定選択のツールボディ群**を作用させる。

        ターゲットは in-place で形だけ置き換え、ツールボディは消える。`target_ref` は
        確定選択には入れない(`move_body.axis_edge` と同じ規律)。
        """
        tool_body_ids = selection.require_bodies(self.document)
        operation = boolean_body_verb(
            self.document, target_ref, tool_body_ids, mode=str(mode)
        )
        changed_body_id = operation.args["target_body_id"]
        return self._after_logged_verb({
            "target_body_id": changed_body_id,
            "mode": operation.args["mode"],
            "removed_bodies": list(operation.args["tool_bodies"]),
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

    def move_sketch_entities(
        self,
        dx: Optional[float] = None,
        dy: Optional[float] = None,
        copy: bool = False,
        mode: str = "translate",
        angle: Optional[float] = None,
        center: Any = None,
        center_entity: Any = None,
    ) -> dict:
        """**確定選択のスケッチの点・辺**を動かす(`copy=True` は複製)。

        対象は引数ではなく選択から取る(`selection.require_sketch_entities`)——
        パラメータパネルを廃止して直接選択へ移した後の、位置の打ち直しの唯一の口である。
        **ソリッドは追従しない**(verb側のdocstring。本人が許容済み)ので `changed_bodies` は空。

        `mode="translate"` は (dx, dy)、`mode="rotate"` は `angle`[度] と中心を取る
        (検査と既定はverb側)。**戻り値のキーはモードによらず同じ** —— 使わない側は `None`
        にする(`web/src/session.ts` の `Applied` が形の変わる封筒を読めないため)。
        """
        targets = selection.require_sketch_entities(self.document)
        sketch_ids = sorted({str(item["sketch"]) for item in targets})
        operation = move_sketch_entities_verb(
            self.document,
            targets,
            dx,
            dy,
            copy=bool(copy),
            mode=str(mode),
            angle=angle,
            center=center,
            center_entity=center_entity,
        )
        args = operation.args
        return self._after_logged_verb({
            "mode": args["mode"],
            "dx": args.get("dx"),
            "dy": args.get("dy"),
            "angle": args.get("angle"),
            "center": args.get("center"),
            "copy": args["copy"],
            "created": list(args["created"]),
            "sketch_ids": sketch_ids,
        })

    def move_body(
        self,
        dx: Optional[float] = None,
        dy: Optional[float] = None,
        dz: Optional[float] = None,
        copy: bool = False,
        mode: str = "translate",
        angle: Optional[float] = None,
        axis_edge: Any = None,
        axis_point: Any = None,
        axis_dir: Any = None,
        center_vertex: Any = None,
    ) -> dict:
        """**確定選択の面/辺/頂点が属するボディ**を動かす(`copy=True` は複製)。

        複数のボディにまたがる選択なら全部を同じ量だけ動かす(1呼び出し = 1 undo)。
        `changed_bodies` には動いたボディと、複製で増えたボディの両方を載せる ——
        画面はここを見て形を取り直すので、どちらかを落とすと片方が古い位置のまま残る。

        `mode="translate"` は (dx, dy, dz)、`mode="rotate"` は軸と `angle`[度] を取る
        (検査と既定はverb側)。**戻り値のキーはモードによらず同じ**で、使わない側は `None`。
        `axis_point` / `axis_dir` は**解決後の数値**(辺で指しても数値で返る)。
        """
        body_ids = selection.require_bodies(self.document)
        operation = move_body_verb(
            self.document,
            body_ids,
            dx,
            dy,
            dz,
            copy=bool(copy),
            mode=str(mode),
            angle=angle,
            axis_edge=axis_edge,
            axis_point=axis_point,
            axis_dir=axis_dir,
            center_vertex=center_vertex,
        )
        args = operation.args
        created = list(args["created"])
        return self._after_logged_verb({
            "mode": args["mode"],
            "dx": args.get("dx"),
            "dy": args.get("dy"),
            "dz": args.get("dz"),
            "angle": args.get("angle"),
            "axis_point": args.get("axis_point"),
            "axis_dir": args.get("axis_dir"),
            "copy": args["copy"],
            "created": created,
            "changed_bodies": list(args["bodies"]) + created,
        })

    def mirror_sketch_entities(self, axis: Any) -> dict:
        """**確定選択のスケッチ図形**を、既存の線分1本(`axis`)を軸に鏡映複製する(常に複製)。

        対象は引数ではなく選択から取る(`selection.require_sketch_entities`)。軸は対象集合
        とは別引数(`move_body.axis_edge` と同じ理由)。**ソリッドは追従しない**
        (`move_sketch_entities` と同じ)。
        """
        targets = selection.require_sketch_entities(self.document)
        sketch_ids = sorted({str(item["sketch"]) for item in targets})
        operation = mirror_sketch_entities_verb(self.document, targets, axis)
        args = operation.args
        return self._after_logged_verb({
            "axis_p0": args["axis_p0"],
            "axis_dir": args["axis_dir"],
            "created": list(args["created"]),
            "sketch_ids": sketch_ids,
        })

    def mirror_body(self, base_plane: Optional[str] = None, plane_face: Any = None) -> dict:
        """**確定選択の面/辺/頂点が属するボディ**を、鏡映面に対して鏡映複製する(常に複製)。

        鏡映面は `base_plane`(XY/XZ/YZ)か `plane_face`(既存の平面な面への参照)のどちらか
        (`duocad/verbs/mirror_body.py` が検査)。対象は引数ではなく選択から取る
        (`selection.require_bodies`)。**元は動かないので `changed_bodies` は複製だけ**
        (`move_body` と違い、動いたボディを取り直す必要がない)。
        """
        body_ids = selection.require_bodies(self.document)
        operation = mirror_body_verb(
            self.document, body_ids, base_plane=base_plane, plane_face=plane_face
        )
        args = operation.args
        created = list(args["created"])
        return self._after_logged_verb({
            "plane_point": args["plane_point"],
            "plane_normal": args["plane_normal"],
            "created": created,
            "changed_bodies": created,
        })

    def delete(self) -> dict:
        """**確定選択のボディとスケッチ図形**をまとめて消す(1回のundoで全部戻る)。

        対象は引数ではなく選択から取る(`selection.require_deletable`)。ボディ由来
        (面/辺/頂点/ボディ)と図形が**混ざっていても1回の呼び出し**で処理する ——
        `require_bodies` と `require_sketch_entities` を2つ呼ぶと、片方しか選ばれて
        いないときに `SelectionKindMismatch` で壊れる(あちらは「1つも無ければ例外」で、
        単一種別しか食わないverb用の口である)。

        **`changed_bodies` は返さない** —— 消えたボディは配りものの `bodies` から丸ごと
        居なくなる(形が変わったのではない)。画面が形を取り直す対象は1つも無い。
        """
        targets = selection.require_deletable(self.document)
        operation = delete_verb(
            self.document, targets.bodies, targets.entities
        )
        return self._after_logged_verb({
            "bodies": list(operation.args["bodies"]),
            "entities": [spec["entity_id"] for spec in operation.args["entities"]],
            "sketch_ids": sorted(
                {str(spec["sketch_id"]) for spec in operation.args["entities"]}
            ),
        })

    # ---------------------------------------------------------------- 形状最適化の条件(第3のモード)
    #
    # **4つとも集合代入**で、対象は確定選択そのもの。**確定選択が空でもよい**
    # (`selection.require_committed_*` のコメント) —— 空の代入が「その役割を全部外す」を
    # 意味するので、解除専用のverbが要らない。
    #
    # `changed_bodies` は**返さない**。形は1つも変わらないので、画面が形を取り直す理由が
    # 無い(役割の色は配信される `state.optimize` から描く)。
    def set_preserve_bodies(self) -> dict:
        """**確定選択のボディ**を保持ソリッドの集合にする(集合代入)。"""
        body_ids = selection.require_committed_bodies(self.document)
        operation = set_preserve_bodies_verb(self.document, body_ids)
        return self._after_logged_verb({"bodies": list(operation.args["bodies"])})

    def set_exclude_bodies(self) -> dict:
        """**確定選択のボディ**を除外ソリッドの集合にする(集合代入)。"""
        body_ids = selection.require_committed_bodies(self.document)
        operation = set_exclude_bodies_verb(self.document, body_ids)
        return self._after_logged_verb({"bodies": list(operation.args["bodies"])})

    def set_fixtures(self) -> dict:
        """**確定選択の面**を拘束面の集合にする(集合代入)。"""
        faces = selection.require_committed_faces(self.document)
        operation = set_fixtures_verb(self.document, faces)
        return self._after_logged_verb({"face_count": len(operation.args["faces"])})

    def set_loads(self, magnitude: Any, direction: Any) -> dict:
        """**確定選択の面だけ**に大きさ[N]と向きを焼く(上書き代入。選択外の荷重面は残る)。

        `face_count` は**確定後の荷重面の総数**であって、この操作で焼いた枚数ではない
        (画面は「いま何枚に荷重が付いているか」を出すのでこちらが要る)。
        """
        faces = selection.require_committed_faces(self.document)
        operation = set_loads_verb(self.document, faces, magnitude, direction)
        mode, vector = optimize.normalize_direction(direction)
        return self._after_logged_verb({
            "face_count": len(operation.args["faces"]),
            "applied_count": len(faces),
            "magnitude": optimize.normalize_magnitude(magnitude),
            "direction": mode,
            "direction_vector": None if vector is None else list(vector),
        })

    def clear_loads(self) -> dict:
        """**確定選択の面**を荷重から外す(選択が空なら全部外す)。"""
        faces = selection.require_committed_faces(self.document)
        operation = clear_loads_verb(self.document, faces)
        return self._after_logged_verb({"face_count": len(operation.args["faces"])})

    # ---------------------------------------------------------------- 形状最適化を走らせる
    # 条件を置く4 verb の**続き**(`duocad/optimize_run.py`)。ここにあるのは委譲と、
    # 「中断の旗」と「結果を1つのボディとして足したことの通知」だけである。
    def optimize_conditions(self) -> dict:
        """4条件がそろって走らせられるか(`optimize_run.conditions_report`)。"""
        return optimize_run.conditions_report(self.document)

    def optimize_resolution(self, rmin_mm: Any = None) -> dict:
        """要素サイズの上限・下限・既定。

        `rmin_mm`(密度フィルタ半径)は**この版では扱えない**(計画書§6.2 —— 二値探索は
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
        投げる。**旗は次の `run_optimization` が畳む** —— ここで畳むと、中断の直後に
        立て直された旗を新しい実行が拾ってしまう。
        """
        self._optimize_cancel.set()
        return {"cancelled": True}

    def run_optimization(
        self,
        *,
        target_safety_factor: Any = optimize_run.DEFAULT_TARGET_SAFETY_FACTOR,
        element_size_mm: Any = None,
        min_width_mm: Any = None,
        max_evaluations: Any = None,
        name: Any = None,
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
        引数で、この版では扱えない**(計画書§6.2「旧引数が外部から届いたら黙って無視せず、
        不支持と分かるエラーにする」)。キーワードとして受け続けているのは、届いたときに
        `TypeError`(引数名を知らない)ではなく`OptimizeInputError`(意味を説明した文)で
        断るため —— 呼び出し側(`net/ws.py`)が転送さえすれば、ここが一箇所で断る。

        戻り値は `optimize_run.run()` のものに、成功時だけ `"applied"`(変更イベントの封筒)を
        足したもの。**`state != "done"` のときは文書を1ビットも変えていない**。
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
        self._optimize_cancel.clear()
        options = optimize_run.OptimizeOptions(
            target_safety_factor=float(target_safety_factor),
            element_size_mm=None if element_size_mm is None else float(element_size_mm),
            min_width_mm=None if min_width_mm is None else float(min_width_mm),
            max_evaluations=None if max_evaluations is None else int(max_evaluations),
            name=None if name is None else str(name),
        )
        result = optimize_run.run(
            self.document,
            options,
            progress=progress,
            should_cancel=self._optimize_cancel.is_set,
            debug=debug,
            debug_kinds=debug_kinds,
        )
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

    def add_mesh_body(
        self, positions: Any, indices: Any, name: Any = None
    ) -> dict:
        """三角形メッシュのボディを1つ足す(verb `add_mesh_body`)。

        **WSのコマンドとしては開けていない**(`catalog` の `ws="none"`) —— 画面から
        三角形を手で打つ操作は無く、入口はMCPと、形状最適化の結果を入れるコード側である。
        """
        body_id = add_mesh_body_verb(
            self.document, positions, indices, None if name is None else str(name)
        )
        return self._after_logged_verb({"body_id": body_id})

    def import_step(self, path: Any) -> dict:
        """STEPを読み、**編集可能なボディまで作る**(verb `import_step`)。

        **STEPの読み込みはこれ1本** —— ソリッドごとに編集用ボディを作る
        1回のverbで、何部品あってもログ1行・undo1回である。
        **メソッド名・WSのコマンド名・ログのverb名を同じ綴りにしてある**(1コマンドを引数で
        2つのverbに打ち分けると、コマンド名と積まれるverbが食い違う)。
        """
        body_ids = import_step_verb(self.document, str(self._resolve(path)))
        return self._after_logged_verb({"body_ids": list(body_ids)})

    def import_svg(self, sketch_id: str, path: Any, scale: Any = None) -> dict:
        """SVGの図形を**いま開いているスケッチ(`sketch_id`)へ**取り込む(verb `import_svg`)。

        **貼る先は引数で選ばない** —— スケッチのverbなので、貼る先は
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
                    f"{target.name} は既にある(ヘッダの保存ボタンかCtrl+Sで上書きできる)"
                    if target.name == self._current_name
                    else f"{target.name} は編集中ではない(先に開けば上書きできる)"
                )
        payload = self.document.log.to_json()
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

    def new_document(self) -> dict:
        """空の文書へ差し替える(「新規」)。**verbではない**(ログに載らない)。

        **今の文書は捨てられ、undoでは戻らない**(`open_document` と同じ)。確認は挟まない ——
        一覧からのクリック1つで別の文書へ移れるのに、空の文書へ移るときだけ確認を挟むと
        挙動が割れる(`web/src/FilesPanel.svelte` の「開くのに確認ダイアログを挟まない」)。

        **`save_document` は呼ばない**(自動保存はスコープ外)。保存していない編集は消える。

        **編集中のファイルも無くなる**(`current_name=None`)。空の文書はどこにも書かれて
        いないので、この直後の引数なし `save_document()` は拒否される —— 名前を付けて
        保存するまで書き込み先が存在しないのは、この操作の意味そのものである。
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
        """溜まった警告を取り出して空にする(出したら消す)。

        **リストを束ね直さず中身を空にする**(`self.warnings = []` にしない)。`DocumentView` が
        同じリストオブジェクトへ積むので、ここで別のリストへ差し替えると、以後 `view` が積んだ
        警告が誰にも読まれないまま消える。
        """
        warnings = list(self.warnings)
        self.warnings.clear()
        return warnings
