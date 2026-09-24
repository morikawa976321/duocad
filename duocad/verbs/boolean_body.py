"""verb `boolean_body` — ターゲットボディにツールボディ群で union/cut/intersect する。

ターゲットは in-place で形だけ置き換え(ID・名前は維持)。ツールボディは消費されて
文書から消える。undo では fillet と delete の両方の巻き戻し(形状スナップショット +
Body スナップショット)が1回で戻る。
"""
from __future__ import annotations

from typing import Any, Dict, Sequence

from .. import kernel
from ..document import BodyId, Document
from ..oplog import Operation, register_verb
from ._common import editable_body, recall, remember

VERB_BOOLEAN_BODY = "boolean_body"
_SNAPSHOT_KEY = "boolean_body:removed"


def _shape_ref(doc: Document, ref: Any, *, kind: str, what: str) -> tuple:
    """`{"body": BodyId, "index": int}` を (Body, 添字) にする(`mirror_body` と同じ形)。"""
    if not isinstance(ref, dict):
        raise ValueError(
            f"{what} must be a reference like "
            '{"body": "<body_id>", "index": 1}, got ' + repr(ref)
        )
    if "body" not in ref or "index" not in ref:
        raise ValueError(
            f'{what} needs both "body" and "index" '
            '(例: {"body": "<body_id>", "index": 1}), got ' + repr(ref)
        )
    declared = ref.get("kind")
    if declared is not None and str(declared) != kind:
        raise ValueError(f"{what} must be a {kind!r} reference, got kind={declared!r}")
    body = doc.get_body(str(ref["body"]))
    if body.shape is None:
        raise ValueError(
            f"{what}: body {body.id!r} has no B-rep shape (メッシュボディには面が無い)"
        )
    try:
        index = int(ref["index"])
    except (TypeError, ValueError):
        raise ValueError(f"{what}: index must be an integer, got {ref['index']!r}") from None
    return body, index


def _validate_target_ref(doc: Document, target_ref: Any) -> BodyId:
    body, index = _shape_ref(doc, target_ref, kind="face", what="target_ref")
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


def _remove_tools(doc: Document, tools: Sequence) -> Dict[str, Any]:
    """ツールボディを消す前に undo 用の退避を取る(`delete._remove` と同じ形)。"""
    positions = {body.id: index for index, body in enumerate(doc.bodies)}
    removed: Dict[str, Any] = {
        "bodies": sorted(
            ((positions[tool.id], tool) for tool in tools),
            key=lambda item: item[0],
        ),
        "active": doc._active_body_id,
    }
    for tool in tools:
        doc.remove_body(tool.id)
    return removed


def boolean_body(
    doc: Document,
    target_ref: Any,
    tool_body_ids: Sequence[BodyId],
    mode: str = "union",
) -> Operation:
    """ターゲットにツール群を作用させる。`target_ref` は面1枚への参照、ツールは BodyId の並び。"""
    target_body_id = _validate_target_ref(doc, target_ref)
    target_body, tools = _validate_participants(doc, target_body_id, tool_body_ids)

    tool_shapes = [tool.shape for tool in tools]
    result = kernel.boolean_bodies(target_body.shape, tool_shapes, str(mode))

    previous_shape = target_body.shape
    target_body.shape = result["shape"]
    removed = _remove_tools(doc, tools)

    operation = doc.log.append(
        VERB_BOOLEAN_BODY,
        {
            "target_body_id": target_body.id,
            "tool_bodies": [tool.id for tool in tools],
            "mode": str(mode),
        },
    )
    remember(doc, target_body.id, operation.seq, previous_shape)
    remember(doc, _SNAPSHOT_KEY, operation.seq, removed)
    return operation


def _do_boolean_body(document: Document, operation: Operation) -> None:
    """redo / replay: 記録どおりにもう一度演算する(ログには触らない)。"""
    args = operation.args
    target_body_id = args["target_body_id"]
    tool_body_ids = [str(body_id) for body_id in args["tool_bodies"]]
    mode = str(args["mode"])
    target_body, tools = _validate_participants(document, target_body_id, tool_body_ids)
    result = kernel.boolean_bodies(
        target_body.shape, [tool.shape for tool in tools], mode
    )
    previous_shape = target_body.shape
    target_body.shape = result["shape"]
    removed = _remove_tools(document, tools)
    remember(document, target_body.id, operation.seq, previous_shape)
    remember(document, _SNAPSHOT_KEY, operation.seq, removed)


def _undo_boolean_body(document: Document, operation: Operation) -> None:
    """undo: ターゲットの形を戻し、消えたツールボディを復元する(1回の undo で両方)。"""
    args = operation.args
    target_body_id = args["target_body_id"]
    document.get_body(target_body_id).shape = recall(
        document, target_body_id, operation.seq
    )
    removed = recall(document, _SNAPSHOT_KEY, operation.seq)
    for index, body in removed["bodies"]:
        document.add_body(body, index=index)
    document._active_body_id = removed["active"]


register_verb(VERB_BOOLEAN_BODY, do=_do_boolean_body, undo=_undo_boolean_body)
