"""`bounding_box` は `BRepBndLib::AddOptimal` を使う(実測: 1形状 18〜60ms)ので、
繰り返し呼ぶ経路はキャッシュが当たることに依存している。
"""

from __future__ import annotations

import pytest
from build123d import Box

from duocad import kernel


def test_a_face_taken_twice_hits_the_same_entry() -> None:
    """`TopoDS_Shape` は hash が一致しても `__eq__` が False なので、素の dict だと当たらない。"""
    shape = Box(40, 20, 10).wrapped
    cache = kernel.BoundingBoxCache()
    first = kernel.bounding_box(kernel.face_of(shape, 1), cache=cache)
    second = kernel.bounding_box(kernel.face_of(shape, 1), cache=cache)
    assert len(cache) == 1
    assert first == second


def test_a_moved_shape_does_not_reuse_the_box_of_its_old_position() -> None:
    """落ちたら: 古い位置の箱を返している(寸法が黙って嘘になる)。"""
    shape = Box(40, 20, 10).wrapped
    cache = kernel.BoundingBoxCache()
    before = kernel.bounding_box(shape, cache=cache)
    moved = kernel.translate_shape(shape, 1000.0, 0.0, 0.0)
    after = kernel.bounding_box(moved, cache=cache)
    assert len(cache) == 2
    assert after["center"][0] == pytest.approx(before["center"][0] + 1000.0)


def test_the_returned_box_is_not_shared_between_calls() -> None:
    """落ちたら: 呼び出し側が返り値を書き換えるとキャッシュが汚れる。"""
    shape = Box(40, 20, 10).wrapped
    cache = kernel.BoundingBoxCache()
    first = kernel.bounding_box(shape, cache=cache)
    first["min"][0] = 12345.0
    assert kernel.bounding_box(shape, cache=cache)["min"][0] != 12345.0
