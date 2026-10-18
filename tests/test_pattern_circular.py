"""円形反復 `pattern_circular` の契約。`pattern_rectangular` の兄弟で語彙・規律を揃える
(数値の場合分けは `duocad/verbs/pattern.py` の docstring)。
"""
from __future__ import annotations

import math

import pytest

from duocad import kernel_sketch, query
from duocad.document import Document
from duocad.region import expanded_entities, region_ref, regions
from duocad.sketch import (
    add_circle,
    add_rectangle,
    get_sketch,
    rotate_point,
    sketch_create,
)
from duocad.verbs import extrude, pattern_circular
from document_support import corner_set as _corner_set, drill_all, hole_axes, replay_through_json

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
    created = pattern_circular(
        document, sketch_id, [circle], CENTER, count, total_angle=total_angle
    )
    return sketch_id, circle, created


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


def test_copies_are_independent_entities_made_by_one_operation():
    """反復は count−1 個の複製を独立した図形として作り、ログには作ったIDを持つ1行だけ載る。"""
    document = Document()
    sketch_id, entity_id, created = hole_ring(document)

    sketch = get_sketch(document, sketch_id)
    assert [entity.id for entity in sketch.entities] == [entity_id, *created]
    assert len(set(created)) == COUNT - 1
    assert sketch.patterns == [], "旧形式の Pattern を置いている"

    circular = [op for op in document.log.entries if op.verb == "pattern_circular"]
    assert len(circular) == 1, "1つの反復が複数行に分かれている"
    args = circular[0].args
    assert args["entities"] == [entity_id]
    assert args["count"] == COUNT
    assert args["total_angle"] == 360.0
    assert args["center"] == list(CENTER)
    assert args["rotate_copies"] is True, "既定はTrue（Fusionの挙動に合わせる）"
    assert args["created"] == created


def test_pattern_can_be_undone():
    """undoで元の1個に戻る（反復だけが消え、元の図形は残る）。"""
    document = Document()
    sketch_id, entity_id, _ = hole_ring(document)
    assert len(regions(document, sketch_id)) == COUNT

    document.log.undo(document)

    sketch = get_sketch(document, sketch_id)
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


def test_a_single_copy_is_refused():
    """`count=1` は複製ができないので ValueError(刻み角の0除算にも行き着かない)。"""
    document = Document()
    sketch_id = sketch_create(document, "XY")
    circle = add_circle(document, sketch_id, FIRST_HOLE, 2 * HOLE_RADIUS)

    with pytest.raises(ValueError):
        pattern_circular(document, sketch_id, [circle], CENTER, 1, total_angle=180.0)

    assert [entity.id for entity in get_sketch(document, sketch_id).entities] == [circle]


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


def test_pattern_survives_save_load_and_replay():
    """保存→再生で穴の位置がそのまま復元される(`args` に数値が載っていること。個数だけでは角度の落ちを見逃す)。"""
    document = Document()
    plate(document)
    sketch_id, _, _ = hole_ring(document)
    drill_all(document, sketch_id)
    expected_axes = hole_axes(document.active_body().shape)

    restored = replay_through_json(document)

    assert len(expected_axes) == COUNT
    assert hole_axes(restored.active_body().shape) == expected_axes


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

    assert len(get_sketch(document, sketch_id).entities) == 1
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

    assert len(get_sketch(document, sketch_id).entities) == 1
    assert len(document.log.entries) == log_before


def test_unknown_entity_is_refused():
    """存在しない図形IDは KeyError（矩形反復と同じ契約）。"""
    document = Document()
    sketch_id = sketch_create(document, "XY")
    add_circle(document, sketch_id, FIRST_HOLE, 2 * HOLE_RADIUS)
    log_before = len(document.log.entries)

    with pytest.raises(KeyError):
        pattern_circular(document, sketch_id, ["entity-ghost"], CENTER, COUNT)

    assert len(get_sketch(document, sketch_id).entities) == 1
    assert len(document.log.entries) == log_before


def test_empty_entity_list_is_refused():
    document = Document()
    sketch_id = sketch_create(document, "XY")
    with pytest.raises(ValueError):
        pattern_circular(document, sketch_id, [], CENTER, COUNT)


