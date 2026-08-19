"""選択機構 — **交互操作の中核**。

## なぜコアに置くのか

実装計画書§2.1の「交互操作」(AIが選択を提案 → 人が過不足を直す → AIが適用)が成立する
**唯一の必要条件**が「選択集合が第一級の共有オブジェクトであること」である。選択をアプリ層
(ブラウザ)に持たせると、AIから読めず・書けず、上の一巡が原理的に閉じない。

満たすべき5条件(§2.1):

1. **永続** — 操作の合間に消えない。UIの一時状態ではない
2. **双方向** — AIが読める / **AIが書ける** / 人が読める / 人が書ける
3. **種別を保持** — 面 / 辺 / 頂点 / 領域を区別する
4. **差分編集可能** — 追加・除去(作り直しではない)
5. **出所を区別** — AIの**提案**か、人が**確定**したものか

## 置き場所と保存

選択は `Document.selection` が持つ(コアの状態)。**保存はしない** — 保存・再生の対象は
操作ログだけであり、選択はセッション状態である。`Document` を保存する経路は
`doc.log.to_json()` しか無いので、`Selection` をログに載せない限り自動的に保存されない。

## 操作ログとの関係(非対称)

**選択の変更は操作ログに積まない**(選択は文書を変更しないため。undoの単位でもない)。
一方 `set_visible` は `Body.visible` という**文書の状態**を変えるのでログに載る。
この非対称は意図的である。ただし**イベントは発火する** — ログに載らないことと
「アプリ層へ知らせない」ことは別で、AIが選択を書き換えたら画面のハイライトは
描き直されなければならない(§2「コマンドは下り、イベントは上り」)。

## 提案と確定(§3.5 の `origin`)

`origin` は**集合単位**で持つ(項目ごとではない)。§3.5のデータモデルがそう定めており、
UI側の要件も「AIの提案は見た目が違い、承認操作がある」＝集合まるごとの見た目である。

    propose_selection(...)   -> origin="proposed"   AIが書き込む経路
    modify_selection(...)    -> origin="user"       人が差分編集する経路(触ること自体が承認)
    commit_selection()       -> origin="user"       中身を変えずに承認する経路

**`origin="proposed"` のままでは verb に渡せない**(`confirmed_items` が
`SelectionNotCommitted`)。これは「AIが自分の提案を人の確認なしに実行できるか」への答えで、
**禁止ではなく「黙って通さない」**である(§6予防6) — AIは `commit_selection()` を明示的に
呼べば自分の提案を実行できる。1手を明示的に踏ませることで、提案のまま実行された操作が
操作ログの上で区別できなくなる事故を防ぐ。

## 可視性は選択機構の一部

`visible=False` のBodyは選択の対象にならない(`selectable_bodies`)。実機でPCケース
(27部品・2835面)を触ったときのピック失敗率63%の原因が「手前の部品が奥の面を隠していた」
ことにあり、可視性は見た目の設定ではなく**何を指せるかの定義**である。述語による選択でも
同じ(`duocad.predicates` は不可視Bodyを走査しない)。

## 参照の形(§3.5)

    ShapeRef  : {"body": BodyId, "kind": "face"|"edge"|"vertex", "index": int, "provenance": None}
    RegionRef : {"sketch": SketchId, "index": int, "provenance": None}

`provenance` は day1 の空スロット(§6予防2)。**添字は現時点のものであって、形が変われば
別の実体を指す**(§3.5・§7)。A1では添字の付け替えを契約にしていない(下記「範囲外」)。

**OCP / build123d を直接importしない**(数え上げは `kernel` 越し、幾何は `query` 越し)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence

from . import kernel  # noqa: F401  face_count / count_subshapes を使う(OCPは kernel の中だけ)
from .document import BodyId, Document


class SelectionNotCommitted(ValueError):
    """提案のまま verb へ渡そうとしたときに `confirmed_items` が送出する。

    `ValueError` の派生なので既存の `pytest.raises(ValueError)` はそのまま通る。
    MCPはメッセージ文字列ではなく `isinstance` で `selection_not_committed` を付ける。
    """


#: ShapeRef の kind(§3.5)。領域は RegionRef 側で表す。
SHAPE_KINDS = ("face", "edge", "vertex")

#: 領域を種別で絞るときの名前。**RegionRef は `kind` キーを持たない**(§3.5のデータモデルが
#: `{sketch, index, provenance}` と定めているため)ので、`"sketch"` キーの有無から導く。
#: `confirmed_items(doc, kind="region")` はこの名前で絞る。
REGION_KIND = "region"

#: `confirmed_items(kind=...)` に渡せる名前の全体
SELECTABLE_KINDS = SHAPE_KINDS + (REGION_KIND,)

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
        """アプリ層・MCPへ渡す素の値。

        戻り値: `{"items": [...], "origin": str, "count": int}`

        `count` を明示的に持たせるのは、**述語が0件だったことを呼び出し元が知れる**ように
        するため(項番19。AIが述語を外したことに気づけないと、交互操作は黙って止まる)。
        """
        return {
            "items": list(self.items),
            "origin": self.origin,
            "count": len(self.items),
        }

    def is_proposed(self) -> bool:
        """AIの提案状態か(`origin == "proposed"`)。"""
        return self.origin == ORIGIN_PROPOSED


def get_selection(doc: Document) -> Selection:
    """現在の選択集合を返す(コピーではなく本体。読み手はAIでも人でもよい)。"""
    return doc.selection


def propose_selection(
    doc: Document,
    refs: Optional[Sequence[dict]] = None,
    predicate: Optional[dict] = None,
) -> Selection:
    """**AIが書き込む経路**。選択を丸ごと置き換え、`origin="proposed"` にする。

    `refs`(参照の明示)か `predicate`(述語・`duocad.predicates` の形式)のどちらか一方を渡す。
    両方 / どちらも無しは ValueError。

    失敗の契約(§6予防6): **落ちたときは選択を変更しない**(項番20)。例外の種類は
    `select_*` と揃える —— **参照の形が不正(必須キーの欠落・未知の `kind`)なら ValueError、
    存在しないBodyを指していれば KeyError**、不正な述語は ValueError。
    「どこが悪いか」で例外が変わらないと、呼び出し側(MCP)がAIへ返す説明を作れない。

    **一致0件はエラーではない** — 空の Selection を返す(項番19)。0件でも**置き換えは起きる**
    (外した述語の後に古い候補が残ると、人が承認したつもりで別の集合にverbがかかる)。
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

    - 同じ参照を2回 `add` しても重複しない(項番3)。等価判定は参照の中身
      (`body`/`kind`/`index`、領域は `sketch`/`index`)で行う。`provenance` は見ない。
    - 選択に無い参照の `remove` は**空振りでよい**(人が既に消したものをもう一度消す操作は
      エラーにしない。UIのトグルがそのまま乗る)。
    - **人が触ること自体が承認**なので `origin` は "user" になる(モジュールdocstring)。

    `add` と `remove` に同じ参照があれば **remove が勝つ**(引き算を後に適用する)。

    **既に入っている参照を再度 add しても、位置は動かさない**(末尾へ移し替えない)。
    人が同じものをもう一度クリックしただけで一覧の並びが変わると、
    `fillet` の辺リストと半径リストの対応も一緒にずれる。

    失敗の契約は `propose_selection` と同じ —— **不正な参照は落ちて、選択を変更しない**
    (形が不正なら ValueError、存在しないBodyは KeyError)。人が触る主経路なので、
    ここを素通りさせると壊れた参照が選択に紛れ込む。
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
    """提案を確定する(`origin="user"`)。**中身は変えない**。

    人が「そのままでよい」と承認する経路であり、AIが自分の提案を実行する前に明示的に
    踏む1手でもある。既に "user" のときに呼んでも失敗しない(冪等)。
    """
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

    `origin == "proposed"` のときは `SelectionNotCommitted`(ValueErrorの派生。
    モジュールdocstringの「黙って通さない」)。ここが、提案と確定の区別を**実際に
    効かせている唯一の場所**である。

    `kind` に渡せるのは `SELECTABLE_KINDS`(面・辺・頂点・領域)。未知の名前は ValueError
    (綴り間違いが「0件」として返ると、verbが黙って何もしない経路になる)。
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
    """参照の種別を返す(`"face"|"edge"|"vertex"|"region"`)。

    **RegionRef は `kind` キーを持たない**(§3.5)ので、`"sketch"` キーがあれば
    `REGION_KIND`、無ければ `ref["kind"]` を返す。種別の判定をこの1箇所に集めるのは、
    `confirmed_items` の絞り込みと `same_ref` の等価判定が別々の規則を持ち始めるのを
    防ぐため。どちらでもない形の辞書は ValueError。
    """
    if not isinstance(ref, dict):
        raise ValueError(f"ref must be a dict, got {type(ref).__name__}")
    if "sketch" in ref:
        return REGION_KIND
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
    """辺を指す ShapeRef を作る(添字は `kernel.edge_count` と同じ1始まり)。

    **フィレット・面取りが食う参照**であり、`query.describe_edges` の添字と一致する。
    """
    body = _visible_body(doc, body_id)
    count = kernel.edge_count(body.shape)
    return _shape_ref(body_id, "edge", edge_index, count)


