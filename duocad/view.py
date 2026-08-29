"""文書を画面へ配るための表現に変える部分(`DocumentView`)。

`Session` が「文書を動かすもの」なら、こちらは**配りもの** —— 文書を一切変えず、読み取って
アプリ層が描ける形へ射影するだけの側である。持ち物はすべて**寿命の管理が要るキャッシュ**で、
その寿命を1クラスに閉じることがこのファイルの目的である(切り出す前は、破棄が
`Session._replace_document` の `.clear()` 5連発、剪定が `state_meta()` の冒頭、
プレビュー中の回避が `preview()` の中、と別々の場所に散っていた)。

**依存の向きは `Session` -> ここの一方向**で、ここから `Session` は見ない。配る封筒に載る値の
うち**持ち主が `Session` のもの**——`can_undo`(取り消せる下限を `Session` が持つ)・
`applied_count`・「いまプレビュー中か」——は、呼び出し側が引数で渡す(`state_meta`)。
旗をこちらへ複製すると、片方だけ立てて戻し忘れる形になる。

**`duocad/net/` を参照しない**(`session.py` と同じ規律)。
**OCPを直接importしない**(境界はkernel.pyだけ)。
"""
from __future__ import annotations

import json
from typing import Optional

from . import kernel, parameters, query, region, resolution, selection, sketch
from .document import Document


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


