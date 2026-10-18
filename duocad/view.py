"""文書を画面へ配るための表現に変える部分(`DocumentView`)。

文書を変えず、読み取ってアプリ層が描ける形へ射影するだけの側で、持ち物はすべて**寿命の管理が要るキャッシュ**。

**依存の向きは `Session` -> ここの一方向**。持ち主が `Session` の値(`can_undo`・`applied_count`・
プレビュー中か)は呼び出し側が引数で渡す(`state_meta`。旗を複製すると戻し忘れる)。
**`duocad/net/` を参照しない・OCPを直接importしない**(境界はkernel.pyだけ)。
"""
from __future__ import annotations

import itertools
import json
from collections import OrderedDict
from typing import Any, Callable, Optional

from . import (
    kernel,
    meshbody,
    optimize,
    query,
    region,
    resolution,
    selection,
    sketch,
)
from .document import Document


def _face_count(body) -> int:
    """`body_meta` が配る面の数。**メッシュボディは常に1**(メッシュは面1枚として配る。`meshbody.geometry`)。
    三角形の枚数を入れると、面添字で指すというB-repの前提がメッシュにも成り立つように見える。
    """
    if body.mesh is not None:
        return 1
    return kernel.face_count(body.shape)


#: 状態配信で本文を伏せるログ引数。値の代わりに文字数だけを配る。
#: `state_meta()` は操作のたびにWebSocketで全部配られるので、量を持つ引数(`import_step` の STEP本文 4.6MB、
#: `import_svg` の `entities`、形状最適化4 verb の `faces` / `previous*`、`add_mesh_body` の三角形 —— 5万三角形で
#: 387 B → 2.29 MB)は素通しにできない。履歴パネルが要るのは件数だけ。伏せるのは配信用の射影だけで、
#: `document.log` と保存(`to_json`)は無傷。**`previous` が数値の操作(`set_parameter`)は素通りする**。
_REDACTED_LOG_ARGS = (
    "step", "entities", "faces", "previous", "previous_faces", "positions", "indices",
)


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


def _memo(cache: dict, body_id: str, source: Any, key: Any, compute: Callable[[], Any]) -> Any:
    """`cache[body_id]` が `(source, key, result)` で、`source` が同一かつ `key` が一致なら `result` を返す。違えば `compute()` して入れる。

    同一性は **`is`**(版ではない。可視性だけ変えたときに測り直さないため)。`id()` だけを控えると、
    旧 shape の解放後に同じアドレスが再利用されて偽ヒットしうるので、source そのものを保持する。
    """
    cached = cache.get(body_id)
    if cached is not None and cached[0] is source and cached[1] == key:
        return cached[2]
    result = compute()
    cache[body_id] = (source, key, result)
    return result


#: 面の三角形の通し番号(`_FaceTessellations.remember` が測った面に振る)。文書・形をまたいで重ならない:
#: 配信は「接続が前回受け取った面と serial が同じなら三角形を省く」(`net/wire.Delivered`)ので、別の形の同じ添字の面と
#: 取り違えない。
_FACE_SERIALS = itertools.count(1)


class _FaceTessellations:
    """1つの形の面ごとの三角形を、測り方(`(偏差, 角度偏差)`)ごとに覚える(`DocumentView._face_cache`)。

    カメラが動いて細かく測る面(focus)が1面でも変わると、ボディを丸ごと測り直していた(PC ケースでズームの行き来が
    1 回 1.3〜1.4 秒。issue #166)。面ごとに覚えれば、測るのは測り方の変わった面だけになる。測り方は細かい側(ズームで
    変わる)と粗い側の数通りしか無いので、`LIMIT` 通りまで覚える(ズームの行き来で同じ測り方へ戻ったときに測り直さない)。
    """

    #: 覚える測り方の数。細かい側はズームの段ごとに変わる(行き来する2段 + 粗い側 + 余裕)。
    LIMIT = 4

    def __init__(self) -> None:
        self.by_recipe: "OrderedDict[tuple, dict]" = OrderedDict()  # (偏差, 角度偏差) -> {面の添字: 面の三角形}
        self.untriangulated: "OrderedDict[tuple, set]" = OrderedDict()
        #: (偏差, 角度偏差) -> {面の添字: 測った回の番号}(`one_batch`)
        self.batch_of: "OrderedDict[tuple, dict]" = OrderedDict()
        self._batches = 0
        self.closed: Optional[bool] = None
        #: まだ警告していない取れなかった面と、解消できなかった境界(`seed_faces` で覚えさせた分。配るときに1回だけ出す)
        self.unwarned_failed: set = set()
        self.unwarned_boundary: list = []

    def missing(self, wanted: dict) -> dict:
        """`{面の添字: 測り方}` のうち覚えていない面を、測り方ごとにまとめる。"""
        groups: dict = {}
        for index, recipe in wanted.items():
            if index not in self.by_recipe.get(recipe, {}):
                groups.setdefault(recipe, []).append(index)
        return groups

    def remember(self, groups: dict, measured: dict) -> None:
        """測った結果を覚える。`groups` の測り方1つが1回の測定(`kernel.tessellate_faces` は組ごとに測る)。"""
        newly_failed = set(measured["untriangulated"])
        for recipe, indices in groups.items():
            self._batches += 1
            faces = self.by_recipe.setdefault(recipe, {})
            failed = self.untriangulated.setdefault(recipe, set())
            batch = self.batch_of.setdefault(recipe, {})
            for index in indices:
                if index in measured["faces"]:
                    faces[index] = dict(measured["faces"][index], serial=next(_FACE_SERIALS))
                if index in newly_failed:
                    failed.add(index)
                else:
                    failed.discard(index)
                batch[index] = self._batches
        self.closed = measured["closed"]

    def one_batch(self, recipe: tuple, indices: list) -> bool:
        """その測り方の `indices` を全部覚えていて、全部が同じ回に測ったものか。"""
        batch = self.batch_of.get(recipe, {})
        batches = {batch.get(index) for index in indices}
        return len(batches) == 1 and None not in batches and all(
            index in self.by_recipe.get(recipe, {}) for index in indices
        )

    def take(self, wanted: dict) -> list:
        """覚えた面を添字の順に返す(使った測り方を新しい側へ寄せ、`LIMIT` を超えた古い測り方を捨てる)。"""
        for recipe in dict.fromkeys(wanted.values()):
            self.by_recipe.move_to_end(recipe)
            self.untriangulated.move_to_end(recipe)
            self.batch_of.move_to_end(recipe)
        while len(self.by_recipe) > self.LIMIT:
            stale, _ = self.by_recipe.popitem(last=False)
            self.untriangulated.pop(stale, None)
            self.batch_of.pop(stale, None)
        return [self.by_recipe[wanted[index]][index] for index in sorted(wanted)]

    def failed(self, wanted: dict) -> list:
        return sorted(index for index, recipe in wanted.items() if index in self.untriangulated.get(recipe, set()))


