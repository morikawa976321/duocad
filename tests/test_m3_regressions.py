"""M3の回帰テスト — **契約のテストが通っていても壊れていた経路**。

`tests/test_parameters.py` / `test_pattern.py` がM3のAPI契約で、このファイルは契約ではなく
**再発防止**である。M3のロジック実装は、契約のテストが131件全部緑のまま次の4つの経路が壊れる。
すべて `set_parameter` が呼ぶ `parameters.rebuild`(ログからの作り直し)の周りにある —
**「全部消して作り直す」は、消してはいけないものと、失敗したときの後始末を持っていないと
静かに文書を壊す**、というのがここでの教訓。

1. アクティブボディの選択が作り直しで捨てられ、`set_parameter` を1回打つだけで
   **以後の書き出し先と編集先が別のボディへ黙って移った**（選択はログに載らない）。
2. 作り直しが途中で失敗すると**空の文書が残った**（ログとカーソルは無傷なので、
   「履歴は5行あるがボディもスケッチも0」という不整合になる）。参照STEPを移動しただけで起きる。
3. `set_parameter` が値を検査せず、`add_circle` がその場で弾く値（NaN・半径0以下）を
   後から書き込めた。NaNは**同じスケッチの正常な領域まで黙って消し**、
   `to_json()` が裸のNaNを吐いてJSONとして壊れる。
4. 外のログを `replay(document)` すると、**例外も出さずに空の文書**ができた
   （作り直しが `document.log` を見ていたため、中身の違うログを再生していた）。
"""
from __future__ import annotations

import json
import math

import pytest

from duocad import query
from duocad.document import Document
from duocad.oplog import OpLog
from duocad.parameters import parameter_of, set_parameter
from duocad.region import region_ref, regions
from duocad.sketch import add_circle, sketch_create
from duocad.verbs import extrude, pattern_rectangular

@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("radius", float("nan")),
        ("radius", float("inf")),
        ("radius", 0.0),
        ("radius", -5.0),
    ],
    ids=["nan", "inf", "zero", "negative"],
)
def test_set_parameter_rejects_values_that_add_circle_would_reject(key, value):
    """`add_circle` が作成時に弾く値は、`set_parameter` からも書き込めない。

    作成時だけ検査していると、同じ壊れ方を後から作れてしまう。とくにNaNは
    例外も出さずに**同じスケッチの正常な領域まで消し**、`to_json()` を壊す。
    """
    document = Document()
    sketch_id = sketch_create(document, "XY")
    circle = add_circle(document, sketch_id, (10.0, 10.0), 2.0)
    name = parameter_of(document, sketch_id, circle, key)
    entries_before = len(document.log.entries)

    with pytest.raises(ValueError):
        set_parameter(document, name, value)

    assert document.parameters[name] == 2.0
    assert len(document.log.entries) == entries_before
    assert len(regions(document, sketch_id)) == 1
    json.loads(document.log.to_json())  # 裸のNaN/Infが混ざっていたら落ちる


@pytest.mark.parametrize("count", [2.5, 0, -3], ids=["fractional", "zero", "negative"])
def test_set_parameter_rejects_pattern_counts_that_are_not_positive_integers(count):
    """反復の個数も、`pattern_rectangular` と同じ規則で後から書き換えられない。

    「個数も間隔も後から変えられる」ことが反復をパラメータ化した理由なので、
    **後から書く経路こそが主経路**。ここが素通りすると、`int()` の切り捨てで
    黙って 2×2 になったり、反復が丸ごと消えた（図形が1つも描かれない）状態になる。
    """
    document = Document()
    sketch_id = sketch_create(document, "XY")
    circle = add_circle(document, sketch_id, (10.0, 10.0), 2.0)
    pattern_id = pattern_rectangular(document, sketch_id, [circle], 10.0, 10.0, 3, 2)
    name = parameter_of(document, sketch_id, pattern_id, "nx")

    with pytest.raises(ValueError):
        set_parameter(document, name, count)

    assert document.parameters[name] == 3.0
    assert len(regions(document, sketch_id)) == 6


