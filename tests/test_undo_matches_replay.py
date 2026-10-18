"""undo した直後の文書は、同じ操作ログを頭から再生した文書と同じ状態になる。

比べるのは active ボディ・名前つきの値の表(`d<n>`)・図形の `params`(`tests/document_support.document_state`)。
ログの再生が「正」で、生きている文書がそこからずれると、保存→開き直しで別の文書になる。
"""
from __future__ import annotations

import json

import pytest

from duocad import region
from duocad.document import Document
from duocad.oplog import OpLog, verb_handler
from duocad.sketch import add_circle, add_line, add_rectangle, get_sketch, sketch_create
from duocad.verbs import delete, delete_items, move_sketch_entities
from document_support import assert_matches_replay, document_state, replay_through_json


def _reload_with(document: Document, edit) -> Document:
    """ログを JSON へ往復させ、`edit(操作の dict の並び)` で書き換えてから再生した文書(旧い版のログを開いた状態)。"""
    payload = json.loads(document.log.to_json())
    edit(payload["operations"])
    reopened = Document()
    reopened.log = OpLog.from_payload(payload)
    reopened.log.replay(reopened)
    return reopened


def _entity_params(document: Document, sketch_id: str, entity_id: str) -> dict:
    sketch = get_sketch(document, sketch_id)
    return next(dict(entity.params) for entity in sketch.entities if entity.id == entity_id)


# --------------------------------------------------------------------------------------
# delete / delete_items の undo は、名前つきの値(d<n>)を元の名前のまま戻す
# --------------------------------------------------------------------------------------
def test_undoing_a_delete_after_another_delete_restores_the_original_names():
    """落ちたら: delete の undo が「最小の空き番号」で名前を振り直し、先に別の図形を消して空けた低い番号を取る。"""
    doc = Document()
    sketch_id = sketch_create(doc, "XY")
    first = add_circle(doc, sketch_id, (0.0, 0.0), 4.0)
    second = add_circle(doc, sketch_id, (10.0, 0.0), 4.0)
    third = add_circle(doc, sketch_id, (20.0, 0.0), 4.0)
    third_names = _entity_params(doc, sketch_id, third)

    delete(doc, entities=[{"sketch": sketch_id, "entity": second}])
    delete(doc, entities=[{"sketch": sketch_id, "entity": third}])
    doc.log.undo(doc)

    assert _entity_params(doc, sketch_id, third) == third_names
    assert_matches_replay(doc)
    doc.log.undo(doc)
    assert_matches_replay(doc)
    assert {first, second, third} == {entity.id for entity in get_sketch(doc, sketch_id).entities}


def test_undoing_a_delete_does_not_block_the_undo_of_an_earlier_sketch_deletion():
    """落ちたら: 先に消したスケッチが空けた番号を後の delete の undo が取り、次の undo(スケッチの復元)が
    「名前が使用中」の ValueError になって履歴が詰まる。"""
    doc = Document()
    first_sketch = sketch_create(doc, "XY")
    add_circle(doc, first_sketch, (0.0, 0.0), 4.0)
    second_sketch = sketch_create(doc, "XZ")
    circle = add_circle(doc, second_sketch, (50.0, 0.0), 4.0)

    delete_items(doc, sketches=[first_sketch])
    delete(doc, entities=[{"sketch": second_sketch, "entity": circle}])
    doc.log.undo(doc)
    assert_matches_replay(doc)
    doc.log.undo(doc)

    assert [sketch.id for sketch in doc.sketches] == [first_sketch, second_sketch]
    assert_matches_replay(doc)


def test_undoing_a_delete_across_sketches_restores_each_entitys_names():
    """落ちたら: 別スケッチの図形をまとめて消した undo が、スケッチごとの添字順で振り直して名前が入れ替わる
    (作成の早い図形が後の番号を取る)。"""
    doc = Document()
    early_sketch = sketch_create(doc, "XY")
    early = add_circle(doc, early_sketch, (0.0, 0.0), 4.0)
    late_sketch = sketch_create(doc, "XZ")
    late = add_circle(doc, late_sketch, (0.0, 0.0), 4.0)

    delete(
        doc,
        entities=[
            {"sketch": late_sketch, "entity": late},
            {"sketch": early_sketch, "entity": early},
        ],
    )
    doc.log.undo(doc)

    assert _entity_params(doc, early_sketch, early) == {"cx": "d1", "cy": "d2", "diameter": "d3"}
    assert_matches_replay(doc)


