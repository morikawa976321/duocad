"""選択機構 — **交互操作の中核**。

選択は `Document.selection` が持つコアの状態で、**保存しない**(保存・再生の対象は操作ログだけ)。

**選択の変更も可視性の変更も操作ログに積まない**(undoの単位ではない。理由は `set_visible`)。
Ctrl+Z / Ctrl+Y では戻らず保存もされない(開き直すと全ボディが可視)。ただし**イベントは発火する**。

選択は**出所を持たない1つの集合**で、人(UI)もAI(MCP)も同じ操作で触る:

    set_selection(...)       置き換え(参照の明示か述語)
    modify_selection(...)    差分編集(add / remove)
    toggle_selection(...)    あれば外し、無ければ足す
    clear_selection()        全解除

**verbの対象集合は `require_selected` / `require_edges` からしか取れない**。MCPもWebSocketも
対象集合を引数で受ける口を持たず、verbの入口(`duocad/entry.py`)がこの2つを呼ぶ ——**AIの経路と人の
経路が同じ規則を通る**のはここで担保されている。

**可視性は選択機構の一部**で、`visible=False` のBodyは選択の対象にならない(`selectable_bodies`。
`duocad.predicates` も走査しない)。PCケース(27部品・2835面)のピック失敗率63%は手前の部品が奥の面を隠していたため(実測)。

参照の形:

    ShapeRef  : {"body": BodyId, "kind": "face"|"edge"|"vertex", "index": int, "provenance": None}
    BodyRef   : {"body": BodyId, "kind": "body", "index": None, "provenance": None}
    RegionRef : {"sketch": SketchId, "index": int, "provenance": None}

**添字は現時点のものであって、形が変われば別の実体を指す**(添字の付け替えは契約にしていない)。

**OCP / build123d を直接importしない**(数え上げは `kernel` 越し、幾何は `query` 越し)。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Iterable, List, NoReturn, Optional, Sequence

from . import kernel, meshbody  # noqa: F401  face_count / count_subshapes を使う(OCPは kernel の中だけ)
from .document import BodyId, Document


class SelectionEmpty(ValueError):
    """verbが食う種別の項目が選択に1つも無い(`require_*` が送出する)。

    `kind` に「何が要るか」を持つ。MCPは `isinstance` で見て `selection_empty` + `required_kind` を付ける(文言では判定しない)。
    """

    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


class SelectionKindMismatch(SelectionEmpty):
    """選択は**空ではない**が、verbが食う種別が1つも入っていない。
    例: フィレット(辺を食う)を呼んだのに確定しているのは面3枚。`SelectionEmpty` の派生なので `require_*` を型で
    捕まえる経路はそのまま通る。分けてあるのはMCPのエラーコードのためで、`_error_payload` はこの型を**先に**見て
    `selection_kind_mismatch` を返す。`present` は実際に確定している種別と件数(`{"face": 3}`)。
    """

    def __init__(self, message: str, kind: str, present: dict) -> None:
        super().__init__(message, kind)
        self.present = dict(present)


class SelectionAmbiguous(ValueError):
    """1つだけ食うverbに複数選ばれている(`require_selected(exactly_one=True)`)。
    **黙って先頭を選ばない**ための型(`SelectionEmpty` と別なのは次の手が違うため: 選び直す vs 減らす)。
    """

    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


SHAPE_KINDS = ("face", "edge", "vertex")

#: **ソリッドを丸ごと指す種別**(形状最適化の保持/除外ソリッド用)。参照の形は
#: `{"body": BodyId, "kind": "body", "index": None, "provenance": None}`(`index` は使わない)。
#: 「ボディの面を全部選択集合に入れる」案は、1ボディ2835面の文書で選択集合が数百件に膨れるので採らない。
#: `require_bodies`(部分形状から属するBodyを逆引き)もこの種別を拾うので、指し方が2つでも行き先は1つ。
BODY_KIND = "body"

BODY_BEARING_KINDS = SHAPE_KINDS + (BODY_KIND,)

#: 領域を種別で絞るときの名前。RegionRef は `kind` キーを持たない(`{sketch, index, provenance}`)ので `"sketch"` キーの有無から導く。
REGION_KIND = "region"

#: スケッチの図形そのものを指す種別(`move_sketch_entities` の対象)。参照の形は
#: `{"sketch": SketchId, "entity": EntityId, "point": str|None, "provenance": None}`。
#: `point` が None なら「辺」= 図形1つ全体、文字列ならその点(`sketch.entity_point_keys` のxキー)。
#: 点と辺で種別を分けないのは、`move_sketch_entities` が両方を1回の呼び出しで食うため。
#: ShapeRef とは別構造: 図形は `entity_id` で名指しでき、足しても平行移動しても指す先が変わらない。
SKETCH_ENTITY_KIND = "sketch_entity"

SELECTABLE_KINDS = BODY_BEARING_KINDS + (REGION_KIND, SKETCH_ENTITY_KIND)

@dataclass
class Selection:
    """選択集合。`Document.selection` が1つだけ持つ(文書と同じ寿命・保存はされない)。

    items : [ShapeRef | RegionRef]  順序は追加順を保つ
    """

    items: List[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        """アプリ層・MCPへ渡す素の値 `{"items": [...], "count": int}`。

        `count` を明示的に持たせるのは、述語が0件だったことを呼び出し元が知れるようにするため。
        """
        return {
            "items": list(self.items),
            "count": len(self.items),
        }


def get_selection(doc: Document) -> Selection:
    """現在の選択集合を返す(コピーではなく本体。読み手はAIでも人でもよい)。"""
    return doc.selection


def fingerprint(doc: Document) -> str:
    """選択の「指紋」。

    **選択の変更は操作ログに乗らない**ので、`len(doc.log.entries)` だけを版番号にすると、AIが考えている間の
    人の**入れ替え**(辺Aを外して辺Bを選ぶ。件数も種別も変わらない)を見逃し、`fillet(radius)` が辺Bを黙って丸める。
    `items` を**順序を保ったまま**JSON化する(`fillet`/`chamfer` は選択順と半径リストを対応させる)。
    """
    selection = get_selection(doc)
    return json.dumps(
        {"items": selection.items},
        sort_keys=True,
        default=str,
    )


def set_selection(
    doc: Document,
    refs: Optional[Sequence[dict]] = None,
    predicate: Optional[dict] = None,
) -> Selection:
    """選択を丸ごと置き換える。

    `refs`(参照の明示)か `predicate`(述語)のどちらか一方。両方 / どちらも無しは ValueError。

    **落ちたときは選択を変更しない**。参照の形が不正なら ValueError、存在しないBodyなら KeyError、不正な述語は
    ValueError(MCPがAIへ返す説明を作るため)。**一致0件はエラーではなく、置き換えは起きる**
    (外した述語の後に古い候補が残ると、別の集合にverbがかかる)。
    """
    if (refs is None) == (predicate is None):
        raise ValueError("set_selection needs exactly one of refs or predicate")

    if refs is not None:
        items = _coerce_refs(doc, refs)
    else:
        from . import predicates

        items = predicates.evaluate(doc, predicate)

    selection = get_selection(doc)
    selection.items = list(items)
    return selection


def modify_selection(
    doc: Document,
    add: Iterable[dict] = (),
    remove: Iterable[dict] = (),
) -> Selection:
    """差分編集(作り直しではない)を行う。

    - 同じ参照を2回 `add` しても重複しない(等価判定は `body`/`kind`/`index`、領域は `sketch`/`index`。`provenance` は見ない)。
    - 選択に無い参照の `remove` は空振りでよい。`add` と `remove` に同じ参照があれば **remove が勝つ**。
    - **既に入っている参照を再度 add しても位置は動かさない**(`fillet` の辺リストと半径リストの対応がずれる)。

    重複は `ref_key` の集合引きで落とす(総当たりだと画面全体のドラッグ(実測2835面)で約0.8秒)。
    不正な参照は落ちて、選択を変更しない。
    """
    add_list = list(add)
    remove_list = list(remove)
    # 全参照を検査してから書き換える(途中で落ちると選択が壊れる)。`bodies`/`counts` は add/remove で共有する。
    bodies: dict = {}
    counts: dict = {}
    coerced_add = _coerce_refs(doc, add_list, bodies=bodies, counts=counts)
    coerced_remove = _coerce_refs(doc, remove_list, bodies=bodies, counts=counts)

    selection = get_selection(doc)
    items = list(selection.items)
    # 既存の鍵を先に集め、追加した分もその場で足す(`add` の中の重複も落ちる)。
    seen = {ref_key(existing) for existing in items}
    for ref in coerced_add:
        key = ref_key(ref)
        if key not in seen:
            seen.add(key)
            items.append(ref)
    if coerced_remove:
        dropped = {ref_key(ref) for ref in coerced_remove}
        items = [existing for existing in items if ref_key(existing) not in dropped]

    selection.items = items
    return selection


def toggle_selection(doc: Document, refs: Iterable[dict]) -> Selection:
    """参照ごとに、選択にあれば外し無ければ足す。UIの素のクリックと同じ操作。

    - 同じ参照が `refs` に2回あれば足してから外すので結果は変わらない。並びの規則・失敗の契約は `modify_selection` と同じ。
    """
    ref_list = _coerce_refs(doc, list(refs))
    items = list(get_selection(doc).items)
    for ref in ref_list:
        key = ref_key(ref)
        if any(ref_key(existing) == key for existing in items):
            items = [existing for existing in items if ref_key(existing) != key]
        else:
            items.append(ref)
    get_selection(doc).items = items
    return get_selection(doc)


def clear_selection(doc: Document) -> Selection:
    """選択を空にする。"""
    selection = get_selection(doc)
    selection.items = []
    return selection


def selected_items(doc: Document, kind: Optional[str] = None) -> List[dict]:
    """選択の項目を返す。`kind` を指定するとその種別だけに絞る。

    `kind` は `SELECTABLE_KINDS`。未知の名前は ValueError(綴り間違いが「0件」になると verb が黙って何もしない)。
    """
    selection = get_selection(doc)
    if kind is not None and kind not in SELECTABLE_KINDS:
        raise ValueError(f"unknown selectable kind: {kind!r}")
    items = list(selection.items)
    if kind is None:
        return items
    return [item for item in items if ref_kind(item) == kind]


def ref_kind(ref: dict) -> str:
    """参照の種別を返す(`"face"|"edge"|"vertex"|"body"|"region"|"sketch_entity"`)。

    RegionRef も SketchEntityRef も `kind` キーを持たない: `"sketch"` キーがあればスケッチ側で、`"entity"` を持てば
    `SKETCH_ENTITY_KIND`、持たなければ `REGION_KIND`。種別判定はここ1箇所(`selected_items` と `same_ref` が別規則を持たないため)。
    どちらでもない形は ValueError。
    """
    if not isinstance(ref, dict):
        raise ValueError(f"ref must be a dict, got {type(ref).__name__}")
    if "sketch" in ref:
        return SKETCH_ENTITY_KIND if "entity" in ref else REGION_KIND
    if "kind" in ref:
        kind = ref["kind"]
        if kind not in BODY_BEARING_KINDS:
            raise ValueError(f"unknown shape kind: {kind!r}")
        return kind
    raise ValueError("ref is neither a ShapeRef nor a RegionRef")


def edge_indices(doc: Document, body_id: Optional[BodyId] = None) -> List[int]:
    """確定した選択のうち辺の添字を、`fillet` / `chamfer` へそのまま渡せる形で返す。

    `body_id` を省略して選択に複数Bodyの辺が混ざっていれば ValueError(verbは1Bodyしか丸められない)。
    """
    edges = selected_items(doc, kind="edge")
    if body_id is not None:
        edges = [item for item in edges if item["body"] == body_id]
    else:
        bodies = {item["body"] for item in edges}
        if len(bodies) > 1:
            raise ValueError(
                f"edge selection spans multiple bodies {sorted(bodies)!r}: "
                'select the edges of one body (add "body" to the predicate)'
            )
    return [int(item["index"]) for item in edges]


# 選択 → verbの対象集合(AIの経路も人の経路もここを通る)
# 例外はコアの語彙(`SelectionEmpty` / `SelectionAmbiguous`)。MCPは型でエラーコードを付け、WebSocketは文言を返す。
def require_selected(
    doc: Document, kind: str, *, exactly_one: bool = False
) -> List[dict]:
    """選択から種別 `kind` の項目を取る。0件は `SelectionEmpty`。

    `exactly_one` のとき2件以上は `SelectionAmbiguous`(黙って先頭を選ばない)。
    """
    items = selected_items(doc, kind=kind)
    if not items:
        _raise_missing(doc, kind)
    if exactly_one and len(items) != 1:
        raise SelectionAmbiguous(
            f"exactly one {kind} required, got {len(items)}", kind
        )
    return items


def require_extrude_targets(doc: Document) -> tuple[str, List[dict]]:
    """選択から押し出す対象を取る: `("region", 領域の参照)` か `("face", 面の参照)`。

    領域と面が**両方**確定していたら ValueError。どちらも無ければ領域を代表にして `require_selected` の拒否。
    """
    regions = selected_items(doc, kind=REGION_KIND)
    faces = selected_items(doc, kind="face")
    if regions and faces:
        raise ValueError(
            f"extrude needs regions or faces, not both (selection has {len(regions)} region(s) "
            f"and {len(faces)} face(s)): 選び直すこと"
        )
    if faces:
        return "face", faces
    return REGION_KIND, require_selected(doc, REGION_KIND)


def require_edges(doc: Document) -> tuple[List[int], BodyId]:
    """選択の辺の添字と対象Bodyを**選択順のまま**返す(`fillet` / `chamfer` 用)。

    順序が効く(verbは `edges` と半径を位置で対応させる)。複数Bodyにまたがる選択は `edge_indices` が ValueError。
    添字とBodyを1回で返すのは、片方だけ渡し忘れる形を無くすため(かつて選択の指すBodyと違うBodyを丸める実装バグがあった)。
    """
    edges = edge_indices(doc)
    if not edges:
        _raise_missing(doc, "edge")
    refs = selected_items(doc, kind="edge")
    return edges, refs[0]["body"]


def require_faces(doc: Document) -> tuple[List[int], BodyId]:
    """選択の面の添字と対象Bodyを**選択順のまま**返す(`shell_body` 用。`require_edges` の面版)。

    0件は `SelectionEmpty` / `SelectionKindMismatch`。複数Bodyにまたがる選択は ValueError(verbは1Bodyしか殻にできない)。
    """
    faces = selected_items(doc, kind="face")
    if not faces:
        _raise_missing(doc, "face")
    bodies = {item["body"] for item in faces}
    if len(bodies) > 1:
        raise ValueError(
            f"face selection spans multiple bodies {sorted(bodies)!r}: "
            'select the faces of one body (add "body" to the predicate)'
        )
    return [int(item["index"]) for item in faces], faces[0]["body"]


def require_sketch_entities(doc: Document) -> List[dict]:
    """選択のスケッチ図形(点・辺)を**選択順のまま**返す(`move_sketch_entities` 用)。

    0件は `SelectionEmpty` / `SelectionKindMismatch`(`require_selected` と同じ拒否)。
    種別は1つ(`SKETCH_ENTITY_KIND`)なので、点と辺が同じ呼び出しでまとめて返る。
    """
    return require_selected(doc, SKETCH_ENTITY_KIND)


def require_bodies(doc: Document) -> List[BodyId]:
    """選択の**面・辺・頂点が属するBody**を、最初に現れた順で重複なく返す(`move_body` 用)。

    ShapeRef が既に `body` を持つので逆引きする。同じBodyの面と辺が両方選ばれていれば1つに畳む。
    面・辺・頂点が1つも無ければ `SelectionEmpty` / `SelectionKindMismatch`(`face` を代表にして拒否)。
    """
    ordered: List[BodyId] = []
    for item in selected_items(doc):
        # `kind="body"` の参照もここへ入る(逆引きが見ないと、ボディを選んだのに move_body が対象を見つけない)。
        if ref_kind(item) not in BODY_BEARING_KINDS:
            continue
        body_id = item["body"]
        if body_id not in ordered:
            ordered.append(body_id)
    if not ordered:
        _raise_missing(doc, "face")
    return ordered


@dataclass(frozen=True)
class DeleteTargets:
    """`require_deletable` が返す削除対象の内訳。

    bodies   : BodyId の並び(最初に現れた順・重複なし)。面/辺/頂点/`kind="body"` のどれから指しても行き先は同じ。
    entities : `{"sketch": SketchId, "entity": EntityId}` の並び(最初に現れた順・重複なし)。点の参照も図形1つへ畳む。

    両方空にはならない(`require_deletable` が先に拒否)が片方だけ空はありうる(`require_bodies` と
    `require_sketch_entities` を2つ呼ぶと、片方だけ選ばれているとき `SelectionKindMismatch` で壊れる)。
    """

    bodies: List[BodyId] = field(default_factory=list)
    entities: List[dict] = field(default_factory=list)


def require_deletable(doc: Document) -> DeleteTargets:
    """選択を**削除できる対象の内訳**へ畳む(verb `delete` 用)。

    - 選択が**丸ごと空** → `SelectionEmpty`(`_raise_missing` と同じ形)
    - **領域は無視する**(領域は図形から計算される見た目で消す操作が無い。図形を消せば消える。範囲選択で
      領域も入りがちなので、拒否するより無視して図形側だけ消す)。
    - ボディにも図形にも解決できない項目が残った(=領域だけ) → 拒否(削除対象が無いのに成功して見えるのを避ける)。
    - ボディ由来と図形が両方あれば両方とも対象(1回のundo = 1行のログ = 1回のverb呼び出し)。

    **名前が `require_` で始まる必要がある**: `catalog.Command.uses_selection` の宣言と実体の一致を
    `tests/test_vertex_pick.py` が接頭辞で照合する(別名だと静かに漏れる)。
    """
    items = selected_items(doc)
    if not items:
        _raise_missing(doc, BODY_KIND)

    bodies: List[BodyId] = []
    entities: List[dict] = []
    seen_entities: set = set()
    for item in items:
        kind = ref_kind(item)
        if kind in BODY_BEARING_KINDS:
            body_id = item["body"]
            if body_id not in bodies:
                bodies.append(body_id)
        elif kind == SKETCH_ENTITY_KIND:
            key = (item["sketch"], item["entity"])
            if key not in seen_entities:
                seen_entities.add(key)
                entities.append({"sketch": key[0], "entity": key[1]})
        # kind == REGION_KIND はここで黙って無視する(docstring参照)。

    if not bodies and not entities:
        _raise_missing(doc, BODY_KIND)
    return DeleteTargets(bodies=bodies, entities=entities)


# 集合代入のverbのための口 —— 空の選択も正当な入力
# 形状最適化の4 verb は集合代入で、**空の選択は「その役割を全部外す」を意味する**(解除専用verbは作らない)。
# 名前が `require_` で始まるのは `tests/test_vertex_pick.py` の接頭辞照合のため。
def require_selected_bodies(doc: Document) -> List[BodyId]:
    """選択の**`kind="body"` の参照**を、最初に現れた順で重複なく返す(**空でもよい**)。

    面・辺・頂点からの逆引き(`require_bodies`)は**しない**(拘束面を選んだついでにそのボディが保持へ入る取り違えを避ける)。
    """
    ordered: List[BodyId] = []
    for item in selected_items(doc, kind=BODY_KIND):
        body_id = item["body"]
        if body_id not in ordered:
            ordered.append(body_id)
    return ordered


def require_selected_faces(doc: Document) -> List[dict]:
    """選択の**面の参照**を選択順のまま返す(**空でもよい**。上のコメントが理由)。"""
    return selected_items(doc, kind="face")


def _kind_counts(items: Iterable[dict]) -> dict:
    """参照の並びを `{種別: 件数}` へ数える(`SelectionKindMismatch.present` の中身)。

    `_raise_missing` と `require_deletable` が**同じ値**を返すための1箇所。
    """
    counts: dict[str, int] = {}
    for item in items:
        item_kind = ref_kind(item)
        counts[item_kind] = counts.get(item_kind, 0) + 1
    return counts


def _raise_missing(doc: Document, kind: str) -> NoReturn:
    """種別 `kind` が選択に無いときの拒否を**1箇所で**組み立てる。

    - 選択が**丸ごと空** → `SelectionEmpty` → MCPは `selection_empty`
    - 空ではないが `kind` が**1つも無い** → `SelectionKindMismatch` → `selection_kind_mismatch`

    後者の文面は**実際に確定しているものを名指しする**。矛盾した指示(面が確定しているのに「その選択の辺に」)を
    渡されるとモデルが `get_selection` を何度も呼ぶ暴走が起きる(2026-08-20実測・600秒応答なし)ので、
    1回目の返り値だけで自己修復できるようにする。
    """
    others = selected_items(doc)
    if not others:
        raise SelectionEmpty(f"no {kind} in the confirmed selection", kind)
    counts = _kind_counts(others)
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
    """Bodyの表示・非表示を切り替える。**undo / redo の対象外**。

    可視性は形ではなく「何を指せるか」の定義。形を変えないものがundoの1手を消費すると、奥の面を見るための
    「手前を隠して戻す」操作が履歴を埋める。しかも `OpLog.append` は redo可能な末尾を捨てるので、undo直後に
    隠すとredoが黙って消えていた。保存もされない(開き直すと全ボディが可視)。

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


