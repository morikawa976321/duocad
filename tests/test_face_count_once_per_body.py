"""`_coerce_refs`(`duocad/selection.py`)がボディごとに数え上げを1回だけ行う契約。

`_coerce_ref` を参照1件ごとに呼ぶと、`kernel.face_count`/`edge_count` の中の
`_face_map`/`_edge_map` が `TopExp.MapShapes_s` で形状全体を毎回走査し直す。同じボディの
同じ形状を選択件数ぶん何度も指す一括選択(ドラッグ選択)では、この全走査が繰り返される
(2026-09-04)。`_coerce_refs` は `(body_id, kind)` の組ごとに数え上げを1回だけ行い、
結果を使い回す。

**既存の複数ボディfixture(`test_selection.py` の `two_bodies`・`test_selection_ref_key.py`
の `many_bodies`)は全て同一寸法の箱**で、面数(6)・辺数(12)・頂点数(8)がボディ間で
偶然一致する。そのため「ボディを取り違えて数え上げ結果を使い回す」クラスの回帰は、
既存テストでは**検出できない**(実装時の変異検査で実際に確認した — キャッシュ鍵から
`body_id` を落としても既存799件は全緑のまま通った)。ここでは意図的に面数・辺数が異なる
形状(箱6面12辺 / 円柱3面)を混ぜ、そのクラスの回帰を検出できるようにする。

**`bodies`/`counts` を `modify_selection` の `add`/`remove` 間で共有する(2026-09-04、
QA指摘)**: `modify_selection` 内部では `_coerce_refs` が add 用・remove 用の2回呼ばれる。
最初の実装は呼び出しごとに新しい辞書を作っていたため、`add` と `remove` に同じボディの
面/辺参照が混ざると1回の `modify_selection` 呼び出しの中で最大2回数えていた ——
完了条件1「1回の `modify_selection`/`propose_selection` で、同じボディの面数・辺数を
2回以上数えない」の文言に対する実質的なギャップだった(括弧内の受入例が `add` のみの
経路だったため既存テストでは見えていなかった)。`_coerce_refs` に `bodies`/`counts` を
外から渡せる引数を足し、`modify_selection` がその2回の呼び出しへ同じ辞書を渡す形で埋めた。
"""
from __future__ import annotations

from typing import List

import pytest
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder

from duocad import kernel, selection
from duocad.document import Body, Document


def _box_shape(dx: float = 10.0, dy: float = 10.0, dz: float = 10.0):
    return BRepPrimAPI_MakeBox(dx, dy, dz).Shape()


def _cylinder_shape(radius: float = 5.0, height: float = 10.0):
    return BRepPrimAPI_MakeCylinder(radius, height).Shape()


@pytest.fixture()
def two_shapes() -> Document:
    """面数・辺数が異なる2つのボディ(箱: 6面12辺 / 円柱: 3面)を持つ文書。"""
    document = Document()
    document.add_body(Body(id="box", name="box", kind="solid", shape=_box_shape()))
    document.add_body(Body(id="cyl", name="cyl", kind="solid", shape=_cylinder_shape()))
    return document


def _count_kernel_calls(monkeypatch: pytest.MonkeyPatch, name: str) -> List[int]:
    """`kernel.<name>` の呼び出し回数を数えるカウンタを仕込む(戻り値の [0] が回数)。"""
    counter = [0]
    original = getattr(kernel, name)

    def counting(shape):
        counter[0] += 1
        return original(shape)

    monkeypatch.setattr(kernel, name, counting)
    return counter


# --------------------------------------------------------------------------------------
# 完了条件1: 選択件数ぶんではなく、ボディごとに1回だけ数える
# --------------------------------------------------------------------------------------
def test_face_count_is_looked_up_once_per_body_not_per_ref(
    two_shapes: Document, monkeypatch: pytest.MonkeyPatch
):
    """箱6面+円柱3面=9件の面参照を1回の `modify_selection` に渡しても、
    `kernel.face_count` はボディの数(2)だけ呼ばれる(9回ではない)。"""
    face_calls = _count_kernel_calls(monkeypatch, "face_count")
    refs = [{"body": "box", "kind": "face", "index": i + 1} for i in range(6)] + [
        {"body": "cyl", "kind": "face", "index": i + 1} for i in range(3)
    ]

    result = selection.modify_selection(two_shapes, add=refs)

    assert len(result.items) == 9
    assert face_calls[0] == 2  # ボディごとに1回(9回なら参照1件ごとに数えている)


def test_edge_count_is_looked_up_once_per_body_not_per_ref(
    two_shapes: Document, monkeypatch: pytest.MonkeyPatch
):
    """辺参照でも同じ(`face_count` とは独立に数える契約なので別カウンタで見る)。"""
    edge_calls = _count_kernel_calls(monkeypatch, "edge_count")
    refs = [{"body": "box", "kind": "edge", "index": i + 1} for i in range(12)]

    result = selection.modify_selection(two_shapes, add=refs)

    assert len(result.items) == 12
    assert edge_calls[0] == 1  # 箱1体ぶんだけ(12回なら参照1件ごとに数えている)


