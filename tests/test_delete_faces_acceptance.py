"""面の削除 `delete_faces`(#190)の受入条件(docs/FACE_EDIT.md「受入条件」)。

**verb `delete_faces` が登録されるまでは xfail(strict)**。登録されると印は自動で外れ、本物の検査になる
(実装の途中で落ちる検査が黙って xfail に紛れない)。落ちたら、受入条件のどれが満たされていないかを意味する。

期待値の出どころ(tools/face_edit_probe で 40×30×20 の箱を実測、2026-10-18): フィレット・面取り・穴の面を
消すと元の箱(24000 mm³・平面6枚)に戻る。箱の上面は周りが平行で閉じない。
"""
from __future__ import annotations

import json

import pytest
from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

import duocad.verbs as verbs
from duocad import catalog, kernel, oplog, query, selection
from duocad.document import Document
from duocad.entry import entry_for
from duocad.verbs import chamfer, fillet
from duocad.verbs.import_editable import import_step
from document_support import kinds_of, new_box, new_box_with_hole, replay_through_json, second_body

pytestmark = pytest.mark.xfail(
    "delete_faces" not in oplog.registered_verbs(),
    reason="受入条件(#190)。verb delete_faces の実装前",
    strict=True,
)

W, D, H = 40.0, 30.0, 20.0
BOX_VOLUME = W * D * H


def _delete_faces(doc, faces, body_id):
    return verbs.delete_faces(doc, faces, body_id=body_id)


def _box(doc: Document) -> str:
    return new_box(doc, W, D, H)


def _top_front_edge(doc: Document, body_id: str) -> int:
    """上面と前面(y=0)の境の辺。添字は決め打ちにしない。"""
    found = [
        edge["index"]
        for edge in query.describe_edges(doc.get_body(body_id).shape)
        if edge["kind"] == "line"
        and all(abs(p[1]) < 1e-6 and abs(p[2] - H) < 1e-6 for p in (edge["start"], edge["end"]))
    ]
    assert len(found) == 1, found
    return found[0]


def _faces_of_kind(doc: Document, body_id: str, kind: str) -> list:
    return [face["index"] for face in query.describe_faces(doc.get_body(body_id).shape) if face["kind"] == kind]


def _slanted_planes(doc: Document, body_id: str) -> list:
    return [
        face["index"]
        for face in query.describe_faces(doc.get_body(body_id).shape)
        if face["kind"] == "plane" and max(abs(n) for n in face["normal"]) < 1 - 1e-6
    ]


def _plane_at(doc: Document, body_id: str, axis: int, value: float) -> int:
    found = [
        face["index"]
        for face in query.describe_faces(doc.get_body(body_id).shape)
        if face["kind"] == "plane" and abs(face["center"][axis] - value) < 1e-6 and abs(abs(face["normal"][axis]) - 1) < 1e-9
    ]
    assert len(found) == 1, found
    return found[0]


def _filleted_box(doc: Document) -> tuple[str, int]:
    body_id = _box(doc)
    fillet(doc, [_top_front_edge(doc, body_id)], 5.0, body_id=body_id)
    cylinders = _faces_of_kind(doc, body_id, "cylinder_partial")
    assert len(cylinders) == 1
    return body_id, cylinders[0]


def _state(doc: Document) -> tuple:
    return (
        json.dumps(doc.log.to_json(), sort_keys=True),
        tuple((body.id, body.shape, query.volume(body.shape), kernel.face_count(body.shape)) for body in doc.bodies),
    )


def _assert_box(doc: Document, body_id: str) -> None:
    shape = doc.get_body(body_id).shape
    assert query.volume(shape) == pytest.approx(BOX_VOLUME, rel=1e-9)
    assert kinds_of(doc, body_id) == {"plane": 6}


# ---------------------------------------------------------------- Issue の受入条件(案)
def test_deleting_the_fillet_face_restores_the_box():
    """直方体の1辺にフィレットをかけた後、そのフィレット面を削除すると、元の直方体(体積・面数とも)に戻る。"""
    doc = Document()
    body_id, cylinder = _filleted_box(doc)

    _delete_faces(doc, [cylinder], body_id)

    _assert_box(doc, body_id)


def test_deleting_the_chamfer_face_restores_the_corner():
    """面取りの面を削除すると、元の角に戻る。"""
    doc = Document()
    body_id = _box(doc)
    chamfer(doc, [_top_front_edge(doc, body_id)], 5.0, body_id=body_id)
    slanted = _slanted_planes(doc, body_id)
    assert len(slanted) == 1

    _delete_faces(doc, slanted, body_id)

    _assert_box(doc, body_id)


def test_deleting_the_inner_face_of_a_through_hole_fills_it():
    """貫通穴の内面を削除すると、穴が埋まる。"""
    doc = Document()
    body_id = new_box_with_hole(doc, 20.0, 4.0)
    hole = _faces_of_kind(doc, body_id, "cylinder_full")
    assert len(hole) == 1

    _delete_faces(doc, hole, body_id)

    assert query.volume(doc.get_body(body_id).shape) == pytest.approx(8000.0, rel=1e-9)
    assert kinds_of(doc, body_id) == {"plane": 6}