def test_a_delete_logged_before_names_were_recorded_still_undoes():
    """名前を控えない旧いログ(`names` が無い delete)の undo は、これまでどおり登録し直して通る。"""
    doc = Document()
    sketch_id = sketch_create(doc, "XY")
    circle = add_circle(doc, sketch_id, (3.0, 4.0), 4.0)
    delete(doc, entities=[{"sketch": sketch_id, "entity": circle}])

    def strip_names(operations: list) -> None:
        for operation in operations:
            if operation["verb"] == "delete":
                for spec in operation["args"]["entities"]:
                    del spec["names"]

    legacy = _reload_with(doc, strip_names)
    legacy.log.undo(legacy)

    assert _entity_params(legacy, sketch_id, circle) == {"cx": "d1", "cy": "d2", "diameter": "d3"}
    assert legacy.parameters == {"d1": 3.0, "d2": 4.0, "d3": 4.0}


def test_a_delete_undo_that_cannot_finish_restores_nothing():
    """落ちたら: 戻せない名前があるとき、手前のスケッチだけ戻った文書が残り、以後の undo が
    「sketch id already exists」で止まる。"""
    doc = Document()
    first_sketch = sketch_create(doc, "XY")
    add_circle(doc, first_sketch, (0.0, 0.0), 4.0)  # d1-d3
    second_sketch = sketch_create(doc, "XZ")
    add_circle(doc, second_sketch, (0.0, 0.0), 4.0)  # d4-d6
    deletion = delete_items(doc, sketches=[first_sketch, second_sketch])
    third_sketch = sketch_create(doc, "YZ")
    add_circle(doc, third_sketch, (0.0, 0.0), 4.0)  # d1-d3
    kept = add_circle(doc, third_sketch, (9.0, 0.0), 4.0)  # d4-d6
    first_of_third = get_sketch(doc, third_sketch).entities[0].id
    delete(doc, entities=[{"sketch": third_sketch, "entity": first_of_third}])  # d1-d3 だけ空く
    assert {"d1", "d2", "d3"}.isdisjoint(doc.parameters) and {"d4", "d5", "d6"} <= set(doc.parameters)

    before = document_state(doc)
    # LIFO を外して delete_items の undo だけを呼ぶ(名前が衝突する状態を作る足場)。
    with pytest.raises(ValueError, match="still in use"):
        verb_handler("delete_items").undo(doc, deletion)

    assert document_state(doc) == before
    assert kept in {entity.id for entity in get_sketch(doc, third_sketch).entities}


# --------------------------------------------------------------------------------------
# move_sketch_entities の undo は、動かす前の値をそのまま書き戻す(逆向きに動かさない)
# --------------------------------------------------------------------------------------
def _whole(sketch_id: str, entity_id: str) -> list:
    return [{"sketch": sketch_id, "entity": entity_id, "point": None}]


def test_undoing_a_translation_restores_the_coordinates_bit_for_bit():
    """落ちたら: undo が逆向きに同じ量を動かして座標が1ulpずれ、同面積の領域の並び(添字)が再生した文書と食い違う。"""
    x, dx = 12.734, 5.192
    assert (x + dx) - dx != x, "逆向きに動かす実装なら1ulpずれる題材であること"
    doc = Document()
    sketch_id = sketch_create(doc, "XY")
    rectangle = add_rectangle(doc, sketch_id, (x, 20.695), 9.859, 13.906)

    move_sketch_entities(doc, _whole(sketch_id, rectangle), dx=dx, dy=0.0)
    doc.log.undo(doc)

    assert doc.parameters[_entity_params(doc, sketch_id, rectangle)["x"]] == x
    assert_matches_replay(doc)


