"""M2契約: 領域検出(`duocad.region`)。

**領域は描くものではなく計算されるもの**(実装計画書§3.3)。円を線分で分けて片側だけを
押し出す、という本人の運用の土台であり、M2の本体はここにある。

期待値は**すべて実測値**(タスクファイル記載・実装計画書§3.3と一致):

    半径20の円          -> 領域1個・面積 1256.637
    それを y=5 の線分で分割 -> 領域2個・面積 826.215 と 430.422(合計が元の円と一致)
    線を2本にすると       -> 領域3個
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
    """半径20の円と、必要なら y=`lines` の水平線を持つスケッチを作って返す。

    線分は円(直径40)より長い x=-30..30 に取る。**端点が円の内側で止まる線は領域を割らない**
    (割るには境界まで届く必要がある)ので、ここは意図的に貫かせている。
    """
    sketch_id = sketch_create(document, "XY")
    add_circle(document, sketch_id, (0.0, 0.0), CIRCLE_RADIUS)
    for y in lines:
        add_line(document, sketch_id, (-30.0, y), (30.0, y))
    return sketch_id


# --------------------------------------------------------------------------------------
# 領域検出 — 実測値で縛る
# --------------------------------------------------------------------------------------
def test_a_single_circle_yields_one_region():
    """半径20の円だけのスケッチ -> 領域1個・面積 1256.637。添字は1始まり。"""
    document = Document()
    sketch_id = circle_sketch(document)

    found = regions(document, sketch_id)

    assert len(found) == 1
    assert found[0].index == 1
    assert math.isclose(found[0].area, CIRCLE_AREA, rel_tol=AREA_REL_TOL)


def test_a_line_splits_the_circle_into_two_regions():
    """y=5 の線分で分割 -> 領域2個・面積 826.215 と 430.422。**合計が元の円と一致**する。

    合計の一致がいちばん効く検査で、領域の取り違え・重複・取りこぼしをまとめて落とす。
    """
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


def test_region_enumeration_order_is_stable():
    """同じスケッチを2回評価すると、同じ順序・同じ添字・同じ面積が返る。

    選択集合は領域を**添字**で指す(§3.5)ので、評価のたびに順序が揺れると、
    保存した選択が別の領域を指すことになる。§7の実測でも「領域数と列挙順は安定」と出ている。
    """
    document = Document()
    sketch_id = circle_sketch(document, lines=(SPLIT_LINE_Y,))

    first = regions(document, sketch_id)
    second = regions(document, sketch_id)

    assert first == second


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
