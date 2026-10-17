"""円形反復 `pattern_circular` の契約。`pattern_rectangular` の兄弟で語彙・規律を揃える
(数値の場合分けは `duocad/verbs/pattern.py` の docstring)。
"""
from __future__ import annotations

import math

import pytest

from duocad import kernel_sketch, query
from duocad.document import Document
from duocad.oplog import OpLog
from duocad.parameters import parameter_of, set_parameter
from duocad.region import expanded_entities, region_ref, regions
from duocad.sketch import (
    add_circle,
    add_rectangle,
    get_sketch,
    rotate_point,
    sketch_create,
)
from duocad.verbs import extrude, pattern_circular
from document_support import corner_set as _corner_set, drill_all, hole_axes

PLATE_SIZE = 60.0
PLATE_THICKNESS = 10.0

#: 反復の中心（板の中央）と、そこから離した最初の穴
CENTER = (30.0, 30.0)
FIRST_HOLE = (30.0 + 18.0, 30.0)
HOLE_RADIUS = 3.0
COUNT = 6


def plate(document: Document) -> None:
    sketch_id = sketch_create(document, "XY")
    add_rectangle(document, sketch_id, (0.0, 0.0), PLATE_SIZE, PLATE_SIZE)
    extrude(
        document,
        [region_ref(sketch_id, regions(document, sketch_id)[0].index)],
        PLATE_THICKNESS,
        mode="join",
    )


def hole_ring(document: Document, count: int = COUNT, total_angle: float = 360.0) -> tuple:
    sketch_id = sketch_create(document, "XY")
    circle = add_circle(document, sketch_id, FIRST_HOLE, 2 * HOLE_RADIUS)
    pattern_id = pattern_circular(
        document, sketch_id, [circle], CENTER, count, total_angle=total_angle
    )
    return sketch_id, circle, pattern_id


def angles_from_center(points: list) -> list:
    return sorted(
        round(math.degrees(math.atan2(y - CENTER[1], x - CENTER[0])) % 360.0, 6)
        for x, y in points
    )


def test_pattern_multiplies_the_entity_into_count_regions():
    """`count=N` で領域が N 個になる（元を含めて N。N-1個の複製が足される）。"""
    document = Document()
    sketch_id, _, _ = hole_ring(document)

    found = regions(document, sketch_id)

    assert len(found) == COUNT
    for region in found:
        assert math.isclose(region.area, math.pi * HOLE_RADIUS**2, rel_tol=1e-6)


def test_pattern_is_one_operation_and_is_not_expanded_into_entities():
    """反復は操作ログに1行で載り、図形はN個に展開されない。"""
    document = Document()
    sketch_id, entity_id, pattern_id = hole_ring(document)

    sketch = get_sketch(document, sketch_id)
    assert len(sketch.entities) == 1, "図形がN個に展開されている"
    assert len(sketch.patterns) == 1
    assert sketch.patterns[0].id == pattern_id
    assert sketch.patterns[0].sources == [entity_id]
    assert sketch.patterns[0].kind == "circular", "矩形反復と区別できないと展開を分けられない"

    circular = [op for op in document.log.entries if op.verb == "pattern_circular"]
    assert len(circular) == 1, "1つの反復が複数行に分かれている"
    args = circular[0].args
    assert args["entities"] == [entity_id]
    assert args["count"] == COUNT
    assert args["total_angle"] == 360.0
    assert args["center"] == list(CENTER)
    assert args["rotate_copies"] is True, "既定はTrue（Fusionの挙動に合わせる）"


def test_pattern_can_be_undone():
    """undoで元の1個に戻る（反復だけが消え、元の図形は残る）。"""
    document = Document()
    sketch_id, entity_id, _ = hole_ring(document)
    assert len(regions(document, sketch_id)) == COUNT

    document.log.undo(document)

    sketch = get_sketch(document, sketch_id)
    assert sketch.patterns == []
    assert [entity.id for entity in sketch.entities] == [entity_id], "元の図形まで消えた"
    assert len(regions(document, sketch_id)) == 1

    document.log.redo(document)
    assert len(regions(document, sketch_id)) == COUNT


