"""verb `offset_faces` — 選んだ面を外向き法線へ距離だけ動かし、隣の面をそれぞれ自分の曲面のまま伸縮させる(#183)。

正の距離で太る(穴の内面なら穴が細くなる)。動かせるのは平面・円筒・球・トーラスと、押し出し・回転・オフセットの面。選んだ面に接線でつながる
一定半径の丸めは、同じ半径のまま付いてくる(外して動かし、丸め直す)。**断る**: 距離が大きすぎて面が消える・つながりが
変わる、解析曲面でない面が内側へ曲がりの半径を超えて折り返す、接線の隣が丸めとして扱えない(一定半径の丸めでない自由曲面・円錐・可変半径・丸めの上の丸め)。方式・関門・実測は docs/FACE_EDIT.md。

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

VERB_OFFSET_FACES = "offset_faces"


def offset_faces(
    doc: Document,
    faces: Sequence[int],
    distance: float,
    body_id: Optional[BodyId] = None,
) -> Operation:
    face_list = [int(index) for index in faces]
    value = float(distance)
    body = editable_body(doc, body_id, what="offset faces")
    result = kernel_face_edit.offset_faces(body.shape, face_list, value)

    previous_shape = body.shape
    body.shape = result["shape"]
    operation = doc.log.append(VERB_OFFSET_FACES, {"faces": face_list, "distance": value, "body_id": body.id})
    operation.provenance = result["provenance"]
    remember(doc, body.id, operation.seq, previous_shape)
    return operation


def _do_offset_faces(document: Document, operation: Operation) -> None:
    """redo / replay: 記録された面と距離で動かし直す(ログには触らない)。"""
    args = operation.args
    body = replayed_body(document, args["body_id"], what="offset faces")
    result = kernel_face_edit.offset_faces(
        body.shape, [int(index) for index in args["faces"]], float(args["distance"]), interactive=False
    )
    reapply_shape(document, body, result, operation)


register_verb(VERB_OFFSET_FACES, do=_do_offset_faces, undo=undo_shape)


@entry(VERB_OFFSET_FACES)
def _enter_offset_faces(document: Document, distance: float) -> dict:
    faces, body_id = selection.require_faces(document)
    changed = offset_faces(document, faces, distance, body_id=body_id).args["body_id"]
    return {"body_id": changed, "changed_bodies": [changed]}
