"""M3契約: 矩形反復(`duocad.verbs.pattern`)。"""
from __future__ import annotations

import json
import math

import pytest

from duocad import query
from duocad.document import Document
from duocad.parameters import parameter_of, resolve, set_parameter
from duocad.region import region_ref, regions
from duocad.sketch import add_circle, add_rectangle, get_sketch, remove_entities, sketch_create
from duocad.verbs import extrude, move_sketch_entities, pattern_rectangular
from document_support import drill_all, hole_axes, replay_through_json

PLATE_WIDTH = 40.0
PLATE_HEIGHT = 30.0
PLATE_THICKNESS = 10.0
PLATE_VOLUME = PLATE_WIDTH * PLATE_HEIGHT * PLATE_THICKNESS

#: 受入対象の部品で実測された穴の半径セット [2.0, 2.5, 5.0, 9.0, 10.0] の最初の2つ。
#: M4/M5のネジ穴にあたり、「1箇所編集して全穴が追随する」の題材そのもの
HOLE_RADIUS = 2.0
HOLE_RADIUS_AFTER = 2.5

FIRST_HOLE = (10.0, 10.0)
SPACING_X = 10.0
SPACING_Y = 10.0
COLUMNS = 3
ROWS = 2

#: 体積比較の相対許容差(M1・M2と同じ)。面積の比較だけは領域検出の側なので 1e-6 のまま
VOLUME_REL_TOL = 1.0e-9


def plate(document: Document) -> None:
    sketch_id = sketch_create(document, "XY")
    add_rectangle(document, sketch_id, (0.0, 0.0), PLATE_WIDTH, PLATE_HEIGHT)
    extrude(document, [region_ref(sketch_id, regions(document, sketch_id)[0].index)],
            PLATE_THICKNESS, mode="join")


def hole_pattern(document: Document) -> tuple:
    sketch_id = sketch_create(document, "XY")
    circle = add_circle(document, sketch_id, FIRST_HOLE, 2 * HOLE_RADIUS)
    created = pattern_rectangular(
        document, sketch_id, [circle], SPACING_X, SPACING_Y, COLUMNS, ROWS
    )
    return sketch_id, circle, created


def centres(document: Document, sketch_id: str) -> list:
    return [
        (resolve(document, entity.params["cx"]), resolve(document, entity.params["cy"]))
        for entity in get_sketch(document, sketch_id).entities
    ]


def test_pattern_multiplies_the_entity_into_nx_times_ny_regions():
    """`pattern_rectangular` で図形が nx × ny 個に増える(格子は元を含む)。"""
    document = Document()
    sketch_id, _, _ = hole_pattern(document)

    found = regions(document, sketch_id)

    assert len(found) == COLUMNS * ROWS
    for region in found:
        assert math.isclose(region.area, math.pi * HOLE_RADIUS**2, rel_tol=1e-6)


def test_copies_are_independent_entities_made_by_one_operation():
    """反復は複製を nx × ny − 1 個の独立した図形として作り、ログには作ったIDを持つ1行だけ載る。"""
    document = Document()
    sketch_id, circle, created = hole_pattern(document)

    sketch = get_sketch(document, sketch_id)
    assert [entity.id for entity in sketch.entities] == [circle, *created]
    assert len(set(created)) == COLUMNS * ROWS - 1
    assert sketch.patterns == [], "旧形式の Pattern を置いている"
    assert centres(document, sketch_id) == [
        (10.0, 10.0), (20.0, 10.0), (30.0, 10.0), (10.0, 20.0), (20.0, 20.0), (30.0, 20.0),
    ]

    entries = document.log.entries
    assert [operation.verb for operation in entries] == [
        "sketch_create", "add_circle", "pattern_rectangular",
    ]
    assert entries[-1].args == {
        "sketch_id": sketch_id,
        "entities": [circle],
        "dx": SPACING_X,
        "dy": SPACING_Y,
        "nx": COLUMNS,
        "ny": ROWS,
        "created": created,
    }
    json.loads(document.log.to_json())


def test_a_copy_does_not_follow_when_the_source_moves():
    """複製は元と繋がらない: 元を動かしても複製は動かず、複製を1つ消しても他は残る。"""
    document = Document()
    sketch_id, circle, created = hole_pattern(document)

    move_sketch_entities(document, [{"sketch": sketch_id, "entity": circle, "point": None}], 0.0, 50.0)
    remove_entities(document, [created[0]], sketch_id=sketch_id)

    assert centres(document, sketch_id) == [
        (10.0, 60.0), (30.0, 10.0), (10.0, 20.0), (20.0, 20.0), (30.0, 20.0),
    ]


def test_pattern_can_be_undone():
    """反復を undo すると元の1個へ戻る(元の図形は残る)。"""
    document = Document()
    sketch_id, circle, _ = hole_pattern(document)
    assert len(regions(document, sketch_id)) == COLUMNS * ROWS

    parameters_before = len(document.parameters)

    undone = document.log.undo(document)

    assert undone is not None and undone.verb == "pattern_rectangular"
    assert len(regions(document, sketch_id)) == 1
    assert len(document.parameters) < parameters_before, "複製の名前つきの値を手放していない"
    assert [entity.id for entity in get_sketch(document, sketch_id).entities] == [circle]

    document.log.redo(document)
    assert len(regions(document, sketch_id)) == COLUMNS * ROWS


