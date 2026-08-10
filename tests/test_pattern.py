"""M3契約: 矩形反復(`duocad.verbs.pattern`)。

**反復は1つのverb**として持つ(実装計画書§2「verbの粒度」)。N個の独立した図形へ展開すると
個数も間隔も後から変えられなくなり、**反復の存在理由そのものが消える**。
展開は幾何を作るときだけ行う。

M3の意義そのものが `test_editing_one_radius_moves_every_hole` である:
受入対象の棚取付具とモニタ取付具は、穴の半径セットが **[2.0, 2.5, 5.0, 9.0, 10.0] で
完全に一致**しており(実測)、M4/M5のネジ穴と座ぐりがこれにあたる。
**半径を1箇所変えて全部の穴が追随する**のがこの機能の存在理由。
"""
from __future__ import annotations

import json
import math

import pytest

from duocad import kernel, query
from duocad.document import Document
from duocad.oplog import OpLog
from duocad.parameters import parameter_of, set_parameter
from duocad.region import region_ref, regions
from duocad.sketch import add_circle, add_rectangle, get_sketch, sketch_create
from duocad.verbs import extrude, pattern_rectangular

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
HOLE_DEPTH = 20.0  # 板(z=0..10)を貫く

#: 体積比較の相対許容差(M1・M2と同じ)。面積の比較だけは領域検出の側なので 1e-6 のまま
VOLUME_REL_TOL = 1.0e-9


def plate(document: Document) -> None:
    """40×30の板(厚さ10)を作る。"""
    sketch_id = sketch_create(document, "XY")
    add_rectangle(document, sketch_id, (0.0, 0.0), PLATE_WIDTH, PLATE_HEIGHT)
    extrude(document, [region_ref(sketch_id, regions(document, sketch_id)[0].index)],
            PLATE_THICKNESS, mode="join")


def hole_pattern(document: Document) -> tuple:
    """穴1つ＋3×2の反復を持つスケッチを作り、(sketch_id, entity_id, pattern_id) を返す。"""
    sketch_id = sketch_create(document, "XY")
    circle = add_circle(document, sketch_id, FIRST_HOLE, HOLE_RADIUS)
    pattern_id = pattern_rectangular(
        document, sketch_id, [circle], SPACING_X, SPACING_Y, COLUMNS, ROWS
    )
    return sketch_id, circle, pattern_id


def drill_all(document: Document, sketch_id: str) -> None:
    """スケッチの全領域を1回の押し出しで切り取る。"""
    refs = [region_ref(sketch_id, region.index) for region in regions(document, sketch_id)]
    extrude(document, refs, HOLE_DEPTH, mode="cut")


def hole_axes(shape) -> list:
    """穴(全周円筒面)の軸の (x, y) を昇順で返す。全周円筒の重心は軸上にある。"""
    return sorted(
        (round(face["centroid"][0], 6), round(face["centroid"][1], 6))
        for face in kernel.tessellate(shape)["faces"]
        if face["kind"] == "cylinder_full"
    )


# --------------------------------------------------------------------------------------
# 反復そのもの
# --------------------------------------------------------------------------------------
def test_pattern_multiplies_the_entity_into_nx_times_ny_regions():
    """`pattern_rectangular` で図形が nx × ny 個に増える(格子は元を含む)。"""
    document = Document()
    sketch_id, _, _ = hole_pattern(document)

    found = regions(document, sketch_id)

    assert len(found) == COLUMNS * ROWS
    for region in found:
        assert math.isclose(region.area, math.pi * HOLE_RADIUS**2, rel_tol=1e-6)


def test_pattern_is_one_operation_and_is_not_expanded_into_entities():
    """反復が**1つのverbとして操作ログに1行**で載り、図形はN個に展開されない。

    展開してしまうと、個数も間隔も後から変えられなくなる(＝編集可能性が消える)。
    スケッチが持つのは元の図形1個と反復1個だけで、6個の円は幾何を作るときの導出物である。
    """
    document = Document()
    sketch_id, circle, pattern_id = hole_pattern(document)

    sketch = get_sketch(document, sketch_id)
    assert [entity.id for entity in sketch.entities] == [circle], "図形が展開されている"
    assert [pattern.id for pattern in sketch.patterns] == [pattern_id]
    assert sketch.patterns[0].sources == [circle]
    # 間隔と個数も**名前つきの値への参照**(図形と同じ扱い。ここが素の数値だと
    # 後から個数・間隔を変えられない = 反復の存在理由が消える)
    pattern_params = sketch.patterns[0].params
    assert set(pattern_params) == {"dx", "dy", "nx", "ny"}
    assert all(isinstance(value, str) for value in pattern_params.values()), pattern_params
    for key in ("dx", "dy", "nx", "ny"):
        assert pattern_params[key] == parameter_of(document, sketch_id, pattern_id, key)

    entries = document.log.entries
    assert [operation.verb for operation in entries] == [
        "sketch_create", "add_circle", "pattern_rectangular",
    ]
    assert entries[-1].args == {
        "sketch_id": sketch_id,
        "pattern_id": pattern_id,
        "entities": [circle],
        "dx": SPACING_X,
        "dy": SPACING_Y,
        "nx": COLUMNS,
        "ny": ROWS,
    }
    json.loads(document.log.to_json())


