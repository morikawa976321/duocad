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

from . import strict


def _faces(shape, indices):
    return [kernel.face_of(shape, i) for i in indices]


def _projection(verb, indices, table, final_count):
    return {"verb": verb, "faces": list(indices), **kernel._face_map_projection({str(k): v for k, v in table.items()}, final_count)}


def kernel_delete_faces(shape, indices):
    """A(`BOPAlgo_RemoveFeatures`)+厳しい版の関門(strict.py)。"""
    try:
        result, table, _ = strict.strict_delete(shape, _faces(shape, indices))
    except strict.FaceEditError as error:
        raise ValueError(f"delete_faces: {error}") from error
    return {"shape": result, "provenance": _projection("delete_faces", indices, table, kernel.face_count(result))}


def kernel_offset_faces(shape, indices, distance):
    """B(全体0・選んだ面だけ距離)。接線でつながる丸めがあれば D(外す→動かす→丸め直す)。厳しい版の関門(strict.py)。"""
    if distance == 0:
        raise ValueError("offset_faces: distance must not be 0")
    try:
        result, table, _ = strict.strict_follow(shape, _faces(shape, indices), distance)
    except strict.FaceEditError as error:
        raise ValueError(f"offset_faces: {error}") from error
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
