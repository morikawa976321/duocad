"""スケッチの契約(`sketch_create` / `add_line` / `add_circle` / `add_rectangle`)。"""
from __future__ import annotations

import dataclasses
import json

import pytest

# 立体の面にスケッチを貼るテストのために、テスト側から直接ソリッドを作る
# (境界検査は `duocad/` 配下だけを対象にしており、テストは対象外)。
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder

from duocad import kernel
from duocad.document import Body, Document
from duocad.sketch import (
    Entity,
    add_circle,
    add_line,
    add_rectangle,
    base_plane,
    entity_params,
    face_plane,
    get_sketch,
    sketch_create,
)


@pytest.fixture()
def box():
    """20mm立方体のソリッドを1つ持つ文書(6面とも平面)。"""
    document = Document()
    document.add_body(
        Body(
            id="box",
            name="box",
            kind="solid",
            shape=BRepPrimAPI_MakeBox(20.0, 20.0, 20.0).Shape(),
        )
    )
    return document


@pytest.fixture()
def cylinder():
    """円柱のソリッドを1つ持つ文書(側面が曲面なので「平面でない面」の実物になる)。"""
    document = Document()
    document.add_body(
        Body(
            id="cyl",
            name="cyl",
            kind="solid",
            shape=BRepPrimAPI_MakeCylinder(10.0, 20.0).Shape(),
        )
    )
    return document


def first_face_index(shape, kind: str) -> int:
    """種別が `kind` の面のうち最初の添字(M1の `kernel.tessellate` が返す分類を使う)。"""
    for face in kernel.tessellate(shape)["faces"]:
        if face["kind"] == kind:
            return face["index"]
    raise AssertionError(f"{kind} の面が無い(この形状ではテストが成立しない)")


def test_entity_kind_is_validated():
    """図形の種別は登録されたものだけを受け付ける。未知の種別は黙って通さない(§6予防6)。"""
    assert Entity(id="e1", kind="circle").params == {}
    assert Entity(id="e2", kind="arc").params == {}, "円弧は2026-08-11から有効な種別"
    # 必須フィールドの包含で見る(補助フィールドの追加まで禁じない)
    assert {"id", "kind", "params"} <= {f.name for f in dataclasses.fields(Entity)}
    with pytest.raises(ValueError):
        Entity(id="e3", kind="spline")


def test_sketch_create_on_a_base_plane():
    """基準平面にスケッチを作れ、文書が持つ。平面の参照はログへ入る形(保存形式)のまま。"""
    document = Document()

    sketch_id = sketch_create(document, "XY")

    assert len(document.sketches) == 1
    created = get_sketch(document, sketch_id)
    assert created.plane == {"kind": "base", "name": "XY", "provenance": None}
    assert created.entities == []


def test_sketch_create_rejects_an_unknown_base_plane():
    """知らない平面名は ValueError。文書にもログにも変更を残さない。"""
    document = Document()

    with pytest.raises(ValueError):
        sketch_create(document, "ZZ")

    assert document.sketches == []
    assert document.log.entries == []


def test_sketch_create_on_a_planar_face_of_an_existing_solid(box):
    """既存ソリッドの**平面状の面**にスケッチを貼れる(`sketch_create` の引数。MCP.md が正)。"""
    body = box.active_body()
    index = first_face_index(body.shape, "plane")

    sketch_id = sketch_create(box, face_plane(body.id, index))

    plane = get_sketch(box, sketch_id).plane
    assert plane["kind"] == "face"
    assert plane["body"] == body.id
    assert plane["index"] == index
    assert plane["provenance"] is None, "貼った面への参照に由来スロットが無い"


def test_sketch_create_rejects_a_non_planar_face(cylinder):
    """平面でない面(円筒面)を指定したら失敗する。**黙って通さない**(§6予防6)。"""
    body = cylinder.active_body()
    index = first_face_index(body.shape, "cylinder_full")

    with pytest.raises(ValueError):
        sketch_create(cylinder, face_plane(body.id, index))

    assert cylinder.sketches == []
    assert cylinder.log.entries == []


