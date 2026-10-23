"""`_coerce_refs`(`duocad/selection.py`)がボディごとに数え上げを1回だけ行う契約。"""
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


def test_a_changed_shape_is_counted_again_in_the_next_call(two_shapes: Document):
    """数え上げは呼び出しをまたいで持ち越さない。落ちたら、形状が変わった後も古い面数で範囲を検査している。"""
    selection.modify_selection(two_shapes, add=[{"body": "cyl", "kind": "face", "index": 1}])
    two_shapes.get_body("cyl").shape = _box_shape()  # 3面 → 6面

    result = selection.modify_selection(two_shapes, add=[{"body": "cyl", "kind": "face", "index": 6}])

    assert [item["index"] for item in result.items] == [1, 6]


def test_add_and_remove_in_the_same_call_share_the_count_cache(
    two_shapes: Document, monkeypatch: pytest.MonkeyPatch
):
    """`add` と `remove` を同時に指定しても、この1回の `modify_selection` 呼び出しの
    中では同じ `(body_id, kind)` を2回数えない(`bodies`/`counts` を2回の
    `_coerce_refs` 呼び出しで共有する)。"""
    face_calls = _count_kernel_calls(monkeypatch, "face_count")
    add_refs = [{"body": "box", "kind": "face", "index": i + 1} for i in range(6)]
    remove_refs = [{"body": "box", "kind": "face", "index": 1}]

    result = selection.modify_selection(two_shapes, add=add_refs, remove=remove_refs)

    assert [item["index"] for item in result.items] == [2, 3, 4, 5, 6]
    assert face_calls[0] == 1  # add・removeを合わせてもこの呼び出し中は1回だけ


def test_the_cache_does_not_confuse_a_smaller_bodys_count_with_a_larger_bodys(
    two_shapes: Document,
):
    """`(body_id, kind)` がキャッシュ鍵であることを直接見る。"""
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
    面数で判定される。"""
    with pytest.raises(ValueError):
        selection.modify_selection(two_shapes, add=[{"body": "cyl", "kind": "face", "index": 4}])
    with pytest.raises(ValueError):
        selection.modify_selection(two_shapes, add=[{"body": "box", "kind": "face", "index": 7}])


def test_hidden_body_face_ref_is_refused_through_modify_selection(two_shapes: Document):
    """不可視ボディへの面参照は、`_coerce_refs` のキャッシュ経路でも今までどおり
    `ValueError` になる(可視性チェックをボディごとに1回へ間引いても、その1回を
    飛ばしてはいけない)。`test_selection.py::test_hidden_bodies_cannot_be_selected`
    は単発の経路(`_select_shape`)しか見ていないので、`modify_selection` 経由の
    バッチ経路をここで別途押さえる。"""
    selection.set_visible(two_shapes, "cyl", False)

    with pytest.raises(ValueError):
        selection.modify_selection(two_shapes, add=[{"body": "cyl", "kind": "face", "index": 1}])

    assert two_shapes.selection.items == []  # 失敗時は選択を変えない