def set_sketch_visible(doc: Document, sketch_id: str, visible: bool) -> dict:
    """スケッチの表示・非表示を切り替える。**`set_visible`(ボディ)と同じ契約** ——
    undo / redo の対象外で、ログに載らないので保存もされない。
    隠したスケッチの領域・図形は指せない(`select_region` / `select_sketch_entity`)。押し出し済みのボディには何もしない。
    """
    from .sketch import get_sketch

    target = get_sketch(doc, sketch_id)
    previous = target.visible
    target.visible = bool(visible)
    return {
        "sketch_id": sketch_id,
        "visible": target.visible,
        "previous": previous,
        "changed": previous != target.visible,
    }


def visibility_fingerprint(doc: Document) -> str:
    """可視性の「指紋」。**楽観ロック(`DocumentTools.current_seq`)のために要る。**

    可視性がログに載らない以上、`len(doc.log.entries)` では「AIが考えている間に人がボディを隠した」を検出できず、
    AIが人の隠したものを黙って表示に戻す。`fingerprint`(選択)と同じ役目。スケッチも混ぜる(IDの接頭辞が違うので1列で足りる)。
    """
    hidden = [body.id for body in doc.bodies if not body.visible]
    hidden.extend(item.id for item in doc.sketches if not item.visible)
    return json.dumps(sorted(hidden))


