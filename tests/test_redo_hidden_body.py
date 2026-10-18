"""redo(やり直し)は、undo の後に隠したボディにも掛かる。可視性の門は、新しく掛ける操作の入口だけに残る。"""
from __future__ import annotations

import pytest

from duocad import query
from duocad.document import Document
from duocad.region import RegionRef, region_ref, regions
from duocad.sketch import add_rectangle, sketch_create
from duocad.verbs import (
    boolean_body,
    chamfer,
    delete,
    extrude,
    extrude_faces,
    fillet,
    loft,
    revolve,
    shell_body,
)
from document_support import new_box, second_body


def _top_face(document: Document, body_id: str) -> int:
    faces = query.describe_faces(document.get_body(body_id).shape)
    return next(face["index"] for face in faces if face["normal"][2] > 0.99)


def _section(document: Document, corner, plane="XY") -> RegionRef:
    sketch_id = sketch_create(document, plane)
    add_rectangle(document, sketch_id, corner, 10.0, 10.0)
    return region_ref(sketch_id, regions(document, sketch_id)[0].index)


def _fillet_an_edge(document: Document, base: str) -> None:
    fillet(document, [1], 2.0, body_id=base)


def _chamfer_an_edge(document: Document, base: str) -> None:
    chamfer(document, [1], 2.0, body_id=base)


def _shell_the_body(document: Document, base: str) -> None:
    shell_body(document, [_top_face(document, base)], 2.0, body_id=base)


def _delete_the_body(document: Document, base: str) -> None:
    delete(document, bodies=[base])


def _cut_a_hole(document: Document, base: str) -> None:
    extrude(document, [_section(document, (5.0, 5.0))], 10.0, mode="cut", body_id=base)


def _extrude_a_face(document: Document, base: str) -> None:
    extrude_faces(document, [{"body": base, "index": _top_face(document, base)}], 5.0, mode="join", body_id=base)


def _revolve_a_cut(document: Document, base: str) -> None:
    revolve(document, [_section(document, (5.0, 3.0))], "x", mode="cut", body_id=base)


def _loft_a_cut(document: Document, base: str) -> None:
    offset = {"kind": "offset", "base": {"kind": "base", "name": "XY"}, "distance": 10.0}
    loft(document, [_section(document, (5.0, 5.0)), _section(document, (5.0, 5.0), offset)], mode="cut", body_id=base)


def _union_a_body(document: Document, base: str) -> None:
    tool = second_body(document)
    boolean_body(document, {"body": base, "kind": "face", "index": 1}, [tool], mode="union")


HIDDEN_BODY_OPERATIONS = [
    _fillet_an_edge,
    _chamfer_an_edge,
    _shell_the_body,
    _delete_the_body,
    _cut_a_hole,
    _extrude_a_face,
    _revolve_a_cut,
    _loft_a_cut,
    _union_a_body,
]


#: `HIDDEN_BODY_OPERATIONS` が断られたあとに、ログに残っていてはいけない verb(題材のスケッチや補助のボディを作る verb は残ってよい)。
REFUSED_VERBS = {"fillet", "chamfer", "shell_body", "delete", "boolean_body"}


def _volume_of(document: Document, body_id: str):
    body = next((body for body in document.bodies if body.id == body_id), None)
    return None if body is None else query.volume(body.shape)


@pytest.mark.parametrize("operation", HIDDEN_BODY_OPERATIONS, ids=lambda function: function.__name__)
def test_redo_applies_to_a_body_hidden_after_the_undo(operation):
    """落ちたら: 可視性(ログに載らず undo でも戻らない状態)が redo の成否を決め、undo の後に隠すと
    「見えないものは指せない」で redo が止まる。"""
    document = Document()
    base = new_box(document, 20.0)
    operation(document, base)
    applied = _volume_of(document, base)
    document.log.undo(document)
    document.get_body(base).visible = False

    document.log.redo(document)

    assert _volume_of(document, base) == applied
    assert not document.log.can_redo


@pytest.mark.parametrize("operation", HIDDEN_BODY_OPERATIONS, ids=lambda function: function.__name__)
def test_a_new_operation_on_a_hidden_body_is_still_refused(operation):
    """可視性の門は、新しく掛ける操作の入口に残る(選択した後・verb を呼ぶ前に隠された競合)。"""
    document = Document()
    base = new_box(document, 20.0)
    document.get_body(base).visible = False
    shape = document.get_body(base).shape

    with pytest.raises(ValueError, match="hidden"):
        operation(document, base)

    assert document.get_body(base).shape is shape
    assert not any(entry.verb in REFUSED_VERBS for entry in document.log.entries[1:])


def test_redo_still_fails_for_a_body_that_no_longer_exists():
    """redo の可視性の門を外しても、対象が無いこと・編集できないことは失敗のまま。"""
    document = Document()
    base = new_box(document, 20.0)
    fillet(document, [1], 2.0, body_id=base)
    document.log.undo(document)
    document.remove_body(base)

    with pytest.raises(KeyError):
        document.log.redo(document)
