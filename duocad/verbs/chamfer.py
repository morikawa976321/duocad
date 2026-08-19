"""verb `chamfer` — 辺を面取りする。

契約(`tests/test_chamfer.py` が正):

    chamfer(doc, edges, distance, body_id=None) -> Operation
        `edges` は1始まりの辺の添字のリスト、`distance` は float か辺ごとのリスト。
        ログに `verb="chamfer"`,
        `args={"edges": [int,...], "distance": float|[float,...], "body_id": ..}` を積む。

まっすぐな辺→平面増、円い辺→円錐。失敗は `IsDone()` で検出し文書・ログを変えない。
OCP を直接importしない。骨格は `fillet.py` と同じで、共通部分は `_common.py`
(統合しない理由は fillet 側のdocstring)。

**面取りの距離は1つしか取れない**(＝45度)。実物のPCケースの脚は半径方向4mm・軸方向22mmの
非対称な面取りなので、**面の種類は作れても寸法までは再現できない**。
"""
from __future__ import annotations

from typing import Optional, Sequence, Union

from .. import kernel
from ..document import BodyId, Document
from ..oplog import Operation, register_verb
from ._common import editable_body, normalize_values, recall, remember

VERB_CHAMFER = "chamfer"


def chamfer(
    doc: Document,
    edges: Sequence[int],
    distance: Union[float, Sequence[float]],
    body_id: Optional[BodyId] = None,
) -> Operation:
    """辺を面取りしてログに積み、Operation を返す。"""
    edge_list = [int(index) for index in edges]
    if not edge_list:
        raise ValueError("chamfer requires at least one edge")

    body = editable_body(doc, body_id, what="chamfer")
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
    body = editable_body(document, args["body_id"], what="chamfer")
    result = kernel.chamfer_edges(body.shape, dict(zip(edge_list, values)))
    previous_shape = body.shape
    body.shape = result["shape"]
    if operation.provenance is None:
        operation.provenance = result["provenance"]
    remember(document, body.id, operation.seq, previous_shape)


def _undo_chamfer(document: Document, operation: Operation) -> None:
    """undo: 面取り前の形状へ戻す。"""
    body_id = operation.args["body_id"]
    document.get_body(body_id).shape = recall(document, body_id, operation.seq)


register_verb(VERB_CHAMFER, do=_do_chamfer, undo=_undo_chamfer)
