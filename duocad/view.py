"""文書を画面へ配るための表現に変える部分(`DocumentView`)。

文書を変えず、読み取ってアプリ層が描ける形へ射影するだけの側で、持ち物はすべて**寿命の管理が要るキャッシュ**。

**依存の向きは `Session` -> ここの一方向**。持ち主が `Session` の値(`can_undo`・`applied_count`・
プレビュー中か)は呼び出し側が引数で渡す(`state_meta`。旗を複製すると戻し忘れる)。
**`duocad/net/` を参照しない・OCPを直接importしない**(境界はkernel.pyだけ)。
"""
from __future__ import annotations

import itertools
import json
import math
from array import array
from collections import OrderedDict
from typing import Any, Callable, Optional

from . import (
    kernel,
    kernel_face_edit,
    kernel_project,
    meshbody,
    optimize,
    parameters,
    query,
    region,
    resolution,
    selection,
    sketch,
)
from .document import Document

from . import (
    brep_topology as _brep_topology,
)


def _face_count(body) -> int:
    """`body_meta` が配る面の数。**メッシュボディは常に1**(メッシュは面1枚として配る。`meshbody.geometry`)。
    三角形の枚数を入れると、面添字で指すというB-repの前提がメッシュにも成り立つように見える。
    """
    if body.mesh is not None:
        return 1
    return _brep_topology.face_count(body.shape)