#: ボディごとに面の三角形を覚えておく形の数(今の形・undo で戻る前の形・プレビューで測った確定前の形)。
FACE_STORES_PER_BODY = 3


class DocumentView:
    """1つの文書の「配りもの」。**文書を変えない読み取り + キャッシュ**だけを持つ。

    `Session` が1つ持ち(`session.view`)、文書の差し替え時に `reset()` を呼ぶ(古い文書の形を配らないため)。
    """

    def __init__(
        self,
        document: Document,
        deviation: float,
        warnings: list,
    ) -> None:
        #: いま配っている文書。差し替えは `reset()` だけが行う。
        self.document = document
        #: **文書の粗さの土台**(`--deviation`)。ズーム連動値が来ていない間だけ使う。画面だけの値で、
        #: STL書き出しは見ない(`io_step.STL_DEVIATION` 固定)。
        self.deviation = float(deviation)
        #: **ズーム連動の目標精度**(`set_view_resolution`)。`(線形偏差[mm], 角度偏差[rad])`。
        #: `None` は未着で、`self.deviation` + `kernel.DEFAULT_ANGULAR_DEFLECTION` を使う。
        #: 画面の設定であって文書の状態ではない(操作ログにもundoにも載らない)。
        self._view_resolution: Optional[tuple] = None
        #: **画面外・背を向けた面に使う粗さ**(`(線形偏差[mm], 角度偏差[rad])`)。
        #: `resolution.coarse_view_resolution` が決める、ズームに依存しない値。`None` は分けない。
        self._view_coarse_resolution: Optional[tuple] = None
        #: **どの面を細かく測るか**(`{body_id: frozenset(1始まりの面添字)}`)。`None` = 全Body全面を細かく。
        #: dictに居ないBodyも全面細かく。空のfrozensetは「そのBodyは全面粗く」。画面の設定で文書の状態ではない。
        self._view_focus: Optional[dict] = None
        #: 細かい側の偏差が下限に張り付いているか(`resolution.deviation_floored`)。張り付いている間は、細かく測る面の組を
        #: 同じ回に測ったものだけで組む(`_tessellate_body`)。測り方が変わるのでレシピに入れる(`body_recipe`)。
        self._view_floored = False
        #: **`Session.warnings` と同じリストオブジェクト**を共有する。`Session.take_warnings()` は
        #: 中身を空にする(束ね直すと、ここが古いリストへ書き続ける)。
        self.warnings = warnings
        # body_id -> (id(shape), visible, 目標精度) 直近の状態(`body_revision`)
        self._revision_state: dict = {}
        self._revision_counter: dict = {}  # body_id -> 現在の版(int)
        #: 形の版(`shape_revision`)。`_revision_*` と同じ持ち方で、鍵に粗さを入れない。
        self._shape_state: dict = {}
        self._shape_counter: dict = {}
        # sketch_id -> (署名, 領域つきペイロード)。領域の計算が重いので内容で引く(C)
        self._sketch_cache: dict = {}
        #: sketch_id -> (描くものの指紋, 版)。版は画面がスケッチを作り直すかの印(`_sketch_state` の `revision`)。
        #: 番号は文書をまたいで単調に増やす(`reset()` で数え直すと、別の文書の同じ id・同じ番号のスケッチを描き直さない)。
        self._sketch_revisions: dict = {}
        self._sketch_revision_seq = 0
        # 5つとも body_id -> (source, key, result)(`_memo`)。source は shape か mesh、key は
        # geometry が `body_recipe`、edges が (偏差, 角度偏差)、他は None
        self._geometry_cache: dict = {}    # tessellate結果
        self._edge_cache: dict = {}        # tessellate_edges結果(D)
        self._chain_cache: dict = {}       # 辺の連なりの器(`kernel.edge_chain_finder`)。粗さに依らない
        self._vertex_cache: dict = {}      # vertex_positions結果
        self._volume_cache: dict = {}      # query.volume結果
        #: body_id -> OrderedDict[id(形) -> (形, `_FaceTessellations`)]。形ごとに `FACE_STORES_PER_BODY` 世代まで覚える
        #: (undo で元の形へ戻ったとき・プレビューで測った新しい形を確定したときに測り直さない)。
        self._face_cache: dict = {}
        self._face_count_cache: dict = {}  # kernel.face_count結果(`body_meta`)
        #: 形状最適化の条件の配りもの(`optimize.state_payload`)の増分維持。
        #: 作り直すのは「条件が変わった」か「条件が指しているボディの形が差し替わった」ときだけ。
        self._optimize_cache = optimize.OptimizeStateCache()

    # ---------------------------------------------------------------- 可変解像度(ズーム連動)
    @property
    def view_deviation(self) -> float:
        """いま測るのに使う線形偏差[mm]。ズーム連動値が来ていればそちら、無ければ土台。"""
        return self.deviation if self._view_resolution is None else self._view_resolution[0]

    @property
    def view_angular_deflection(self) -> float:
        """いま測るのに使う角度偏差[**ラジアン**]。度ではない(`duocad/resolution.py`)。"""
        if self._view_resolution is None:
            return kernel.DEFAULT_ANGULAR_DEFLECTION
        return self._view_resolution[1]

    def _body_focus(self, body_id: str) -> Optional[frozenset]:
        """そのBodyで**細かく測る面**の集合。`None` は全面(指定が無い/そのBodyの指定が無い)。"""
        if self._view_focus is None:
            return None
        return self._view_focus.get(body_id, None)

    def body_recipe(self, body_id: str) -> tuple:
        """そのBodyを**どう測るか**の実効レシピ。キャッシュの鍵であり、版の鍵でもある。

        全面を粗く測るBodyのレシピには細かい側の値が入らないので、ズームでレシピが変わらず
        `body_revision` が上がらず、`delta()` が何も送らない。細かい側の値を無条件に入れると
        画面外のBodyまでズームのたびに配り直す(2026-08-30に14.5秒かかっていた形)。
        """
        fine = self._body_focus(body_id)
        if fine is None:
            return ("all", self._view_resolution, self._view_floored)
        if not fine:
            return ("coarse", self._view_coarse_resolution or self._view_resolution)
        return (
            "mixed",
            self._view_resolution,
            self._view_coarse_resolution,
            tuple(sorted(fine)),
            self._view_floored,
        )

    def _tessellation_args(self, body_id: str) -> tuple:
        """`kernel.tessellate_selective` に渡す `(fine_faces, dev, ang, coarse_dev, coarse_ang)`。"""
        fine = self._body_focus(body_id)
        coarse = self._view_coarse_resolution
        if coarse is None:
            # 分けない(`size_hint` が無い・まだ画面から届いていない)。
            return (fine, self.view_deviation, self.view_angular_deflection, None, None)
        return (
            fine,
            self.view_deviation,
            self.view_angular_deflection,
            coarse[0],
            coarse[1],
        )

    def body_view_resolution(self, body_id: str) -> tuple:
        """そのBody**全体**に使う `(偏差, 角度偏差)`。面ごとに分けられない配りもの(辺)が使う。
        細かい面が1つでもあれば細かい側、1つも無ければ粗い側(辺が要るのはフィレット/面取り中の1Bodyだけで画面内)。
        """
        fine = self._body_focus(body_id)
        if fine is None or fine or self._view_coarse_resolution is None:
            return (self.view_deviation, self.view_angular_deflection)
        return self._view_coarse_resolution

    @staticmethod
    def _normalize_focus(focus) -> Optional[dict]:
        """画面から届いた `focus` を `{body_id: frozenset(int)}` へ正規化する。
        型違い・整数でない・負の添字は黙って落とす(落ちて空集合になれば「全面粗く」になるだけで形は壊れない)。
        """
        if focus is None:
            return None
        if not isinstance(focus, dict):
            return None
        normalized: dict = {}
        for body_id, indices in focus.items():
            if not isinstance(body_id, str):
                continue
            if indices is None:
                continue  # **明示的な「全面細かく」**。キーごと置かない
            if not isinstance(indices, (list, tuple, set, frozenset)):
                continue
            picked = set()
            for value in indices:
                if isinstance(value, bool) or not isinstance(value, int):
                    continue
                if value >= 1:
                    picked.add(value)
            normalized[body_id] = frozenset(picked)
        return normalized

    def set_view_resolution(
        self,
        world_per_pixel: float,
        size_hint: float = 0.0,
        focus=None,
    ) -> bool:
        """画面の見え方から目標精度を決め直す。**変わったら True**(呼び出し側の再配信の合図)。

        受け取るのは**カメラの事実**(画面1画素ぶんの世界長さ[mm]とモデルの外接対角[mm])で、導出済みの
        偏差ではない。式は `duocad/resolution.py` にあり、ブラウザ側 `web/src/adaptiveResolution.ts`
        と定数まで一致している(検査あり)。

        `focus` は face単位の高精度化(2026-08-30)。`{body_id: [1始まりの面添字]}` で、入っている面だけを
        細かく、残りを粗く測る。省略・キーの無いBody・`None` は全面細かく、空リストは全面粗く。
        視錐台・背面の判定はカメラを持つ画面側(`web/src/faceCulling.ts`)。粗い側は
        `resolution.coarse_view_resolution` が決めるズーム非依存の値。

        **キャッシュは捨てない**。`geometry()` / `edges()` の鍵は `body_recipe()` なので、画面外で粗いまま
        据え置かれるBodyは測ったものが生き、版も上がらず `delta()` が何も送らない。一括で捨てると
        27部品2835面で往復14.5秒かかっていた形に戻る。スケッチはブラウザ側で折るので、細かい側が動いたときだけ捨てる。

        **目標精度は文書に1つ**(単一ブラウザ前提。2026-08-27本人決定、`Server` のdocstring「同時編集は前提にしない」)。
        2つのブラウザが別ズームだと、後から来た方の粗さで両方が測り直される。同時編集を仕様にするなら
        接続ごとに分ける(`DocumentView` のキャッシュも)。
        """
        target = resolution.view_resolution(world_per_pixel, size_hint)
        coarse = resolution.coarse_view_resolution(world_per_pixel, size_hint)
        if coarse == target:
            coarse = None  # 分ける意味が無い(`size_hint` が無い/引ききっている)
        normalized = self._normalize_focus(focus)
        floored = resolution.deviation_floored(world_per_pixel, size_hint)
        if (
            self._view_resolution == target
            and self._view_coarse_resolution == coarse
            and self._view_focus == normalized
            and self._view_floored == floored
        ):
            return False
        resolution_changed = self._view_resolution != target
        self._view_resolution = target
        self._view_coarse_resolution = coarse
        self._view_focus = normalized
        self._view_floored = floored
        # geometry / edges のキャッシュは捨てない(`set_view_resolution` のdocstring)。
        if resolution_changed:
            # スケッチはブラウザ側で折るので面ごとの粗さを持たない(細かい側で1つ)。
            self._sketch_cache.clear()
        return True

    # ---------------------------------------------------------------- 文書の差し替え
    def reset(self, document: Document) -> None:
        """配る先を `document` へ差し替え、**古い文書について測ったものを全部捨てる**。

        `Session._replace_document` から呼ぶ。Bodyのidは再生で再び現れうるので、古い形状を指した
        キャッシュが残ると別の文書の形を配る。

        **版の数え上げ(`_revision_counter`)は消さない**。消すと版が1へ戻り、同じidのBodyを知っている
        クライアントが「版が同じ=変わっていない」と判断して前の文書の形を描き続ける。
        `_revision_state` だけ捨てれば、次の `body_revision` で必ず1つ上がる。
        """
        self.document = document
        self._geometry_cache.clear()
        self._face_cache.clear()
        self._edge_cache.clear()
        self._chain_cache.clear()
        self._vertex_cache.clear()
        self._volume_cache.clear()
        self._face_count_cache.clear()
        self._sketch_cache.clear()
        self._sketch_revisions.clear()
        self._optimize_cache.clear()
        self._revision_state.clear()
        self._shape_state.clear()
        # **どの面を細かく測るかも捨てる**。面の添字は文書が変われば別のものを指す。
        self._view_focus = None

    # ---------------------------------------------------------------- 状態
    def state(self, *, can_undo: bool, applied_count: int, previewing: bool) -> dict:
        """アプリ層へ渡す全状態。bodiesは配列(部品ごと)。**geometryを含む(重い)**。
        WebSocket配信(`Server._push`)は `state_meta()` + `delta()` を使い、これはテストと `get_state` の
        全量確認用。**入口は `Session.state()`**。
        """
        meta = self.state_meta(
            can_undo=can_undo, applied_count=applied_count, previewing=previewing
        )
        meta["bodies"] = [self.body_state(body) for body in self.document.bodies]
        return meta

    def state_meta(self, *, can_undo: bool, applied_count: int, previewing: bool) -> dict:
        """`state()` の軽量版。**bodiesの各要素にgeometryを含まない**(テセレーションを呼ばない)。27部品でも数KB(state()は6MB超)。"""
        self._prune_body_caches(previewing)
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
            "can_undo": can_undo,
            "can_redo": self.document.log.can_redo,
            # 位置を配る(真偽だけだと「3回undoした状態」と「1回undoした状態」が同じに見える)。
            "redo_count": self.document.log.redo_count,
            "applied_count": applied_count,
            # 名前つきの寸法値。パラメータ編集パネルが現在値を要る。
            "parameters": dict(self.document.parameters),
            "selection": selection.get_selection(self.document).to_dict(),
            "sketches": self.sketches_state(),
            # 形状最適化の条件。画面がverb起動時に役割が付いている対象をプリセレクトするのに要る。
            # 契約と指紋が合わなかった条件(`stale`)の扱いは `duocad/optimize.py` の `state_payload` が正。
            "optimize": optimize.state_payload(
                self.document, self._optimize_cache, previewing=previewing
            ),
        }

    # ---------------------------------------------------------------- C: スケッチの配信
    def sketches_state(self) -> list:
        """全スケッチを**アプリ層が描ける形**で返す。

        1件ぶん:
            {"sketch_id", "name": <str|None>, "plane": <PlaneRef>,
             "frame": {"origin","u","v","normal"},          # (u,v) と3Dの往復の規則
             "entities": [{"entity_id","kind","params","refs"}],   # 打ち直し用(名前つき)
             "drawn":    [{"kind","params"}],                      # 反復を展開した描画用
             "regions":  [{"index","area","centroid","positions","indices"}],
             "revision": int}                                          # 描くものが変わったら上がる版

        `entities` は**指せるもの**(点・辺の選択と `move_sketch_entities` の対象)、`drawn` は反復を展開した
        「実際に見えるもの」。描画は `drawn`、ピックは `entities`(複製は導出物なので指せない)。
        `refs`(パラメータ名)は値の打ち直し(`set_parameter`)の口。

        **領域の計算は重い**(`BRepAlgoAPI_Splitter`+テセレーション)ので**スケッチの内容の署名でキャッシュする**
        (署名には解決済みの数値が入り、`set_parameter` で作り直される)。
        """
        # undoで消えたスケッチのキャッシュを落とす(セッション寿命で溜め込まない)
        alive = {item.id for item in self.document.sketches}
        for sketch_id in [key for key in self._sketch_cache if key not in alive]:
            self._sketch_cache.pop(sketch_id, None)
        for sketch_id in [key for key in self._sketch_revisions if key not in alive]:
            self._sketch_revisions.pop(sketch_id, None)
        return [self._sketch_state(item) for item in self.document.sketches]

    def _sketch_state(self, item) -> dict:
        try:
            drawn = region.expanded_entities(self.document, item.id)
        except Exception as error:  # 壊れたスケッチで画面全体を落とさない
            self.warnings.append(f"スケッチ {item.id} を描けない: {type(error).__name__}: {error}")
            drawn = []
        # **貼った先のボディの形状は署名に入れない**。面に貼ったスケッチの姿勢は `item.plane["frame"]` が持つ
        # (`duocad.region.plane_frame`)ので、後からのフィレット等で枠も領域も変わらない。
        # `rebuild()` で姿勢が解き直されると `plane` が変わるので署名が拾う。
        signature = json.dumps(
            [item.plane, drawn, self.view_deviation], sort_keys=True, default=str
        )
        cached = self._sketch_cache.get(item.id)
        if cached is not None and cached[0] == signature:
            payload = dict(cached[1])
        else:
            frame: Optional[dict] = None
            regions: list = []
            try:
                frame = region.plane_frame(self.document, item.id)
                if drawn:
                    regions = region.regions_with_geometry(
                        self.document,
                        item.id,
                        self.view_deviation,
                        on_error=lambda index, error, sketch_id=item.id: self.warnings.append(
                            f"スケッチ {sketch_id} の領域 {index} を三角形にできない: {type(error).__name__}: {error}"
                        ),
                    )
            except Exception as error:
                self.warnings.append(
                    f"スケッチ {item.id} の領域を計算できない: {type(error).__name__}: {error}"
                )
            payload = {"frame": frame, "drawn": drawn, "regions": regions}
            self._sketch_cache[item.id] = (signature, payload)
            payload = dict(payload)
        payload["sketch_id"] = item.id
        payload["plane"] = item.plane
        payload["visible"] = item.visible
        payload["name"] = item.name
        # 図形そのものは軽いのでキャッシュしない。**try で包む**(上の `drawn` だけ守っても、
        # 同じパラメータ解決のこちらが落ちると `state_meta()` ごと落ち、以後の配信が全部止まる)。
        try:
            payload["entities"] = [
                {
                    "entity_id": entity.id,
                    "kind": entity.kind,
                    "params": sketch.entity_params(self.document, item.id, entity.id),
                # 値が名前つきパラメータならその名前、素の数値なら None。
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
        # 版: 画面が描くもの(枠・展開した図形・領域 = 署名、点と辺の取っ手 = `entities`)が変わったときだけ上がる。
        # 選択・表示・名前では上がらない(画面は版が同じスケッチを作り直さず、選択の色だけ塗り直す)。
        fingerprint = (signature, json.dumps(payload["entities"], sort_keys=True, default=str))
        known = self._sketch_revisions.get(item.id)
        if known is None or known[0] != fingerprint:
            self._sketch_revision_seq += 1
            known = (fingerprint, self._sketch_revision_seq)
            self._sketch_revisions[item.id] = known
        payload["revision"] = known[1]
        return payload

    # ---------------------------------------------------------------- M3.5: 差分送信
    def body_revision(self, body_id: str) -> int:
        """そのBodyの版。**「経路」ではなく「状態」から導く** — 形状の同一性(id)とvisibleのタプルが
        前回の記録と違ったら+1する(押し出し・undo・redo・可視性変更のどれでも自動的に効く)。
        """
        body = self.document.get_body(body_id)
        # 粗さも「状態」のうち。入れないと、ズームで目標精度が変わっても差分配信が「版が同じ」と
        # 判断して古い粗さのメッシュが画面に残る。メッシュボディは `shape` が常に None なので
        # **メッシュの同一性も鍵に入れる**(`id(None)` は常に同じ値で、版が二度と上がらない)。
        # メッシュボディは持っている三角形をそのまま配り粗さが効かないので、粗さは入れない(入れると、ズームのたびに
        # 変わっていないメッシュ全体を配り直す)。
        recipe = None if body.mesh is not None else self.body_recipe(body_id)
        key = (id(body.shape), id(body.mesh), body.visible, recipe)
        if self._revision_state.get(body_id) != key:
            self._revision_state[body_id] = key
            self._revision_counter[body_id] = self._revision_counter.get(body_id, 0) + 1
        return self._revision_counter[body_id]

    def shape_revision(self, body_id: str) -> int:
        """そのBodyの**形**の版。`body_revision` から粗さ(`body_recipe`)を除いたもので、形の差し替えと可視性でだけ上がる。

        画面はこれで「形が変わった配信」と「粗さだけ変わった配信(カメラ由来)」を見分ける。プレビューを投げ直すのは
        形が変わったときだけでよい(カメラを回すたびに投機実行が走っていた。issue #169)。
        """
        body = self.document.get_body(body_id)
        key = (id(body.shape), id(body.mesh), body.visible)
        if self._shape_state.get(body_id) != key:
            self._shape_state[body_id] = key
            self._shape_counter[body_id] = self._shape_counter.get(body_id, 0) + 1
        return self._shape_counter[body_id]

    def geometry(self, body_id: str) -> dict:
        """1Bodyのテセレーション結果(キャッシュ付き)。不可視Bodyは ValueError。
        キャッシュは**形状の同一性**で判定する(版ではない。可視性だけ変えたときに測り直さないため)。
        """
        body = self.document.get_body(body_id)
        if not body.visible:
            raise ValueError(f"body {body_id!r} is not visible: 不可視Bodyは測らない")
        if body.mesh is not None:
            # 持っている三角形をそのまま配る(可変解像度は効かない)。`body_state()` は状態配信のたびに
            # 呼ばれ、`meshbody.geometry` は毎回 positions/indices をリストへ写す(数万三角形で十数万要素)
            # ので**メッシュの同一性**(`is`)でキャッシュする。
            return _memo(self._geometry_cache, body_id, body.mesh, None, lambda: meshbody.geometry(body.mesh))
        recipe = self.body_recipe(body_id)
        return _memo(self._geometry_cache, body_id, body.shape, recipe, lambda: self._tessellate_body(body))

    def _face_store(self, body_id: str, shape: Any) -> "_FaceTessellations":
        """その形の面の三角形の控え(無ければ作る)。ボディごとに `FACE_STORES_PER_BODY` 個の形まで覚える。"""
        stores = self._face_cache.setdefault(body_id, OrderedDict())
        held = stores.get(id(shape))
        if held is not None and held[0] is shape:
            stores.move_to_end(id(shape))
            return held[1]
        store = _FaceTessellations()
        stores[id(shape)] = (shape, store)
        while len(stores) > FACE_STORES_PER_BODY:
            stores.popitem(last=False)
        return store

    def seed_faces(self, body_id: str, shape: Any, deviation: float, angular: float, geometry: dict) -> None:
        """全面を同じ測り方で測った結果(`kernel.tessellate` の戻り値)を、その形の面の三角形として覚えさせる。

        プレビュー(`Session.preview_job`)は確定後の形そのものを画面の粗さで全面測っている(確定は覚えた結果の形を
        使い回す。`kernel.remembered`)ので、確定後の配信で同じ形を測り直さない(PC ケースの最大ボディで 0.15 秒)。
        """
        recipe = (float(deviation), float(angular))
        indices = [face["index"] for face in geometry["faces"]]
        measured = {
            "faces": {face["index"]: face for face in geometry["faces"]},
            "untriangulated": list(geometry.get("untriangulated", [])),
            "closed": kernel.is_closed_shell(shape),
        }
        store = self._face_store(body_id, shape)
        store.remember({recipe: indices}, measured)
        # プレビューは警告を積まない。確定後に配るとき1回だけ出す(`_tessellate_body`)
        store.unwarned_failed |= set(measured["untriangulated"])
        store.unwarned_boundary.extend((geometry.get("boundary") or {}).get("unresolved") or [])

    def _tessellate_body(self, body) -> dict:
        """B-rep を測り、取れなかった面・解消できなかった境界の警告を積む(**測ったときだけ**出す)。

        面ごとに測り方(細かい側・粗い側)を決め、覚えていない面だけを測る(`_FaceTessellations`)。測り方の決め方と
        戻り値の形は `kernel.tessellate_selective` と同じ。
        """
        fine, deviation, angular, coarse_deviation, coarse_angular = self._tessellation_args(body.id)
        total = self._body_face_count(body)
        coarse = (
            max(float(deviation if coarse_deviation is None else coarse_deviation), float(deviation)),
            max(float(angular if coarse_angular is None else coarse_angular), float(angular)),
        )
        detailed = (float(deviation), float(angular))
        fine_set = set(range(1, total + 1)) if fine is None else {int(i) for i in fine if 1 <= int(i) <= total}
        # 細かい面が1つでもあり、粗い側が細かい側と同じなら全面を細かい側で測る(報告する `detail` と数は求められた面のまま)
        everything_detailed = bool(fine_set) and (len(fine_set) == total or coarse == detailed)
        wanted = {
            index: detailed if everything_detailed or index in fine_set else coarse for index in range(1, total + 1)
        }
        store = self._face_store(body.id, body.shape)
        groups = store.missing(wanted)
        if self._view_floored:
            # 別々の回に測った面の境目の隙間は偏差の2倍程度で、偏差が画素から決まっていれば1画素未満だが、
            # 下限に張り付くと画素に対して大きくなる(再現: 1画素 1e-6 mm で 40 画素)。細かい面の組は1回で測ったものだけを使う
            fine_group = [index for index, recipe in wanted.items() if recipe == detailed]
            if fine_group and not store.one_batch(detailed, fine_group):
                groups[detailed] = fine_group
        measured = None
        if groups:
            measured = kernel.tessellate_faces(body.shape, groups)
            store.remember(groups, measured)
        faces = [dict(face, detail="fine" if face["index"] in fine_set else "coarse") for face in store.take(wanted)]
        untriangulated = store.failed(wanted)
        if faces and len(untriangulated) == len(faces):
            raise ValueError(f"三角形分割が1面も取れなかった(deviation={deviation}, coarse={coarse[0]})")
        result = {
            "deviation": detailed[0] if fine_set else coarse[0],
            "face_count": len(faces),
            "bbox": kernel.bounding_box_of_faces(faces),
            "faces": faces,
            "untriangulated": untriangulated,
            "coarse_deviation": coarse[0],
            "fine_face_count": len(fine_set),
            "closed": bool(store.closed),
            # 診断は測ったときだけ(覚えていた面には無い)
            "boundary": measured["boundary"] if measured is not None else {},
        }
        # 警告は測ったとき(と、プレビューで測って覚えさせた形を初めて配るとき)だけ出す
        fresh_failed = set(measured["untriangulated"]) if measured is not None else set()
        newly_failed = sorted(set(untriangulated) & (fresh_failed | store.unwarned_failed))
        store.unwarned_failed -= set(untriangulated)
        if newly_failed:
            self.warnings.append(
                f"三角形分割が取れなかった面({body.name}): {newly_failed}(画面に出ません)"
            )
        unresolved = ((result.get("boundary") or {}).get("unresolved") or []) + store.unwarned_boundary
        store.unwarned_boundary = []
        if unresolved:
            # 境界折れ線の交差を再分割で解消できなかった面は分割していない(kernel_meshing)。
            described = [f"{entry['face']}:{','.join(sorted({i['kind'] for i in entry['issues']}))}" for entry in unresolved]
            self.warnings.append(
                f"境界の交差を解消できなかった面({body.name}): {described}(画面に出ません)"
            )
        return result

    def edges(self, body_id: str) -> dict:
        """1Bodyの辺を折れ線で返す(キャッシュ付き)。

        戻り値: `{"body_id", "revision", "edges": [kernel.tessellate_edges の1件, ...]}`

        **`state` にも `delta` にも入れない**(辺が要るのはフィレット・面取り中だけで、常時配ると
        27部品(2835面)の初回配信にその数倍の折れ線が乗る)。`revision` は、クライアントが「今の形の辺か」
        を判定するためで、古い辺を指したままフィレットをかける(別の辺が丸まる)経路を閉じる。
        """
        body = self._visible_brep(body_id, "辺")
        revision = self.body_revision(body_id)
        edge_deviation, edge_angular = self.body_view_resolution(body_id)
        described = _memo(
            self._edge_cache, body_id, body.shape, (edge_deviation, edge_angular),
            lambda: kernel.tessellate_edges(body.shape, edge_deviation, edge_angular),
        )
        return {"body_id": body_id, "revision": revision, "edges": described}

    def edge_chains(self, body_id: str, edge_indices: list) -> dict:
        """頼まれた辺それぞれが属する連なり(フィレット・面取りで1本として振る舞う辺の組)を返す。

        戻り値: `{"body_id", "revision", "chains": [[連なりの辺の添字(昇順)], ...]}`(頼んだ辺の順。同じ連なりは1つにまとめる)

        `edges()` に入れない理由: 連なりは `MakeFillet.Add` で求め、大きな部品では折れ線の数十倍かかる(PC ケースの
        最大ボディで折れ線 13ms・全辺の連なり 0.5 秒)。画面はホバー・クリックした辺の分だけ頼む(辺1本で `Add` 1回)。
        連なりは形にだけ依存するので、鍵は形の同一性だけ(ズームで粗さが変わっても求め直さない)。
        """
        body = self._visible_brep(body_id, "辺")
        revision = self.body_revision(body_id)
        finder = _memo(self._chain_cache, body_id, body.shape, None, lambda: kernel.edge_chain_finder(body.shape))
        groups = finder.of(edge_indices)
        chains = list(dict.fromkeys(groups[int(index)] for index in edge_indices))
        return {"body_id": body_id, "revision": revision, "chains": [list(group) for group in chains]}

    def vertices(self, body_id: str) -> dict:
        """1Bodyの頂点を1始まりの添字つきの3D座標で返す(キャッシュ付き)。

        戻り値: `{"body_id", "revision", "vertices": [{"index", "point": [x,y,z]}, ...]}`

        `edges()` と同じく配信しない。画面に出るのは**選択に入った頂点だけ**(どれが選ばれるかを
        事前に知らないので全頂点を返す)。`revision` の理由も辺と同じ。
        """
        body = self._visible_brep(body_id, "頂点")
        revision = self.body_revision(body_id)
        described = _memo(
            self._vertex_cache, body_id, body.shape, None, lambda: query.describe_vertices(body.shape)
        )
        return {"body_id": body_id, "revision": revision, "vertices": described}

    def _visible_brep(self, body_id: str, what: str):
        """辺・頂点(`what`)を配ってよい Body。存在 → B-rep → 形あり → 可視の順で断る。
        **不可視Bodyの辺・頂点は配らない**(隠した部品の辺だけが宙に浮き、しかも指せてしまう)。
        `geometry()` はこれを使わない(不可視を先に断り、メッシュは断らずに配る)。
        """
        body = self.document.get_body(body_id)
        if body.mesh is not None:
            meshbody.reject(body, f"{what}を数える")
        if body.shape is None:
            raise ValueError(f"body {body_id!r} has no shape ({what}を数えられない)")
        if not body.visible:
            raise ValueError(f"body {body_id!r} is not visible: 隠れているものの{what}は指せない")
        return body

    def _prune_body_caches(self, previewing: bool) -> None:
        """undoで消えたBodyのキャッシュを6つのdictから落とす。

        **`_revision_counter` は絶対に剪定しない**(版が1へ戻ると前の文書の形を描き続ける。`reset()`)。
        **プレビュー中(`previewing`)は何も落とさない** —— `Session.preview()` は文書を投機的に書き換えて
        元へ戻すので、その最中の生存集合で消すと戻した後に再測定が走る(PCケース27部品で693ms/回)。
        """
        if previewing:
            return
        alive = {body.id for body in self.document.bodies}
        for cache in (
            self._geometry_cache, self._edge_cache, self._chain_cache, self._vertex_cache, self._volume_cache,
            self._face_count_cache, self._face_cache,
        ):
            for body_id in [key for key in cache if key not in alive]:
                cache.pop(body_id, None)

    def delta(self, known_revisions: dict) -> dict:
        """クライアントが持っている版との差分だけを返す(未知のIDは必ずbodiesへ)。

        戻り値:
            {
              "bodies":    [<state()の bodies と同じ形。ただし版が変わったBodyだけ>],
              "unchanged": [BodyId, ...],   # 版が一致したBody
            }

        クライアントが知っているのに `bodies` にも `unchanged` にも現れないIDは**文書から消えた**
        (undoでの消滅。専用の "removed" は持たない)。
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

        **公開メソッド**: `DocumentTools._body_summary`(mcp_server.py)がMCP封筒の `bodies[]` 要素にも使う
        (MCP側は封筒の契約に合わせて `revision` キーを落とす)。`volume` と `face_count` はキャッシュ付き(`_memo`)。
        `face_count` は 27 部品で合計 2 ms だが、配信のたびに OCP を 27 回呼ぶと、ワーカーでプレビューを計算している間は
        1回ごとに GIL の受け渡しを待つ(軽い要求の往復が 84 ms になっていた。issue #168)。
        """
        return {
            "body_id": body.id,
            "name": body.name,
            "kind": body.kind,
            "visible": body.visible,
            "revision": self.body_revision(body.id),
            "shape_revision": self.shape_revision(body.id),
            "face_count": self._body_face_count(body),
            "volume": self._body_volume(body),
        }

    def _body_face_count(self, body) -> int:
        """`_face_count` のキャッシュ付き(形・メッシュの同一性で引く)。"""
        source = body.mesh if body.mesh is not None else body.shape
        return _memo(self._face_count_cache, body.id, source, None, lambda: _face_count(body))

    def _body_volume(self, body) -> float:
        """bodyの体積(キャッシュ付き。`geometry()` と同じ `_memo`)。
        `query.volume` は面ごとの数値積分で、キャッシュが無いと可視性切替だけで全ボディ測り直していた(PCケースで693ms/回)。
        """
        if body.mesh is not None:
            return _memo(self._volume_cache, body.id, body.mesh, None, lambda: meshbody.volume(body.mesh))
        return _memo(self._volume_cache, body.id, body.shape, None, lambda: query.volume(body.shape))

    def body_state(self, body) -> dict:
        """1Bodyぶんの状態(stateとdeltaが共有する)。`body_meta` にgeometryを足したもの。"""
        meta = self.body_meta(body)
        meta["geometry"] = self.geometry(body.id) if body.visible else None
        return meta
