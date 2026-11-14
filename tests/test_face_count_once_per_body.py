"""`_coerce_refs`(`duocad/selection.py`)がボディごとに数え上げを1回だけ行う契約。"""
from __future__ import annotations

import pytest
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder

from duocad import selection
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


def test_a_changed_shape_is_counted_again_in_the_next_call(two_shapes: Document):
    """数え上げは呼び出しをまたいで持ち越さない。落ちたら、形状が変わった後も古い面数で範囲を検査している。"""
    selection.modify_selection(two_shapes, add=[{"body": "cyl", "kind": "face", "index": 1}])
    two_shapes.get_body("cyl").shape = _box_shape()  # 3面 → 6面

    result = selection.modify_selection(two_shapes, add=[{"body": "cyl", "kind": "face", "index": 6}])

    assert [item["index"] for item in result.items] == [1, 6]


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