def test_separate_modify_selection_calls_each_recount(
    two_shapes: Document, monkeypatch: pytest.MonkeyPatch
):
    """`add` 用・`remove` 用に**別々の** `modify_selection` 呼び出しを行う場合
    (1回の呼び出しに `add` と `remove` を同時に渡す場合とは別のシナリオ ——
    そちらは `test_add_and_remove_in_the_same_call_share_the_count_cache` が見る)。

    キャッシュの寿命は「1回の `modify_selection` 呼び出し」に閉じる契約なので、
    呼び出しをまたいで使い回されないのは仕様どおり。各呼び出しにつき箱1体ぶん
    (1回)を数え直す。"""
    face_calls = _count_kernel_calls(monkeypatch, "face_count")
    add_refs = [{"body": "box", "kind": "face", "index": i + 1} for i in range(6)]
    selection.modify_selection(two_shapes, add=add_refs)
    face_calls[0] = 0

    remove_refs = [{"body": "box", "kind": "face", "index": i + 1} for i in range(3)]
    selection.modify_selection(two_shapes, remove=remove_refs)

    assert face_calls[0] == 1  # 呼び出しが別なので独立に1回数え直す(0にはならない)


def test_add_and_remove_in_the_same_call_share_the_count_cache(
    two_shapes: Document, monkeypatch: pytest.MonkeyPatch
):
    """完了条件1「1回の `modify_selection`/`propose_selection` で、同じボディの
    面数・辺数を2回以上数えない」の `add`+`remove` 同時指定版(2026-09-04、QA指摘)。

    `modify_selection(add=, remove=)` の内部では `_coerce_refs` が `add` 用・
    `remove` 用の2回呼ばれるが、**この1回の `modify_selection` 呼び出しの中**では
    同じ `(body_id, kind)` を2回数えてはいけない —— `bodies`/`counts` を2回の
    `_coerce_refs` 呼び出しで共有することで満たす。"""
    face_calls = _count_kernel_calls(monkeypatch, "face_count")
    add_refs = [{"body": "box", "kind": "face", "index": i + 1} for i in range(6)]
    remove_refs = [{"body": "box", "kind": "face", "index": 1}]

    result = selection.modify_selection(two_shapes, add=add_refs, remove=remove_refs)

    assert [item["index"] for item in result.items] == [2, 3, 4, 5, 6]
    assert face_calls[0] == 1  # add・removeを合わせてもこの呼び出し中は1回だけ


# --------------------------------------------------------------------------------------
# 変異検査(必須): キャッシュ鍵から body_id を落とすと、この境目で赤くなる
# --------------------------------------------------------------------------------------
def test_the_cache_does_not_confuse_a_smaller_bodys_count_with_a_larger_bodys(
    two_shapes: Document,
):
    """`(body_id, kind)` がキャッシュ鍵であることを直接見る。

    先に円柱(3面)の面を1件処理させてから、箱(6面)の6番目の面を同じ
    `modify_selection` 呼び出しに混ぜる。キャッシュ鍵が `kind` だけ(`body_id` を
    見ない)だと、箱の検査に円柱の面数3が使い回され、`face index out of range:
    6 (1..3)` と**誤って**弾かれる —— 2026-09-04の変異検査で実際に再現した回帰。
    """
    refs = [
        {"body": "cyl", "kind": "face", "index": 1},
        {"body": "box", "kind": "face", "index": 6},
    ]

    result = selection.modify_selection(two_shapes, add=refs)

    assert len(result.items) == 2
    assert result.items == [
        {"body": "cyl", "kind": "face", "index": 1, "provenance": None},
        {"body": "box", "kind": "face", "index": 6, "provenance": None},
    ]


def test_out_of_range_index_is_still_rejected_per_bodys_own_count(two_shapes: Document):
    """面数の異なる2ボディが同じ呼び出しに混ざっても、範囲外はそれぞれの実際の
    面数で判定される(完了条件3の、面数が食い違うボディでの回帰版)。"""
    with pytest.raises(ValueError):
        selection.modify_selection(two_shapes, add=[{"body": "cyl", "kind": "face", "index": 4}])
    with pytest.raises(ValueError):
        selection.modify_selection(two_shapes, add=[{"body": "box", "kind": "face", "index": 7}])


# --------------------------------------------------------------------------------------
# 可視性の門番(`_visible_body`)はキャッシュ経路でも飛ばさない
# --------------------------------------------------------------------------------------
def test_hidden_body_face_ref_is_refused_through_modify_selection(two_shapes: Document):
    """不可視ボディへの面参照は、`_coerce_refs` のキャッシュ経路でも今までどおり
    `ValueError` になる(可視性チェックをボディごとに1回へ間引いても、その1回を
    飛ばしてはいけない)。`test_selection.py::test_hidden_bodies_cannot_be_selected`
    は `select_face` を直接呼ぶ経路しか見ていないので、`modify_selection` 経由の
    バッチ経路をここで別途押さえる。"""
    selection.set_visible(two_shapes, "cyl", False)

    with pytest.raises(ValueError):
        selection.modify_selection(two_shapes, add=[{"body": "cyl", "kind": "face", "index": 1}])

    assert two_shapes.selection.items == []  # 失敗時は選択を変えない


def test_hidden_body_face_ref_is_refused_through_propose_selection(two_shapes: Document):
    """`propose_selection` 側の `_coerce_refs` 呼び出しでも同じ門番が効く。"""
    selection.set_visible(two_shapes, "cyl", False)

    with pytest.raises(ValueError):
        selection.propose_selection(two_shapes, refs=[{"body": "cyl", "kind": "face", "index": 1}])
