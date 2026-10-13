"""verb `shell_body` — 選んだ面を抜き(開口)、ボディを外形から内側へ一様な厚さの殻にする。

**抜く面は1枚以上**。閉じた中空(面を選ばない)は作らない(2026-10-14 本人判断。3Dプリントでは
中の材料を抜けない)。外形寸法は変わらず、内側の角は尖ったまま。

**OCCTのシェルは成功を装って何もしないことがある**(厚すぎる・抜く面がフィレット面に接する)。
検査は `kernel_shell.shell_solid` が持ち、落ちたら操作ごと捨てて文書もログも変えない。

**`faces` は1始まりの面の添字**(形状変更のたびに振り直される。直近の添字だけを渡す)。
"""
from __future__ import annotations

from typing import Optional, Sequence

from .. import kernel_shell, selection
from ..document import BodyId, Document
from ..entry import entry
from ..oplog import Operation, register_verb
from ._common import editable_body, reapply_shape, remember, undo_shape

VERB_SHELL_BODY = "shell_body"


def shell_body(
    doc: Document,
    faces: Sequence[int],
    thickness: float,
    body_id: Optional[BodyId] = None,
) -> Operation:
    face_list = [int(index) for index in faces]
    value = float(thickness)
    body = editable_body(doc, body_id, what="shell", require_visible=True)
    result = kernel_shell.shell_solid(body.shape, face_list, value)

    previous_shape = body.shape
    body.shape = result["shape"]
    operation = doc.log.append(
        VERB_SHELL_BODY,
        {"faces": face_list, "thickness": value, "body_id": body.id},
    )
    operation.provenance = result["provenance"]
    remember(doc, body.id, operation.seq, previous_shape)
    return operation


def _do_shell_body(document: Document, operation: Operation) -> None:
    """redo / replay: 記録された面と厚さで殻にし直す(ログには触らない)。"""
    args = operation.args
    body = editable_body(document, args["body_id"], what="shell", require_visible=True)
    result = kernel_shell.shell_solid(
        body.shape, [int(index) for index in args["faces"]], float(args["thickness"])
    )
    reapply_shape(document, body, result, operation)


register_verb(VERB_SHELL_BODY, do=_do_shell_body, undo=undo_shape)


# --------------------------------------------------------------------------------------
# 入口(`duocad/entry.py`) —— 抜く面を選択から取り、上の verb を掛け、応答に載せるものを返す
# --------------------------------------------------------------------------------------
@entry(VERB_SHELL_BODY)
def _enter_shell_body(document: Document, thickness: float) -> dict:
    faces, body_id = selection.require_faces(document)
    changed = shell_body(document, faces, thickness, body_id=body_id).args["body_id"]
    return {"body_id": changed, "changed_bodies": [changed]}
