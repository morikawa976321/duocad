"""`bounding_box` は `BRepBndLib::AddOptimal` を使う(実測: 1形状 18〜60ms)ので、
繰り返し呼ぶ経路はキャッシュが当たることに依存している。
"""

from __future__ import annotations

import pytest
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

from duocad import kernel

from duocad import (
    brep_topology as _brep_topology,
)


def _box() -> object:
    return BRepPrimAPI_MakeBox(40.0, 20.0, 10.0).Shape()


def test_a_face_taken_twice_hits_the_same_entry() -> None:
    """`TopoDS_Shape` は hash が一致しても `__eq__` が False なので、素の dict だと当たらない。"""
    shape = _box()
    cache = kernel.BoundingBoxCache()
    first = kernel.bounding_box(_brep_topology.face_of(shape, 1), cache=cache)
    second = kernel.bounding_box(_brep_topology.face_of(shape, 1), cache=cache)
    assert len(cache) == 1
    assert first == second


def test_a_moved_shape_does_not_reuse_the_box_of_its_old_position() -> None:
    """落ちたら: 古い位置の箱を返している(寸法が黙って嘘になる)。"""
    shape = _box()
    cache = kernel.BoundingBoxCache()
    before = kernel.bounding_box(shape, cache=cache)
    moved = kernel.translate_shape(shape, 1000.0, 0.0, 0.0)
    after = kernel.bounding_box(moved, cache=cache)
    assert len(cache) == 2
    assert after["center"][0] == pytest.approx(before["center"][0] + 1000.0)


def test_the_returned_box_is_not_shared_between_calls() -> None:
    """落ちたら: 呼び出し側が返り値を書き換えるとキャッシュが汚れる。"""
    shape = _box()
    cache = kernel.BoundingBoxCache()
    first = kernel.bounding_box(shape, cache=cache)
    first["min"][0] = 12345.0
    assert kernel.bounding_box(shape, cache=cache)["min"][0] != 12345.0