def test_the_same_entity_can_be_patterned_twice():
    """反復の元は使い回せる(複製は独立した図形なので二重に展開されることが無い)。"""
    document = Document()
    sketch_id, entity_id, created = hole_ring(document)

    again = pattern_circular(document, sketch_id, [entity_id], CENTER, 3, total_angle=90.0)

    assert len(again) == 2
    assert len(get_sketch(document, sketch_id).entities) == 1 + len(created) + len(again)


def test_a_shape_centred_on_the_pivot_is_refused():
    """回転中心と図形の中心が一致する円は、どの個数でも全複製が元と重なるので拒否する。"""
    document = Document()
    sketch_id = sketch_create(document, "XY")
    circle = add_circle(document, sketch_id, CENTER, 2 * HOLE_RADIUS)  # 中心が回転中心と一致
    log_before = len(document.log.entries)

    with pytest.raises(ValueError):
        pattern_circular(document, sketch_id, [circle], CENTER, COUNT)

    assert len(get_sketch(document, sketch_id).entities) == 1
    assert len(document.log.entries) == log_before


def _centred(kind: str):
    """回転中心 (0, 0) に中心がある図形を1つ持つスケッチ。"""
    from duocad.sketch import add_arc, add_ellipse, add_line, add_rectangle

    document = Document()
    sketch_id = sketch_create(document, "XY")
    entity = {
        "arc": lambda: add_arc(document, sketch_id, (0.0, 0.0), 10.0, 0.0, 20.0),
        "line": lambda: add_line(document, sketch_id, (-10.0, 0.0), (10.0, 0.0)),
        "rectangle": lambda: add_rectangle(document, sketch_id, (-5.0, -2.0), 10.0, 4.0),
        "square": lambda: add_rectangle(document, sketch_id, (-3.0, -3.0), 6.0, 6.0),
        "ellipse": lambda: add_ellipse(document, sketch_id, (0.0, 0.0), 20.0, 8.0, 0.0),
        "round_ellipse": lambda: add_ellipse(document, sketch_id, (0.0, 0.0), 8.0, 8.0, 0.0),
        "full_arc": lambda: add_arc(document, sketch_id, (0.0, 0.0), 10.0, 0.0, 360.0),
    }[kind]()
    return document, sketch_id, entity


@pytest.mark.parametrize(
    "kind, count, total_angle",
    [
        ("arc", 4, 120.0),         # 0〜20 度の円弧を 40 度刻みで 4 本: どれも重ならない
        ("line", 3, 360.0),        # 線分を 120 度刻み: 180 度でないので重ならない
        ("rectangle", 3, 360.0),   # 10×4 の長方形を 120 度刻み
        ("ellipse", 3, 360.0),
    ],
)
def test_a_shape_centred_on_the_pivot_whose_copies_do_not_coincide_is_repeated(kind, count, total_angle):
    """落ちたら、中心が回転中心と一致するだけで、重ならない複製まで「全複製が元と重なる」と断っている。"""
    document, sketch_id, entity = _centred(kind)
    created = pattern_circular(document, sketch_id, [entity], (0.0, 0.0), count, total_angle)
    assert len(created) == count - 1


@pytest.mark.parametrize(
    "kind, count, total_angle",
    [
        ("line", 2, 360.0),       # 180 度で線分が自分に重なる
        ("rectangle", 2, 360.0),  # 長方形は 180 度回すと自分に重なる
        ("square", 4, 360.0),     # 正方形は 90 度で自分に重なる
        ("ellipse", 2, 360.0),
        ("round_ellipse", 3, 360.0),  # 縦横が同じ楕円は円
        ("full_arc", 2, 360.0),       # 始終の角が同じ(0〜360 度)円弧は全周で、円と同じ
    ],
)
def test_a_copy_that_lands_on_the_original_is_still_refused(kind, count, total_angle):
    document, sketch_id, entity = _centred(kind)
    with pytest.raises(ValueError, match="coincide"):
        pattern_circular(document, sketch_id, [entity], (0.0, 0.0), count, total_angle)
    assert len(get_sketch(document, sketch_id).entities) == 1


def test_without_rotating_the_copies_a_centred_shape_always_coincides():
    document, sketch_id, entity = _centred("arc")
    with pytest.raises(ValueError, match="coincide"):
        pattern_circular(document, sketch_id, [entity], (0.0, 0.0), 4, 120.0, rotate_copies=False)
