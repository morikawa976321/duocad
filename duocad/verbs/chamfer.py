"""verb `chamfer` — 辺を面取りする。まっすぐな辺→平面増、円い辺→円錐。

失敗は `IsDone()` で検出し、文書・ログを変えない。OCP を直接importしない。骨格は `fillet.py`
と同じで、共通部分は `_common.py`(統合しない理由は fillet 側のdocstring)。

**面取りの距離は1つしか取れない**(＝45度)。実物のPCケースの脚は半径方向4mm・軸方向22mmの
非対称な面取りなので、**面の種類は作れても寸法までは再現できない**。
"""
from __future__ import annotations

from typing import Optional, Sequence, Union

from .. import kernel
from ..document import BodyId, Document
from ..oplog import Operation, register_verb
from ..entry import entry
from ._common import edge_entry, editable_body, normalize_values, reapply_shape, remember, undo_shape

VERB_CHAMFER = "chamfer"


def chamfer(
    doc: Document,
    edges: Sequence[int],
    distance: Union[float, Sequence[float]],
    body_id: Optional[BodyId] = None,
) -> Operation:
    edge_list = [int(index) for index in edges]
    if not edge_list:
        raise ValueError("chamfer requires at least one edge")

    body = editable_body(doc, body_id, what="chamfer", require_visible=True)
    values, stored_distance = normalize_values(edge_list, distance, label="distance")
    result = kernel.chamfer_edges(body.shape, dict(zip(edge_list, values)))

    previous_shape = body.shape
    body.shape = result["shape"]
    operation = doc.log.append(
        VERB_CHAMFER,
        {"edges": edge_list, "distance": stored_distance, "body_id": body.id},
    )
    operation.provenance = result["provenance"]
    remember(doc, body.id, operation.seq, previous_shape)
    return operation


def _do_chamfer(document: Document, operation: Operation) -> None:
    """redo / replay: 記録された辺と長さで面取りし直す(ログには触らない)。"""
    args = operation.args
    edge_list = [int(index) for index in args["edges"]]
    values, _ = normalize_values(edge_list, args["distance"], label="distance")
    body = editable_body(document, args["body_id"], what="chamfer", require_visible=True)
    result = kernel.chamfer_edges(body.shape, dict(zip(edge_list, values)))
    reapply_shape(document, body, result, operation)


register_verb(VERB_CHAMFER, do=_do_chamfer, undo=undo_shape)


# --------------------------------------------------------------------------------------
# 入口(`duocad/entry.py`) —— 対象を確定選択から取り、上の verb を掛け、応答に載せるものを返す
# --------------------------------------------------------------------------------------
@entry(VERB_CHAMFER)
def _enter_chamfer(document: Document, distance: Union[float, Sequence[float]]) -> dict:
    return edge_entry(document, chamfer, distance)
