"""M2契約: 領域検出(`duocad.region`)。

期待値は**すべて実測値**(docs/DESIGN_DECISIONS.md「領域は計算されるもの」の実測と一致):
"""
from __future__ import annotations

import math

import pytest

from duocad.document import Document
from duocad.region import regions
from duocad.sketch import add_circle, add_line, sketch_create

CIRCLE_RADIUS = 20.0
#: 実測値。真値は π·20² = 1256.6370614… で、下の許容差の中に入る
CIRCLE_AREA = 1256.637
#: y=5 の線分で切ったときの2領域(大きい方 / 小さい方)。真値は 826.2152… / 430.4218…
LARGER_AREA = 826.215
SMALLER_AREA = 430.422
SPLIT_LINE_Y = 5.0

#: 面積比較の相対許容差。上の実測値は小数3桁までなので真値との差は最大でも約 3e-7 —
#: 1e-6 はそれを確実に包み、かつ領域の取り違え(桁が変わる)は確実に落とせる幅。
AREA_REL_TOL = 1.0e-6


def circle_sketch(document: Document, lines: tuple = ()) -> str:
    """半径20の円と、必要なら y=`lines` の水平線を持つスケッチを作って返す。"""
    sketch_id = sketch_create(document, "XY")
    add_circle(document, sketch_id, (0.0, 0.0), 2 * CIRCLE_RADIUS)
    for y in lines:
        add_line(document, sketch_id, (-30.0, y), (30.0, y))
    return sketch_id


# 領域検出 — 実測値で縛る
def test_a_single_circle_yields_one_region():
    """半径20の円だけのスケッチ -> 領域1個・面積 1256.637。添字は1始まり。"""
    document = Document()
    sketch_id = circle_sketch(document)

    found = regions(document, sketch_id)

    assert len(found) == 1
    assert found[0].index == 1
    assert math.isclose(found[0].area, CIRCLE_AREA, rel_tol=AREA_REL_TOL)


def test_a_line_splits_the_circle_into_two_regions():
    """y=5 の線分で分割 -> 領域2個・面積 826.215 と 430.422。**合計が元の円と一致**する。"""
    document = Document()
    sketch_id = circle_sketch(document, lines=(SPLIT_LINE_Y,))

    found = regions(document, sketch_id)

    assert len(found) == 2
    assert sorted(region.index for region in found) == [1, 2]
    areas = sorted(region.area for region in found)
    assert math.isclose(areas[0], SMALLER_AREA, rel_tol=AREA_REL_TOL)
    assert math.isclose(areas[1], LARGER_AREA, rel_tol=AREA_REL_TOL)
    assert math.isclose(sum(areas), CIRCLE_AREA, rel_tol=AREA_REL_TOL)


def test_two_lines_split_the_circle_into_three_regions():
    """線を2本にすると領域が3個になる(分割が1回きりの特別扱いになっていないこと)。"""
    document = Document()
    sketch_id = circle_sketch(document, lines=(SPLIT_LINE_Y, -SPLIT_LINE_Y))

    found = regions(document, sketch_id)

    assert len(found) == 3
    assert sorted(region.index for region in found) == [1, 2, 3]
    assert math.isclose(
        sum(region.area for region in found), CIRCLE_AREA, rel_tol=AREA_REL_TOL
    )


def test_regions_are_numbered_largest_first():
    """添字は面積の降順で振られる。落ちたら、保存した選択(領域を**添字**で指す)が別の領域を指す。"""
    document = Document()
    sketch_id = circle_sketch(document, lines=(SPLIT_LINE_Y,))

    found = regions(document, sketch_id)

    assert [region.index for region in found] == [1, 2]
    assert math.isclose(found[0].area, LARGER_AREA, rel_tol=AREA_REL_TOL)


def test_regions_of_a_sketch_without_entities_is_empty():
    """図形が1つも無いスケッチの領域は空(例外にしない。まだ何も描いていないだけ)。"""
    document = Document()
    sketch_id = sketch_create(document, "XY")

    assert regions(document, sketch_id) == []


def test_regions_of_an_unknown_sketch_fails():
    """存在しないスケッチIDは KeyError(黙って空リストを返さない)。"""
    document = Document()

    with pytest.raises(KeyError):
        regions(document, "sketch-does-not-exist")
