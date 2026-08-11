"""verb `chamfer` — 辺を面取りする(v1.1/v1.2から前倒し・2026-08-11)。

契約(`tests/test_chamfer.py` が正):

    chamfer(doc, edges, distance, body_id=None) -> Operation
        `edges` は1始まりの辺の添字のリスト、`distance` は float か辺ごとのリスト。
        ログに `verb="chamfer"`,
        `args={"edges": [int,...], "distance": float|[float,...], "body_id": ..}` を積む。

まっすぐな辺→平面増、円い辺→円錐。失敗は `IsDone()` で検出し文書・ログを変えない。
OCP を直接importしない。
"""
from __future__ import annotations

import math
from typing import Any, List, Optional, Sequence, Union
from weakref import WeakKeyDictionary

from .. import kernel
from ..document import Body, BodyId, Document
from ..oplog import Operation, register_verb

VERB_CHAMFER = "chamfer"

_SNAPSHOTS: "WeakKeyDictionary[Document, dict]" = WeakKeyDictionary()


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

    body = _editable_body(doc, body_id)
    values, stored_distance = _normalize_values(edge_list, distance, label="distance")
    distances = {edge: value for edge, value in zip(edge_list, values)}
    result = kernel.chamfer_edges(body.shape, distances)

    previous_shape = body.shape
    body.shape = result["shape"]
    operation = doc.log.append(
        VERB_CHAMFER,
        {"edges": edge_list, "distance": stored_distance, "body_id": body.id},
    )
    operation.provenance = result["provenance"]
    _remember(doc, body.id, operation.seq, previous_shape)
    return operation


def _normalize_values(
    edges: Sequence[int],
    value: Union[float, Sequence[float]],
    *,
    label: str,
) -> tuple[List[float], Union[float, List[float]]]:
    if isinstance(value, (int, float)):
        values = [float(value)] * len(edges)
        stored: Union[float, List[float]] = float(value)
    else:
        values = [float(item) for item in value]
        if len(values) != len(edges):
            raise ValueError(
                f"{label} list length {len(values)} != edges length {len(edges)}"
            )
        stored = values
    for item in values:
        if not math.isfinite(item) or item <= 0.0:
            raise ValueError(f"{label} must be finite and > 0, got {item!r}")
    return values, stored


def _editable_body(doc: Document, body_id: Optional[BodyId]) -> Body:
    if body_id is None:
        body = doc.active_body()
        if body is None:
            raise ValueError("no active body to chamfer (document is empty)")
        body_id = body.id
    if not doc.is_editable(body_id):
        raise ValueError(
            f"body {body_id!r} is not editable (kind='reference'). "
            "make_editable_copy() で編集用のソリッドを作ってから操作すること"
        )
    body = next(candidate for candidate in doc.bodies if candidate.id == body_id)
    if body.shape is None:
        raise ValueError(f"body {body_id!r} has no shape to chamfer")
    return body


def _remember(doc: Document, body_id: BodyId, seq: int, shape: Any) -> None:
    _SNAPSHOTS.setdefault(doc, {})[(body_id, seq)] = shape


def _recall(doc: Document, body_id: BodyId, seq: int) -> Any:
    snapshots = _SNAPSHOTS.get(doc)
    if snapshots is None or (body_id, seq) not in snapshots:
        raise KeyError(
            f"no snapshot for undo of seq={seq} on body {body_id!r}: "
            "保存したログを再生せずにundoしようとしている。"
            "OpLog.replay(document) してから undo すること"
        )
    return snapshots.pop((body_id, seq))


def _do_chamfer(document: Document, operation: Operation) -> None:
    """redo / replay: 記録された辺と距離で面取りし直す(ログには触らない)。"""
    args = operation.args
    edge_list = [int(index) for index in args["edges"]]
    values, _ = _normalize_values(edge_list, args["distance"], label="distance")
    body = _editable_body(document, args["body_id"])
    result = kernel.chamfer_edges(
        body.shape, {edge: value for edge, value in zip(edge_list, values)}
    )
    previous_shape = body.shape
    body.shape = result["shape"]
    if operation.provenance is None:
        operation.provenance = result["provenance"]
    _remember(document, body.id, operation.seq, previous_shape)


def _undo_chamfer(document: Document, operation: Operation) -> None:
    """undo: 面取り前の形状へ戻す。"""
    body_id = operation.args["body_id"]
    body = next(
        (candidate for candidate in document.bodies if candidate.id == body_id), None
    )
    if body is None:
        raise KeyError(f"no such body: {body_id!r}")
    body.shape = _recall(document, body_id, operation.seq)


register_verb(VERB_CHAMFER, do=_do_chamfer, undo=_undo_chamfer)
