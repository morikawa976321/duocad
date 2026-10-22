"""鏡映軸・回転中心に別のスケッチの図形を指すとき、(u, v) が同じ平面上の同じ点を指さないなら断る。"""
from __future__ import annotations

import pytest

from duocad.document import Document
from duocad.sketch import add_circle, add_line, get_sketch, sketch_create
from duocad.verbs import mirror_sketch_entities, move_sketch_entities

OFFSET_XY = {"kind": "offset", "base": {"kind": "base", "name": "XY"}, "distance": 10.0}


def _whole(sketch_id: str, entity_id: str) -> list:
    return [{"sketch": sketch_id, "entity": entity_id, "point": None}]


def _count(document: Document, sketch_id: str) -> int:
    return len(get_sketch(document, sketch_id).entities)


def test_a_mirror_axis_on_another_plane_is_refused():
    """落ちたら: XY のスケッチの線を軸に XZ のスケッチの円を鏡映でき、軸の (u, v) が XZ の座標として当たって別の場所に映る。"""
    document = Document()
    axis_sketch = sketch_create(document, "XY")
    axis = add_line(document, axis_sketch, (0.0, 0.0), (0.0, 10.0))
    target_sketch = sketch_create(document, "XZ")
    circle = add_circle(document, target_sketch, (5.0, 5.0), 4.0)

    with pytest.raises(ValueError, match="平面が違う"):
        mirror_sketch_entities(document, _whole(target_sketch, circle), {"sketch": axis_sketch, "entity": axis})

    assert _count(document, target_sketch) == 1


def test_a_mirror_axis_on_a_parallel_offset_plane_is_allowed():
    """原点が法線方向にずれただけのスケッチは (u, v) が同じ点を指すので通す。"""
    document = Document()
    axis_sketch = sketch_create(document, "XY")
    axis = add_line(document, axis_sketch, (0.0, 0.0), (0.0, 10.0))
    target_sketch = sketch_create(document, OFFSET_XY)
    circle = add_circle(document, target_sketch, (5.0, 5.0), 4.0)

    mirror_sketch_entities(document, _whole(target_sketch, circle), {"sketch": axis_sketch, "entity": axis})

    assert _count(document, target_sketch) == 2


def test_a_mirror_axis_in_another_sketch_on_the_same_plane_is_allowed():
    document = Document()
    axis_sketch = sketch_create(document, "XY")
    axis = add_line(document, axis_sketch, (0.0, 0.0), (0.0, 10.0))
    target_sketch = sketch_create(document, "XY")
    circle = add_circle(document, target_sketch, (5.0, 5.0), 4.0)

    mirror_sketch_entities(document, _whole(target_sketch, circle), {"sketch": axis_sketch, "entity": axis})

    assert _count(document, target_sketch) == 2


def test_a_rotation_center_on_another_plane_is_refused():
    document = Document()
    center_sketch = sketch_create(document, "XY")
    center_circle = add_circle(document, center_sketch, (1.0, 2.0), 4.0)
    target_sketch = sketch_create(document, "XZ")
    line = add_line(document, target_sketch, (0.0, 0.0), (10.0, 0.0))
    center = {"sketch": center_sketch, "entity": center_circle, "point": "cx"}

    with pytest.raises(ValueError, match="平面が違う"):
        move_sketch_entities(
            document, _whole(target_sketch, line), mode="rotate", angle=30.0, center_entity=center
        )

    assert len(document.log.entries) == 4


def test_a_rotation_center_on_a_parallel_offset_plane_is_allowed():
    document = Document()
    center_sketch = sketch_create(document, "XY")
    center_circle = add_circle(document, center_sketch, (1.0, 2.0), 4.0)
    target_sketch = sketch_create(document, OFFSET_XY)
    line = add_line(document, target_sketch, (0.0, 0.0), (10.0, 0.0))

    operation = move_sketch_entities(
        document,
        _whole(target_sketch, line),
        mode="rotate",
        angle=30.0,
        center_entity={"sketch": center_sketch, "entity": center_circle, "point": "cx"},
    )

    assert operation.args["center"] == [1.0, 2.0]
