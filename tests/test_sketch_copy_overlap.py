"""図形の複製(鏡映・回転コピー)は、元と丸め誤差の範囲で重なるなら新しく作らない。保存済みの文書の再生は断らない。"""
from __future__ import annotations

import math

import pytest

from duocad import parameters
from duocad.document import Document
from duocad.sketch import add_circle, add_ellipse, add_line, add_rectangle, entity_kind_ops, get_sketch, sketch_create
from duocad.verbs import mirror_sketch_entities, move_sketch_entities
from document_support import replay_through_json


def _diagonal_axis_and_circle() -> tuple:
    """斜めの軸 (0,0)-(10,10) と、その上に中心を置いた円。軸の単位ベクトルは `(1/√2, 1/√2)`。"""
    document = Document()
    sketch_id = sketch_create(document, "XY")
    axis = add_line(document, sketch_id, (0.0, 0.0), (10.0, 10.0))
    circle = add_circle(document, sketch_id, (3.0, 3.0), 4.0)
    return document, sketch_id, axis, circle


def _entity_count(document: Document, sketch_id: str) -> int:
    return len(get_sketch(document, sketch_id).entities)


def test_a_mirror_that_lands_one_ulp_off_the_original_is_refused():
    """落ちたら: 斜めの軸の上の円が 1ulp ずれた複製になって通り、同じ円が2つの半円の領域に割れる。"""
    document, sketch_id, axis, circle = _diagonal_axis_and_circle()
    length = math.hypot(10.0, 10.0)
    current = parameters.resolve_all(document, get_sketch(document, sketch_id).entities[1].params)
    mirrored = entity_kind_ops("circle").mirror(current, 0.0, 0.0, 10.0 / length, 10.0 / length)
    assert mirrored["cx"] != current["cx"], "厳密一致では弾けない題材であること"

    with pytest.raises(ValueError, match="元と完全に重なる"):
        mirror_sketch_entities(
            document,
            [{"sketch": sketch_id, "entity": circle}],
            {"sketch": sketch_id, "entity": axis},
        )

    assert _entity_count(document, sketch_id) == 2


def test_a_saved_mirror_that_is_one_ulp_off_still_replays():
    """落ちたら: 重なる複製を断る前に作られた複製を含む保存済みの文書が、再生で ValueError になって開けない。"""
    document, sketch_id, axis, circle = _diagonal_axis_and_circle()
    length = math.hypot(10.0, 10.0)
    document.log.append(
        "mirror_sketch_entities",
        {
            "targets": [{"sketch": sketch_id, "entity": circle}],
            "axis_p0": [0.0, 0.0],
            "axis_dir": [10.0 / length, 10.0 / length],
            "created": ["entity-old-copy"],
        },
    )

    replayed = replay_through_json(document)

    assert _entity_count(replayed, sketch_id) == 3


def test_a_rotated_copy_that_lands_next_to_the_original_is_refused():
    document = Document()
    sketch_id = sketch_create(document, "XY")
    line = add_line(document, sketch_id, (0.0, 0.0), (10.0, 0.0))

    with pytest.raises(ValueError, match="元と完全に重なる"):
        move_sketch_entities(
            document,
            [{"sketch": sketch_id, "entity": line, "point": None}],
            mode="rotate",
            angle=1.0e-12,
            center=[5.0, 0.0],
            copy=True,
        )

    assert _entity_count(document, sketch_id) == 1


def test_a_saved_rotated_copy_next_to_the_original_still_replays():
    document = Document()
    sketch_id = sketch_create(document, "XY")
    line = add_line(document, sketch_id, (0.0, 0.0), (10.0, 0.0))
    document.log.append(
        "move_sketch_entities",
        {
            "targets": [{"sketch": sketch_id, "entity": line, "point": None}],
            "mode": "rotate",
            "angle": 1.0e-12,
            "center": [5.0, 0.0],
            "copy": True,
            "created": ["entity-old-copy"],
        },
    )

    replayed = replay_through_json(document)

    assert _entity_count(replayed, sketch_id) == 2


def test_a_rotated_copy_of_a_rectangle_without_angle_is_not_taken_for_an_overlap():
    """落ちたら、`angle` を持たない旧い長方形(キーが4個)の回転コピーを、元のキーだけで比べて「元と完全に重なる」と
    誤って断る(保存済みのログの再生も止まる)。"""
    from duocad.verbs._common import overlaps_original

    old_rectangle = {"x": 0.0, "y": 0.0, "width": 10.0, "height": 4.0}
    rotated = {**old_rectangle, "angle": 30.0}
    assert not overlaps_original("rectangle", rotated, old_rectangle, fresh=True)
    assert not overlaps_original("rectangle", rotated, old_rectangle, fresh=False)
    # 向きが同じ(angle=0)なら、無いキーは 0.0 として重なる
    assert overlaps_original("rectangle", {**old_rectangle, "angle": 0.0}, old_rectangle, fresh=False)