class DocumentView:
    """1つの文書の「配りもの」。**文書を変えない読み取り + キャッシュ**だけを持つ。

    `Session` が1つだけ持ち(`session.view`)、文書を差し替えるときに `reset()` を呼ぶ。
    **古い文書への参照を握ったままにしない**のが `reset()` の役目で、これが無いと
    別の文書のために測った形を配ることになる。
    """

    def __init__(
        self,
        document: Document,
        deviation: float,
        warnings: list,
    ) -> None:
        #: いま配っている文書。差し替えは `reset()` だけが行う。
        self.document = document
        #: **文書の粗さの土台**(セッション起動時の `--deviation`)。ビューポートの
        #: ズーム連動値(下記)が来ていない間だけ使われる。STL書き出しはこちらを見る
        #: (`Session.export_stl_file`) —— 書き出す形の粗さがカメラで変わってはいけない。
        self.deviation = float(deviation)
        #: **ズーム連動の目標精度**(`set_view_resolution`)。`(線形偏差[mm], 角度偏差[rad])`。
        #: `None` は「まだ画面から届いていない」で、そのときは `self.deviation` +
        #: `kernel.DEFAULT_ANGULAR_DEFLECTION` を使う(ズーム連動を入れる前と同じ)。
        #: **画面の設定であって文書の状態ではない** —— 操作ログにもundoにも載らない。
        self._view_resolution: Optional[tuple] = None
        #: **画面外・背を向けた面に使う粗さ**(`(線形偏差[mm], 角度偏差[rad])`)。
        #: `duocad/resolution.py` の `coarse_view_resolution` が決める値で、
        #: **ズームに依存しない**(モデルの大きさだけで決まる)。`None` は分けないという意味。
        self._view_coarse_resolution: Optional[tuple] = None
        #: **どの面を細かく測るか**(`{body_id: frozenset(1始まりの面添字)}`)。
        #: `None` は「画面から指定が無い」= 全Body全面を細かく(ズーム連動を入れた直後と同じ)。
        #: dictに**居ないBody**も全面を細かく扱う。空のfrozensetは「そのBodyは全面粗く」。
        #: **画面の設定であって文書の状態ではない** —— 操作ログにもundoにも載らない。
        self._view_focus: Optional[dict] = None
        #: **`Session.warnings` と同じリストオブジェクト**を共有する(2つの持ち主を作らない)。
        #: 吸い出す口は `Session.take_warnings()` にだけ置いてあり、そちらは**中身を空にする**
        #: (新しいリストへ束ね直さない —— 束ね直すと、ここが古いリストへ書き続ける)。
        self.warnings = warnings
        # body_id -> (id(shape), visible, 目標精度) 直近の状態(`body_revision`)
        self._revision_state: dict = {}
        self._revision_counter: dict = {}  # body_id -> 現在の版(int)
        # sketch_id -> (署名, 領域つきペイロード)。領域の計算が重いので内容で引く(C)
        self._sketch_cache: dict = {}
        self._geometry_cache: dict = {}    # body_id -> (shape, tessellate結果)
        self._edge_cache: dict = {}        # body_id -> (shape, tessellate_edges結果)(D)
        self._vertex_cache: dict = {}      # body_id -> (shape, vertex_positions結果)
        self._volume_cache: dict = {}      # body_id -> (shape, query.volume結果)

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

        **ここが「画面外のBodyを配り直さない」の要である。** 全面を粗く測るBodyのレシピには
        細かい側の値が入らないので、寄っても引いてもレシピが変わらず、
        `body_revision` が上がらず、`delta()` が「変わっていない」と判断して何も送らない。
        逆に、細かい側の値をレシピへ無条件に入れると**画面に出ていないBodyまで
        ズームのたびに配り直す**(それが2026-08-30に14.5秒かかっていた形である)。
        """
        fine = self._body_focus(body_id)
        if fine is None:
            return ("all", self._view_resolution)
        if not fine:
            return ("coarse", self._view_coarse_resolution or self._view_resolution)
        return (
            "mixed",
            self._view_resolution,
            self._view_coarse_resolution,
            tuple(sorted(fine)),
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

        細かい面が1つでもあれば細かい側、1つも無ければ粗い側。**辺は面ごとに分けない** ——
        辺が要るのはフィレット/面取りを起動している1Bodyだけで、そこは必ず画面の中にある。
        """
        fine = self._body_focus(body_id)
        if fine is None or fine or self._view_coarse_resolution is None:
            return (self.view_deviation, self.view_angular_deflection)
        return self._view_coarse_resolution

    @staticmethod
    def _normalize_focus(focus) -> Optional[dict]:
        """画面から届いた `focus` を `{body_id: frozenset(int)}` へ正規化する(不正は捨てる)。

        **信じない**: 型が違う値・整数でない添字・負の添字は黙って落とす。落とした結果が
        空集合になれば「そのBodyは全面粗く」になるだけで、形が壊れることはない
        (粗く測るのは**測り方**であって、面が消えるわけではない)。
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

        受け取るのは**カメラの事実**(画面1画素ぶんの世界長さ[mm]と、モデルの外接対角[mm])
        であって、導出済みの偏差ではない。変換式の持ち主を1つにするための線引きで、
        式そのものは `duocad/resolution.py` にあり、ブラウザ側の
        `web/src/adaptiveResolution.ts` と定数まで一致している(検査あり)。

        `focus` は **face単位の高精度化**(2026-08-30)。`{body_id: [1始まりの面添字]}` で、
        **入っている面だけを細かく、残りを粗く**測る。キーの無いBody・`None` は「全面細かく」
        (＝この引数を渡さない旧クライアントは今までどおり動く)。空リストは「全面粗く」。
        判定するのは画面側である —— 視錐台に入っているか・カメラに背を向けているかは
        **カメラを持っている側にしか分からない**ので、サーバは受け取った事実に従うだけにする
        (`web/src/faceCulling.ts`)。粗い側の値は `resolution.coarse_view_resolution` が
        決める **ズームに依存しない**粗さで、これが後述のキャッシュ据え置きを成立させている。

        **キャッシュは捨てない**(2026-08-30に改めた)。`geometry()` / `edges()` の鍵には
        `body_recipe()`(実効レシピ)が入っているので、レシピの変わらないBody —— つまり
        **画面外で粗いまま据え置かれるBody** —— は測ったものがそのまま生き、
        版も上がらないので `delta()` が何も送らない。ここで一括で捨てると、
        face単位に分けた意味が丸ごと消える。粗さだけを見て捨てていたのが、
        27部品2835面で往復14.5秒かかっていた形である
        (engine [[one-global-deviation-costs-on-large-assemblies]])。
        スケッチはブラウザ側で折る経路で面を持たないので、細かい側が動いたときだけ捨てる。

        **目標精度は文書に1つ**(接続ごとには持たない)。これは**単一ブラウザ前提**
        (2026-08-27本人決定。engine [[duocad-assumes-a-single-browser]] / `Server` の
        docstring「同時編集は前提にしない」)に乗った選択である —— 2つのブラウザが別々の
        ズームで開いていれば、後から来た方の粗さで両方が測り直され、先客は自分のカメラに
        合わない粗さのまま残る。**同時編集を仕様にするなら、ここを接続ごとに分ける**
        (そのときは `DocumentView` のキャッシュも接続ごとになる)。
        """
        target = resolution.view_resolution(world_per_pixel, size_hint)
        coarse = resolution.coarse_view_resolution(world_per_pixel, size_hint)
        if coarse == target:
            coarse = None  # 分ける意味が無い(`size_hint` が無い/引ききっている)
        normalized = self._normalize_focus(focus)
        if (
            self._view_resolution == target
            and self._view_coarse_resolution == coarse
            and self._view_focus == normalized
        ):
            return False
        resolution_changed = self._view_resolution != target
        self._view_resolution = target
        self._view_coarse_resolution = coarse
        self._view_focus = normalized
        # **geometry / edges のキャッシュはここでは捨てない。** 鍵に `body_recipe()` が
        # 入っているので、レシピの変わっていないBody(=画面外で粗いままのBody)は
        # 測ったものがそのまま生き、`delta()` も送らない。ここで一括で捨てると、
        # face単位に分けた意味が丸ごと消える(全Bodyを毎回測り直すことになる)。
        if resolution_changed:
            # スケッチはブラウザ側で折るので面ごとの粗さを持たない(細かい側で1つ)。
            self._sketch_cache.clear()
        return True

    # ---------------------------------------------------------------- 文書の差し替え
    def reset(self, document: Document) -> None:
        """配る先を `document` へ差し替え、**古い文書について測ったものを全部捨てる**。

        `Session._replace_document`(`open_document` / `new_document`)から呼ぶ。Bodyのidは
        再生で作り直されるが、同じidが再び現れる可能性があり、古い形状を指したキャッシュが
        残っていると別の文書の形を配ることになる。

        **版の数え上げ(`_revision_counter`)は消さない**。消すと版が1へ戻り、同じidのBodyを
        知っているクライアントが「版が同じ=変わっていない」と判断して、**前の文書の形を
        描き続ける**(idはログに記録されるので再生で同じidが現れうる)。状態のほう
        (`_revision_state`)だけ捨てれば、次の `body_revision` で必ず1つ上がる。
        """
        self.document = document
        self._geometry_cache.clear()
        self._edge_cache.clear()
        self._vertex_cache.clear()
        self._volume_cache.clear()
        self._sketch_cache.clear()
        self._revision_state.clear()
        # **どの面を細かく測るかも捨てる**。面の添字は文書が変われば別のものを指す。
        self._view_focus = None

    # ---------------------------------------------------------------- 状態
    def state(self, *, can_undo: bool, applied_count: int, previewing: bool) -> dict:
        """アプリ層へ渡す全状態。bodiesは配列(部品ごと)。**geometryを含む(重い)**。

        WebSocket配信(`Server._push`)はこれを毎回は使わない — `state_meta()` + `delta()`
        を組み合わせて、版が変わったBodyの `geometry` だけを送る(`duocad/net/ws.py` 参照)。
        `state()` はテストと `get_state` の全量確認用に残す。**入口は `Session.state()`**。
        """
        meta = self.state_meta(
            can_undo=can_undo, applied_count=applied_count, previewing=previewing
        )
        meta["bodies"] = [self.body_state(body) for body in self.document.bodies]
        return meta

    def state_meta(self, *, can_undo: bool, applied_count: int, previewing: bool) -> dict:
        """`state()` の軽量版。**bodiesの各要素にgeometryを含まない**(テセレーションを呼ばない)。

        パーツ一覧(名前・面数・可視性)のように形状そのものが要らない用途、および
        WebSocket配信のたびに送る「土台」の情報に使う。27部品でも数KB(state()は6MB超)。
        """
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
            # **どこまで戻れるか**を画面に出すために位置を配る。
            # 真偽だけだと「3回undoした状態」と「1回undoした状態」が同じに見える。
            "redo_count": self.document.log.redo_count,
            "applied_count": applied_count,
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
        signature = json.dumps(
            [item.plane, drawn, self.view_deviation], sort_keys=True, default=str
        )
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
                        self.document, item.id, self.view_deviation
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
        # **粗さも「状態」のうち**(2026-08-30、可変解像度)。ズームで目標精度が変われば
        # 配るべきgeometryも変わるので、ここに入れておかないと差分配信が
        # 「版が同じ=変わっていない」と判断して**古い粗さのメッシュを画面に残す**。
        key = (id(body.shape), body.visible, self.body_recipe(body_id))
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
        recipe = self.body_recipe(body_id)
        cached = self._geometry_cache.get(body_id)
        if cached is not None and cached[0] is body.shape and cached[1] == recipe:
            return cached[2]
        fine, deviation, angular, coarse_deviation, coarse_angular = self._tessellation_args(body_id)
        result = kernel.tessellate_selective(
            body.shape, fine, deviation, angular, coarse_deviation, coarse_angular
        )
        if result["untriangulated"]:
            self.warnings.append(
                f"三角形分割が取れなかった面({body.name}): {result['untriangulated']}(画面に出ません)"
            )
        self._geometry_cache[body_id] = (body.shape, recipe, result)
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
        edge_deviation, edge_angular = self.body_view_resolution(body_id)
        cached = self._edge_cache.get(body_id)
        if cached is not None and cached[0] is body.shape and cached[1] == (edge_deviation, edge_angular):
            described = cached[2]
        else:
            described = kernel.tessellate_edges(body.shape, edge_deviation, edge_angular)
            self._edge_cache[body_id] = (body.shape, (edge_deviation, edge_angular), described)
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

    def _prune_body_caches(self, previewing: bool) -> None:
        """undoで消えたBodyのキャッシュを4つのdictから落とす(セッション寿命で溜め込まない)。

        `sketches_state()` の剪定(生存集合との差分をpop)と同じ形に揃えてある。**`_revision_counter`
        は絶対に剪定しない** —— 版が1へ戻ると、同じ `body_id` を知っているクライアントが
        「版が同じ=変わっていない」と判断して前の文書の形を描き続ける
        (`reset()` のコメントと同じ理由)。`_revision_state` は次の `body_revision` が
        必ず版を1つ上げるだけなので落としてよいが、ここでは触らない(必須ではないため)。

        **プレビュー中(`previewing`)は何も落とさない** —— `Session.preview()` は
        `duocad.speculative.speculative` で文書を投機的に書き換えて必ず元へ戻す。その最中の
        生存集合で消すと、戻した後に `_volume_cache` 等の再測定が走ってしまう
        (PCケース27部品で693ms/回の再測定を避けるために入れたキャッシュなので、
        ここを無効化すると入れた目的が消える)。
        """
        if previewing:
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

        `state_meta()` を通じて**接続のたびに全Bodyぶん**呼ばれる(`_push`。MCPが引くのは
        `Session.body_meta()` の委譲越し)。`volume` は
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