# --------------------------------------------------------------------------------------
# 参照(ShapeRef / RegionRef)を作る
def selectable_bodies(doc: Document) -> List[BodyId]:
    """選択の対象になりうるBodyのIDを `doc.bodies` の順で返す(不可視Bodyは含めない)。"""
    return [body.id for body in doc.bodies if body.visible]


def select_face(doc: Document, body_id: BodyId, face_index: int) -> dict:
    """面を指す ShapeRef を作る。不可視Body・存在しないBody・範囲外の添字は例外。

    **メッシュボディは断る**(`_reject_mesh`)。
    """
    return _select_shape(doc, body_id, "face", face_index)


def select_edge(doc: Document, body_id: BodyId, edge_index: int) -> dict:
    """辺を指す ShapeRef を作る(添字は `query.describe_edges` / `fillet` と同じ1始まり)。"""
    return _select_shape(doc, body_id, "edge", edge_index)


def select_vertex(doc: Document, body_id: BodyId, vertex_index: int) -> dict:
    """頂点を指す ShapeRef を作る(添字は `kernel.count_subshapes(shape, "vertex")` の1始まり)。"""
    return _select_shape(doc, body_id, "vertex", vertex_index)


def _select_shape(doc: Document, body_id: BodyId, kind: str, index: int) -> dict:
    """面・辺・頂点で同じ手順: 可視Bodyを引く → メッシュを断る → 数える → `_shape_ref`。
    検査の順序(存在 → 可視 → メッシュ → 範囲)は `_coerce_refs` の一括経路と同じ。"""
    body = _visible_body(doc, body_id)
    _reject_mesh(body, f"{_KIND_LABELS[kind]}を指す")
    return _shape_ref(body_id, kind, index, _subshape_count(body.shape, kind))


