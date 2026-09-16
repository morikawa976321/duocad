"""verb `shell` — 面を開口にして中空にする(壁の厚みは1つ・内側へ削る)。

**`BRepOffsetAPI_MakeThickSolid` は例外も `IsDone()=False` も出さずに、入力をそのまま返して
失敗することがある**。体積・ソリッド数・`BRepCheck` の検査は `kernel.shell_solid` の側で行い、
失敗したら操作ごと捨てて文書を変更しない・ログにも積まない。

**由来は `kernel.normalize_provenance` 経由**。OCP を直接importしない。

`fillet.py` / `chamfer.py` と骨格が同じでも **1モジュールへ統合はしない**(`oplog.py` にverb名の
分岐を書かないための設計)。
"""
from __future__ import annotations

from typing import Optional, Sequence

from .. import kernel
from ..document import BodyId, Document
from ..oplog import Operation, register_verb
from ._common import editable_body, recall, remember

VERB_SHELL = "shell"


def shell(
    doc: Document,
    faces: Sequence[int],
    thickness: float,
    body_id: Optional[BodyId] = None,
) -> Operation:
    """面 `faces`(1始まりの添字・1枚以上)を開口にして中空にし、ログに積んで Operation を返す。

    失敗時(添字が範囲外・厚みが0以下・壁が残らない・形が変わらない・無効な形状)は例外を送出し、
    **document にもログにも変更を残さない** — 失敗しうる幾何計算を append より先に済ませる。
    """
    face_list = [int(index) for index in faces]
    if not face_list:
        raise ValueError("shell requires at least one opening face")

    body = editable_body(doc, body_id, what="shell", require_visible=True)
    result = kernel.shell_solid(body.shape, face_list, float(thickness))

    previous_shape = body.shape
    body.shape = result["shape"]
    operation = doc.log.append(
        VERB_SHELL,
        {"faces": face_list, "thickness": float(thickness), "body_id": body.id},
    )
    operation.provenance = result["provenance"]
    remember(doc, body.id, operation.seq, previous_shape)
    return operation


def _do_shell(document: Document, operation: Operation) -> None:
    """redo / replay: 記録された面と厚みで中空にし直す(ログには触らない)。"""
    args = operation.args
    face_list = [int(index) for index in args["faces"]]
    body = editable_body(document, args["body_id"], what="shell", require_visible=True)
    result = kernel.shell_solid(body.shape, face_list, float(args["thickness"]))
    previous_shape = body.shape
    body.shape = result["shape"]
    if operation.provenance is None:
        operation.provenance = result["provenance"]
    remember(document, body.id, operation.seq, previous_shape)


def _undo_shell(document: Document, operation: Operation) -> None:
    body_id = operation.args["body_id"]
    document.get_body(body_id).shape = recall(document, body_id, operation.seq)


register_verb(VERB_SHELL, do=_do_shell, undo=_undo_shell)