def test_replaying_a_foreign_log_fails_loudly_instead_of_emptying_the_document():
    """`set_parameter` を含むログを**別の文書へ直接** `replay` したら、黙って空にせず止まる。

    作り直しは「どのログを再生するか」を知る必要があるが、`OpLog.replay(document)` は
    `document.log` を差し替えない（M0の契約）。中身の違うログを見て再生すると、
    例外も出さずに空の文書ができていた。契約どおり `doc.log` へ束ねてから再生すれば通る。
    """
    document = Document()
    sketch_id = sketch_create(document, "XY")
    circle = add_circle(document, sketch_id, (0.0, 0.0), 20.0)
    extrude(document, [region_ref(sketch_id, regions(document, sketch_id)[0].index)], 10.0)
    set_parameter(document, parameter_of(document, sketch_id, circle, "radius"), 10.0)
    expected = query.volume(document.active_body().shape)

    stray = Document()
    with pytest.raises(ValueError):
        OpLog.from_json(document.log.to_json()).replay(stray)
    # `replay` は元々トランザクショナルではない(M0)ので、途中まで適用された状態で止まる。
    # 肝心なのは**黙って別物の文書を作らない**ことで、そのために例外で止める。
    assert len(stray.log.entries) == 0, "再生に使ったログが文書のログに化けている"

    proper = Document()
    proper.log = OpLog.from_json(document.log.to_json())
    proper.log.replay(proper)

    assert math.isclose(query.volume(proper.active_body().shape), expected, rel_tol=1e-9)
    assert math.isclose(expected, math.pi * 10.0**2 * 10.0, rel_tol=1e-9)


def test_set_parameter_keeps_the_active_body():
    """`set_parameter` を1回打っても、作業対象(アクティブボディ)が移らない。

    **アクティブの選択はログに載らない**ので、作り直し(全部消して再生)がそのまま
    捨ててしまう。捨てると再生の最後に `add_body` されたボディへ勝手に移り、
    **以後の書き出し先と編集先が黙って別のボディへ変わる**(値を1つ変えただけで
    作業対象が変わる、という気づきようのない壊れ方)。

    2つ目のボディを作った**あとで**1つ目へ戻すのが要点 —— 再生順の最後は常に
    2つ目なので、復元していなければここで必ず2つ目に化ける。
    """
    document = Document()
    first_sketch = sketch_create(document, "XY")
    circle = add_circle(document, first_sketch, (0.0, 0.0), 5.0)
    first = extrude(
        document,
        [region_ref(first_sketch, regions(document, first_sketch)[0].index)],
        10.0,
        mode="new",
    ).args["body_id"]

    second_sketch = sketch_create(document, "XY")
    add_circle(document, second_sketch, (100.0, 0.0), 5.0)
    second = extrude(
        document,
        [region_ref(second_sketch, regions(document, second_sketch)[0].index)],
        10.0,
        mode="new",
    ).args["body_id"]
    assert document.active_body().id == second

    document.set_active_body(first)
    set_parameter(document, parameter_of(document, first_sketch, circle, "radius"), 7.0)

    assert document.active_body().id == first, "作り直しで作業対象が黙って移っている"
    assert math.isclose(
        query.volume(document.get_body(first).shape),
        math.pi * 7.0**2 * 10.0,
        rel_tol=1e-9,
    ), "形が新しい値に追随していない"


def test_a_rebuild_that_fails_leaves_the_document_as_it_was():
    """作り直しが途中で失敗したら、bodies / sketches / parameters / アクティブを元へ戻す。

    退避が無いと「ログは4行あるがボディもスケッチも0」という**不整合な文書**が残る
    (ログとカーソルは無傷なので、壊れたことにすら気づけない)。

    ここでは反復の個数を 3×2 から 1×2 へ減らして失敗させる —— 押し出しの対象にした
    6番目の領域が消えるので、再生中の `extrude` が ValueError で落ちる。値の検査
    (`_require_usable_value`)は通る値なので、**検査ではなく作り直しの途中**で落ちる
    という、この後始末が要る唯一の状況を作れる。
    """
    document = Document()
    sketch_id = sketch_create(document, "XY")
    circle = add_circle(document, sketch_id, (10.0, 10.0), 2.0)
    pattern_id = pattern_rectangular(document, sketch_id, [circle], 10.0, 10.0, 3, 2)
    found = regions(document, sketch_id)
    assert len(found) == 6
    extrude(document, [region_ref(sketch_id, found[-1].index)], 5.0, mode="new")

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