def _subshape_count(shape, kind: str) -> int:
    """`kind` の部分形状の数。**呼ぶ時点で `kernel` の属性から引く**(表へ束ねると
    `tests/test_face_count_once_per_body.py` の `kernel.face_count` 差し替えが効かない)。"""
    if kind == "face":
        return kernel.face_count(shape)
    if kind == "edge":
        return kernel.edge_count(shape)
    return kernel.count_subshapes(shape, "vertex")


#: 断り文に出す種別の呼び名。
_KIND_LABELS = {"face": "面", "edge": "辺", "vertex": "頂点"}


def _reject_mesh(body, what: str) -> None:
    """メッシュボディへの面/辺/頂点の参照を断る。**選択の粒度はボディ単位だけ**。

    三角形メッシュには面の指紋も面添字も無い(`meshbody.geometry` の添字1は payload の形をそろえるだけ)ので、
    面を選べると保存も再現もできない選択になる。メッシュは `kind="body"`(`select_body`)で指す。

    **弾くと、そのボディを含むドラッグ一括選択は丸ごと落ちる**(`modify_selection` の契約どおり。範囲外の添字と
    同じ挙動で、**わざとそろえてある** — 読み飛ばすと「囲んだのに入っていない」が見えなくなる)。
    """
    if body.mesh is not None:
        meshbody.reject(body, what)