def test_undoing_a_rotation_restores_the_coordinates_bit_for_bit():
    doc = Document()
    sketch_id = sketch_create(doc, "XY")
    line = add_line(doc, sketch_id, (0.1, 0.2), (7.3, 5.9))
    before = document_state(doc)

    move_sketch_entities(doc, _whole(sketch_id, line), mode="rotate", angle=37.3, center=[1.3, 2.7])
    assert document_state(doc) != before
    doc.log.undo(doc)

    assert document_state(doc) == before


def test_undoing_a_move_of_one_point_keeps_the_other_values_of_the_entity():
    doc = Document()
    sketch_id = sketch_create(doc, "XY")
    line = add_line(doc, sketch_id, (0.1, 0.2), (7.3, 5.9))
    before = document_state(doc)

    move_sketch_entities(doc, [{"sketch": sketch_id, "entity": line, "point": "x1"}], dx=0.3, dy=0.7)
    doc.log.undo(doc)

    assert document_state(doc) == before


def _legacy_rectangle() -> tuple:
    """`angle` を持たない時代のログを開いた状態の長方形(params は4個)。"""
    doc = Document()
    sketch_id = sketch_create(doc, "XY")
    rectangle = add_rectangle(doc, sketch_id, (0.0, 0.0), 10.0, 4.0)

    def drop_angle(operations: list) -> None:
        for operation in operations:
            if operation["verb"] == "add_rectangle":
                del operation["args"]["params"]["angle"]

    legacy = _reload_with(doc, drop_angle)
    assert set(_entity_params(legacy, sketch_id, rectangle)) == {"x", "y", "width", "height"}
    return legacy, sketch_id, rectangle


def test_undoing_the_rotation_of_a_rectangle_without_angle_removes_the_angle_it_added():
    """落ちたら: 回転が足した angle キーと名前つきの値(d5)が undo 後も残り、以後の図形の名前が再生より1つずれる。"""
    legacy, sketch_id, rectangle = _legacy_rectangle()

    move_sketch_entities(legacy, _whole(sketch_id, rectangle), mode="rotate", angle=30.0, center=[0.0, 0.0])
    assert "angle" in _entity_params(legacy, sketch_id, rectangle)
    legacy.log.undo(legacy)

    assert "angle" not in _entity_params(legacy, sketch_id, rectangle)
    assert set(legacy.parameters) == {"d1", "d2", "d3", "d4"}
    assert_matches_replay(legacy)
    add_circle(legacy, sketch_id, (0.0, 0.0), 3.0)
    assert_matches_replay(legacy)


def test_redoing_the_rotation_of_a_rectangle_without_angle_matches_the_replay():
    legacy, sketch_id, rectangle = _legacy_rectangle()
    move_sketch_entities(legacy, _whole(sketch_id, rectangle), mode="rotate", angle=30.0, center=[0.0, 0.0])
    legacy.log.undo(legacy)

    legacy.log.redo(legacy)

    assert "angle" in _entity_params(legacy, sketch_id, rectangle)
    assert_matches_replay(legacy)


def test_a_move_logged_without_previous_values_still_undoes_by_moving_back():
    """旧いログ(`previous` が無い move)の undo は、これまでどおり逆向きに動かして通る。"""
    doc = Document()
    sketch_id = sketch_create(doc, "XY")
    line = add_line(doc, sketch_id, (0.0, 0.0), (10.0, 0.0))
    move_sketch_entities(doc, _whole(sketch_id, line), dx=3.0, dy=2.0)

    def drop_previous(operations: list) -> None:
        for operation in operations:
            if operation["verb"] == "move_sketch_entities":
                del operation["args"]["previous"]

    legacy = _reload_with(doc, drop_previous)
    legacy.log.undo(legacy)

    restored = {key: legacy.parameters[ref] for key, ref in _entity_params(legacy, sketch_id, line).items()}
    assert restored == pytest.approx({"x0": 0.0, "y0": 0.0, "x1": 10.0, "y1": 0.0}, abs=1e-12)
