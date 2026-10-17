"""名前つきの寸法値(`duocad.parameters`)の契約。"""
from __future__ import annotations

import json
import math

import pytest

from duocad import query
from duocad.document import Document
from document_support import replay_through_json
from duocad.parameters import parameter_of, set_parameter
from duocad.region import region_ref, regions
from duocad.sketch import (
    add_circle,
    add_line,
    add_rectangle,
    entity_params,
    get_sketch,
    sketch_create,
)
from duocad.verbs import extrude

CIRCLE_RADIUS = 20.0
SPLIT_LINE_Y = 5.0

#: 体積比較の相対許容差(M1・M2と同じ。ブーリアンの積分順序で1ULP級の差が出るので `==` は使わない)
VOLUME_REL_TOL = 1.0e-9


def split_circle(document: Document) -> str:
    """半径20の円を y=5 の線分で割ったスケッチ(領域2個)。M2のテストと同じ形。"""
    sketch_id = sketch_create(document, "XY")
    add_circle(document, sketch_id, (0.0, 0.0), 2 * CIRCLE_RADIUS)
    add_line(document, sketch_id, (-30.0, SPLIT_LINE_Y), (30.0, SPLIT_LINE_Y))
    return sketch_id


def test_numbers_become_named_values_and_entities_hold_references():
    """図形を数値で作ると、その数値が**名前つきの値として文書に登録**され、
    図形側は値そのものではなく**参照**を持つ(座標に焼き込まれていない)。
    """
    document = Document()
    sketch_id = sketch_create(document, "XY")
    circle = add_circle(document, sketch_id, (0.0, 0.0), 2 * CIRCLE_RADIUS)

    entity = get_sketch(document, sketch_id).entities[0]
    assert all(
        isinstance(value, str) for value in entity.params.values()
    ), f"数値が図形に焼き込まれている: {entity.params!r}"

    # 図形が持つ参照そのものがパラメータ名である(別表で対応づける実装を許さない)
    for key in ("cx", "cy", "diameter"):
        assert entity.params[key] == parameter_of(document, sketch_id, circle, key)

    # 参照をたどると文書のパラメータに行き着き、値は打った数値
    radius_name = parameter_of(document, sketch_id, circle, "diameter")
    assert document.parameters[radius_name] == 2 * CIRCLE_RADIUS
    assert set(document.parameters) == {
        parameter_of(document, sketch_id, circle, key) for key in ("cx", "cy", "diameter")
    }

    # M2の契約: 解決済みの数値を返すアクセサは変わらない
    assert entity_params(document, sketch_id, circle) == {
        "cx": 0.0, "cy": 0.0, "diameter": 2 * CIRCLE_RADIUS,
    }


def test_every_entity_kind_holds_references_not_numbers():
    """線・長方形でも数値が参照になる(円は上のテスト)。落ちたら、参照化が種別ごとに漏れている。"""
    document = Document()
    sketch_id = sketch_create(document, "XY")
    add_line(document, sketch_id, (0.0, 0.0), (40.0, 0.0))
    add_rectangle(document, sketch_id, (1.0, 2.0), 40.0, 30.0)

    for entity in get_sketch(document, sketch_id).entities:
        assert all(isinstance(value, str) for value in entity.params.values()), entity.params


def test_parameter_names_are_released_by_undo_and_reused():
    """undoで消えた図形のパラメータは文書から消え、次の登録で同じ名前が再利用される。"""
    document = Document()
    sketch_id = sketch_create(document, "XY")
    circle = add_circle(document, sketch_id, (1.0, 2.0), 6.0)
    assert set(document.parameters) == {"d1", "d2", "d3"}

    document.log.undo(document)
    assert document.parameters == {}

    circle = add_circle(document, sketch_id, (4.0, 5.0), 12.0)
    assert document.parameters == {"d1": 4.0, "d2": 5.0, "d3": 12.0}

    name = parameter_of(document, sketch_id, circle, "diameter")
    set_parameter(document, name, 7.0)

    replayed = replay_through_json(document)
    assert replayed.parameters == document.parameters
    assert replayed.parameters[name] == 7.0


def test_parameter_names_are_d1_d2_d3_in_creation_order():
    """パラメータ名は `d1` `d2` … で、図形を作った順・`params` の並び順に振られる。"""
    document = Document()
    sketch_id = sketch_create(document, "XY")
    circle = add_circle(document, sketch_id, (1.0, 2.0), 6.0)

    assert [parameter_of(document, sketch_id, circle, key) for key in ("cx", "cy", "diameter")] == [
        "d1", "d2", "d3",
    ]
    assert document.parameters == {"d1": 1.0, "d2": 2.0, "d3": 6.0}


def test_parameter_of_rejects_unknown_keys_and_owners():
    """知らない図形・知らないキーは KeyError(黙って None を返さない)。"""
    document = Document()
    sketch_id = sketch_create(document, "XY")
    circle = add_circle(document, sketch_id, (0.0, 0.0), 10.0)

    with pytest.raises(KeyError):
        parameter_of(document, sketch_id, circle, "height")
    with pytest.raises(KeyError):
        parameter_of(document, sketch_id, "entity-does-not-exist", "diameter")


def test_set_parameter_is_logged_and_undoable():
    """`set_parameter` が操作ログに載り、**undoで元の形状に戻る**。"""
    document = Document()
    sketch_id = sketch_create(document, "XY")
    circle = add_circle(document, sketch_id, (0.0, 0.0), 2 * CIRCLE_RADIUS)
    extrude(document, [region_ref(sketch_id, regions(document, sketch_id)[0].index)], 10.0)
    before = query.volume(document.active_body().shape)
    name = parameter_of(document, sketch_id, circle, "diameter")

    operation = set_parameter(document, name, 20.0)

    assert operation.verb == "set_parameter"
    assert operation.args == {"name": name, "value": 20.0, "previous": 2 * CIRCLE_RADIUS}
    json.loads(document.log.to_json())
    assert not math.isclose(query.volume(document.active_body().shape), before, rel_tol=VOLUME_REL_TOL)

    undone = document.log.undo(document)

    assert undone is not None and undone.verb == "set_parameter"
    assert document.parameters[name] == 2 * CIRCLE_RADIUS
    assert math.isclose(query.volume(document.active_body().shape), before, rel_tol=1e-9)

    document.log.redo(document)
    assert document.parameters[name] == 20.0
    assert math.isclose(
        query.volume(document.active_body().shape), math.pi * 10.0**2 * 10.0, rel_tol=VOLUME_REL_TOL
    )


def test_set_parameter_rejects_an_unknown_name():
    """存在しないパラメータ名は KeyError。**文書が変更されない**。"""
    document = Document()
    sketch_id = sketch_create(document, "XY")
    add_circle(document, sketch_id, (0.0, 0.0), 2 * CIRCLE_RADIUS)
    parameters_before = dict(document.parameters)
    entries_before = len(document.log.entries)

    with pytest.raises(KeyError):
        set_parameter(document, "d999", 1.0)

    assert document.parameters == parameters_before
    assert len(document.log.entries) == entries_before


def test_parameters_survive_save_load_and_replay():
    """保存 → 読み込み → 再生でパラメータも復元される。"""
    document = Document()
    sketch_id = split_circle(document)
    circle = document.sketches[0].entities[0].id
    name = parameter_of(document, sketch_id, circle, "diameter")
    set_parameter(document, name, 30.0)
    expected_regions = regions(document, sketch_id)

    replayed = replay_through_json(document)

    assert replayed.parameters == document.parameters
    assert replayed.parameters[name] == 30.0
    assert regions(replayed, sketch_id) == expected_regions