def test_extruding_a_circular_pattern_opens_count_holes():
    """反復した円を `cut` で押し出すと穴が **count個** できる。"""
    document = Document()
    plate(document)
    sketch_id, _, _ = hole_ring(document)

    drill_all(document, sketch_id)

    body = document.active_body()
    assert len(hole_axes(body.shape)) == COUNT
    expected = PLATE_SIZE**2 * PLATE_THICKNESS - COUNT * math.pi * HOLE_RADIUS**2 * PLATE_THICKNESS
    assert math.isclose(query.volume(body.shape), expected, rel_tol=1e-9)


def test_a_full_circle_divides_360_by_count():
    """全周のときの刻みは360/count。6個なら60度ずつ(`count-1`で割ると72度になり、
    6個目が元の位置へ戻って重なる)。
    """
    document = Document()
    plate(document)
    sketch_id, _, _ = hole_ring(document)
    drill_all(document, sketch_id)

    angles = angles_from_center(hole_axes(document.active_body().shape))

    assert len(angles) == COUNT
    step = 360.0 / COUNT
    for index, angle in enumerate(angles):
        assert math.isclose(angle, index * step, abs_tol=1e-4), f"{angles}"


def test_a_partial_angle_includes_both_ends():
    """全周未満のときは端点を含むので刻みは`total_angle/(count-1)`。180度に3個なら
    0/90/180度(`total_angle/count`で割ると60度刻みになり180度に届かない)。
    """
    document = Document()
    plate(document)
    sketch_id, _, _ = hole_ring(document, count=3, total_angle=180.0)
    drill_all(document, sketch_id)

    angles = angles_from_center(hole_axes(document.active_body().shape))

    assert len(angles) == 3
    for index, angle in enumerate(angles):
        assert math.isclose(angle, index * 90.0, abs_tol=1e-4), f"{angles}"


def test_a_single_copy_needs_no_step():
    """`count=1` は複製が無い（元だけ）。刻み角を計算しようとして0除算しないこと。"""
    document = Document()
    sketch_id, _, _ = hole_ring(document, count=1, total_angle=180.0)

    assert len(regions(document, sketch_id)) == 1


def test_rotate_copies_false_keeps_the_original_orientation():
    """`rotate_copies=False` の複製は元と同じ向き(中心から見た4隅が元と一致する)。

    落ちたら、複製が回っている。題材は正方形でない長方形(円や正方形は90度回しても区別が付かない)。
    """
    document = Document()
    sketch_id = sketch_create(document, "XY")
    rect = add_rectangle(document, sketch_id, (36.0, 28.0), 8.0, 4.0)
    pattern_circular(
        document, sketch_id, [rect], CENTER, 4, total_angle=360.0, rotate_copies=False
    )

    expanded = expanded_entities(document, sketch_id)
    assert len(expanded) == 4
    shapes = [_corners_about_centre(item["params"]) for item in expanded]
    assert all(shape == pytest.approx(shapes[0], abs=1e-9) for shape in shapes)


def _corners_about_centre(params: dict) -> list:
    corners = kernel_sketch.rectangle_corners(params)
    cu = sum(u for u, _ in corners) / len(corners)
    cv = sum(v for _, v in corners) / len(corners)
    return sorted((round(u - cu, 9), round(v - cv, 9)) for u, v in corners)