def select_body(doc: Document, body_id: BodyId) -> dict:
    """**ソリッドを丸ごと**指す参照を作る(`BODY_KIND` のdocstring)。

    不可視Body・存在しないBodyは例外(`_visible_body`)。`index` は `None` を明示的に持たせる(キーごと落とすと
    「まだ埋めていない」と「持たない」を見分けられない)。
    """
    body = _visible_body(doc, body_id)
    return {"body": body.id, "kind": BODY_KIND, "index": None, "provenance": None}


def select_region(doc: Document, sketch_id: str, region_index: int) -> dict:
    """スケッチの領域を指す RegionRef を作る。

    **参照は `region.region_ref(sketch_id, index).to_dict()` を使い回す**(別形式のdictを組むと操作ログの
    `selection` に2つの形が混ざる)。添字は `region.regions(doc, sketch_id)` の領域列の1始まりで、
    **その場で数え直して範囲を検査する**(図形を足すだけで領域数が変わる)。

    存在しない `sketch_id` は KeyError、範囲外の添字は ValueError。
    """
    from . import region as region_mod

    _visible_sketch(doc, sketch_id)
    count = len(region_mod.regions(doc, sketch_id))
    value = int(region_index)
    if not 1 <= value <= count:
        raise ValueError(f"region index out of range: {value} (1..{count})")
    return region_mod.region_ref(sketch_id, value).to_dict()


