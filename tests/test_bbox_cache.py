"""`kernel.BoundingBoxCache` の同一性と寿命。

`bounding_box` は `BRepBndLib::AddOptimal` を使う(実測: 1形状 18〜60ms)ので、繰り返し
呼ぶ経路はキャッシュが当たることに依存している。落ちたとき:

- 「取り出し直した面が当たる」が落ちる → 鍵の同一性が壊れ、当たらないまま遅くなっている
- 「動かした形は当たらない」が落ちる → 古い位置の箱を返している(寸法が黙って嘘になる)
- 「文書を切り替えると空」が落ちる → 別の文書の形状を握り続けている
"""

from __future__ import annotations

import pytest
from build123d import Box

from duocad import kernel
from duocad.document import SOLID_KIND, Body
from duocad.server import Session

DEVIATION = 0.2


@pytest.fixture()
def session() -> Session:
    return Session(None, DEVIATION)


def test_a_face_taken_twice_hits_the_same_entry() -> None:
    """`TopoDS_Shape` は hash が一致しても `__eq__` が False なので、素の dict だと当たらない。"""
    shape = Box(40, 20, 10).wrapped
    cache = kernel.BoundingBoxCache()
    first = kernel.bounding_box(kernel.face_of(shape, 1), cache=cache)
    second = kernel.bounding_box(kernel.face_of(shape, 1), cache=cache)
    assert len(cache) == 1
    assert first == second


def test_a_moved_shape_does_not_reuse_the_box_of_its_old_position() -> None:
    shape = Box(40, 20, 10).wrapped
    cache = kernel.BoundingBoxCache()
    before = kernel.bounding_box(shape, cache=cache)
    moved = kernel.translate_shape(shape, 1000.0, 0.0, 0.0)
    after = kernel.bounding_box(moved, cache=cache)
    assert len(cache) == 2
    assert after["center"][0] == pytest.approx(before["center"][0] + 1000.0)


def test_the_returned_box_is_not_shared_between_calls() -> None:
    shape = Box(40, 20, 10).wrapped
    cache = kernel.BoundingBoxCache()
    first = kernel.bounding_box(shape, cache=cache)
    first["min"][0] = 12345.0
    assert kernel.bounding_box(shape, cache=cache)["min"][0] != 12345.0


def test_switching_the_document_leaves_an_empty_cache(session: Session) -> None:
    body_id = session.document.add_body(
        Body(id="b1", name="b1", kind=SOLID_KIND, shape=Box(40, 20, 10).wrapped)
    )
    kernel.bounding_box(
        session.document.get_body(body_id).shape, cache=session.document.bbox_cache
    )
    assert len(session.document.bbox_cache) == 1
    previous = session.document.bbox_cache
    session.new_document()
    assert session.document.bbox_cache is not previous
    assert len(session.document.bbox_cache) == 0