def test_rotated_copies_of_a_rectangle_are_all_rectangles():
    """回した複製が**すべて長方形**(参照元と同じ構成)で、4隅が元の4隅を回した集合と一致する。

    落ちたら、円形反復の展開が長方形を別の種別へ落としているか、複製の向きが
    ずれている。
    """
    document = Document()
    sketch_id = sketch_create(document, "XY")
    source = add_rectangle(document, sketch_id, (36.0, 28.0), 8.0, 4.0, angle=20.0)
    pattern_circular(
        document, sketch_id, [source], CENTER, 4, total_angle=360.0, rotate_copies=True
    )

    expanded = expanded_entities(document, sketch_id)
    assert [item["kind"] for item in expanded] == ["rectangle"] * 4

    base = expanded[0]["params"]
    for index, item in enumerate(expanded):
        turned = [
            (round(u, 9), round(v, 9))
            for u, v in (
                rotate_point(cu, cv, CENTER[0], CENTER[1], index * 90.0)
                for cu, cv in kernel_sketch.rectangle_corners(base)
            )
        ]
        assert _corner_set(item["params"]) == pytest.approx(sorted(turned), abs=1e-9)


def test_changing_the_count_changes_the_number_of_holes():
    """`count` は名前つきの値。変更すると穴の数が追随する。"""
    document = Document()
    sketch_id, _, pattern_id = hole_ring(document)
    pattern = get_sketch(document, sketch_id).patterns[0]
    name = pattern.params["count"]
    assert isinstance(name, str), "素の数値を持っていたら後から変えられない"
    # `parameter_of` は (doc, sketch_id, owner_id, key) — 矩形反復のテストと同じ引き方
    assert parameter_of(document, sketch_id, pattern_id, "count") == name

    set_parameter(document, name, 3.0)

    assert len(regions(document, sketch_id)) == 3


def test_changing_the_total_angle_moves_the_copies():
    """`total_angle` を変更すると配置角度が追随する。"""
    document = Document()
    plate(document)
    sketch_id, _, _ = hole_ring(document, count=3, total_angle=180.0)
    name = get_sketch(document, sketch_id).patterns[0].params["total_angle"]

    set_parameter(document, name, 90.0)
    drill_all(document, sketch_id)

    angles = angles_from_center(hole_axes(document.active_body().shape))
    assert len(angles) == 3
    for index, angle in enumerate(angles):
        assert math.isclose(angle, index * 45.0, abs_tol=1e-4), f"{angles}"


def test_changing_the_center_moves_the_whole_ring():
    """`center` を変更すると配置全体が移動する（中心も名前つきの値）。"""
    document = Document()
    plate(document)
    sketch_id, _, _ = hole_ring(document)
    params = get_sketch(document, sketch_id).patterns[0].params
    assert isinstance(params["cx"], str) and isinstance(params["cy"], str)

    set_parameter(document, params["cx"], CENTER[0] + 5.0)
    drill_all(document, sketch_id)

    axes = hole_axes(document.active_body().shape)
    assert len(axes) == COUNT
    mean_x = sum(x for x, _ in axes) / len(axes)
    mean_y = sum(y for _, y in axes) / len(axes)
    assert math.isclose(mean_x, CENTER[0] + 5.0, abs_tol=1e-4), "中心を動かしても配置が動いていない"
    assert math.isclose(mean_y, CENTER[1], abs_tol=1e-4)


def test_pattern_survives_save_load_and_replay():
    """保存→再生で反復がそのまま復元される（`args` に数値が載っていること）。"""
    document = Document()
    sketch_id, _, _ = hole_ring(document)
    saved = document.log.to_json()

    restored = Document()
    log = OpLog.from_json(saved)
    log.replay(restored)
    restored.log = log

    restored_sketch_id = restored.sketches[0].id
    assert len(regions(restored, restored_sketch_id)) == COUNT


# 失敗を黙って通さない
#: 0 / 負 / 非整数の3分岐は `test_pattern.py` が網羅済み(`parameters.is_positive_int` は共有)。
#: ここは呼び出し口の配線確認なので境界の 0 だけ通す。
@pytest.mark.parametrize("count", [0])
def test_count_must_be_a_positive_integer(count):
    """`count < 1` や非整数は ValueError。**文書にもログにも残さない**。"""
    document = Document()
    sketch_id = sketch_create(document, "XY")
    circle = add_circle(document, sketch_id, FIRST_HOLE, 2 * HOLE_RADIUS)
    log_before = len(document.log.entries)

    with pytest.raises(ValueError):
        pattern_circular(document, sketch_id, [circle], CENTER, count)

    assert get_sketch(document, sketch_id).patterns == []
    assert len(document.log.entries) == log_before