def select_sketch_entity(
    doc: Document, sketch_id: str, entity_id: str, point: Optional[str] = None
) -> dict:
    """スケッチの図形(または図形上の点)を指す参照を作る。

    `point` を省く(None)と**図形全体 = 辺**、文字列ならその点で、`sketch.entity_point_keys(kind)` の**xキー**でなければ
    ValueError(yキーや寸法キーは通さない)。存在しないスケッチ・図形は KeyError。**添字ではなく `entity_id` で指す**ので
    他の図形を足しても平行移動しても指す先が変わらない(`duocad-selection-resets-on-shape-change` の根拠が当たらない)。
    """
    from .sketch import entity_point_pair, get_entity

    sketch = _visible_sketch(doc, sketch_id)
    entity = get_entity(sketch, entity_id)
    if point is not None:
        entity_point_pair(entity.kind, str(point))
    return {
        "sketch": sketch_id,
        "entity": entity_id,
        "point": None if point is None else str(point),
        "provenance": None,
    }


def ref_key(ref: dict) -> tuple:
    """参照の**同一性の鍵**。等しい鍵 ⇔ 同じ実体を指す(`same_ref` はこれで定義される)。

    **等価判定を書き下ろす場所はここ1つだけ**(別に書くと「重複が残る / 別物を同一視する」が静かに入る)。
    ハッシュ可能なタプルなので `modify_selection` は集合引きで重複を落とせる。

    | 種別 | 鍵 |
    |---|---|
    | region | `("region", sketch, int(index))` |
    | sketch_entity | `("sketch_entity", sketch, entity, point)` |
    | body | `("body", body)` —— **添字を見ない** |
    | face / edge / vertex | `(kind, body, int(index))` |

    - **`provenance` は鍵に入れない**。**`int(index)` に正規化する**(`"3"` と `3` は同じ実体)。
    - **`point` は `.get("point")`**(キーの欠落と `None` を畳む。点と辺は別物)。
    - **種別は第1要素だけが持つ**(`ref["kind"]` を別要素として重ねると、鍵から種別を抜く変異を検査で示せなくなる)。

    形が参照でなければ `ref_kind` と同じ ValueError。
    """
    kind = ref_kind(ref)
    if kind == REGION_KIND:
        return (REGION_KIND, ref["sketch"], int(ref["index"]))
    if kind == SKETCH_ENTITY_KIND:
        return (SKETCH_ENTITY_KIND, ref["sketch"], ref["entity"], ref.get("point"))
    if kind == BODY_KIND:
        return (BODY_KIND, ref["body"])
    return (kind, ref["body"], int(ref["index"]))


