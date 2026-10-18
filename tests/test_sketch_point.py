"""スケッチの点の図形(`add_point`)。辺を持たず領域を割らない・ほかの図形と同じ経路で動かせる・選択は点として扱う。"""
from __future__ import annotations

import pytest

from duocad import parameters, selection
from duocad.document import Document
from duocad.region import regions
from duocad.server import Session
from duocad.sketch import add_line, add_point, add_rectangle, get_sketch, sketch_create
from duocad.verbs import mirror_sketch_entities, move_sketch_entities, pattern_circular, pattern_rectangular
from document_support import replay_through_json


def _params(document: Document, sketch_id: str, entity_id: str) -> dict:
    entity = next(item for item in get_sketch(document, sketch_id).entities if item.id == entity_id)
    return parameters.resolve_all(document, entity.params)


def _point_ref(sketch_id: str, entity_id: str) -> dict:
    return {"sketch": sketch_id, "entity": entity_id, "point": "cx"}


def test_a_point_does_not_change_the_regions():
    """落ちたら、点の図形が領域の検出に辺として入るか、検出を壊している(点を描いただけで押し出せる面が変わる)。"""
    document = Document()
    sketch_id = sketch_create(document, "XY")
    add_rectangle(document, sketch_id, (0.0, 0.0), 20.0, 10.0)
    before = [(item.index, round(item.area, 6)) for item in regions(document, sketch_id)]

    add_point(document, sketch_id, (5.0, 5.0))  # 内側
    add_point(document, sketch_id, (20.0, 3.0))  # 輪郭の上
    add_point(document, sketch_id, (40.0, 40.0))  # 外側

    assert [(item.index, round(item.area, 6)) for item in regions(document, sketch_id)] == before


def test_a_sketch_of_only_points_has_no_regions():
    document = Document()
    sketch_id = sketch_create(document, "XY")
    add_point(document, sketch_id, (1.0, 2.0))
    assert regions(document, sketch_id) == []


def test_a_point_survives_undo_redo_and_replay():
    document = Document()
    sketch_id = sketch_create(document, "XY")
    point = add_point(document, sketch_id, (3.0, -4.0))

    document.log.undo(document)
    assert get_sketch(document, sketch_id).entities == []
    document.log.redo(document)
    assert _params(document, sketch_id, point) == {"cx": 3.0, "cy": -4.0}

    restored = replay_through_json(document)
    assert [entity.kind for entity in get_sketch(restored, sketch_id).entities] == ["point"]
    assert _params(restored, sketch_id, point) == {"cx": 3.0, "cy": -4.0}


@pytest.mark.parametrize("bad", [[1.0], [1.0, 2.0, 3.0], ["1", "2"], [float("nan"), 0.0]])
def test_a_point_needs_two_finite_numbers(bad):
    document = Document()
    sketch_id = sketch_create(document, "XY")
    with pytest.raises(ValueError):
        add_point(document, sketch_id, bad)
    assert get_sketch(document, sketch_id).entities == []


def test_a_point_moves_rotates_and_mirrors_like_other_shapes():
    document = Document()
    sketch_id = sketch_create(document, "XY")
    point = add_point(document, sketch_id, (10.0, 0.0))

    move_sketch_entities(document, [_point_ref(sketch_id, point)], dx=1.0, dy=2.0)
    assert _params(document, sketch_id, point) == pytest.approx({"cx": 11.0, "cy": 2.0})

    move_sketch_entities(document, [_point_ref(sketch_id, point)], mode="rotate", angle=90.0, center=[0.0, 0.0])
    assert _params(document, sketch_id, point) == pytest.approx({"cx": -2.0, "cy": 11.0})

    axis = add_line(document, sketch_id, (0.0, -5.0), (0.0, 5.0))  # v 軸
    mirror_sketch_entities(document, [_point_ref(sketch_id, point)], {"sketch": sketch_id, "entity": axis})
    copies = [entity for entity in get_sketch(document, sketch_id).entities if entity.kind == "point" and entity.id != point]
    assert len(copies) == 1
    assert parameters.resolve_all(document, copies[0].params) == pytest.approx({"cx": 2.0, "cy": 11.0})


def test_patterns_copy_a_point_as_independent_points():
    document = Document()
    sketch_id = sketch_create(document, "XY")
    point = add_point(document, sketch_id, (10.0, 0.0))

    created = pattern_rectangular(document, sketch_id, [point], 5.0, 5.0, 3, 2)
    created += pattern_circular(document, sketch_id, [point], (0.0, 0.0), 4)

    kinds = {entity.id: entity.kind for entity in get_sketch(document, sketch_id).entities}
    assert len(created) == 5 + 3
    assert all(kinds[entity_id] == "point" for entity_id in created)


def test_a_point_on_the_pivot_is_refused_by_a_circular_pattern():
    """落ちたら、回転中心に置いた点の複製が元と同じ位置に重なったまま作られる。"""
    document = Document()
    sketch_id = sketch_create(document, "XY")
    point = add_point(document, sketch_id, (0.0, 0.0))
    with pytest.raises(ValueError, match="coincide"):
        pattern_circular(document, sketch_id, [point], (0.0, 0.0), 4)
    assert len(get_sketch(document, sketch_id).entities) == 1


def test_selecting_a_point_entity_always_means_its_point(session: Session):
    """落ちたら、点の図形を `point` 無しで選んだ参照と `point: "cx"` の参照が別の項目になり、同じ点が2度選ばれる。"""
    created = session.apply("sketch_create", {"base_plane": "XY"})
    sketch_id = created["sketch_id"]
    point = session.apply("add_point", {"sketch_id": sketch_id, "point": [1.0, 1.0]})["entity_id"]

    session.set_selection(refs=[{"sketch": sketch_id, "entity": point}])
    session.modify_selection(add=[_point_ref(sketch_id, point)])

    items = selection.get_selection(session.document).items
    assert len(items) == 1
    assert items[0]["point"] == "cx"
