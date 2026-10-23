"""verb `delete_faces` — 選んだ面を取り除き、周りの面をそれぞれ自分の曲面のまま延ばして閉じ直す(#190)。

フィレット・面取り・穴・ボスなどを、作った履歴の無いボディ(STEP)からも消せる。**閉じられない形は断る**
(箱の上面だけ・段の面・消すと新しい面が要る形・残す面が削られる形)。穴や丸めは、それを作る面をまとめて選ぶ
(壁だけでは止まり穴の底や座ぐりの段が閉じない)。方式・関門・実測は docs/FACE_EDIT.md。

計算は子プロセス(`kernel_worker`)で行い、断ったら文書もログも変えない。ログの再生(redo・文書を開く)には期限を掛けない。

**`faces` は1始まりの面の添字**(形状変更のたびに振り直される。直近の添字だけを渡す)。
"""
from __future__ import annotations

from typing import Optional, Sequence

from .. import kernel_face_edit, selection
from ..document import BodyId, Document
from ..entry import entry
from ..oplog import Operation, register_verb
from ._common import editable_body, reapply_shape, remember, replayed_body, undo_shape

VERB_DELETE_FACES = "delete_faces"


def delete_faces(doc: Document, faces: Sequence[int], body_id: Optional[BodyId] = None) -> Operation:
    face_list = [int(index) for index in faces]
    body = editable_body(doc, body_id, what="delete faces")
    result = kernel_face_edit.delete_faces(body.shape, face_list)

    previous_shape = body.shape
    body.shape = result["shape"]
    operation = doc.log.append(VERB_DELETE_FACES, {"faces": face_list, "body_id": body.id})
    operation.provenance = result["provenance"]
    remember(doc, body.id, operation.seq, previous_shape)
    return operation


def _do_delete_faces(document: Document, operation: Operation) -> None:
    """redo / replay: 記録された面を消し直す(ログには触らない)。"""
    args = operation.args
    body = replayed_body(document, args["body_id"], what="delete faces")
    result = kernel_face_edit.delete_faces(body.shape, [int(index) for index in args["faces"]], interactive=False)
    reapply_shape(document, body, result, operation)


register_verb(VERB_DELETE_FACES, do=_do_delete_faces, undo=undo_shape)


@entry(VERB_DELETE_FACES)
def _enter_delete_faces(document: Document) -> dict:
    faces, body_id = selection.require_faces(document)
    changed = delete_faces(document, faces, body_id=body_id).args["body_id"]
    return {"body_id": changed, "changed_bodies": [changed]}