def same_ref(left: dict, right: dict) -> bool:
    """2つの参照が同じ実体を指すか(`provenance` は見ない)。

    重複除去と `remove` の両方がこの判定を使う。判定の実体は `ref_key`(別々に書くと集合引きで落ちる重複とずれる)。種別が違えば別物。
    """
    return ref_key(left) == ref_key(right)


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
    if kind not in BODY_BEARING_KINDS:
        raise ValueError(f"unknown shape kind: {kind!r}")
    if "body" not in ref:
        raise ValueError("ShapeRef is missing 'body'")
    if kind == BODY_KIND:
        return select_body(doc, ref["body"])
    if "index" not in ref:
        raise ValueError("ShapeRef is missing 'index'")

    return _select_shape(doc, ref["body"], kind, ref["index"])


def _coerce_refs(
    doc: Document,
    refs: Sequence[dict],
    *,
    bodies: Optional[dict] = None,
    counts: Optional[dict] = None,
) -> List[dict]:
    """`_coerce_ref` を複数件まとめて処理する版。
    `select_*` 経由だと参照1件ごとに `kernel.face_count` 等が `TopExp.MapShapes_s` で形状全体を走査し直すので、
    同じボディを何度も指すドラッグ選択で選択件数ぶん全走査になる。`(body_id, kind)` ごとに数え上げを1回だけ行って使い回す。
    **通す/弾くの判定は `_coerce_ref` と1件も変えない**(門番・範囲外検査・例外の種類とメッセージは同じ)。

    `bodies`/`counts` は呼び出し元と共有できるよう引数で受ける(`modify_selection` が add/remove の2回で同じ辞書を渡す。
    `None` なら呼び出し1回限りの空辞書)。`kernel.py` にはキャッシュを持たない(`_face_map` の戻りは可変で17箇所が共有すると壊れる)。
    region / sketch_entity / body 種別・壊れた参照は `_coerce_ref` 単発へ流す。
    """
    if bodies is None:
        bodies = {}
    if counts is None:
        counts = {}
    out: List[dict] = []
    for ref in refs:
        if (
            isinstance(ref, dict)
            and "sketch" not in ref
            and ref.get("kind") in SHAPE_KINDS
            and "body" in ref
            and "index" in ref
        ):
            kind = ref["kind"]
            body_id = ref["body"]
            count_key = (body_id, kind)
            if count_key not in counts:
                if body_id not in bodies:
                    bodies[body_id] = _visible_body(doc, body_id)
                body = bodies[body_id]
                # 単発の `select_*` と同じ門番(このループは `select_face` 等を通らないので断りもここへ写す)。
                _reject_mesh(body, f"{_KIND_LABELS[kind]}を指す")
                counts[count_key] = _subshape_count(body.shape, kind)
            out.append(_shape_ref(body_id, kind, ref["index"], counts[count_key]))
        else:
            out.append(_coerce_ref(doc, ref))
    return out