def select_vertex(doc: Document, body_id: BodyId, vertex_index: int) -> dict:
    """頂点を指す ShapeRef を作る(添字は `kernel.count_subshapes(shape, "vertex")` の1始まり)。"""
    body = _visible_body(doc, body_id)
    count = kernel.count_subshapes(body.shape, "vertex")
    return _shape_ref(body_id, "vertex", vertex_index, count)


def select_region(doc: Document, sketch_id: str, region_index: int) -> dict:
    """スケッチの領域を指す RegionRef を作る(§3.5)。

    **`duocad.region` に既にある契約を使い回すこと** —— 参照そのものは
    `region.region_ref(sketch_id, index).to_dict()` が作る(`RegionRef` は dataclass で、
    `to_dict()` / `from_dict()` と `provenance` スロットが既に契約になっている)。
    ここで別形式のdictを組み立て直すと、操作ログの `selection` に2つの形が混ざる。

    領域は**描くものではなく計算されるもの**(§3.3)なので、添字は
    `region.regions(doc, sketch_id)` が返した領域列の1始まりの位置であり、
    **その場で数え直して範囲を検査する**(図形を1本足しただけで領域数が変わるため)。

    存在しない `sketch_id` は KeyError(`sketch.get_sketch` の契約に合わせる)、
    範囲外の添字は ValueError。
    """
    from . import region as region_mod
    from .sketch import get_sketch

    get_sketch(doc, sketch_id)  # 存在しなければ KeyError
    count = len(region_mod.regions(doc, sketch_id))
    value = int(region_index)
    if not 1 <= value <= count:
        raise ValueError(f"region index out of range: {value} (1..{count})")
    return region_mod.region_ref(sketch_id, value).to_dict()


def same_ref(left: dict, right: dict) -> bool:
    """2つの参照が同じ実体を指すか(`provenance` は見ない)。

    重複除去(項番3)と `remove`(項番2)の両方がこの1つの判定を使う。
    別々に書くと「addでは重複扱いなのにremoveでは別物」という非対称が静かに入る。

    種別が違えば別物(`ref_kind` で判定する)。ShapeRef は `body`+`kind`+`index`、
    RegionRef は `sketch`+`index` で比べる。
    """
    left_kind = ref_kind(left)
    right_kind = ref_kind(right)
    if left_kind != right_kind:
        return False
    if left_kind == REGION_KIND:
        return left["sketch"] == right["sketch"] and int(left["index"]) == int(right["index"])
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