#: -90.0 は 0.0 と同じ分岐(`not 0 < angle <= 360`)なので落とした。
@pytest.mark.parametrize("total_angle", [0.0, 361.0, float("nan")])
def test_total_angle_must_be_usable(total_angle):
    """`total_angle` は 0 < angle <= 360。0・負・360超・NaNは ValueError。"""
    document = Document()
    sketch_id = sketch_create(document, "XY")
    circle = add_circle(document, sketch_id, FIRST_HOLE, 2 * HOLE_RADIUS)
    log_before = len(document.log.entries)

    with pytest.raises(ValueError):
        pattern_circular(document, sketch_id, [circle], CENTER, COUNT, total_angle=total_angle)

    assert get_sketch(document, sketch_id).patterns == []
    assert len(document.log.entries) == log_before


def test_unknown_entity_is_refused():
    """存在しない図形IDは KeyError（矩形反復と同じ契約）。"""
    document = Document()
    sketch_id = sketch_create(document, "XY")
    add_circle(document, sketch_id, FIRST_HOLE, 2 * HOLE_RADIUS)
    log_before = len(document.log.entries)

    with pytest.raises(KeyError):
        pattern_circular(document, sketch_id, ["entity-ghost"], CENTER, COUNT)

    assert get_sketch(document, sketch_id).patterns == []
    assert len(document.log.entries) == log_before


def test_empty_entity_list_is_refused():
    document = Document()
    sketch_id = sketch_create(document, "XY")
    with pytest.raises(ValueError):
        pattern_circular(document, sketch_id, [], CENTER, COUNT)


def test_patterning_the_same_entity_twice_is_refused():
    """1つの図形を2つの反復に使わない（矩形反復と同じ。二重に展開されるため）。"""
    document = Document()
    sketch_id, entity_id, _ = hole_ring(document)
    log_before = len(document.log.entries)

    with pytest.raises(ValueError):
        pattern_circular(document, sketch_id, [entity_id], CENTER, 3)

    assert len(get_sketch(document, sketch_id).patterns) == 1
    assert len(document.log.entries) == log_before


def test_a_shape_centred_on_the_pivot_is_refused():
    """回転中心と図形の中心が一致する場合は拒否する。"""
    document = Document()
    sketch_id = sketch_create(document, "XY")
    circle = add_circle(document, sketch_id, CENTER, 2 * HOLE_RADIUS)  # 中心が回転中心と一致
    log_before = len(document.log.entries)

    with pytest.raises(ValueError):
        pattern_circular(document, sketch_id, [circle], CENTER, COUNT)

    assert get_sketch(document, sketch_id).patterns == []
    assert len(document.log.entries) == log_before


def test_reducing_the_count_below_one_is_refused_afterwards_too():
    """`set_parameter` 経由でも `count` の検査が効く。"""
    document = Document()
    sketch_id, _, _ = hole_ring(document)
    name = get_sketch(document, sketch_id).patterns[0].params["count"]

    with pytest.raises(ValueError):
        set_parameter(document, name, 0.0)
    assert len(regions(document, sketch_id)) == COUNT


def test_changing_the_total_angle_to_an_unusable_value_is_refused():
    """`total_angle` も `set_parameter` 経由で検査する。"""
    document = Document()
    sketch_id, _, _ = hole_ring(document)
    name = get_sketch(document, sketch_id).patterns[0].params["total_angle"]

    for bad in (0.0, -90.0, 361.0):
        with pytest.raises(ValueError):
            set_parameter(document, name, bad)
    assert len(regions(document, sketch_id)) == COUNT