def _visible_sketch(doc: Document, sketch_id: str):
    """存在して可視なスケッチを返す。無ければ KeyError、不可視なら ValueError(`_visible_body` と同じ線)。"""
    from .sketch import get_sketch

    target = get_sketch(doc, sketch_id)
    if not target.visible:
        raise ValueError(f"sketch {sketch_id!r} is hidden: 見えないものは指せない")
    return target


def _visible_body(doc: Document, body_id: BodyId):
    """存在して可視なBodyを返す。無ければ KeyError、不可視なら ValueError。

    **`body` を明示して不可視Bodyを指した場合も0件ではなくエラー**(「一致0件」と「見えないので対象外」はAIから区別が付かず、
    存在する面を見失ったと誤読して条件を緩める)。
    """
    body = doc.get_body(body_id)
    if not body.visible:
        raise ValueError(f"body {body_id!r} is hidden: 見えないものは指せない")
    return body


def _shape_ref(body_id: BodyId, kind: str, index: int, count: int) -> dict:
    """1始まりの添字を検査して ShapeRef を作る(範囲外は ValueError)。"""
    value = int(index)
    if not 1 <= value <= count:
        raise ValueError(f"{kind} index out of range: {value} (1..{count})")
    return {"body": body_id, "kind": kind, "index": value, "provenance": None}
