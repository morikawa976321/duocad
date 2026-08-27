"""1つの文書を持つサーバ側の状態(`Session`)。WebSocketの接続とは独立(接続が切れても文書は残る)。

verbの呼び出し・選択・パラメータ・操作ログ・保存/開くを、1つの文書の上でまとめる。
**イベントループのスレッドだけが触る**前提の上に立つ(別スレッドから入ってくる経路は
`duocad/net/tools_on_loop.py` が畳む)。誰へどう配るかは `duocad/net/ws.py` の仕事で、
ここが持つのは変更を購読者へ知らせるところまで。

**`duocad/net/` を参照しない**(向きは `net/` -> ここの一方向)。
**OCPを直接importしない**(境界はkernel.pyだけ)。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional  # Any: Session verb wrappers / listeners

from . import catalog, io_step, kernel, parameters, query, region, selection, sketch
from .document import Document
from .oplog import OpLog
from .paths import DEFAULT_MODELS_DIR
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
        self._prune_body_caches()
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
            "applied_count": self.applied_count,
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

    def _prune_body_caches(self) -> None:
        """undoで消えたBodyのキャッシュを4つのdictから落とす(セッション寿命で溜め込まない)。

        `sketches_state()` の剪定(生存集合との差分をpop)と同じ形に揃えてある。**`_revision_counter`
        は絶対に剪定しない** —— 版が1へ戻ると、同じ `body_id` を知っているクライアントが
        「版が同じ=変わっていない」と判断して前の文書の形を描き続ける
        (`_replace_document` のコメントと同じ理由)。`_revision_state` は次の `body_revision` が
        必ず版を1つ上げるだけなので落としてよいが、ここでは触らない(必須ではないため)。

        **プレビュー中(`_suppress_events` > 0)は呼ばない** —— `preview()` は
        `duocad.speculative.speculative` で文書を投機的に書き換えて必ず元へ戻す。その最中の
        生存集合で消すと、戻した後に `_volume_cache` 等の再測定が走ってしまう
        (PCケース27部品で693ms/回の再測定を避けるために入れたキャッシュなので、
        ここを無効化すると入れた目的が消える)。
        """
        if self._suppress_events:
            return
        alive = {body.id for body in self.document.bodies}
        for cache in (self._geometry_cache, self._edge_cache, self._vertex_cache, self._volume_cache):
            for body_id in [key for key in cache if key not in alive]:
                cache.pop(body_id, None)

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
