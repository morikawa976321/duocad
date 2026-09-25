"""verb `boolean_body` — ターゲットボディにツールボディ群で union/cut/intersect する。

ターゲットは in-place で形だけ置き換え(ID・名前は維持)。ツールボディは消費されて
文書から消える。undo では fillet と delete の両方の巻き戻し(形状スナップショット +
Body スナップショット)が1回で戻る。
"""
from __future__ import annotations

from typing import Any, Sequence

from .. import kernel
from ..document import BodyId, Document
from ..oplog import Operation, register_verb
from ._common import detach_bodies, editable_body, reattach_bodies, recall, remember, resolve_shape_ref

VERB_BOOLEAN_BODY = "boolean_body"
_SNAPSHOT_KEY = "boolean_body:removed"


def _validate_target_ref(doc: Document, target_ref: Any) -> BodyId:
    body, index = resolve_shape_ref(
        doc, target_ref, kind="face", what="target_ref", mesh_hint="(メッシュボディには面が無い)"
    )
    if index < 1 or index > kernel.face_count(body.shape):
        raise ValueError(
            f"target_ref: face index {index} is out of range for body {body.id!r} "
            f"(1..{kernel.face_count(body.shape)})"
        )
    return body.id


def _validate_participants(
    doc: Document, target_body_id: BodyId, tool_body_ids: Sequence[BodyId]
) -> tuple:
    """全部検査してから1つも変えない —— 途中で落ちると半端な文書が残る。"""
    if not tool_body_ids:
        raise ValueError(
            "boolean_body requires at least one tool body "
            "(確定選択からツールを取る verb なので、呼ぶ前に選ぶこと)"
        )
    tool_ids = [str(body_id) for body_id in tool_body_ids]
    if len(set(tool_ids)) != len(tool_ids):
        raise ValueError(f"boolean_body got duplicate tool body ids: {tool_ids!r}")
    if str(target_body_id) in tool_ids:
        raise ValueError(
            f"boolean_body: target body {target_body_id!r} cannot also be a tool"
        )

    target_body = editable_body(
        doc, target_body_id, what="boolean_body", require_visible=True
    )
    if not kernel.is_solid(target_body.shape):
        raise ValueError(f"boolean_body: target body {target_body.id!r} is not a solid")

    tools = []
    for tool_id in tool_ids:
        tool = editable_body(doc, tool_id, what="boolean_body", require_visible=True)
        if not kernel.is_solid(tool.shape):
            raise ValueError(f"boolean_body: tool body {tool_id!r} is not a solid")
        tools.append(tool)
    return target_body, tools


def _apply(doc: Document, target_body, tools: Sequence, mode: str) -> tuple:
    """演算(kernel)→ ターゲットの形を差し替え → ツールを退避して消す。戻りは `(前の形, 退避)`。
    幾何の失敗は差し替えより先に上がるので、落ちたときは文書に何も残らない。"""
    result = kernel.boolean_bodies(target_body.shape, [tool.shape for tool in tools], mode)
    previous_shape = target_body.shape
    target_body.shape = result["shape"]
    return previous_shape, detach_bodies(doc, [tool.id for tool in tools])


def _remember_undo(doc: Document, target_id: BodyId, seq: int, previous_shape: Any, detached: dict) -> None:
    remember(doc, target_id, seq, previous_shape)
    remember(doc, _SNAPSHOT_KEY, seq, detached)


def boolean_body(
    doc: Document,
    target_ref: Any,
    tool_body_ids: Sequence[BodyId],
    mode: str = "union",
) -> Operation:
    """ターゲットにツール群を作用させる。`target_ref` は面1枚への参照、ツールは BodyId の並び。"""
    target_body_id = _validate_target_ref(doc, target_ref)
    target_body, tools = _validate_participants(doc, target_body_id, tool_body_ids)
    previous_shape, detached = _apply(doc, target_body, tools, str(mode))
    operation = doc.log.append(
        VERB_BOOLEAN_BODY,
        {
            "target_body_id": target_body.id,
            "tool_bodies": [tool.id for tool in tools],
            "mode": str(mode),
        },
    )
    _remember_undo(doc, target_body.id, operation.seq, previous_shape, detached)
    return operation


def _do_boolean_body(document: Document, operation: Operation) -> None:
    """redo / replay: 記録どおりにもう一度演算する(ログには触らない)。"""
    args = operation.args
    target_body, tools = _validate_participants(
        document, args["target_body_id"], [str(body_id) for body_id in args["tool_bodies"]]
    )
    previous_shape, detached = _apply(document, target_body, tools, str(args["mode"]))
    _remember_undo(document, target_body.id, operation.seq, previous_shape, detached)


def _undo_boolean_body(document: Document, operation: Operation) -> None:
    """undo: ターゲットの形を戻し、消えたツールボディを元の添字と active ごと復元する(1回の undo で両方)。"""
    target_body_id = operation.args["target_body_id"]
    document.get_body(target_body_id).shape = recall(document, target_body_id, operation.seq)
    reattach_bodies(document, recall(document, _SNAPSHOT_KEY, operation.seq))


register_verb(VERB_BOOLEAN_BODY, do=_do_boolean_body, undo=_undo_boolean_body)