@pytest.mark.parametrize(
    ("dx", "dy", "nx", "ny"),
    [(10.0, 10.0, 0, 2), (10.0, 10.0, 3, -1), (10.0, 10.0, 2.5, 2)],
    ids=["zero_columns", "negative_rows", "fractional_columns"],
)
def test_pattern_rejects_counts_that_are_not_positive_integers(dx, dy, nx, ny):
    """個数は1以上の整数。0・負数・非整数は ValueError で、文書にもログにも残さない。"""
    document = Document()
    sketch_id = sketch_create(document, "XY")
    circle = add_circle(document, sketch_id, FIRST_HOLE, 2 * HOLE_RADIUS)
    entries_before = len(document.log.entries)

    with pytest.raises(ValueError):
        pattern_rectangular(document, sketch_id, [circle], dx, dy, nx, ny)

    assert [entity.id for entity in get_sketch(document, sketch_id).entities] == [circle]
    assert len(document.log.entries) == entries_before


@pytest.mark.parametrize(
    ("dx", "dy", "nx", "ny"),
    [(10.0, 10.0, 1, 1), (0.0, 10.0, 3, 1), (0.0, 0.0, 2, 2)],
    ids=["no_copies", "zero_spacing_along_the_row", "zero_spacing"],
)
def test_pattern_rejects_making_no_copies_or_copies_on_the_source(dx, dy, nx, ny):
    """複製が1つもできない指定・元と重なる複製ができる指定は ValueError(文書もログも不変)。"""
    document = Document()
    sketch_id = sketch_create(document, "XY")
    circle = add_circle(document, sketch_id, FIRST_HOLE, 2 * HOLE_RADIUS)
    entries_before = len(document.log.entries)
    parameters_before = dict(document.parameters)

    with pytest.raises(ValueError):
        pattern_rectangular(document, sketch_id, [circle], dx, dy, nx, ny)

    assert [entity.id for entity in get_sketch(document, sketch_id).entities] == [circle]
    assert document.parameters == parameters_before
    assert len(document.log.entries) == entries_before


def test_pattern_rejects_an_empty_or_foreign_entity_list():
    """反復する図形が無い / 別スケッチの図形は失敗する(黙って空の反復を作らない)。"""
    document = Document()
    first = sketch_create(document, "XY")
    add_circle(document, first, FIRST_HOLE, 2 * HOLE_RADIUS)
    second = sketch_create(document, "XY")
    foreign = add_circle(document, second, FIRST_HOLE, 2 * HOLE_RADIUS)
    entries_before = len(document.log.entries)

    with pytest.raises(ValueError):
        pattern_rectangular(document, first, [], SPACING_X, SPACING_Y, COLUMNS, ROWS)
    with pytest.raises(KeyError):
        pattern_rectangular(document, first, [foreign], SPACING_X, SPACING_Y, COLUMNS, ROWS)

    assert len(get_sketch(document, first).entities) == 1
    assert len(document.log.entries) == entries_before


def test_a_copy_can_be_patterned_again():
    """複製は素の図形なので、もう一度反復の元にできる(反復の反復)。"""
    document = Document()
    sketch_id, _, created = hole_pattern(document)

    again = pattern_rectangular(document, sketch_id, [created[-1]], 10.0, 0.0, 2, 1)

    assert len(again) == 1
    assert centres(document, sketch_id)[-1] == (40.0, 20.0)


def test_extruding_a_pattern_opens_nx_times_ny_holes():
    """反復した図形群を押し出すと、穴が nx × ny 個できる。"""
    document = Document()
    plate(document)
    sketch_id, _, _ = hole_pattern(document)

    drill_all(document, sketch_id)

    shape = document.active_body().shape
    assert query.describe_shape(shape)["faces_by_type"]["cylinder_full"] == COLUMNS * ROWS
    assert math.isclose(
        query.volume(shape),
        PLATE_VOLUME - COLUMNS * ROWS * math.pi * HOLE_RADIUS**2 * PLATE_THICKNESS,
        rel_tol=VOLUME_REL_TOL,
    )


def test_editing_one_radius_moves_every_hole():
    """元の穴の半径を編集すると、作り直しで複製も元から作り直されて全部の穴が追随する。"""
    document = Document()
    plate(document)
    sketch_id, circle, _ = hole_pattern(document)
    drill_all(document, sketch_id)

    set_parameter(
        document, parameter_of(document, sketch_id, circle, "diameter"), 2 * HOLE_RADIUS_AFTER
    )

    shape = document.active_body().shape
    assert query.describe_shape(shape)["faces_by_type"]["cylinder_full"] == COLUMNS * ROWS
    assert math.isclose(
        query.volume(shape),
        PLATE_VOLUME - COLUMNS * ROWS * math.pi * HOLE_RADIUS_AFTER**2 * PLATE_THICKNESS,
        rel_tol=VOLUME_REL_TOL,
    )
    # 領域の側でも全部が新しい半径になっている(1つだけ追随する、が起きない)
    for region in regions(document, sketch_id):
        assert math.isclose(region.area, math.pi * HOLE_RADIUS_AFTER**2, rel_tol=1e-6)


def test_pattern_survives_save_load_and_replay():
    """反復とパラメータ変更を含む文書が、保存 → 読み込み → 再生で同じ形になる。"""
    document = Document()
    plate(document)
    sketch_id, circle, _ = hole_pattern(document)
    drill_all(document, sketch_id)
    set_parameter(
        document, parameter_of(document, sketch_id, circle, "diameter"), 2 * HOLE_RADIUS_AFTER
    )
    expected_volume = query.volume(document.active_body().shape)
    expected_axes = hole_axes(document.active_body().shape)

    replayed = replay_through_json(document)

    body = replayed.active_body()
    assert body is not None
    assert math.isclose(query.volume(body.shape), expected_volume, rel_tol=1e-9)
    assert hole_axes(body.shape) == expected_axes
    assert len(get_sketch(replayed, sketch_id).entities) == COLUMNS * ROWS
