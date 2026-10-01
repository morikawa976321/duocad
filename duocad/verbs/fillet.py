"""verb `fillet` — 辺を丸める。まっすぐな辺のフィレットは面を1枚増やすだけだが、円い辺の
フィレットは**球ではなくトーラス**になる(半径が全周円筒面の半径以下の場合)。

**OCCTのフィレットは例外を投げずに失敗し、しかも半径に対して単調でない**。`IsDone()` だけ
では不十分で、範囲検査での代用もできない —— 毎回 `IsDone` / 空でない / 体積>0 / `BRepCheck`
の全関門を通す必要がある(確認は `kernel.fillet_edges` の側)。いずれかで落ちたら操作ごと
捨てて文書を変更しない・ログにも積まない。

**`edges` は1始まりの辺の添字**(面の添字と同じ流儀)で、順序は選択順であって昇順ではない。
**面・辺の添字はフィレット・面取りのたびに形状ごと振り直される**(同じ面が例えば i=5→9 の
ように動く) —— 生の添字を保存・再利用せず、`query.describe_edges` / `edges_of_face` から
得た直近の添字だけを渡すこと。フィレットと面取りは同じボディへ順にかけられるが、先にかけた
方で添字が変わるので、後段は選び直した添字を使う。

**由来は `kernel.normalize_provenance` 経由**。OCP を直接importしない。

`chamfer.py` とは骨格が同じで、違うのは呼ぶkernelの関数と引数の名前だけである(共通部分は
`_common.py`)。**1モジュールへ統合はしない** —— verbを分けるのは `oplog.py` にverb名の分岐を
書かないための設計であり、「今たまたま似ている」ことは統合の理由にならない。
"""
from __future__ import annotations

from typing import Optional, Sequence, Union

from .. import kernel
from ..document import BodyId, Document
from ..oplog import Operation, register_verb
from ..entry import entry
from ._common import edge_entry, editable_body, normalize_values, reapply_shape, remember, undo_shape

VERB_FILLET = "fillet"


def fillet(
    doc: Document,
    edges: Sequence[int],
    radius: Union[float, Sequence[float]],
    body_id: Optional[BodyId] = None,
) -> Operation:
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
    reapply_shape(document, body, result, operation)


register_verb(VERB_FILLET, do=_do_fillet, undo=undo_shape)


# --------------------------------------------------------------------------------------
# 入口(`duocad/entry.py`) —— 対象を確定選択から取り、上の verb を掛け、応答に載せるものを返す
# --------------------------------------------------------------------------------------
@entry(VERB_FILLET)
def _enter_fillet(document: Document, radius: Union[float, Sequence[float]]) -> dict:
    return edge_entry(document, fillet, radius)