@pytest.mark.parametrize("kind,angle", [("line", 180), ("rectangle", 180), ("square", 90), ("ellipse", 180)])
def test_rotational_symmetry_cannot_create_a_duplicate(kind, angle):
    document = Document()
    sketch_id = sketch_create(document, "XY")
    if kind == "line":
        entity_id = add_line(document, sketch_id, [-5, 0], [5, 0])
    elif kind == "ellipse":
        entity_id = add_ellipse(document, sketch_id, [0, 0], 10, 6, 25)
    else:
        height = 10 if kind == "square" else 6
        entity_id = add_rectangle(document, sketch_id, [-5, -height / 2], 10, height)
    before = document.log.to_json(created=0)
    with pytest.raises(ValueError, match="元と完全に重なる"):
        move_sketch_entities(document, [{"sketch": sketch_id, "entity": entity_id}],
                             mode="rotate", angle=angle, center=[0, 0], copy=True)
    assert document.log.to_json(created=0) == before
    assert _entity_count(document, sketch_id) == 1


@pytest.mark.parametrize("axis_end", [[0, 10], [10, 0], [10, 10]])
def test_mirror_symmetry_of_rectangles_is_refused(axis_end):
    document = Document()
    sketch_id = sketch_create(document, "XY")
    axis = add_line(document, sketch_id, [0, 0], axis_end)
    height = 10 if axis_end == [10, 10] else 6
    entity_id = add_rectangle(document, sketch_id, [-5, -height / 2], 10, height)
    before = document.log.to_json(created=0)
    with pytest.raises(ValueError, match="元と完全に重なる"):
        mirror_sketch_entities(document, [{"sketch": sketch_id, "entity": entity_id}],
                               {"sketch": sketch_id, "entity": axis})
    assert document.log.to_json(created=0) == before
    assert _entity_count(document, sketch_id) == 2


@pytest.mark.parametrize("verb", ["mirror_sketch_entities", "move_sketch_entities"])
def test_saved_geometrically_overlapping_copies_still_replay_and_redo(verb):
    document = Document()
    sketch_id = sketch_create(document, "XY")
    entity_id = add_rectangle(document, sketch_id, [-5, -3], 10, 6)
    args = {"targets": [{"sketch": sketch_id, "entity": entity_id}], "created": ["entity-old-copy"]}
    if verb == "mirror_sketch_entities":
        args.update(axis_p0=[0, 0], axis_dir=[0, 1])
    else:
        args.update(mode="rotate", angle=180, center=[0, 0], copy=True)
    document.log.append(verb, args)
    restored = replay_through_json(document)
    assert _entity_count(restored, sketch_id) == 2
    restored.log.undo(restored)
    restored.log.redo(restored)
    assert _entity_count(restored, sketch_id) == 2


@pytest.mark.parametrize("kind,values,current,expected", [
    ("line", {"x0": 0, "y0": 1e-8, "x1": 10, "y1": 1e-8}, {"x0": 0, "y0": 0, "x1": 10, "y1": 0}, False),
    ("arc", {"cx": 0, "cy": 0, "radius": 5, "start_deg": 360, "end_deg": 450},
     {"cx": 0, "cy": 0, "radius": 5, "start_deg": 0, "end_deg": 90}, True),
    ("arc", {"cx": 0, "cy": 0, "radius": 5, "start_deg": 90, "end_deg": 360},
     {"cx": 0, "cy": 0, "radius": 5, "start_deg": 0, "end_deg": 90}, False),
    ("ellipse", {"cx": 0, "cy": 0, "width": 10, "height": 6, "angle": 30},
     {"cx": 0, "cy": 0, "width": 10, "height": 6, "angle": -30}, False),
    ("bezier", {"x0": 0, "y0": 0, "cx1": 1, "cy1": 2, "cx2": -1, "cy2": -2, "x1": 0, "y1": 0},
     {"x0": 0, "y0": 0, "cx1": -1, "cy1": -2, "cx2": 1, "cy2": 2, "x1": 0, "y1": 0}, True),
])
def test_geometry_comparison_preserves_distinct_shapes_and_small_separations(kind, values, current, expected):
    from duocad.verbs._common import overlaps_original
    assert overlaps_original(kind, values, current, fresh=True) is expected


def test_point_copies_reject_overlap_and_preserve_translation(session):
    from session_support import select

    sketch_id = session.sketch_create("XY")["sketch_id"]
    entity_id = session.add_point(sketch_id, [3, 4])["entity_id"]
    select(session, [{"sketch": sketch_id, "entity": entity_id}])
    with pytest.raises(ValueError, match="元と完全に重なる"):
        session.move_sketch_entities(mode="rotate", angle=180, center=[3, 4], copy=True)
    result = session.move_sketch_entities(dx=10, dy=0, copy=True)
    assert len(result["created"]) == 1
    copied = get_sketch(session.document, sketch_id).entities[-1]
    assert parameters.resolve_all(session.document, copied.params) == {"cx": 13, "cy": 4}
