"""M3の回帰テスト — **契約のテストが通っていても壊れていた経路**。"""
from __future__ import annotations

import math

import pytest

from duocad import query
from duocad.document import Document
from duocad.oplog import OpLog
from document_support import assert_finite_json, install_live_pattern, replay_through_json
from duocad.parameters import parameter_of, set_parameter
from duocad.region import region_ref, regions
from duocad.sketch import add_circle, sketch_create
from duocad.verbs import extrude

@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("diameter", float("nan")),
        ("diameter", float("inf")),
        ("diameter", 0.0),
        ("diameter", -5.0),
    ],
    ids=["nan", "inf", "zero", "negative"],
)
def test_set_parameter_rejects_values_that_add_circle_would_reject(key, value):
    """`add_circle` が作成時に弾く値は、`set_parameter` からも書き込めない。"""
    document = Document()
    sketch_id = sketch_create(document, "XY")
    circle = add_circle(document, sketch_id, (10.0, 10.0), 4.0)
    name = parameter_of(document, sketch_id, circle, key)
    entries_before = len(document.log.entries)

    with pytest.raises(ValueError):
        set_parameter(document, name, value)

    assert document.parameters[name] == 4.0
    assert len(document.log.entries) == entries_before
    assert len(regions(document, sketch_id)) == 1
    assert_finite_json(document.log.to_json())


@pytest.mark.parametrize("count", [2.5, 0, -3], ids=["fractional", "zero", "negative"])
def test_set_parameter_rejects_pattern_counts_that_are_not_positive_integers(count):
    """旧形式の反復(保存済みの古い文書に残る)の個数も、`pattern_rectangular` と同じ規則で後から書き換えられない。"""
    document = Document()
    sketch_id = sketch_create(document, "XY")
    circle = add_circle(document, sketch_id, (10.0, 10.0), 4.0)
    pattern_id = install_live_pattern(
        document, "pattern_rectangular", sketch_id, [circle], dx=10.0, dy=10.0, nx=3, ny=2
    )
    name = parameter_of(document, sketch_id, pattern_id, "nx")

    with pytest.raises(ValueError):
        set_parameter(document, name, count)

    assert document.parameters[name] == 3.0
    assert len(regions(document, sketch_id)) == 6


def test_replaying_a_foreign_log_fails_loudly_instead_of_emptying_the_document():
    """`set_parameter` を含むログを**別の文書へ直接** `replay` したら、黙って空にせず止まる。"""
    document = Document()
    sketch_id = sketch_create(document, "XY")
    circle = add_circle(document, sketch_id, (0.0, 0.0), 40.0)
    extrude(document, [region_ref(sketch_id, regions(document, sketch_id)[0].index)], 10.0)
    set_parameter(document, parameter_of(document, sketch_id, circle, "diameter"), 20.0)
    expected = query.volume(document.active_body().shape)

    stray = Document()
    with pytest.raises(ValueError):
        OpLog.from_json(document.log.to_json()).replay(stray)
    # `replay` は元々トランザクショナルではない(M0)ので、途中まで適用された状態で止まる。
    # 肝心なのは**黙って別物の文書を作らない**ことで、そのために例外で止める。
    assert len(stray.log.entries) == 0, "再生に使ったログが文書のログに化けている"

    proper = replay_through_json(document)

    assert math.isclose(query.volume(proper.active_body().shape), expected, rel_tol=1e-9)
    assert math.isclose(expected, math.pi * 10.0**2 * 10.0, rel_tol=1e-9)


def test_set_parameter_keeps_the_active_body():
    """`set_parameter` を1回打っても、作業対象(アクティブボディ)が移らない。"""
    document = Document()
    first_sketch = sketch_create(document, "XY")
    circle = add_circle(document, first_sketch, (0.0, 0.0), 10.0)
    first = extrude(
        document,
        [region_ref(first_sketch, regions(document, first_sketch)[0].index)],
        10.0,
        mode="new",
    ).args["body_id"]

    second_sketch = sketch_create(document, "XY")
    add_circle(document, second_sketch, (100.0, 0.0), 10.0)
    second = extrude(
        document,
        [region_ref(second_sketch, regions(document, second_sketch)[0].index)],
        10.0,
        mode="new",
    ).args["body_id"]
    assert document.active_body().id == second

    document.set_active_body(first)
    set_parameter(document, parameter_of(document, first_sketch, circle, "diameter"), 14.0)

    assert document.active_body().id == first, "作り直しで作業対象が黙って移っている"
    assert math.isclose(
        query.volume(document.get_body(first).shape),
        math.pi * 7.0**2 * 10.0,
        rel_tol=1e-9,
    ), "形が新しい値に追随していない"


def test_a_rebuild_that_fails_leaves_the_document_as_it_was():
    """作り直しが途中で失敗したら、bodies / sketches / parameters / アクティブを元へ戻す。
    落ちたら、ログは無傷なのに文書だけが途中まで作り直された不整合な状態が残る。

    旧形式の反復の個数を 3×2 から 1×2 へ減らすと、押し出しの対象にした6番目の領域が消えて
    再生中の `extrude` が落ちる(値の検査は通る)。スケッチを1つその押し出しより後ろに
    置くのは意図的 —— 全スケッチを作り直した後で落ちると、退避を戻さなくても値で一致する。
    """
    document = Document()
    sketch_id = sketch_create(document, "XY")
    circle = add_circle(document, sketch_id, (10.0, 10.0), 4.0)
    pattern_id = install_live_pattern(
        document, "pattern_rectangular", sketch_id, [circle], dx=10.0, dy=10.0, nx=3, ny=2
    )
    found = regions(document, sketch_id)
    assert len(found) == 6
    extrude(document, [region_ref(sketch_id, found[-1].index)], 5.0, mode="new")
    later = sketch_create(document, "XY")
    add_circle(document, later, (50.0, 50.0), 2.0)

    name = parameter_of(document, sketch_id, pattern_id, "nx")
    bodies_before = list(document.bodies)
    sketches_before = list(document.sketches)
    parameters_before = dict(document.parameters)
    active_before = document.active_body().id
    volume_before = query.volume(document.get_body(active_before).shape)
    entries_before = len(document.log.entries)

    with pytest.raises(ValueError):
        set_parameter(document, name, 1.0)

    assert document.bodies == bodies_before, "作り直しの失敗でボディが消えている"
    assert document.sketches == sketches_before, "作り直しの失敗でスケッチが消えている"
    assert document.parameters == parameters_before
    assert document.active_body().id == active_before
    assert math.isclose(
        query.volume(document.get_body(active_before).shape), volume_before, rel_tol=1e-9
    )
    assert len(document.log.entries) == entries_before
    assert len(regions(document, sketch_id)) == 6, "反復が減ったまま残っている"
