"""選択機構 — **交互操作の中核**。

選択は `Document.selection` が持つ(コアの状態)。**保存はしない** —— 保存・再生の対象は
操作ログだけであり、選択はセッション状態である。

**選択の変更も可視性の変更も操作ログに積まない**(どちらもundoの単位ではない。2026-08-26)。
可視性は `Body.visible` という**文書の状態**を変えるが、それでもログには載せない —— 理由は
`set_visible` の docstring。したがって Ctrl+Z / Ctrl+Y では戻らず、**保存もされない**
(保存の実体は操作ログのJSONなので、開き直した文書は全ボディが可視で始まる)。ただし
**イベントは発火する** —— AIが選択や可視性を書き換えたら画面は描き直される。

`origin` は**集合単位**で持つ(項目ごとではない):

    propose_selection(...)   -> origin="proposed"   AIが書き込む経路
    modify_selection(...)    -> origin="user"       人が差分編集する経路(触ること自体が承認)
    commit_selection()       -> origin="user"       中身を変えずに承認する経路

**verbの対象集合は `require_confirmed` / `require_edges` からしか取れない**。MCPもWebSocketも
対象集合を引数で受ける口を持たず、`Session` のverbラッパがこの2つを呼ぶ ——**AIの経路と人の
経路が同じ規則を通る**のはここで担保されている。**`origin="proposed"` のままでは verb に
渡せない**(`confirmed_items` が `SelectionNotCommitted`)。禁止ではなく**黙って通さない**で、
AIは `commit_selection()` を明示的に呼べば自分の提案を実行できる。

**可視性は選択機構の一部**で、`visible=False` のBodyは選択の対象にならない
(`selectable_bodies`。`duocad.predicates` も不可視Bodyを走査しない)。実機でPCケース
(27部品・2835面)を触ったときのピック失敗率63%の原因が「手前の部品が奥の面を隠していた」
ことにある。

参照の形:

    ShapeRef  : {"body": BodyId, "kind": "face"|"edge"|"vertex", "index": int, "provenance": None}
    RegionRef : {"sketch": SketchId, "index": int, "provenance": None}

**添字は現時点のものであって、形が変われば別の実体を指す**(添字の付け替えは契約にしていない)。

**OCP / build123d を直接importしない**(数え上げは `kernel` 越し、幾何は `query` 越し)。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Iterable, List, NoReturn, Optional, Sequence

from . import kernel  # noqa: F401  face_count / count_subshapes を使う(OCPは kernel の中だけ)
from .document import BodyId, Document


class SelectionNotCommitted(ValueError):
    """提案のまま verb へ渡そうとしたときに `confirmed_items` が送出する。

    `ValueError` の派生。MCPはメッセージ文字列ではなく `isinstance` で判定する。
    """


class SelectionEmpty(ValueError):
    """verbが食う種別の項目が確定選択に1つも無い(`require_*` が送出する)。

    `kind` に「何が要るか」を持つ。MCPは `isinstance` でこの型を見て `selection_empty` +
    `required_kind` を付ける(文言では判定しない)。アプリ層は文言をそのまま画面へ返す。
    """

    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


class SelectionKindMismatch(SelectionEmpty):
    """確定選択は**空ではない**が、verbが食う種別が1つも入っていない。

    例: フィレット(辺を食う)を呼んだのに、確定しているのは面3枚。

    **`SelectionEmpty` の派生にしてある**ので、`require_*` を型で捕まえている経路はそのまま
    通る。分けてあるのはMCPのエラーコードのためで、`_error_payload` は**この型を先に**見て
    `selection_kind_mismatch` を返す。`present` に**実際に確定している種別と件数**を持つ
    (`{"face": 3}`)。文面の生成元と同じ値なので、画面もモデルも数え直さなくてよい。
    """

    def __init__(self, message: str, kind: str, present: dict) -> None:
        super().__init__(message, kind)
        self.present = dict(present)


class SelectionAmbiguous(ValueError):
    """1つだけ食うverbに複数選ばれている(`require_confirmed(exactly_one=True)`)。

    **黙って先頭を選ばない**ための型。`SelectionEmpty` と別なのは、次に打つ手が違うため
    (選び直す vs 減らす)。
    """

    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


#: ShapeRef の kind(§3.5)。領域は RegionRef 側で表す。
SHAPE_KINDS = ("face", "edge", "vertex")

#: 領域を種別で絞るときの名前。**RegionRef は `kind` キーを持たない**(データモデルが
#: `{sketch, index, provenance}` と定めている)ので、`"sketch"` キーの有無から導く。
REGION_KIND = "region"

#: `confirmed_items(kind=...)` に渡せる名前の全体
#: スケッチの図形そのものを指す種別(2026-08-30。`move_sketch_entities` の対象)。
#:
#: 参照の形は `{"sketch": SketchId, "entity": EntityId, "point": str|None, "provenance": None}`。
#: **`point` が None なら「辺」= 図形1つ全体**、文字列ならその点(`sketch.entity_point_keys` の
#: xキー)を指す。点と辺で種別を分けないのは、`move_sketch_entities` が両方を1回の呼び出しで
#: 食うからである —— 分けると `require_confirmed` を2回呼んで自前で混ぜることになり、
#: 「対象集合は1つの種別で取る」という他のverbの形と揃わない。
#:
#: **ShapeRef(面・辺・頂点)とは別構造**にしてある。あちらはBodyの上のOCCT部分形状で、
#: 添字は形が変われば別の実体を指す。スケッチの図形は `entity_id` で名指しでき、
#: 図形を足しても平行移動しても指す先が変わらない。
SKETCH_ENTITY_KIND = "sketch_entity"

#: `confirmed_items(kind=...)` に渡せる名前の全体
SELECTABLE_KINDS = SHAPE_KINDS + (REGION_KIND, SKETCH_ENTITY_KIND)

#: 出所(§3.5 `origin`)。"user"=人が確定した / "proposed"=AIの提案
ORIGIN_USER = "user"
ORIGIN_PROPOSED = "proposed"


@dataclass
class Selection:
    """選択集合。`Document.selection` が1つだけ持つ(文書と同じ寿命・保存はされない)。

    items  : [ShapeRef | RegionRef]  順序は追加順を保つ(UIの一覧がちらつかないため)
    origin : "user" | "proposed"     集合単位。空集合の origin は常に "user"
    """

    items: List[dict] = field(default_factory=list)
    origin: str = ORIGIN_USER

    def to_dict(self) -> dict:
        """アプリ層・MCPへ渡す素の値 `{"items": [...], "origin": str, "count": int}`。

        `count` を明示的に持たせるのは、**述語が0件だったことを呼び出し元が知れる**ように
        するため(AIが述語を外したことに気づけないと、交互操作は黙って止まる)。
        """
        return {
            "items": list(self.items),
            "origin": self.origin,
            "count": len(self.items),
        }

    def is_proposed(self) -> bool:
        return self.origin == ORIGIN_PROPOSED


def get_selection(doc: Document) -> Selection:
    """現在の選択集合を返す(コピーではなく本体。読み手はAIでも人でもよい)。"""
    return doc.selection


def fingerprint(doc: Document) -> str:
    """選択の「指紋」。

    **選択の変更は操作ログに乗らない**ので、`len(doc.log.entries)` だけを版番号にすると、
    AIが考えている間に人が「辺Aを外して辺Bを選び直して確定する」ような**入れ替え**
    (件数も種別も変わらない)を見逃す —— `fillet(radius)` は確定選択から対象を取るので、
    版が動いたと誤認しなければAIは辺Aのつもりで辺Bを黙って丸める。

    `origin` と `items` を**順序を保ったまま**JSON化する。順序を保つのは `fillet`/`chamfer` が
    選択順と半径・距離のリストを対応させるため(並べ替えも実質的な変更として検出したい)。
    """
    selection = get_selection(doc)
    return json.dumps(
        {"origin": selection.origin, "items": selection.items},
        sort_keys=True,
        default=str,
    )


def propose_selection(
    doc: Document,
    refs: Optional[Sequence[dict]] = None,
    predicate: Optional[dict] = None,
) -> Selection:
    """**AIが書き込む経路**。選択を丸ごと置き換え、`origin="proposed"` にする。

    `refs`(参照の明示)か `predicate`(述語)のどちらか一方を渡す。両方 / どちらも無しは ValueError。

    失敗の契約: **落ちたときは選択を変更しない**。例外の種類は `select_*` と揃える ——
    **参照の形が不正(必須キーの欠落・未知の `kind`)なら ValueError、存在しないBodyを指して
    いれば KeyError**、不正な述語は ValueError。「どこが悪いか」で例外が変わらないと、
    呼び出し側(MCP)がAIへ返す説明を作れない。

    **一致0件はエラーではない**(空の Selection を返す)。0件でも**置き換えは起きる** ——
    外した述語の後に古い候補が残ると、人が承認したつもりで別の集合にverbがかかる。
    """
    if (refs is None) == (predicate is None):
        raise ValueError("propose_selection needs exactly one of refs or predicate")

    if refs is not None:
        items = [_coerce_ref(doc, ref) for ref in refs]
    else:
        from . import predicates

        items = predicates.evaluate(doc, predicate)

    selection = get_selection(doc)
    selection.items = list(items)
    selection.origin = ORIGIN_PROPOSED
    return selection


def modify_selection(
    doc: Document,
    add: Iterable[dict] = (),
    remove: Iterable[dict] = (),
) -> Selection:
    """**人が直す経路**。差分編集(作り直しではない)を行い、`origin="user"` にする。

    - 同じ参照を2回 `add` しても重複しない。等価判定は参照の中身(`body`/`kind`/`index`、
      領域は `sketch`/`index`)で行い、`provenance` は見ない。
    - 選択に無い参照の `remove` は**空振りでよい**(UIのトグルがそのまま乗る)。
    - `add` と `remove` に同じ参照があれば **remove が勝つ**。
    - **既に入っている参照を再度 add しても、位置は動かさない**。同じものをもう一度
      クリックしただけで並びが変わると、`fillet` の辺リストと半径リストの対応もずれる。

    失敗の契約は `propose_selection` と同じ —— **不正な参照は落ちて、選択を変更しない**。
    """
    add_list = list(add)
    remove_list = list(remove)
    # 全参照を検査してから書き換える(途中で落ちると選択が壊れる)
    coerced_add = [_coerce_ref(doc, ref) for ref in add_list]
    coerced_remove = [_coerce_ref(doc, ref) for ref in remove_list]

    selection = get_selection(doc)
    items = list(selection.items)
    for ref in coerced_add:
        if not any(same_ref(existing, ref) for existing in items):
            items.append(ref)
    if coerced_remove:
        items = [
            existing
            for existing in items
            if not any(same_ref(existing, ref) for ref in coerced_remove)
        ]

    selection.items = items
    selection.origin = ORIGIN_USER
    return selection


def commit_selection(doc: Document) -> Selection:
    """提案を確定する(`origin="user"`)。**中身は変えない**。既に "user" でも失敗しない(冪等)。"""
    selection = get_selection(doc)
    selection.origin = ORIGIN_USER
    return selection


def clear_selection(doc: Document) -> Selection:
    """選択を空にする。空集合の `origin` は常に "user" に戻す。"""
    selection = get_selection(doc)
    selection.items = []
    selection.origin = ORIGIN_USER
    return selection


def confirmed_items(doc: Document, kind: Optional[str] = None) -> List[dict]:
    """**verbへ渡してよい**項目を返す。`kind` を指定するとその種別だけに絞る。

    `origin == "proposed"` のときは `SelectionNotCommitted`。ここが、提案と確定の区別を
    **実際に効かせている唯一の場所**である。

    `kind` に渡せるのは `SELECTABLE_KINDS`。未知の名前は ValueError(綴り間違いが「0件」として
    返ると、verbが黙って何もしない経路になる)。
    """
    selection = get_selection(doc)
    if selection.is_proposed():
        raise SelectionNotCommitted(
            "selection is still proposed: commit_selection() first"
        )
    if kind is not None and kind not in SELECTABLE_KINDS:
        raise ValueError(f"unknown selectable kind: {kind!r}")
    items = list(selection.items)
    if kind is None:
        return items
    return [item for item in items if ref_kind(item) == kind]


def ref_kind(ref: dict) -> str:
    """参照の種別を返す(`"face"|"edge"|"vertex"|"region"|"sketch_entity"`)。

    **RegionRef も SketchEntityRef も `kind` キーを持たない**ので、`"sketch"` キーがあれば
    スケッチ側の2種で、そのうち `"entity"` を持つものが `SKETCH_ENTITY_KIND`、
    持たないものが `REGION_KIND`(領域は計算されるものなので添字しか持たない)。
    種別の判定をこの1箇所に集めるのは、`confirmed_items` の絞り込みと `same_ref` の等価判定が
    別々の規則を持ち始めるのを防ぐため。どちらでもない形の辞書は ValueError。
    """
    if not isinstance(ref, dict):
        raise ValueError(f"ref must be a dict, got {type(ref).__name__}")
    if "sketch" in ref:
        return SKETCH_ENTITY_KIND if "entity" in ref else REGION_KIND
    if "kind" in ref:
        kind = ref["kind"]
        if kind not in SHAPE_KINDS:
            raise ValueError(f"unknown shape kind: {kind!r}")
        return kind
    raise ValueError("ref is neither a ShapeRef nor a RegionRef")


def edge_indices(doc: Document, body_id: Optional[BodyId] = None) -> List[int]:
    """確定した選択のうち辺の添字を、`fillet` / `chamfer` へそのまま渡せる形で返す。

    `body_id` を省略した場合、選択に複数Bodyの辺が混ざっていれば ValueError
    (verbは1つのBodyしか丸められないので、どれのつもりかを黙って決めない)。
    """
    edges = confirmed_items(doc, kind="edge")
    if body_id is not None:
        edges = [item for item in edges if item["body"] == body_id]
    else:
        bodies = {item["body"] for item in edges}
        if len(bodies) > 1:
            raise ValueError(
                f"edge selection spans multiple bodies {sorted(bodies)!r}: "
                "pass body_id explicitly"
            )
    return [int(item["index"]) for item in edges]


# --------------------------------------------------------------------------------------
# 選択 → verbの対象集合(**AIの経路も人の経路もここを通る**)
# --------------------------------------------------------------------------------------
# 例外はコアの語彙(`SelectionEmpty` / `SelectionAmbiguous` / `SelectionNotCommitted`)で
# 投げる。MCPは型を見てエラーコードを付け、WebSocketは文言を画面へ返す ——
# **拒否の規則はここ1箇所**で、呼び出し側は名前を付けるだけ。
def require_confirmed(
    doc: Document, kind: str, *, exactly_one: bool = False
) -> List[dict]:
    """確定選択から種別 `kind` の項目を取る。0件は `SelectionEmpty`。

    `exactly_one` のとき2件以上は `SelectionAmbiguous`(黙って先頭を選ばない ——
    指示と結果がずれる唯一の経路になる)。
    """
    items = confirmed_items(doc, kind=kind)
    if not items:
        _raise_missing(doc, kind)
    if exactly_one and len(items) != 1:
        raise SelectionAmbiguous(
            f"exactly one {kind} required, got {len(items)}", kind
        )
    return items


def require_edges(doc: Document) -> tuple[List[int], BodyId]:
    """確定選択の辺の添字と対象Bodyを**選択順のまま**返す(`fillet` / `chamfer` 用)。

    順序が効く: verbは `edges` と半径(長さ)を位置で対応させるので、並べ替えると割り当てが
    黙ってずれる。複数Bodyにまたがる選択は `edge_indices` が ValueError。
    """
    edges = edge_indices(doc)
    if not edges:
        _raise_missing(doc, "edge")
    refs = confirmed_items(doc, kind="edge")
    return edges, refs[0]["body"]


def require_sketch_entities(doc: Document) -> List[dict]:
    """確定選択のスケッチ図形(点・辺)を**選択順のまま**返す(`move_sketch_entities` 用)。

    0件は `SelectionEmpty` / `SelectionKindMismatch`(`require_confirmed` と同じ拒否)。
    種別は1つ(`SKETCH_ENTITY_KIND`)なので、点と辺が同じ呼び出しでまとめて返る。
    """
    return require_confirmed(doc, SKETCH_ENTITY_KIND)


def require_bodies(doc: Document) -> List[BodyId]:
    """確定選択の**面・辺・頂点が属するBody**を、最初に現れた順で重複なく返す(`move_body` 用)。

    **「ボディ選択」という新しい概念は作らない**(2026-08-30本人判断) —— ShapeRef が既に
    `body` を持つので、そこから逆引きすれば足りる。同じBodyの面と辺が両方選ばれていれば
    1つに畳む(同じボディを2回動かさない)。

    面・辺・頂点が1つも確定していなければ `SelectionEmpty` / `SelectionKindMismatch`。
    3種のどれでもよいので `require_confirmed` を種別ごとに呼べない —— 先に3種を集めて、
    全部空のときだけ `face` を代表にして拒否を組み立てる。
    """
    ordered: List[BodyId] = []
    for item in confirmed_items(doc):
        if ref_kind(item) not in SHAPE_KINDS:
            continue
        body_id = item["body"]
        if body_id not in ordered:
            ordered.append(body_id)
    if not ordered:
        _raise_missing(doc, "face")
    return ordered


def _raise_missing(doc: Document, kind: str) -> NoReturn:
    """種別 `kind` が確定選択に無いときの拒否を**1箇所で**組み立てる。

    - 確定選択が**丸ごと空** → `SelectionEmpty` → MCPは `selection_empty`
    - 空ではないが `kind` が**1つも無い** → `SelectionKindMismatch` → `selection_kind_mismatch`

    後者の文面は**実際に確定しているものを名指しする**。矛盾した指示(例: 面が確定して
    いるのに「その選択の辺に」)を渡されると、モデルが `get_selection` 等を何度も呼んで
    様子見する暴走が起きる(2026-08-20実測・600秒応答なし)。**1回目のverb呼び出しの
    返り値だけで自己修復できる**ようにするのが狙い。
    """
    others = confirmed_items(doc)
    if not others:
        raise SelectionEmpty(f"no {kind} in the confirmed selection", kind)
    counts: dict[str, int] = {}
    for item in others:
        item_kind = ref_kind(item)
        counts[item_kind] = counts.get(item_kind, 0) + 1
    described = ", ".join(
        f"{count} {item_kind}" for item_kind, count in sorted(counts.items())
    )
    raise SelectionKindMismatch(
        f"{kind} required but the confirmed selection has {described} instead "
        f"(no {kind}): select {kind}(s) again before calling this verb",
        kind,
        counts,
    )


# --------------------------------------------------------------------------------------
# 可視性 — 選択機構の一部。**verbではない**(操作ログに積まない)
# --------------------------------------------------------------------------------------
def set_visible(doc: Document, body_id: BodyId, visible: bool) -> dict:
    """Bodyの表示・非表示を切り替える。**undo / redo の対象外**(2026-08-26本人決定)。

    可視性は形ではなく「何を指せるか」の定義である。形を1つも変えないものがundoの1手を
    消費すると、**奥の面を見るために手前を隠して戻すという「見るための操作」が編集の履歴を
    埋め**、直前の押し出しへ戻るのにCtrl+Zを何度も叩くことになる。しかも `OpLog.append` は
    redo可能な末尾を捨てるので、undo直後に何かを隠すとredoが黙って消えていた。

    ログに載せない結果として**保存もされない**(保存の実体は `OpLog.to_json`)。開き直した
    文書は全ボディが可視で始まる —— 選択と同じ「セッション状態」の扱いにする。

    存在しない body_id は KeyError(文書は何も変わらない)。
    """
    body = doc.get_body(body_id)
    previous = body.visible
    body.visible = bool(visible)
    return {
        "body_id": body_id,
        "visible": body.visible,
        "previous": previous,
        "changed": previous != body.visible,
    }


def visibility_fingerprint(doc: Document) -> str:
    """可視性の「指紋」。**楽観ロック(`DocumentTools.current_seq`)のために要る。**

    可視性がログに載らなくなった以上、`len(doc.log.entries)` では「AIが考えている間に人が
    ボディを隠した」を検出できない。見逃すと、AIが**人が隠したものを黙って表示に戻す**
    (逆も同じ)。`fingerprint`(選択)と同じ役目を可視性について果たす。
    """
    return json.dumps(sorted(body.id for body in doc.bodies if not body.visible))


# --------------------------------------------------------------------------------------
# 参照(ShapeRef / RegionRef)を作る — 種別を保持するための入口(項番1)
# --------------------------------------------------------------------------------------
def selectable_bodies(doc: Document) -> List[BodyId]:
    """選択の対象になりうるBodyのIDを `doc.bodies` の順で返す(不可視Bodyは含めない)。"""
    return [body.id for body in doc.bodies if body.visible]


def select_face(doc: Document, body_id: BodyId, face_index: int) -> dict:
    """面を指す ShapeRef を作る。不可視Body・存在しないBody・範囲外の添字は例外。"""
    body = _visible_body(doc, body_id)
    count = kernel.face_count(body.shape)
    return _shape_ref(body_id, "face", face_index, count)


def select_edge(doc: Document, body_id: BodyId, edge_index: int) -> dict:
    """辺を指す ShapeRef を作る(添字は `query.describe_edges` / `fillet` と同じ1始まり)。"""
    body = _visible_body(doc, body_id)
    count = kernel.edge_count(body.shape)
    return _shape_ref(body_id, "edge", edge_index, count)


def select_vertex(doc: Document, body_id: BodyId, vertex_index: int) -> dict:
    """頂点を指す ShapeRef を作る(添字は `kernel.count_subshapes(shape, "vertex")` の1始まり)。"""
    body = _visible_body(doc, body_id)
    count = kernel.count_subshapes(body.shape, "vertex")
    return _shape_ref(body_id, "vertex", vertex_index, count)


def select_region(doc: Document, sketch_id: str, region_index: int) -> dict:
    """スケッチの領域を指す RegionRef を作る。

    **`duocad.region` に既にある契約を使い回すこと** —— 参照そのものは
    `region.region_ref(sketch_id, index).to_dict()` が作る。ここで別形式のdictを組み立て直すと、
    操作ログの `selection` に2つの形が混ざる。

    領域は**描くものではなく計算されるもの**なので、添字は `region.regions(doc, sketch_id)` が
    返した領域列の1始まりの位置であり、**その場で数え直して範囲を検査する**(図形を1本
    足しただけで領域数が変わるため)。

    存在しない `sketch_id` は KeyError、範囲外の添字は ValueError。
    """
    from . import region as region_mod
    from .sketch import get_sketch

    get_sketch(doc, sketch_id)  # 存在しなければ KeyError
    count = len(region_mod.regions(doc, sketch_id))
    value = int(region_index)
    if not 1 <= value <= count:
        raise ValueError(f"region index out of range: {value} (1..{count})")
    return region_mod.region_ref(sketch_id, value).to_dict()


def select_sketch_entity(
    doc: Document, sketch_id: str, entity_id: str, point: Optional[str] = None
) -> dict:
    """スケッチの図形(または図形上の点)を指す参照を作る。

    `point` を省く(None)と**図形全体 = 辺**を指す。文字列を渡すとその点で、名前は
    `sketch.entity_point_keys(kind)` の**xキー**でなければ ValueError
    (`"y0"` のようなyキーや、半径・幅のような寸法キーは点ではないので通さない)。

    存在しないスケッチ・図形は KeyError。**添字ではなく `entity_id` で指す**ので、
    他の図形を足しても、平行移動しても、指す先が変わらない(面・辺・頂点の添字とは
    そこが違う —— `duocad-selection-resets-on-shape-change` の根拠が当たらない)。
    """
    from .sketch import entity_point_pair, get_entity, get_sketch

    sketch = get_sketch(doc, sketch_id)  # 無ければ KeyError
    entity = get_entity(sketch, entity_id)  # 無ければ KeyError
    if point is not None:
        entity_point_pair(entity.kind, str(point))  # その種別に無い点なら ValueError
    return {
        "sketch": sketch_id,
        "entity": entity_id,
        "point": None if point is None else str(point),
        "provenance": None,
    }


def same_ref(left: dict, right: dict) -> bool:
    """2つの参照が同じ実体を指すか(`provenance` は見ない)。

    重複除去と `remove` の両方がこの1つの判定を使う。別々に書くと「addでは重複扱いなのに
    removeでは別物」という非対称が静かに入る。種別が違えば別物。
    """
    left_kind = ref_kind(left)
    right_kind = ref_kind(right)
    if left_kind != right_kind:
        return False
    if left_kind == REGION_KIND:
        return left["sketch"] == right["sketch"] and int(left["index"]) == int(right["index"])
    if left_kind == SKETCH_ENTITY_KIND:
        return (
            left["sketch"] == right["sketch"]
            and left["entity"] == right["entity"]
            # **点と辺は別物**(同じ図形の (x0,y0) と「図形全体」を混同しない)
            and left.get("point") == right.get("point")
        )
    return (
        left["body"] == right["body"]
        and left["kind"] == right["kind"]
        and int(left["index"]) == int(right["index"])
    )


# --------------------------------------------------------------------------------------
def _coerce_ref(doc: Document, ref: dict) -> dict:
    """手書きの参照を検査し、`select_*` と同じ形へ正規化する。失敗時は選択を変えない前提。"""
    if not isinstance(ref, dict):
        raise ValueError(f"ref must be a dict, got {type(ref).__name__}")

    if "sketch" in ref:
        if "entity" in ref:
            return select_sketch_entity(
                doc, ref["sketch"], ref["entity"], ref.get("point")
            )
        if "index" not in ref:
            raise ValueError("RegionRef is missing 'index'")
        return select_region(doc, ref["sketch"], ref["index"])

    if "kind" not in ref:
        raise ValueError("ShapeRef is missing 'kind'")
    kind = ref["kind"]
    if kind not in SHAPE_KINDS:
        raise ValueError(f"unknown shape kind: {kind!r}")
    if "body" not in ref:
        raise ValueError("ShapeRef is missing 'body'")
    if "index" not in ref:
        raise ValueError("ShapeRef is missing 'index'")

    body_id = ref["body"]
    index = ref["index"]
    if kind == "face":
        return select_face(doc, body_id, index)
    if kind == "edge":
        return select_edge(doc, body_id, index)
    return select_vertex(doc, body_id, index)


def _visible_body(doc: Document, body_id: BodyId):
    """存在して可視なBodyを返す。無ければ KeyError、不可視なら ValueError。"""
    body = next((b for b in doc.bodies if b.id == body_id), None)
    if body is None:
        raise KeyError(f"no such body: {body_id!r}")
    if not body.visible:
        raise ValueError(f"body {body_id!r} is hidden: 見えないものは指せない")
    return body


def _shape_ref(body_id: BodyId, kind: str, index: int, count: int) -> dict:
    """1始まりの添字を検査して ShapeRef を作る(範囲外は ValueError)。"""
    value = int(index)
    if not 1 <= value <= count:
        raise ValueError(f"{kind} index out of range: {value} (1..{count})")
    return {"body": body_id, "kind": kind, "index": value, "provenance": None}
