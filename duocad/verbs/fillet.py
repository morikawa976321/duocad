"""verb `fillet` — 辺を丸める。

契約(`tests/test_fillet.py` が正):

    fillet(doc, edges, radius, body_id=None) -> Operation
        `edges` は**1始まりの辺の添字**のリスト。`radius` は float か辺ごとのリスト。
        ログに `verb="fillet"`,
        `args={"edges": [int,...], "radius": float|[float,...], "body_id": ..}` を積む。

**OCCTのフィレットは例外を投げずに失敗し、しかも半径に対して単調でない**。
`IsDone()` を必ず確認し、失敗したら操作ごと捨てて文書を変更しない・ログにも積まない
(確認は `kernel.fillet_edges` の側)。

**由来は `kernel.normalize_provenance` 経由**。OCP を直接importしない。

`chamfer.py` とは骨格が同じで、違うのは**呼ぶkernelの関数と引数の名前だけ**である
(共通部分は `_common.py` にまとめてある)。**1モジュールへ統合はしない** ——
verbを分けるのは `oplog.py` にverb名の分岐を書かないための設計であり、
「今たまたま似ている」ことは統合の理由にならない(片方だけ引数が増えれば元に戻る)。
"""
from __future__ import annotations

from typing import Optional, Sequence, Union

from .. import kernel
from ..document import BodyId, Document
from ..oplog import Operation, register_verb
from ._common import editable_body, normalize_values, recall, remember

VERB_FILLET = "fillet"


def fillet(
    doc: Document,
    edges: Sequence[int],
    radius: Union[float, Sequence[float]],
    body_id: Optional[BodyId] = None,
) -> Operation:
    """辺を丸めてログに積み、Operation を返す。"""
    edge_list = [int(index) for index in edges]
    if not edge_list:
        raise ValueError("fillet requires at least one edge")

    body = editable_body(doc, body_id, what="fillet", require_visible=True)
    values, stored_radius = normalize_values(edge_list, radius, label="radius")
    result = kernel.fillet_edges(body.shape, dict(zip(edge_list, values)))

    previous_shape = body.shape
    body.shape = result["shape"]
    operation = doc.log.append(
        VERB_FILLET,
        {"edges": edge_list, "radius": stored_radius, "body_id": body.id},
    )
    operation.provenance = result["provenance"]
    remember(doc, body.id, operation.seq, previous_shape)
    return operation


def _do_fillet(document: Document, operation: Operation) -> None:
    """redo / replay: 記録された辺と半径で丸め直す(ログには触らない)。"""
    args = operation.args
    edge_list = [int(index) for index in args["edges"]]
    values, _ = normalize_values(edge_list, args["radius"], label="radius")
    body = editable_body(document, args["body_id"], what="fillet", require_visible=True)
    result = kernel.fillet_edges(body.shape, dict(zip(edge_list, values)))
    previous_shape = body.shape
    body.shape = result["shape"]
    if operation.provenance is None:
        operation.provenance = result["provenance"]
    remember(document, body.id, operation.seq, previous_shape)


def _undo_fillet(document: Document, operation: Operation) -> None:
    """undo: 丸める前の形状へ戻す。"""
    body_id = operation.args["body_id"]
    document.get_body(body_id).shape = recall(document, body_id, operation.seq)


register_verb(VERB_FILLET, do=_do_fillet, undo=_undo_fillet)