def test_pattern_can_be_undone():
    """反復を undo すると元の1個へ戻る(元の図形は残る)。"""
    document = Document()
    sketch_id, circle, _ = hole_pattern(document)
    assert len(regions(document, sketch_id)) == COLUMNS * ROWS

    undone = document.log.undo(document)

    assert undone is not None and undone.verb == "pattern_rectangular"
    assert get_sketch(document, sketch_id).patterns == []
    assert len(regions(document, sketch_id)) == 1
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
    circle = add_circle(document, sketch_id, FIRST_HOLE, HOLE_RADIUS)
    entries_before = len(document.log.entries)

    with pytest.raises(ValueError):
        pattern_rectangular(document, sketch_id, [circle], dx, dy, nx, ny)

    assert get_sketch(document, sketch_id).patterns == []
    assert len(document.log.entries) == entries_before


def test_pattern_rejects_an_empty_or_foreign_entity_list():
    """反復する図形が無い / 別スケッチの図形は失敗する(黙って空の反復を作らない)。

    例外の種類はM0〜M2の流儀に合わせる: 「値がおかしい」は `ValueError`、
    「そのIDのものが無い」は `KeyError`(`get_sketch` / `entity_params` と同じ)。
    """
    document = Document()
    first = sketch_create(document, "XY")
    add_circle(document, first, FIRST_HOLE, HOLE_RADIUS)
    second = sketch_create(document, "XY")
    foreign = add_circle(document, second, FIRST_HOLE, HOLE_RADIUS)
    entries_before = len(document.log.entries)

    with pytest.raises(ValueError):
        pattern_rectangular(document, first, [], SPACING_X, SPACING_Y, COLUMNS, ROWS)
    with pytest.raises(KeyError):
        pattern_rectangular(document, first, [foreign], SPACING_X, SPACING_Y, COLUMNS, ROWS)

    assert get_sketch(document, first).patterns == []
    assert len(document.log.entries) == entries_before


def test_pattern_rejects_patterning_the_same_entity_twice():
    """既に反復に使われている図形を二重に反復させない(ValueError・文書とログは不変)。

    通すと同じ位置に辺が二重に重なり、領域検出が黙って壊れる。
    """
    document = Document()
    sketch_id, circle, _ = hole_pattern(document)
    entries_before = len(document.log.entries)

    with pytest.raises(ValueError):
        pattern_rectangular(document, sketch_id, [circle], 5.0, 5.0, 2, 2)

    assert len(get_sketch(document, sketch_id).patterns) == 1
    assert len(document.log.entries) == entries_before


# --------------------------------------------------------------------------------------
# 反復した図形を押し出す
# --------------------------------------------------------------------------------------
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


def test_changing_the_count_and_the_spacing_moves_the_holes():
    """`nx` や `dx` を後から変更でき、**穴の数・間隔が追随する**。

    形状はログからの導出物なので、`set_parameter` が再生して作り直す(§3.1)。
    穴の位置は全周円筒面の重心(=軸)で確かめる。
    """
    document = Document()
    plate(document)
    sketch_id, _, pattern_id = hole_pattern(document)
    drill_all(document, sketch_id)
    assert hole_axes(document.active_body().shape) == [
        (10.0, 10.0), (10.0, 20.0), (20.0, 10.0), (20.0, 20.0), (30.0, 10.0), (30.0, 20.0),
    ]

    set_parameter(document, parameter_of(document, sketch_id, pattern_id, "nx"), 2)

    shape = document.active_body().shape
    assert query.describe_shape(shape)["faces_by_type"]["cylinder_full"] == 2 * ROWS
    assert hole_axes(shape) == [(10.0, 10.0), (10.0, 20.0), (20.0, 10.0), (20.0, 20.0)]

    set_parameter(document, parameter_of(document, sketch_id, pattern_id, "dx"), 8.0)

    shape = document.active_body().shape
    assert hole_axes(shape) == [(10.0, 10.0), (10.0, 20.0), (18.0, 10.0), (18.0, 20.0)]
    assert math.isclose(
        query.volume(shape),
        PLATE_VOLUME - 2 * ROWS * math.pi * HOLE_RADIUS**2 * PLATE_THICKNESS,
        rel_tol=VOLUME_REL_TOL,
    )


def test_editing_one_radius_moves_every_hole():
    """**穴の半径を1箇所だけ編集すると、反復で作った全部の穴が追随する。**

    M3の存在理由そのもの。実測の裏付け: 棚取付具とモニタ取付具は穴の半径セットが
    [2.0, 2.5, 5.0, 9.0, 10.0] で完全に一致しており、M4/M5のネジ穴と座ぐりである。
    反復をN個の図形に展開していたら、ここでN箇所を直すことになる。
    """
    document = Document()
    plate(document)
    sketch_id, circle, _ = hole_pattern(document)
    drill_all(document, sketch_id)

    set_parameter(
        document, parameter_of(document, sketch_id, circle, "radius"), HOLE_RADIUS_AFTER
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
        document, parameter_of(document, sketch_id, circle, "radius"), HOLE_RADIUS_AFTER
    )
    expected_volume = query.volume(document.active_body().shape)
    expected_axes = hole_axes(document.active_body().shape)

    replayed = Document()
    replayed.log = OpLog.from_json(document.log.to_json())
    replayed.log.replay(replayed)

    body = replayed.active_body()
    assert body is not None
    assert math.isclose(query.volume(body.shape), expected_volume, rel_tol=1e-9)
    assert hole_axes(body.shape) == expected_axes
    assert len(get_sketch(replayed, sketch_id).patterns) == 1
