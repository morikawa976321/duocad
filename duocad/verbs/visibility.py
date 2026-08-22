"""verb `set_visible` — Bodyの表示・非表示を切り替える。

**可視性は選択機構の一部**で、`visible=False` のBodyは選択の対象にならない
(`duocad.selection`)。存在しない body_id は KeyError で、文書にもログにも何も残さない。
"""
from __future__ import annotations

from ..document import BodyId, Document
from ..oplog import Operation, register_verb

VERB_SET_VISIBLE = "set_visible"


def set_visible(doc: Document, body_id: BodyId, visible: bool) -> Operation:
    body = doc.get_body(body_id)
    previous = body.visible
    body.visible = bool(visible)
    return doc.log.append(
        VERB_SET_VISIBLE,
        {"body_id": body_id, "visible": bool(visible), "previous": previous},
    )


def _do_set_visible(document: Document, operation: Operation) -> None:
    document.get_body(operation.args["body_id"]).visible = operation.args["visible"]


def _undo_set_visible(document: Document, operation: Operation) -> None:
    document.get_body(operation.args["body_id"]).visible = operation.args["previous"]


register_verb(VERB_SET_VISIBLE, do=_do_set_visible, undo=_undo_set_visible)