def test_a_fillet_of_a_step_body_can_be_deleted(tmp_path):
    """STEP で取り込んだボディ(構築の履歴を持たない)のフィレットを削除できる。"""
    box = BRepPrimAPI_MakeBox(W, D, H).Shape()
    maker = BRepFilletAPI_MakeFillet(box)
    top_front = [
        edge["index"]
        for edge in query.describe_edges(box)
        if all(abs(p[1]) < 1e-6 and abs(p[2] - H) < 1e-6 for p in (edge["start"], edge["end"]))
    ]
    maker.Add(5.0, kernel.edge_of(box, top_front[0]))
    maker.Build()
    step_path = tmp_path / "filleted.step"
    kernel.write_step(maker.Shape(), str(step_path))
    doc = Document()
    (body_id,) = import_step(doc, step_path)
    cylinders = _faces_of_kind(doc, body_id, "cylinder_partial")
    assert len(cylinders) == 1

    _delete_faces(doc, cylinders, body_id)

    _assert_box(doc, body_id)


def test_faces_that_cannot_close_raise_and_leave_document_and_log_unchanged():
    """削除すると形が閉じない面の組はエラーになり、文書もログも変わらない(箱の上面: 周りの4面が平行で閉じない)。"""
    doc = Document()
    body_id = _box(doc)
    before = _state(doc)

    with pytest.raises(ValueError):
        _delete_faces(doc, [_plane_at(doc, body_id, 2, H)], body_id)

    assert _state(doc) == before


def test_replaying_the_log_gives_the_same_shape():
    """記録したログを再生すると同じ形になる(面の並びまで同じ。後続の verb は添字で面を指すため)。"""
    doc = Document()
    body_id, cylinder = _filleted_box(doc)
    _delete_faces(doc, [cylinder], body_id)

    restored = replay_through_json(doc)

    assert query.describe_faces(restored.get_body(body_id).shape) == query.describe_faces(doc.get_body(body_id).shape)
    assert query.volume(restored.get_body(body_id).shape) == pytest.approx(BOX_VOLUME, rel=1e-9)


# ---------------------------------------------------------------- Issue の要件から足したもの
def test_a_partly_removable_set_is_refused_as_a_whole():
    """消せる面と消せない面を一緒に選ぶと、消せる面だけ消して成功にしない(全部か何もしないか)。
    OCCT の Defeaturing は消せない面を警告だけで残し、残りを消した形を返す(docs/FACE_EDIT.md で実測)。"""
    doc = Document()
    body_id, cylinder = _filleted_box(doc)
    bottom = _plane_at(doc, body_id, 2, 0.0)
    before = _state(doc)

    with pytest.raises(ValueError):
        _delete_faces(doc, [cylinder, bottom], body_id)

    assert _state(doc) == before


def test_delete_faces_is_logged_undoable_and_redoable():
    """1回の操作としてログに1行積まれ、Ctrl+Z(undo)で戻り、redo でもう一度消える。"""
    doc = Document()
    body_id, cylinder = _filleted_box(doc)
    filleted_volume = query.volume(doc.get_body(body_id).shape)
    entries_before = len(doc.log.entries)

    operation = _delete_faces(doc, [cylinder], body_id)

    assert operation.verb == "delete_faces"
    assert operation.args == {"faces": [cylinder], "body_id": body_id}
    assert len(doc.log.entries) == entries_before + 1
    doc.log.undo(doc)
    assert query.volume(doc.get_body(body_id).shape) == pytest.approx(filleted_volume, rel=1e-9)
    doc.log.redo(doc)
    _assert_box(doc, body_id)


def test_provenance_lists_the_deleted_faces_and_maps_every_other_face():
    """面の由来の記録(`face_map` / `deleted` / `new_faces`)に載る。消した面は `deleted`、残りは1枚ずつ対応し、新しい面は無い。"""
    doc = Document()
    body_id, cylinder = _filleted_box(doc)
    faces_before = kernel.face_count(doc.get_body(body_id).shape)

    operation = _delete_faces(doc, [cylinder], body_id)

    provenance = operation.provenance
    assert provenance["deleted"] == [cylinder]
    assert provenance["new_faces"] == []
    survivors = {old: new for old, new in provenance["face_map"].items() if int(old) != cylinder}
    assert len(survivors) == faces_before - 1
    assert all(len(new) == 1 for new in survivors.values())
    json.dumps(doc.log.to_json())


def test_the_entry_takes_the_faces_from_the_selection():
    doc = Document()
    body_id, cylinder = _filleted_box(doc)
    selection.set_selection(doc, refs=[selection.select_face(doc, body_id, cylinder)])

    result = entry_for("delete_faces")(doc)

    assert result == {"body_id": body_id, "changed_bodies": [body_id]}
    _assert_box(doc, body_id)


def test_the_entry_refuses_faces_of_two_bodies():
    doc = Document()
    body_id, cylinder = _filleted_box(doc)
    other = second_body(doc)
    selection.set_selection(
        doc, refs=[selection.select_face(doc, body_id, cylinder), selection.select_face(doc, other, 1)]
    )
    before = _state(doc)

    with pytest.raises(ValueError):
        entry_for("delete_faces")(doc)

    assert _state(doc) == before


def test_ai_can_call_it_over_mcp():
    """AI(MCP)からも呼べる。対象は選択から取り、引数は持たない(target set を引数にしない契約)。"""
    descriptor = next(d for d in catalog.tool_descriptors() if d["name"] == "delete_faces")
    assert descriptor["inputSchema"].get("required", []) == []