#: 状態配信で本文を伏せるログ引数。値の代わりに文字数だけを配る。
#: `state_meta()` は操作のたびにWebSocketで全部配られるので、量を持つ引数(`import_step` の STEP本文 4.6MB、
#: `import_svg` の `entities`、形状最適化4 verb の `faces` / `previous*`、`add_mesh_body` の三角形 —— 5万三角形で
#: 387 B → 2.29 MB)は素通しにできない。履歴パネルが要るのは件数だけ。伏せるのは配信用の射影だけで、
#: `document.log` と保存(`to_json`)は無傷。**`previous` が数値の操作(`set_parameter`)は素通りする**。
_REDACTED_LOG_ARGS = (
    "step", "entities", "faces", "previous", "previous_faces", "positions", "indices", "brep",
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


class HiddenBody(ValueError):
    """不可視Bodyの辺・頂点を頼まれた(`DocumentView._visible_brep`)。WS の `get_edges` / `get_vertices` は
    これを失敗にせず `hidden` の返事にする(画面の可視性はサーバより遅れて届く。`duocad/net/ws.py`)。"""


class _FaceTessellations:
    """1つの形の面ごとの三角形を、測り方(`(偏差, 角度偏差)`)ごとに覚える(`DocumentView._face_cache`)。

    カメラが動いて細かく測る面(focus)が1面でも変わると、ボディを丸ごと測り直していた(PC ケースでズームの行き来が
    1 回 1.3〜1.4 秒。issue #166)。面ごとに覚えれば、測るのは測り方の変わった面だけになる。測り方は細かい側(ズームで
    変わる)と粗い側の数通りしか無いので、`LIMIT` 通りまで覚える(ズームの行き来で同じ測り方へ戻ったときに測り直さない)。
    """

    #: 覚える測り方の数。細かい側はズームの段ごとに変わる(行き来する2段 + 粗い側 + 余裕)。
    LIMIT = 4

    def __init__(self, shape: Any = None) -> None:
        #: 覚えている形(面の同一性の鍵を引くため。`lend`)
        self.shape = shape
        self._keys: Optional[list] = None  # 面の添字の順の、面の同一性の鍵(`kernel.face_keys`。要るときに1回)
        self._index_of: dict = {}  # 鍵 -> 添字
        self._placements: Optional[list] = None  # 面の添字の順の (位置を外した鍵, 位置)(`kernel.face_placements`)
        self._bare_index_of: dict = {}  # 位置を外した鍵 -> 添字
        self.by_recipe: "OrderedDict[tuple, dict]" = OrderedDict()  # (偏差, 角度偏差) -> {面の添字: 面の三角形}
        self.untriangulated: "OrderedDict[tuple, set]" = OrderedDict()
        #: (偏差, 角度偏差) -> {面の添字: 測った回の番号}(`one_batch`)
        self.batch_of: "OrderedDict[tuple, dict]" = OrderedDict()
        self._batches = 0
        self.closed: Optional[bool] = None
        #: 配ったことがあるか(`_tessellate_body`)。プレビューが覚えさせただけの形(`seed_faces`)は、追い出すときに先に捨てる
        self.delivered = False
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
        """測った結果を覚える。`groups` の測り方1つが1回の測定(`kernel.tessellate_faces` は組ごとに測る)。
        `serial` を持って来た面(プレビューが元の形から借りた面)はそれを引き継ぐ(同じ三角形なので、画面は送り直しを要らない)。"""
        newly_failed = set(measured["untriangulated"])
        for recipe, indices in groups.items():
            self._batches += 1
            faces = self.by_recipe.setdefault(recipe, {})
            failed = self.untriangulated.setdefault(recipe, set())
            batch = self.batch_of.setdefault(recipe, {})
            for index in indices:
                if index in measured["faces"]:
                    face = measured["faces"][index]
                    faces[index] = dict(face, serial=face.get("serial") or next(_FACE_SERIALS))
                if index in newly_failed:
                    failed.add(index)
                else:
                    failed.discard(index)
                batch[index] = self._batches
        if measured.get("closed") is not None:
            self.closed = measured["closed"]

    def keys(self) -> list:
        """面の添字の順の、面の同一性の鍵。"""
        if self._keys is None:
            self._keys = kernel.face_keys(self.shape)
            self._index_of = {key: index for index, key in enumerate(self._keys, start=1)}
        return self._keys

    def placements(self) -> list:
        """面の添字の順の `(位置を外した面の同一性の鍵, 位置)`。"""
        if self._placements is None:
            self._placements = kernel.face_placements(self.shape)
            self._bare_index_of = {key: index for index, (key, _placement) in enumerate(self._placements, start=1)}
        return self._placements

    def lend_placed(self, bare_key: Any, recipe: tuple) -> Optional[tuple]:
        """位置だけ違う同じ面(TShape と向きが同じ)をその測り方で測った `(三角形, その面の位置)`。無ければ None。"""
        placements = self.placements()
        index = self._bare_index_of.get(bare_key)
        if index is None or index in self.untriangulated.get(recipe, ()):
            return None
        face = self.by_recipe.get(recipe, {}).get(index)
        return None if face is None else (face, placements[index - 1][1])

    def measured(self, recipe: tuple) -> dict:
        """その測り方で測れた面(添字 -> 三角形)の写し。"""
        failed = self.untriangulated.get(recipe, set())
        return {index: face for index, face in self.by_recipe.get(recipe, {}).items() if index not in failed}

    def lend(self, key: Any, recipe: tuple) -> Optional[dict]:
        """同じ面(`key`)をその測り方で測った三角形。持っていない・取れなかった面は None。"""
        self.keys()
        index = self._index_of.get(key)
        if index is None or index in self.untriangulated.get(recipe, ()):
            return None
        return self.by_recipe.get(recipe, {}).get(index)

    def adopt(self, recipe: tuple, index: int, face: dict) -> None:
        """別の形で測った同じ面の三角形を、この形の `index` の面として覚える。回は面ごとに新しく振る(下限に張り付いたときの
        「1回で測った組」に数えない)。`serial` はそのまま(三角形が同じなので、画面が持っていれば送り直さない)、
        位置を変えて写した三角形(`serial` を持たない)には新しく振る。"""
        self._batches += 1
        self.by_recipe.setdefault(recipe, {})[index] = dict(face, index=index, serial=face.get("serial") or next(_FACE_SERIALS))
        self.untriangulated.setdefault(recipe, set()).discard(index)
        self.batch_of.setdefault(recipe, {})[index] = self._batches

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


def _remap_focus(shape: Any, basis: Any, fine: frozenset) -> frozenset:
    """`basis` の面の添字で書いた細かく測る面 `fine` を、`shape` の面の添字へ直す。`basis` にそのまま残った面(同じ面・子の対・
    位置だけ違う面)は `basis` での指定を引き継ぎ、残っていない面(新しい面・変わった面)は細かい側にする(画面が測り直して
    送ってくるまで、見えているかもしれない面を粗く出さない)。"""
    same = dict(kernel.kept_pairs(shape, basis))
    noted = kernel.kept_faces(basis)
    if noted is not None and noted[0] is shape and noted[2] is None:  # undo で子の結果から元の形へ戻った
        for position, index in noted[1]:
            same.setdefault(index, position)
    if not same:  # 移動(TShape はそのままで位置だけ違う)
        before = {key: index for index, (key, _placement) in enumerate(kernel.face_placements(basis), start=1)}
        for position, (key, _placement) in enumerate(kernel.face_placements(shape), start=1):
            if key in before:
                same[position] = before[key]
    total = _brep_topology.face_count(shape)
    return frozenset(position for position in range(1, total + 1) if position not in same or same[position] in fine)


#: ボディごとに面の三角形を覚えておく形の数(今の形・undo で戻る前の形・プレビューで測った確定前の形)。
FACE_STORES_PER_BODY = 3
#: 消えたボディの形の面の三角形を覚えておく数(削除・undo で消えたボディが undo / redo で戻ったときに貸す。`_borrow_faces`)。
RETIRED_FACE_STORES = 6


#: スケッチごとに塗り(領域の三角形)を覚えておく粗さの段の数。寄って戻る・引いて戻るで使い回す。
SKETCH_FILLS_PER_SKETCH = 4
#: 辺が直線だけになる図形。これだけのスケッチの塗りは偏差によらない(`_SketchFills.straight`)。
_STRAIGHT_KINDS = frozenset({"point", "line", "rectangle"})


class _SketchFills:
    """1つのスケッチについて、描くもの(姿勢と展開した図形)が同じ間に使い回すもの。すべて描くものだけで決まる。"""

    __slots__ = ("document", "content", "frame", "detection", "straight", "fills")

    def __init__(self, document: Document, content: str) -> None:
        #: 作った文書(開き直したら作り直し、壊れたスケッチの知らせも出し直す)
        self.document = document
        self.content = content
        self.frame: Optional[dict] = None
        #: `None` は図形が無いか、枠・検出に失敗した(どの粗さでも領域は空)
        self.detection: Optional[region.Detection] = None
        #: 図形が直線だけ。領域の輪郭は偏差によらず厳密で、平面の三角形分割も偏差によらない(実測: 長方形・交わる線分の
        #: スケッチで偏差 5 〜 0.001 mm の塗りがバイト単位で一致)ので、塗りを1つだけ持ってどの粗さにも使う。
        self.straight = False
        #: 偏差(`straight` なら `None`) -> 領域の一覧(`region.fill`)。最近使った順に `SKETCH_FILLS_PER_SKETCH` 個まで。
        #: 三角形は `array` に詰めて持つ(実測: 円 384 個の外周を持つ 385 領域・2.8 万三角形で、数の list は 1 段 5.2 MB)。
        self.fills: "OrderedDict[Optional[float], list]" = OrderedDict()

    def lookup(self, deviation: Optional[float]) -> Optional[list]:
        packed = self.fills.get(deviation)
        if packed is None:
            return None
        self.fills.move_to_end(deviation)
        return [dict(item, positions=item["positions"].tolist(), indices=item["indices"].tolist()) for item in packed]

    def keep(self, deviation: Optional[float], regions: list) -> None:
        self.fills[deviation] = [
            dict(item, positions=array("d", item["positions"]), indices=array("q", item["indices"])) for item in regions
        ]
        while len(self.fills) > SKETCH_FILLS_PER_SKETCH:
            self.fills.popitem(last=False)


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
        #: `None` は未着で、文書の外接対角から決めた値を使う(`resolution.document_resolution`。空の文書は `self.deviation`)。
        #: 画面の設定であって文書の状態ではない(操作ログにもundoにも載らない)。
        self._view_resolution: Optional[tuple] = None
        #: 未着の間の粗さの覚え `(表示中のボディの形の id の組, 粗さ)`。形が変わらない限り外接箱を測り直さない。
        self._document_resolution_memo: Optional[tuple] = None
        #: **画面外・背を向けた面に使う粗さ**(`(線形偏差[mm], 角度偏差[rad])`)。
        #: `resolution.coarse_view_resolution` が決める、ズームに依存しない値。`None` は分けない。
        self._view_coarse_resolution: Optional[tuple] = None
        #: **どの面を細かく測るか**(`{body_id: frozenset(1始まりの面添字)}`)。`None` = 全Body全面を細かく。
        #: dictに居ないBodyも全面細かく。空のfrozensetは「そのBodyは全面粗く」。画面の設定で文書の状態ではない。
        self._view_focus: Optional[dict] = None
        #: `focus` が届いたときの各ボディの形(`focus` の面の添字はこの形のもの)と、今の形の添字に直した写し
        #: (body_id -> (今の形, 届いたときの形, 届いた集合, 直した集合))。`_body_focus`
        self._focus_basis: dict = {}
        self._focus_remapped: dict = {}
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
        #: sketch_id -> `_SketchFills`。粗さが変わっても残す(`_sketch_cache` は `set_view_resolution` が捨てる)。
        self._sketch_fills: dict = {}
        #: (sketch_id, 失敗の種類) -> 最後に知らせた文。同じ失敗を配信のたびに積み直さない(直れば消す)。
        self._sketch_failures: dict = {}
        #: sketch_id -> (描くものの指紋, 版)。版は画面がスケッチを作り直すかの印(`_sketch_state` の `revision`)。
        #: 番号は文書をまたいで単調に増やす(`reset()` で数え直すと、別の文書の同じ id・同じ番号のスケッチを描き直さない)。
        self._sketch_revisions: dict = {}
        self._sketch_revision_seq = 0
        # 6つとも body_id -> (source, key, result)(`_memo`)。source は shape か mesh、key は
        # geometry が `body_recipe`、edges が (偏差, 角度偏差)、guides が平面、他は None
        self._geometry_cache: dict = {}    # tessellate結果
        self._edge_cache: dict = {}        # tessellate_edges結果(D)
        self._chain_cache: dict = {}       # 辺の連なりの器(`kernel.edge_chain_finder`)。粗さに依らない
        self._vertex_cache: dict = {}      # vertex_positions結果
        self._volume_cache: dict = {}      # query.volume結果
        self._guide_cache: dict = {}       # 平面との交わり(`sketch_guides`)。直近の1平面だけ覚える
        #: body_id -> OrderedDict[id(形) -> (形, `_FaceTessellations`)]。形ごとに `FACE_STORES_PER_BODY` 世代まで覚える
        #: (undo で元の形へ戻ったとき・プレビューで測った新しい形を確定したときに測り直さない)。
        self._face_cache: dict = {}
        #: 消えたボディの `_face_cache` の中身(id(形) -> (形, `_FaceTessellations`))。新しい順に `RETIRED_FACE_STORES` 個
        self._retired_faces: "OrderedDict[int, tuple]" = OrderedDict()
        self._face_count_cache: dict = {}  # brep_topology.face_count結果(`body_meta`)
        #: 形状最適化の条件の配りもの(`optimize.state_payload`)の増分維持。
        #: 作り直すのは「条件が変わった」か「条件が指しているボディの形が差し替わった」ときだけ。
        self._optimize_cache = optimize.OptimizeStateCache()

    # ---------------------------------------------------------------- 可変解像度(ズーム連動)
    @property
    def view_deviation(self) -> float:
        """いま測るのに使う線形偏差[mm]。ズーム連動値が来ていればそちら、無ければ文書の外接対角から決めた値。"""
        return self._effective_resolution()[0]

    @property
    def view_angular_deflection(self) -> float:
        """いま測るのに使う角度偏差[**ラジアン**]。度ではない(`duocad/resolution.py`)。"""
        return self._effective_resolution()[1]

    def _effective_resolution(self) -> tuple:
        """`_view_resolution` があればそれ、未着なら表示中のボディの外接対角から決めた値(形が変わるまで覚える)。"""
        if self._view_resolution is not None:
            return self._view_resolution
        shapes = tuple(
            id(body.shape) for body in self.document.bodies
            if body.visible and body.mesh is None and body.shape is not None
        )
        memo = self._document_resolution_memo
        if memo is not None and memo[0] == shapes:
            return memo[1]
        lows: list = []
        highs: list = []
        for body in self.document.bodies:
            if body.visible and body.mesh is None and body.shape is not None:
                box = kernel.bounding_box(body.shape, cache=self.document.bbox_cache)
                lows.append(box["min"])
                highs.append(box["max"])
        if not lows:
            return (self.deviation, kernel.DEFAULT_ANGULAR_DEFLECTION)  # 空の文書は覚えない
        diagonal = math.dist(
            [min(low[axis] for low in lows) for axis in range(3)],
            [max(high[axis] for high in highs) for axis in range(3)],
        )
        found = resolution.document_resolution(diagonal)
        self._document_resolution_memo = (shapes, found)
        return found

    def _body_focus(self, body_id: str) -> Optional[frozenset]:
        """そのBodyで**細かく測る面**の集合。`None` は全面(指定が無い/そのBodyの指定が無い)。

        画面の `focus` は届いたときの形の面の添字なので、操作で形が変わってから画面が測り直して送ってくるまでは、今の形の添字に
        直して使う(`_remap_focus`)。添字のまま当てると、番号のずれた残った面を別の測り方で測り直す(--part big を上から見た画面で、
        辺1本のフィレットの後の配信 10 秒のうち 4 秒)。"""
        if self._view_focus is None:
            return None
        fine = self._view_focus.get(body_id, None)
        basis = self._focus_basis.get(body_id)
        if fine is None or basis is None:
            return fine
        shape = next((body.shape for body in self.document.bodies if body.id == body_id), None)
        if shape is None or shape is basis:
            return fine
        held = self._focus_remapped.get(body_id)
        if held is None or held[0] is not shape or held[1] is not basis or held[2] is not fine:
            held = (shape, basis, fine, _remap_focus(shape, basis, fine))
            self._focus_remapped[body_id] = held
        return held[3]

    def body_recipe(self, body_id: str) -> tuple:
        """そのBodyを**どう測るか**の実効レシピ。キャッシュの鍵であり、版の鍵でもある。

        全面を粗く測るBodyのレシピには細かい側の値が入らないので、ズームでレシピが変わらず
        `body_revision` が上がらず、`delta()` が何も送らない。細かい側の値を無条件に入れると
        画面外のBodyまでズームのたびに配り直す(2026-08-30に14.5秒かかっていた形)。
        """
        fine = self._body_focus(body_id)
        if fine is None:
            return ("all", self._effective_resolution(), self._view_floored)
        if not fine:
            return ("coarse", self._view_coarse_resolution or self._effective_resolution())
        return (
            "mixed",
            self._effective_resolution(),
            self._view_coarse_resolution,
            tuple(sorted(fine)),
            self._view_floored,
        )

    def _tessellation_args(self, body_id: str) -> tuple:
        """面ごとの測り方を決める `(fine_faces, dev, ang, coarse_dev, coarse_ang)`。"""
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
        basis = {} if normalized is None else {
            body.id: body.shape for body in self.document.bodies if body.id in normalized and body.shape is not None
        }
        floored = resolution.deviation_floored(world_per_pixel, size_hint)
        if (
            self._view_resolution == target
            and self._view_coarse_resolution == coarse
            and self._view_focus == normalized
            and self._view_floored == floored
            and basis.keys() == self._focus_basis.keys()
            and all(self._focus_basis[body_id] is shape for body_id, shape in basis.items())
        ):
            return False
        resolution_changed = self._view_resolution != target
        self._view_resolution = target
        self._view_coarse_resolution = coarse
        self._view_focus = normalized
        self._focus_basis = basis
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
        self._retired_faces.clear()
        self._edge_cache.clear()
        self._chain_cache.clear()
        self._vertex_cache.clear()
        self._volume_cache.clear()
        self._guide_cache.clear()
        self._face_count_cache.clear()
        self._sketch_cache.clear()
        self._sketch_revisions.clear()
        self._sketch_failures.clear()
        self._optimize_cache.clear()
        self._revision_state.clear()
        self._shape_state.clear()
        # **どの面を細かく測るかも捨てる**。面の添字は文書が変われば別のものを指す。
        # 目標の粗さも捨てる(前の文書に寄っていた細かさのまま、新しい文書の全面を最初の配信で測らない)。
        # 画面は文書の切り替えで見え方を送り直す(`resolutionTracker.reapply`)。
        self._view_resolution = None
        self._view_coarse_resolution = None
        self._view_floored = False
        self._view_focus = None
        self._focus_basis = {}
        self._focus_remapped.clear()

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
        return {
            "units": self.document.units,
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
        for sketch_id in [key for key in self._sketch_fills if key not in alive]:
            self._sketch_fills.pop(sketch_id, None)
        for key in [key for key in self._sketch_failures if key[0] not in alive]:
            self._sketch_failures.pop(key, None)
        return [self._sketch_state(item) for item in self.document.sketches]

    def _sketch_state(self, item) -> dict:
        try:
            drawn = region.expanded_entities(self.document, item.id)
            self._sketch_failures.pop((item.id, "expand"), None)
        except Exception as error:  # 壊れたスケッチで画面全体を落とさない
            self._warn_sketch_once(
                item.id, "expand", f"スケッチ {item.id} を描けない: {type(error).__name__}: {error}"
            )
            drawn = []
        # **貼った先のボディの形状は署名に入れない**。面に貼ったスケッチの姿勢は `item.plane["frame"]` が持つ
        # (`duocad.region.plane_frame`)ので、後からのフィレット等で枠も領域も変わらない。
        # `rebuild()` で姿勢が解き直されると `plane` が変わるので署名が拾う。
        content = json.dumps([item.plane, drawn], sort_keys=True, default=str)
        # 直線だけなら塗りが粗さに依らない(`_SketchFills.straight`)ので、ズームの段で署名も版も動かさない。
        straight = all(entity["kind"] in _STRAIGHT_KINDS for entity in drawn)
        signature = (content, None if straight else self.view_deviation)
        cached = self._sketch_cache.get(item.id)
        if cached is not None and cached[0] == signature:
            payload = dict(cached[1])
        else:
            held = self._sketch_drawing(item, drawn, content)
            payload = {"frame": held.frame, "drawn": drawn, "regions": self._sketch_fill(item, held)}
            self._sketch_cache[item.id] = (signature, payload)
            payload = dict(payload)
        payload["sketch_id"] = item.id
        payload["plane"] = item.plane
        payload["visible"] = item.visible
        payload["name"] = item.name
        # 図形そのものは軽いのでキャッシュしない。**try で包む**(上の `drawn` だけ守っても、
        # 同じパラメータ解決のこちらが落ちると `state_meta()` ごと落ち、以後の配信が全部止まる)。
        try:
            # `sketch.entity_params` と同じ引き方(同じ id の図形が2つあれば先の方)を、図形ごとの走査をせずに1回で
            first: dict = {}
            for entity in sketch.get_sketch(self.document, item.id).entities:
                first.setdefault(entity.id, entity)
            payload["entities"] = [
                {
                    "entity_id": entity.id,
                    "kind": entity.kind,
                    "params": parameters.resolve_all(self.document, first[entity.id].params),
                # 値が名前つきパラメータならその名前、素の数値なら None。
                    "refs": {
                        key: ref if isinstance(ref, str) else None
                        for key, ref in entity.params.items()
                    },
                }
                for entity in item.entities
            ]
            self._sketch_failures.pop((item.id, "entities"), None)
        except Exception as error:
            self._warn_sketch_once(
                item.id, "entities", f"スケッチ {item.id} の図形を読めない: {type(error).__name__}: {error}"
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

    def _warn_sketch_once(self, sketch_id: str, kind: str, message: str) -> None:
        """同じ失敗(同じ文)は、直るまで1回だけ警告に積む。"""
        if self._sketch_failures.get((sketch_id, kind)) == message:
            return
        self._sketch_failures[(sketch_id, kind)] = message
        self.warnings.append(message)

    def _sketch_drawing(self, item, drawn: list, content: str) -> "_SketchFills":
        """描くもの(`content`)が同じ間に使い回す枠と領域の検出。失敗は描くものが変わるまで1回だけ知らせる。"""
        held = self._sketch_fills.get(item.id)
        if held is not None and held.document is self.document and held.content == content:
            return held
        held = _SketchFills(self.document, content)
        try:
            held.frame = region.plane_frame(self.document, item.id)
            if drawn:
                held.detection = region.detect(item.plane, drawn)
                held.straight = all(entity["kind"] in _STRAIGHT_KINDS for entity in drawn)
        except Exception as error:
            self.warnings.append(
                f"スケッチ {item.id} の領域を計算できない: {type(error).__name__}: {error}"
            )
        self._sketch_fills[item.id] = held
        return held

    def _sketch_fill(self, item, held: "_SketchFills") -> list:
        """いまの粗さ(`view_deviation`)の塗り。同じ粗さに戻ったら測り直さない。"""
        deviation = self.view_deviation
        regions = held.lookup(None if held.straight else deviation)
        if regions is not None:
            return regions
        regions = []
        if held.detection is not None:
            try:
                regions = region.fill(
                    held.detection,
                    deviation,
                    on_error=lambda index, error, sketch_id=item.id: self.warnings.append(
                        f"スケッチ {sketch_id} の領域 {index} を三角形にできない: {type(error).__name__}: {error}"
                    ),
                )
            except Exception as error:
                self.warnings.append(
                    f"スケッチ {item.id} の領域を計算できない: {type(error).__name__}: {error}"
                )
        held.keep(None if held.straight else deviation, regions)
        return regions

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
        """その形の面の三角形の控え(無ければ作る)。ボディごとに `FACE_STORES_PER_BODY` 個の形まで覚える。

        追い出すのは古い順だが、ボディの今の形は残し、プレビューが覚えさせただけの形(配っていない)を先に捨てる。半径を打ち直すたびに
        ゴーストの結果を覚えさせる(`seed_faces`)ので、古い順だけだと画面に出ている形が押し出され、undo やカメラで全面を測り直す
        (穴 384 個の板で 10 秒)。"""
        stores = self._face_cache.setdefault(body_id, OrderedDict())
        held = stores.get(id(shape))
        if held is not None and held[0] is shape:
            stores.move_to_end(id(shape))
            return held[1]
        store = _FaceTessellations(shape)
        stores[id(shape)] = (shape, store)
        if len(stores) > FACE_STORES_PER_BODY:
            current = next((body.shape for body in self.document.bodies if body.id == body_id), None)
            while len(stores) > FACE_STORES_PER_BODY:
                candidates = [key for key, (kept, kept_store) in stores.items() if kept is not current and kept_store is not store]
                if not candidates:
                    break
                stale = next((key for key in candidates if not stores[key][1].delivered), candidates[0])
                del stores[stale]
        return store

    @property
    def floored(self) -> bool:
        """細かい側の偏差が下限に張り付いているか(別々に測った面の境目の隙間が画素より大きくなりうる)。"""
        return self._view_floored

    def measured_faces(self, body_id: str, shape: Any, recipe: tuple) -> dict:
        """その形をその測り方で測って覚えている面(添字 -> 三角形)の写し。無ければ空(プレビューが別スレッドで借りる。`Session.preview_job`)。"""
        held = self._face_cache.get(body_id, {}).get(id(shape))
        if held is None or held[0] is not shape:
            return {}
        return held[1].measured((float(recipe[0]), float(recipe[1])))

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

    def return_kept_faces(self, body_id: str, source: Any, shape: Any, recipe: tuple, pairs: dict) -> None:
        """プレビューの結果 `shape`(`seed_faces` で覚えさせた後)の面のうち、元の形 `source` の面がそのまま残ったもの(`pairs`: 元の添字 ->
        結果の添字)を、元の形のその測り方の三角形として覚えさせる。

        ゴーストは全面を細かい側で測るが、画面は背を向けた面・画面外の面を粗く測っているので、元の形はそれらの面を細かい側で持っていない。
        返さないと、半径を打ち直すたびのゴーストがそれらを測り直す(--part big の上から見た画面で 73 面・1 回 2〜4 秒)。"""
        stores = self._face_cache.get(body_id, {})
        lender, target = stores.get(id(shape)), stores.get(id(source))
        if lender is None or lender[0] is not shape or target is None or target[0] is not source:
            return
        recipe = (float(recipe[0]), float(recipe[1]))
        measured = lender[1].measured(recipe)
        held = target[1].by_recipe.get(recipe, {})
        for index, position in pairs.items():
            face = measured.get(position)
            if face is not None and index not in held:
                target[1].adopt(recipe, index, face)

    def _wanted_faces(self, body) -> tuple:
        """面ごとの測り方 `(wanted {面の添字: 測り方}, 細かく測る面の集合, 細かい側, 粗い側)`。"""
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
        return wanted, fine_set, detailed, coarse

    def _missing_groups(self, store: "_FaceTessellations", wanted: dict, detailed: tuple) -> dict:
        """まだ測る面を測り方ごとに(借りられる面はここで借りる)。"""
        groups = store.missing(wanted)
        if groups:
            groups = self._borrow_faces(store, groups)
        if self._view_floored:
            # 別々の回に測った面の境目の隙間は偏差の2倍程度で、偏差が画素から決まっていれば1画素未満だが、
            # 下限に張り付くと画素に対して大きくなる(再現: 1画素 1e-6 mm で 40 画素)。細かい面の組は1回で測ったものだけを使う
            fine_group = [index for index, recipe in wanted.items() if recipe == detailed]
            if fine_group and not store.one_batch(detailed, fine_group):
                groups[detailed] = fine_group
        return groups

    def refinement_jobs(self) -> list:
        """いまの測り方に足りない面があるボディの `(body_id, 形, 測る面の組, 閉じた立体かも測るか)`(カメラの要求の後に、
        ループの外で測る分。`net/refinement.py`)。"""
        jobs = []
        for body in self.document.bodies:
            if not body.visible or body.mesh is not None or body.shape is None:
                continue
            wanted, _fine_set, detailed, _coarse = self._wanted_faces(body)
            store = self._face_store(body.id, body.shape)
            groups = self._missing_groups(store, wanted, detailed)
            if groups:
                jobs.append((body.id, body.shape, groups, store.closed is None))
        return jobs

    def adopt_refinement(self, body_id: str, shape: Any, groups: dict, measured: dict) -> bool:
        """ループの外で測った面を、その形の面として覚える(ループの上で測ったのと同じく、組ごと1回の測定として。下限に張り付いた
        ズームの「細かい面は1回で測る」組を崩さない)。ボディの形が替わっていたら捨てて False(測った形の面は配らない)。
        警告は次に配るときに出す。"""
        body = next((item for item in self.document.bodies if item.id == body_id), None)
        if body is None or body.shape is not shape:
            return False
        store = self._face_store(body_id, shape)
        store.remember(groups, measured)
        store.unwarned_failed |= set(measured["untriangulated"])
        store.unwarned_boundary.extend((measured.get("boundary") or {}).get("unresolved") or [])
        return True

    def _tessellate_body(self, body) -> dict:
        """B-rep を測り、取れなかった面・解消できなかった境界の警告を積む(**測ったときだけ**出す)。

        面ごとに測り方(細かい側・粗い側)を決め、覚えていない面だけを測る(`_FaceTessellations`)。
        """
        wanted, fine_set, detailed, coarse = self._wanted_faces(body)
        deviation = detailed[0]
        store = self._face_store(body.id, body.shape)
        store.delivered = True
        groups = self._missing_groups(store, wanted, detailed)
        measured = None
        if groups:
            measured = kernel.tessellate_faces(body.shape, groups, closed=store.closed is None)
            store.remember(groups, measured)
        if store.closed is None:
            store.closed = kernel.is_closed_shell(body.shape)  # 全部を借りて測らなかった
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
                f"表示できない面があります({body.name} の面 {newly_failed}): 表示用の三角形分割が取れず、画面に出ません(表示だけが欠けます)"
            )
        unresolved = ((result.get("boundary") or {}).get("unresolved") or []) + store.unwarned_boundary
        store.unwarned_boundary = []
        if unresolved:
            # 境界折れ線の交差を再分割で解消できなかった面は分割していない(kernel_meshing)。
            described = [f"{entry['face']}:{','.join(sorted({i['kind'] for i in entry['issues']}))}" for entry in unresolved]
            self.warnings.append(
                f"表示できない面があります({body.name} の面 {described}): 境界線の交差を解消できず、画面に出ません(表示だけが欠けます)"
            )
        return result

    def _borrow_faces(self, store: "_FaceTessellations", groups: dict) -> dict:
        """覚えていない面のうち、別の形にそのまま残っている同じ面(TShape・位置・向きが同じ)を同じ測り方で測ったものがあれば
        借りて覚え、まだ測る面だけを返す。貸し手は同じボディの前の形(操作の前・undo の行き来)・他のボディ(分割した片側)・
        消えたボディ(削除の undo)。押し出しやブーリアンでは結果の面の大半が入力の面のまま残る(733 面のボディに足して 732 面)。

        別々に測った面の境目は、粗さの組を分けて測るときと同じ隙間(偏差の2倍程度)が開きうる。下限に張り付いたときの
        1回で測る規則は、借りた面に別の回を振ることで保つ(`_FaceTessellations.adopt`)。
        """
        stored = [held for stores in self._face_cache.values() for held in stores.values()] + list(self._retired_faces.values())
        lenders = [held for _shape, held in stored if held is not store]
        if not lenders:
            return groups
        # 子プロセスの結果・確定したプレビューの結果・複製は、入力の形の何番の面と同じかを覚えている(`kernel.note_kept_faces`)
        kept = kernel.kept_faces(store.shape)
        origin = None
        if kept is not None:
            origin = next((held for shape, held in stored if shape is kept[0] and held is not store), None)
        sources = dict(kept[1]) if origin is not None else {}
        transform = kept[2] if origin is not None else None
        keys = store.keys()
        if transform is not None and store.closed is None and len(sources) == len(keys):
            store.closed = origin.closed  # 形全体を剛体で写した複製は、元と同じ面と辺のつながり
        left: dict = {}
        for recipe, indices in groups.items():
            rest = []
            from_origin = origin.measured(recipe) if origin is not None else {}
            for index in indices:
                key = keys[index - 1]
                face = next((found for found in (lender.lend(key, recipe) for lender in lenders) if found is not None), None)
                if face is None and index in sources:
                    face = from_origin.get(sources[index])
                    if face is not None and transform is not None:
                        face = kernel.transformed_face(face, transform)
                if face is None:
                    face = self._placed_face(store, index, recipe, lenders)
                if face is None:
                    rest.append(index)
                else:
                    store.adopt(recipe, index, face)
            if rest:
                left[recipe] = rest
        return left

    @staticmethod
    def _placed_face(store: "_FaceTessellations", index: int, recipe: tuple, lenders: list) -> Optional[dict]:
        """位置だけ違う同じ面(移動したボディ・同じ部品を別の位置に置いた STEP)の三角形を、位置の差を掛けて写す。"""
        bare_key, placement = store.placements()[index - 1]
        for lender in lenders:
            found = lender.lend_placed(bare_key, recipe)
            if found is not None:
                moved = kernel.moved_face(found[0], found[1], placement)
                if moved is not None:
                    return moved
        return None

    def edges(self, body_id: str) -> dict:
        """1Bodyの辺を折れ線で返す(キャッシュ付き)。

        戻り値: `{"body_id", "revision", "shape_revision", "deviation", "edges": [kernel.tessellate_edges の1件, ...]}`

        **`state` にも `delta` にも入れない**(画面はカーソルの近くのボディの分だけ頼む。常時配ると
        27部品(2835面)の初回配信にその数倍の折れ線が乗る)。`shape_revision` は、クライアントが「今の形の辺か」
        を判定するためで、古い辺を指したままフィレットをかける(別の辺が丸まる)経路を閉じる(`revision` は粗さでも上がるので、
        添字が使えるかは形の版で見る)。
        `deviation` は折れ線の弦の偏差[mm]: 曲面の上の辺は面の三角形と別々に弦へ寄るので、画面の遮蔽判定が
        この分の奥行きの差を許す(`web/src/occlusion.ts`)。
        """
        body = self._visible_brep(body_id, "辺")
        revision = self.body_revision(body_id)
        edge_deviation, edge_angular = self.body_view_resolution(body_id)
        described = _memo(
            self._edge_cache, body_id, body.shape, (edge_deviation, edge_angular),
            lambda: kernel.tessellate_edges(body.shape, edge_deviation, edge_angular),
        )
        return {
            "body_id": body_id,
            "revision": revision,
            "shape_revision": self.shape_revision(body_id),
            "deviation": edge_deviation,
            "edges": described,
        }

    def edge_chains(self, body_id: str, edge_indices: list) -> dict:
        """頼まれた辺それぞれが属する連なり(フィレット・面取りで1本として振る舞う辺の組)を返す。

        戻り値: `{"body_id", "revision", "shape_revision", "chains": [[連なりの辺の添字(昇順)], ...]}`(頼んだ辺の順。同じ連なりは
        1つにまとめる。今の形のものかは `shape_revision` で見る: 連なりは形にだけ依存し、`revision` は粗さでも上がる)

        `edges()` に入れない理由: 連なりは `MakeFillet.Add` で求め、大きな部品では折れ線の数十倍かかる(PC ケースの
        最大ボディで折れ線 13ms・全辺の連なり 0.5 秒)。画面はホバー・クリックした辺の分だけ頼む(辺1本で `Add` 1回)。
        連なりは形にだけ依存するので、鍵は形の同一性だけ(ズームで粗さが変わっても求め直さない)。
        """
        body = self._visible_brep(body_id, "辺")
        revision = self.body_revision(body_id)
        finder = _memo(self._chain_cache, body_id, body.shape, None, lambda: kernel.edge_chain_finder(body.shape))
        groups = finder.of(edge_indices)
        chains = list(dict.fromkeys(groups[int(index)] for index in edge_indices))
        return {
            "body_id": body_id,
            "revision": revision,
            "shape_revision": self.shape_revision(body_id),
            "chains": [list(group) for group in chains],
        }

    def face_chains(self, body_id: str, face_indices: list) -> dict:
        """頼まれた面それぞれから選ぶ丸めの連なり(`kernel_face_edit.blend_chain`。丸めでない面はその面だけ)を返す。

        `"origins"` は頼まれた面、`"chains"` はそれぞれの連なり(同じ並び)。辺の連なりと違って面の連なりは互いに重なる
        (丸めの角の継ぎ面は、そこで会う丸めのどれの連なりにも入る)ので、起点の面ごとに返す。画面はクリックした面の分だけ頼む(1面で数十 ms)。
        """
        body = self._visible_brep(body_id, "面")
        origins = list(dict.fromkeys(int(index) for index in face_indices))
        return {
            "body_id": body_id,
            "revision": self.body_revision(body_id),
            "shape_revision": self.shape_revision(body_id),
            "origins": origins,
            "chains": [kernel_face_edit.blend_chain(body.shape, index) for index in origins],
        }

    def face_edges(self, body_id: str, face_indices: list) -> dict:
        """頼まれた面の外周の辺(`query.edges_of_face`。穴の縁は入れない)を、それぞれの連なり(`edge_chains`)まで広げ、
        全部の面の分まとめて昇順で返す。

        戻り値: `{"body_id", "revision", "shape_revision", "faces": [頼まれた面(重複を畳む)], "boundary_edges": [辺の添字(昇順)]}`

        `Session.select_face_boundaries` が選択の面をこの辺へ読み替える。連なりまで広げるのは、フィレット・面取りが
        辺1本から連なり全体へかかるため(画面の辺の選び方と同じ単位。docs/contracts/ui-selection-viewport.md)。
        """
        body = self._visible_brep(body_id, "面")
        faces = list(dict.fromkeys(int(index) for index in face_indices))
        boundary = sorted({edge for face in faces for edge in query.edges_of_face(body.shape, face)})
        finder = _memo(self._chain_cache, body_id, body.shape, None, lambda: kernel.edge_chain_finder(body.shape))
        groups = finder.of(boundary)
        edges = sorted({member for index in boundary for member in groups[index]})
        return {
            "body_id": body_id,
            "revision": self.body_revision(body_id),
            "shape_revision": self.shape_revision(body_id),
            "faces": faces,
            "boundary_edges": edges,
        }

    def vertices(self, body_id: str) -> dict:
        """1Bodyの頂点を1始まりの添字つきの3D座標で返す(キャッシュ付き)。

        戻り値: `{"body_id", "revision", "shape_revision", "vertices": [{"index", "point": [x,y,z]}, ...]}`

        `edges()` と同じく配信しない。画面に印が出るのはホバー中と選択中の頂点だけ(どれが指されるかを
        事前に知らないので全頂点を返す)。`shape_revision` の理由も辺と同じ。
        """
        body = self._visible_brep(body_id, "頂点")
        revision = self.body_revision(body_id)
        described = _memo(
            self._vertex_cache, body_id, body.shape, None, lambda: query.describe_vertices(body.shape)
        )
        return {
            "body_id": body_id,
            "revision": revision,
            "shape_revision": self.shape_revision(body_id),
            "vertices": described,
        }

    def sketch_guides(self, sketch_id: str) -> dict:
        """スケッチ `sketch_id` の平面と、見えている B-rep のボディの交わり(`kernel_project.section_guides`)をまとめて返す。

        戻り値: `{"sketch_id", "entities": [{"kind", "params"}], "points": [[u, v]], "errors": [str]}`

        `edges()` と同じく配信しない。画面はスケッチモードに入ったときと、見えているボディの形が変わったときに頼む。
        メッシュのボディと隠したボディは見ない。1体の失敗で全体を断らず、そのボディを抜いて `errors` に書く
        (画面は裏で頼んでいるので札を出さない)。
        """
        plane = sketch.get_sketch(self.document, sketch_id).plane
        key = json.dumps(plane, sort_keys=True, default=str)
        entities: list = []
        points: list = []
        errors: list = []
        for body in self.document.bodies:
            if not body.visible or body.shape is None:
                continue
            shape = body.shape
            try:
                found = _memo(self._guide_cache, body.id, shape, key, lambda: kernel_project.section_guides(plane, shape))
            except Exception as error:
                errors.append(f"{body.id}: {type(error).__name__}: {error}")
                continue
            entities.extend(found["entities"])
            points.extend(found["points"])
        return {"sketch_id": sketch_id, "entities": entities, "points": points, "errors": errors}

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
            raise HiddenBody(f"body {body_id!r} is not visible: 隠れているものの{what}は指せない")
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
        for body_id in [key for key in self._face_cache if key not in alive]:
            for key, held in self._face_cache[body_id].items():
                self._retired_faces[key] = held
                self._retired_faces.move_to_end(key)
        while len(self._retired_faces) > RETIRED_FACE_STORES:
            self._retired_faces.popitem(last=False)
        for cache in (
            self._geometry_cache, self._edge_cache, self._chain_cache, self._vertex_cache, self._volume_cache,
            self._guide_cache, self._face_count_cache, self._face_cache, self._focus_remapped,
        ):
            for body_id in [key for key in cache if key not in alive]:
                cache.pop(body_id, None)

    def delta(self, known_revisions: dict, defer: frozenset = frozenset()) -> dict:
        """クライアントが持っている版との差分だけを返す(未知のIDは必ずbodiesへ)。

        戻り値:
            {
              "bodies":    [<state()の bodies と同じ形。ただし版が変わったBodyだけ>],
              "unchanged": [BodyId, ...],   # 版が一致したBody
            }

        クライアントが知っているのに `bodies` にも `unchanged` にも現れないIDは**文書から消えた**
        (undoでの消滅。専用の "removed" は持たない)。

        `defer` のボディは、クライアントが版を知っていれば今回は送らない(ループの外で測っている最中。呼び手は送らなかった版を
        覚えず、測り終えてから配る。`net/refinement.py`)。
        """
        bodies_out = []
        unchanged = []
        for body in self.document.bodies:
            current = self.body_revision(body.id)
            if known_revisions.get(body.id) == current or (body.id in defer and body.id in known_revisions):
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