def test_line_circle_and_rectangle_are_kept_in_the_sketch():
    """線・円・長方形を追加でき、スケッチが追加順に図形を保持する。"""
    document = Document()
    sketch_id = sketch_create(document, "XY")

    line = add_line(document, sketch_id, (0.0, 0.0), (40.0, 0.0))
    circle = add_circle(document, sketch_id, (20.0, 15.0), 6.0)
    rectangle = add_rectangle(document, sketch_id, (0.0, 0.0), 40.0, 30.0)

    entities = get_sketch(document, sketch_id).entities
    assert [entity.kind for entity in entities] == ["line", "circle", "rectangle"]
    assert [entity.id for entity in entities] == [line, circle, rectangle]
    assert len({line, circle, rectangle}) == 3, "図形のIDが重複している"
    assert entity_params(document, sketch_id, line) == {
        "x0": 0.0, "y0": 0.0, "x1": 40.0, "y1": 0.0,
    }
    assert entity_params(document, sketch_id, circle) == {
        "cx": 20.0, "cy": 15.0, "diameter": 6.0,
    }
    assert entity_params(document, sketch_id, rectangle) == {
        "x": 0.0, "y": 0.0, "width": 40.0, "height": 30.0, "angle": 0.0,
    }


def test_adding_an_entity_to_an_unknown_sketch_fails():
    """存在しないスケッチへの図形追加は KeyError(黙って新しいスケッチを作らない)。"""
    document = Document()

    with pytest.raises(KeyError):
        add_circle(document, "sketch-does-not-exist", (0.0, 0.0), 10.0)

    assert document.log.entries == []


def test_sketch_and_entities_are_logged_and_undoable():
    """スケッチの作成・図形の追加が操作ログに1行ずつ載り、undoで1つずつ戻る。"""
    document = Document()
    sketch_id = sketch_create(document, "XY")
    circle_id = add_circle(document, sketch_id, (0.0, 0.0), 40.0)
    line_id = add_line(document, sketch_id, (-30.0, 5.0), (30.0, 5.0))
    rectangle_id = add_rectangle(document, sketch_id, (0.0, 0.0), 40.0, 30.0)

    entries = document.log.entries
    assert [operation.verb for operation in entries] == [
        "sketch_create", "add_circle", "add_line", "add_rectangle",
    ]
    assert entries[0].args == {"sketch_id": sketch_id, "plane": base_plane("XY"), "number": 1}
    assert entries[1].args == {
        "sketch_id": sketch_id,
        "entity_id": circle_id,
        "kind": "circle",
        "params": {"cx": 0.0, "cy": 0.0, "diameter": 40.0},
    }
    assert entries[2].args == {
        "sketch_id": sketch_id,
        "entity_id": line_id,
        "kind": "line",
        "params": {"x0": -30.0, "y0": 5.0, "x1": 30.0, "y1": 5.0},
    }
    assert entries[3].args == {
        "sketch_id": sketch_id,
        "entity_id": rectangle_id,
        "kind": "rectangle",
        "params": {"x": 0.0, "y": 0.0, "width": 40.0, "height": 30.0, "angle": 0.0},
    }
    json.loads(document.log.to_json())  # 保存形式はJSON(§3.6)

    for expected_verb in ("add_rectangle", "add_line", "add_circle"):
        assert document.log.undo(document).verb == expected_verb
    assert get_sketch(document, sketch_id).entities == []
    assert document.log.undo(document).verb == "sketch_create"
    assert document.sketches == []

    for _ in range(4):
        document.log.redo(document)
    assert [entity.id for entity in get_sketch(document, sketch_id).entities] == [
        circle_id, line_id, rectangle_id,
    ]
    assert len(document.log.entries) == 4, "redoが操作を二重に記録している"
