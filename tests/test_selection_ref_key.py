"""参照の同一性の鍵(`selection.ref_key`)の契約。

**選択の等価判定を書き下ろす場所は `ref_key` 1つだけ**であり、`same_ref` はその鍵の一致で
定義される。鍵を別に書き下ろすと「重複が残る / 別物を同一視する」が**静かに**入る ——
`modify_selection` の集合引きが落とす重複と、`same_ref` が「同じ」と言う組が食い違っても、
どちらも例外を投げないからである(2026-09-04にM^2の総当たりを鍵引きへ置き換えたときの理由)。

ここで固定するのは4つ:

1. **鍵の一致 ⇔ `same_ref`** —— 参照の総当たりで、独立に書き下ろした判定と突き合わせる。
2. **種別ごとの等価 / 非等価** —— 添字の正規化・`point` の畳み方・Bodyが添字を見ないこと・
   種別が違えば別物であること・`provenance` を見ないこと。
3. **並び順の保存** —— 既に入っている参照を再度 add しても位置が動かない
   (`fillet` の辺リストと半径リストの対応がずれる)。
4. **件数に対して線形** —— 鍵の引き当て回数を数える。総当たりならM^2に増える。
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List

import pytest
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

from duocad import selection
from duocad.document import Body, Document
from duocad.selection import modify_selection, ref_key, same_ref
from duocad.sketch import add_circle, sketch_create

#: クライアント側の同じ規則の置き場(`refKey` / `sameRef`)。
COMMAND_TS = Path(__file__).resolve().parents[1] / "web" / "src" / "commands" / "command.ts"

BOX_X, BOX_Y, BOX_Z = 30.0, 30.0, 40.0

#: 1つの直方体から作れる参照の数(面6 + 辺12 + 頂点8 + ボディ1)。
REFS_PER_BOX = 6 + 12 + 8 + 1


def _box_shape(dx: float = BOX_X, dy: float = BOX_Y, dz: float = BOX_Z):
    return BRepPrimAPI_MakeBox(dx, dy, dz).Shape()


@pytest.fixture()
def doc() -> Document:
    """直方体を1つ持つ文書(面6・辺12・頂点8)。"""
    document = Document()
    document.add_body(Body(id="box", name="box", kind="solid", shape=_box_shape()))
    return document


@pytest.fixture()
def many_bodies() -> Document:
    """直方体を8つ持つ文書。線形性を2つの規模で見るために使う。"""
    document = Document()
    for n in range(8):
        document.add_body(
            Body(id=f"b{n}", name=f"b{n}", kind="solid", shape=_box_shape())
        )
    return document


def _all_refs(doc: Document) -> List[dict]:
    """文書の全Bodyの 面 / 辺 / 頂点 / ボディ の参照を作る(添字は1始まり)。"""
    refs: List[dict] = []
    for body in doc.bodies:
        refs.append({"body": body.id, "kind": "body"})
        for index in range(1, 7):
            refs.append({"body": body.id, "kind": "face", "index": index})
        for index in range(1, 13):
            refs.append({"body": body.id, "kind": "edge", "index": index})
        for index in range(1, 9):
            refs.append({"body": body.id, "kind": "vertex", "index": index})
    return refs


# --------------------------------------------------------------------------------------
# 1. 鍵の一致 ⇔ same_ref
# --------------------------------------------------------------------------------------
def _same_ref_written_out(left: dict, right: dict) -> bool:
    """`ref_key` を**使わずに**書き下ろした同一性判定(照合用の第2の実装)。

    鍵の側を1フィールド落としたり、正規化を忘れたりすると、この判定とずれる。
    `ref_key` から導いてしまうと何も検査しないことになるので、意図的に重複して書く。
    """
    left_sketch = "sketch" in left
    right_sketch = "sketch" in right
    if left_sketch != right_sketch:
        return False
    if left_sketch:
        left_entity = "entity" in left
        right_entity = "entity" in right
        if left_entity != right_entity:
            return False
        if left_entity:
            return (
                left["sketch"] == right["sketch"]
                and left["entity"] == right["entity"]
                and left.get("point") == right.get("point")
            )
        return left["sketch"] == right["sketch"] and int(left["index"]) == int(
            right["index"]
        )
    if left["kind"] != right["kind"]:
        return False
    if left["kind"] == "body":
        return left["body"] == right["body"]
    return left["body"] == right["body"] and int(left["index"]) == int(right["index"])


def test_the_key_agrees_with_a_separately_written_judgement(doc: Document, sketch_refs):
    """参照の総当たりで、鍵の一致と独立に書いた判定が1組も食い違わない。

    鍵から1フィールド抜く / 余分なフィールドを足す、のどちらもここで落ちる。
    """
    # **添字付きのBody参照**を混ぜる —— `_coerce_ref` を通った参照は `index` が必ず None に
    # なるので、これを corpus に入れないと「Bodyは添字を見ない」が総当たりで検査されない。
    corpus = _all_refs(doc) + sketch_refs + [{"body": "box", "kind": "body", "index": 4}]
    for left in corpus:
        for right in corpus:
            expected = _same_ref_written_out(left, right)
            assert (ref_key(left) == ref_key(right)) is expected, (left, right)
            assert same_ref(left, right) is expected, (left, right)


def test_same_ref_is_the_key_comparison(doc: Document):
    """`same_ref` は鍵の一致そのもの(判定を2箇所に書かない)。"""
    left = {"body": "box", "kind": "face", "index": 1}
    right = {"body": "box", "kind": "face", "index": 2}
    assert same_ref(left, right) == (ref_key(left) == ref_key(right))
    assert same_ref(left, left) is True


def test_the_key_is_hashable_and_usable_as_a_set_member(doc: Document):
    """鍵はそのまま `set` / `dict` に入る(集合引きの前提)。"""
    keys = {ref_key(ref) for ref in _all_refs(doc)}
    assert len(keys) == REFS_PER_BOX


def test_a_ref_that_is_neither_shape_nor_region_is_a_value_error():
    """形が参照でなければ `ref_kind` と同じ ValueError(黙って別物にしない)。"""
    with pytest.raises(ValueError):
        ref_key({"nonsense": 1})
    with pytest.raises(ValueError):
        ref_key({"body": "box", "kind": "solid", "index": 1})


def test_the_client_derives_same_ref_from_the_same_key():
    """クライアント側(`command.ts`)も `sameRef` を `refKey` の一致で定義している。

    TS側で種別ごとの分岐を `sameRef` に書き戻すと、**Setが落とす重複と `sameRef` が
    「同じ」と言う組がずれる** —— どちらも例外を投げないので、画面にだけ二重の
    ハイライトが出る形で表に出る。ここは実行できないので静的に見る。
    """
    source = COMMAND_TS.read_text(encoding="utf-8")
    assert "export function sameRef(" in source, "sameRef が消えた(命名を変えたら追随する)"
    body = source.split("export function sameRef(", 1)[1].split("\n}", 1)[0]
    assert "refKey(left) === refKey(right)" in body, body
    # 種別ごとの分岐が sameRef 側へ戻っていない(鍵は `refKey` 1箇所だけが持つ)
    assert "'sketch_entity'" not in body and "'region'" not in body, body
    # Bodyの鍵は添字を見ない(コアの `ref_key` と同じ)
    assert "return `body:${item.body}`" in source, "TS側の body の鍵に添字が混ざっている"


def test_the_client_key_follows_the_same_rule_as_the_core() -> None:
    """TS側の `refKey` / `sameRef` を **Nodeで実際に評価**して、コアと同じ規則か見る。

    静的な文字列比較(上のテスト)は「判定を2箇所に書いていないか」しか見られない。
    規則そのもの(Bodyが添字を見ない・点と辺が別物・種別が違えば別物)は走らせて確かめる。
    `tests/eval_grid_scale.mjs` と同じやり方。**skipしない** —— 足場が無いときのskipは
    無人実行では緑と区別が付かない。
    """
    evaluator = Path(__file__).resolve().parent / "eval_selection_ref_key.mjs"
    assert evaluator.exists(), f"評価の口が無い: {evaluator}"
    try:
        completed = subprocess.run(
            ["node", str(evaluator)],
            capture_output=True,
            cwd=str(evaluator.parent.parent),
            timeout=60,
        )
    except FileNotFoundError as error:
        raise AssertionError(
            "node が見つからない。TS側の鍵を評価できないので検査できない"
        ) from error
    assert completed.returncode == 0, (
        "web/src/commands/command.ts の鍵がコアの ref_key と食い違う:\n"
        + completed.stdout.decode("utf-8", "replace")
        + completed.stderr.decode("utf-8", "replace")
    )


# --------------------------------------------------------------------------------------
# 2. 種別ごとの等価 / 非等価
# --------------------------------------------------------------------------------------
def test_the_kind_is_part_of_the_key():
    """**面3番と辺3番は別物**(同じBody・同じ添字でも種別が違えば別)。"""
    face = {"body": "box", "kind": "face", "index": 3}
    edge = {"body": "box", "kind": "edge", "index": 3}
    vertex = {"body": "box", "kind": "vertex", "index": 3}
    assert ref_key(face) != ref_key(edge)
    assert ref_key(edge) != ref_key(vertex)
    assert ref_key(face) != ref_key(vertex)
    assert not same_ref(face, edge)


def test_a_string_index_is_the_same_entity_as_the_int():
    """添字は `int` へ正規化する(手書きの `"3"` と `3` は同じ面)。"""
    assert ref_key({"body": "box", "kind": "face", "index": "3"}) == ref_key(
        {"body": "box", "kind": "face", "index": 3}
    )
    assert ref_key({"sketch": "s", "index": "2"}) == ref_key({"sketch": "s", "index": 2})


def test_the_body_key_ignores_the_index():
    """Bodyの参照は**添字を見ない**(Bodyは他のBodyの部分形状ではない)。"""
    plain = {"body": "box", "kind": "body"}
    with_index = {"body": "box", "kind": "body", "index": 7}
    other_index = {"body": "box", "kind": "body", "index": 9}
    assert ref_key(plain) == ref_key(with_index) == ref_key(other_index)
    assert same_ref(with_index, other_index)
    assert ref_key(plain) != ref_key({"body": "other", "kind": "body"})


def test_a_body_and_a_face_of_that_body_are_different_entities():
    """Bodyそのものと、そのBodyの面1番は別物。"""
    assert ref_key({"body": "box", "kind": "body"}) != ref_key(
        {"body": "box", "kind": "face", "index": 1}
    )


def test_a_sketch_entity_point_is_not_the_whole_entity():
    """**点と辺は別物**、かつ `point` の欠落と `None` は同じもの。"""
    whole = {"sketch": "s", "entity": "e", "point": None}
    missing = {"sketch": "s", "entity": "e"}
    x0 = {"sketch": "s", "entity": "e", "point": "x0"}
    x1 = {"sketch": "s", "entity": "e", "point": "x1"}
    assert ref_key(whole) == ref_key(missing)
    assert ref_key(whole) != ref_key(x0)
    assert ref_key(x0) != ref_key(x1)
    assert not same_ref(whole, x0)
    assert same_ref(whole, missing)


def test_a_region_and_a_sketch_entity_are_different_entities():
    """領域と図形は別の種別(どちらも `sketch` キーを持つ)。"""
    region = {"sketch": "s", "index": 1}
    entity = {"sketch": "s", "entity": "e", "point": None}
    assert ref_key(region) != ref_key(entity)
    assert not same_ref(region, entity)


def test_a_region_of_another_sketch_is_a_different_entity():
    assert ref_key({"sketch": "a", "index": 1}) != ref_key({"sketch": "b", "index": 1})
    assert ref_key({"sketch": "a", "index": 1}) != ref_key({"sketch": "a", "index": 2})


def test_provenance_is_not_part_of_the_key():
    """`provenance` は同一性に関与しない(由来違いで二重に持たない)。"""
    bare = {"body": "box", "kind": "face", "index": 1}
    with_provenance = {"body": "box", "kind": "face", "index": 1, "provenance": {"op": 3}}
    assert ref_key(bare) == ref_key(with_provenance)
    assert same_ref(bare, with_provenance)
    region_bare = {"sketch": "s", "index": 1}
    region_prov = {"sketch": "s", "index": 1, "provenance": None}
    assert ref_key(region_bare) == ref_key(region_prov)


# --------------------------------------------------------------------------------------
# 3. 並び順の保存(`fillet` の辺リストと半径リストの対応)
# --------------------------------------------------------------------------------------
def test_re_adding_an_existing_ref_does_not_move_it(doc: Document):
    """既に入っている参照を再度 add しても位置が動かない。"""
    edges = [{"body": "box", "kind": "edge", "index": i} for i in (1, 2, 3, 4)]
    modify_selection(doc, add=edges)
    before = [ref_key(item) for item in doc.selection.items]

    modify_selection(doc, add=[edges[0], edges[2]])
    assert [ref_key(item) for item in doc.selection.items] == before


def test_a_new_ref_lands_at_the_end(doc: Document):
    """新しい参照は末尾に足す(既存の並びは1件も動かない)。"""
    edges = [{"body": "box", "kind": "edge", "index": i} for i in (5, 6, 7)]
    modify_selection(doc, add=edges)
    modify_selection(doc, add=[edges[1], {"body": "box", "kind": "edge", "index": 8}])
    assert [item["index"] for item in doc.selection.items] == [5, 6, 7, 8]


def test_duplicates_inside_one_add_list_collapse_to_the_first(doc: Document):
    """1回の `add` の中の重複も落ちる(最初の1件の位置が残る)。"""
    face1 = {"body": "box", "kind": "face", "index": 1}
    face2 = {"body": "box", "kind": "face", "index": 2}
    modify_selection(doc, add=[face1, face2, face1, {"body": "box", "kind": "face", "index": "1"}])
    assert [item["index"] for item in doc.selection.items] == [1, 2]


def test_remove_keeps_the_order_of_what_is_left(doc: Document):
    """除去しても、残ったものの相対順序は変わらない。"""
    edges = [{"body": "box", "kind": "edge", "index": i} for i in (1, 2, 3, 4, 5)]
    modify_selection(doc, add=edges)
    modify_selection(doc, remove=[edges[1], edges[3]])
    assert [item["index"] for item in doc.selection.items] == [1, 3, 5]


def test_remove_uses_the_same_key_as_add(doc: Document):
    """add で重複扱いになるものは remove でも当たる(非対称を作らない)。"""
    modify_selection(doc, add=[{"body": "box", "kind": "face", "index": 1}])
    # 添字を文字列で、`provenance` 付きで消しても当たる。
    modify_selection(
        doc, remove=[{"body": "box", "kind": "face", "index": "1", "provenance": {"op": 1}}]
    )
    assert doc.selection.items == []


def test_removing_a_face_does_not_touch_the_edge_of_the_same_index(doc: Document):
    """種別が違えば remove も当たらない。"""
    modify_selection(
        doc,
        add=[
            {"body": "box", "kind": "face", "index": 3},
            {"body": "box", "kind": "edge", "index": 3},
        ],
    )
    modify_selection(doc, remove=[{"body": "box", "kind": "face", "index": 3}])
    assert [selection.ref_kind(item) for item in doc.selection.items] == ["edge"]


# --------------------------------------------------------------------------------------
# 4. 件数に対して線形
# --------------------------------------------------------------------------------------
#: import時の素の `ref_key`。**カウンタを二重に掛けない**ために掴んでおく
#: (`selection.ref_key` を読み直すと、前のカウンタを包んで回数が二重に増える)。
_ORIGINAL_REF_KEY = selection.ref_key


def _count_key_calls(monkeypatch) -> List[int]:
    """`ref_key` の呼び出し回数を数えるカウンタを仕込む(戻り値の [0] が回数)。"""
    counter = [0]

    def counting(ref: dict):
        counter[0] += 1
        return _ORIGINAL_REF_KEY(ref)

    monkeypatch.setattr(selection, "ref_key", counting)
    return counter


def test_modify_selection_looks_up_each_ref_a_bounded_number_of_times(
    many_bodies: Document, monkeypatch
):
    """鍵の引き当て回数が **2*既存 + 追加 + 除去** で頭打ちになる(総当たりならM^2)。

    実時間ではなく回数で見るのは、計測が機械の負荷で揺れるため。
    """
    refs = _all_refs(many_bodies)
    assert len(refs) == 8 * REFS_PER_BOX

    counter = _count_key_calls(monkeypatch)
    modify_selection(many_bodies, add=refs)
    assert counter[0] <= len(refs)  # 既存0件 + 追加M件

    half = refs[: len(refs) // 2]
    counter[0] = 0
    modify_selection(many_bodies, add=half, remove=[])
    assert counter[0] <= 2 * len(refs) + len(half)

    counter[0] = 0
    modify_selection(many_bodies, remove=half)
    assert counter[0] <= 2 * len(refs) + len(half)


def test_the_lookup_count_grows_linearly_with_the_selection(monkeypatch):
    """規模を4倍にしても引き当て回数は4倍前後(総当たりなら16倍になる)。"""

    def cost(box_count: int) -> int:
        document = Document()
        for n in range(box_count):
            document.add_body(
                Body(id=f"b{n}", name=f"b{n}", kind="solid", shape=_box_shape())
            )
        refs = _all_refs(document)
        counter = _count_key_calls(monkeypatch)
        modify_selection(document, add=refs)
        return counter[0]

    small = cost(2)
    large = cost(8)
    assert large <= small * 8  # 線形なら約4倍。二乗なら16倍で必ず超える


# --------------------------------------------------------------------------------------
# 領域・スケッチ図形の参照(総当たり用の corpus)
# --------------------------------------------------------------------------------------
@pytest.fixture()
def sketch_refs() -> List[dict]:
    """領域とスケッチ図形の参照(実在の文書は要らない —— 鍵は形だけを見る)。"""
    document = Document()
    sketch_id = sketch_create(document, "XY")
    entity_id = add_circle(document, sketch_id, (0.0, 0.0), 20.0)
    return [
        {"sketch": sketch_id, "index": 1},
        {"sketch": sketch_id, "index": 2},
        {"sketch": "other-sketch", "index": 1},
        {"sketch": sketch_id, "entity": entity_id, "point": None},
        {"sketch": sketch_id, "entity": entity_id},
        {"sketch": sketch_id, "entity": entity_id, "point": "cx"},
        {"sketch": sketch_id, "entity": "other-entity", "point": None},
    ]
