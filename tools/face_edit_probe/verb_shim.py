"""受入テストの検算用の仮の verb(pytest プラグイン)。試作の方式(A・B・D)を duocad の verb の作法で包む。

    .venv/Scripts/python.exe -m pytest -q -p tools.face_edit_probe.verb_shim tests/test_delete_faces_acceptance.py tests/test_offset_faces_acceptance.py

これを読み込むと `delete_faces` / `offset_faces` が登録され、受入テストの xfail の印が外れて本物の検査になる。
**製品のコードではない**(由来の取り方・検査の文言・プレビューの扱いは仮)。本実装は docs/FACE_EDIT.md「実装計画」。
"""
from __future__ import annotations

import duocad.verbs as verbs_package
from duocad import catalog, kernel, selection
from duocad.entry import entry
from duocad.oplog import register_verb
from duocad.verbs._common import editable_body, reapply_shape, remember, undo_shape

from . import blend, methods
from .provenance import successors
from .shapes import Case
from .worker import as_solid, gate


def _faces(shape, indices):
    return [kernel.face_of(shape, i) for i in indices]


def _projection(verb, indices, table, final_count):
    return {"verb": verb, "faces": list(indices), **kernel._face_map_projection({str(k): v[0] for k, v in table.items()}, final_count)}


def kernel_delete_faces(shape, indices):
    faces = _faces(shape, indices)
    result, algo = methods.defeature(shape, faces)
    reason = gate(Case("shim", "delete", shape, faces, None), result)
    if reason:
        raise ValueError(f"delete_faces: {reason}")
    table, unresolved, _ = successors(algo.History(), shape, result)
    if unresolved:
        raise ValueError(f"delete_faces: lost track of faces {unresolved}")
    return {"shape": result, "provenance": _projection("delete_faces", indices, table, kernel.face_count(result))}


class _Generated:
    """MakeOffset の由来は Modified でなく Generated に1対1で入る(docs/FACE_EDIT.md で実測)。"""

    def __init__(self, algo):
        self.algo = algo

    def Modified(self, face):
        return self.algo.Generated(face)

    def IsDeleted(self, face):
        return False


def kernel_offset_faces(shape, indices, distance):
    if distance == 0:
        raise ValueError("offset_faces: distance must not be 0")
    faces = _faces(shape, indices)
    blends, _ = blend.find_blends(shape, faces)
    if blends:
        result = blend.move_following_blends(shape, faces, distance, mover="B")
        table = {i: ([i], True, False, 1) for i in range(1, kernel.face_count(shape) + 1)}  # 仮: D の由来は未実装
    else:
        raw, algo = methods.offset_faces(shape, faces, distance)
        result = as_solid(raw)
        table, unresolved, _ = successors(_Generated(algo), shape, result)
        if unresolved:
            raise ValueError(f"offset_faces: lost track of faces {unresolved}")
    reason = gate(Case("shim", "move", shape, faces, None, distance), result, following_blends=bool(blends))
    if reason:
        raise ValueError(f"offset_faces: {reason}")
    return {"shape": result, "provenance": _projection("offset_faces", indices, table, kernel.face_count(result))}


def delete_faces(doc, faces, body_id=None):
    face_list = [int(i) for i in faces]
    if not face_list:
        raise ValueError("delete_faces requires at least one face")
    body = editable_body(doc, body_id, what="delete faces")
    result = kernel_delete_faces(body.shape, face_list)
    previous = body.shape
    body.shape = result["shape"]
    operation = doc.log.append("delete_faces", {"faces": face_list, "body_id": body.id})
    operation.provenance = result["provenance"]
    remember(doc, body.id, operation.seq, previous)
    return operation


def offset_faces(doc, faces, distance, body_id=None):
    face_list = [int(i) for i in faces]
    if not face_list:
        raise ValueError("offset_faces requires at least one face")
    value = float(distance)
    body = editable_body(doc, body_id, what="offset faces")
    result = kernel_offset_faces(body.shape, face_list, value)
    previous = body.shape
    body.shape = result["shape"]
    operation = doc.log.append("offset_faces", {"faces": face_list, "distance": value, "body_id": body.id})
    operation.provenance = result["provenance"]
    remember(doc, body.id, operation.seq, previous)
    return operation


def _do_delete(document, operation):
    body = editable_body(document, operation.args["body_id"], what="delete faces")
    reapply_shape(document, body, kernel_delete_faces(body.shape, operation.args["faces"]), operation)


def _do_offset(document, operation):
    body = editable_body(document, operation.args["body_id"], what="offset faces")
    result = kernel_offset_faces(body.shape, operation.args["faces"], float(operation.args["distance"]))
    reapply_shape(document, body, result, operation)


register_verb("delete_faces", do=_do_delete, undo=undo_shape)
register_verb("offset_faces", do=_do_offset, undo=undo_shape)


@entry("delete_faces")
def _enter_delete(document):
    faces, body_id = selection.require_faces(document)
    changed = delete_faces(document, faces, body_id=body_id).args["body_id"]
    return {"body_id": changed, "changed_bodies": [changed]}


@entry("offset_faces")
def _enter_offset(document, distance):
    faces, body_id = selection.require_faces(document)
    changed = offset_faces(document, faces, distance, body_id=body_id).args["body_id"]
    return {"body_id": changed, "changed_bodies": [changed]}


verbs_package.delete_faces = delete_faces
verbs_package.offset_faces = offset_faces
_RETURNS = (catalog.Result("body_id", "string"), catalog.Result("changed_bodies", "string[]"))
catalog.COMMANDS = catalog.COMMANDS + (
    catalog.Command(
        name="delete_faces", category="feature", description="(検算用の仮)", returns=_RETURNS,
        session="delete_faces", resets_selection=True,
    ),
    catalog.Command(
        name="offset_faces", category="feature", description="(検算用の仮)", returns=_RETURNS,
        params=(catalog.Param("distance", "number", "外向き法線へ動かす距離(mm)", dimension=True),),
        session="offset_faces", resets_selection=True,
    ),
)
