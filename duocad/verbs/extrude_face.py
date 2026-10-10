"""verb `extrude_face` — **再生専用**。新しい操作は `extrude`(面を受ける)で記録される。

意図的な変則: 公開面(カタログ・Session・MCP・WebSocket)からは外したが、保存済みの古いログを
読めるよう登録だけ残してある。1面の正の距離は `join`、負の距離は `cut` として
`kernel_extrude.extrude_faces` へ渡す(`tests/test_extrude_face_replay.py` が固定)。
undo は `extrude` と同じ形状のスナップショット。
"""
from __future__ import annotations

from .. import kernel_extrude
from ..document import Document
from ..oplog import Operation, register_verb
from ._common import editable_body, reapply_shape, undo_shape

VERB_EXTRUDE_FACE = "extrude_face"


def _do_extrude_face(document: Document, operation: Operation) -> None:
    """redo / replay: 記録された添字と距離で押し出し直す(ログには触らない)。"""
    args = operation.args
    body = editable_body(document, args["body_id"], what="extrude", require_visible=True)
    distance = float(args["distance"])
    result = kernel_extrude.extrude_faces(
        body.shape,
        [int(args["face_index"])],
        distance,
        "join" if distance > 0 else "cut",
        target_shape=body.shape,
    )
    reapply_shape(document, body, result, operation)


register_verb(VERB_EXTRUDE_FACE, do=_do_extrude_face, undo=undo_shape)
