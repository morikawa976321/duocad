"""verb `mirror_sketch_entities` — 選択のスケッチ図形を、既存の線分1本を軸に鏡映複製する。

**常に複製を作る**(in-place反転の口は無い)。対象は選択のスケッチ図形そのもの(点単位は扱わない:
鏡映は図形全体の形を変える操作)。

鏡映軸は既存の線分1本への参照 `{"sketch": sketch_id, "entity": entity_id}`。対象の選択とは別引数
(混ぜると鏡映される側か軸か区別できない)。軸自身が対象に混ざっていてもよく、**軸に使った図形は
対象から黙って外す**(直線は軸まわりに自分自身へ映り、元と重なる複製にしかならない)。軸の2端点の
座標(u, v)へ解決し、ログには解決済みの数値 `{"axis_p0", "axis_dir"}` だけを載せる(軸に使った線分が
対象に混ざっていると、複製後に参照を引き直すと別の線分になる)。
"""
from __future__ import annotations

import math
from typing import Any, List, Sequence

from .. import parameters
from ..document import Document
from ..oplog import Operation, register_verb
from .. import selection
from ..entry import entry
from ..sketch import (
    add_entities,
    entity_kind_ops,
    get_entity,
    get_sketch,
    new_entity_id,
    remove_entities,
    require_valid_params,
)
from ._common import overlaps_original

VERB_MIRROR_SKETCH_ENTITIES = "mirror_sketch_entities"


def _normalize_targets(doc: Document, targets: Sequence[dict]) -> List[tuple]:
    """選択の参照を `(sketch_id, entity_id)` の重複なしの並びへ畳む。

    点は見ない(モジュールdocstring)。存在しないスケッチ・図形は KeyError。
    """
    if not targets:
        raise ValueError("mirror_sketch_entities requires at least one target")
    ordered: List[tuple] = []
    seen: set = set()
    for ref in targets:
        sketch_id = str(ref["sketch"])
        entity_id = str(ref["entity"])
        get_entity(get_sketch(doc, sketch_id), entity_id)  # 存在検査(無ければ KeyError)
        key = (sketch_id, entity_id)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(key)
    return ordered


def _axis_line(doc: Document, axis: Any) -> tuple:
    """軸への参照を `(sketch_id, entity_id, ax, ay, ux, uy)` にする(点+単位方向は解決済み)。

    `kind` が `"line"` でなければ ValueError(それ以外の図形は鏡映軸にできない)。
    `sketch_id`/`entity_id` は、軸自身が対象に混ざっていたら外す(`mirror_sketch_entities`)ための識別。
    """
    if not isinstance(axis, dict) or "sketch" not in axis or "entity" not in axis:
        raise ValueError(
            "axis must be a reference like "
            '{"sketch": "<sketch_id>", "entity": "<entity_id>"}, got ' + repr(axis)
        )
    axis_sketch, axis_entity = str(axis["sketch"]), str(axis["entity"])
    entity = get_entity(get_sketch(doc, axis_sketch), axis_entity)
    if entity.kind != "line":
        raise ValueError(f"axis must reference a line entity, got kind={entity.kind!r}")
    values = parameters.resolve_all(doc, entity.params)
    ax, ay = float(values["x0"]), float(values["y0"])
    bx, by = float(values["x1"]), float(values["y1"])
    length = math.hypot(bx - ax, by - ay)
    if not length > 0.0:
        raise ValueError("axis line has zero length (鏡映軸の向きが決まらない)")
    return (axis_sketch, axis_entity, ax, ay, (bx - ax) / length, (by - ay) / length)


def _mirrored_plan(
    doc: Document, normalized: Sequence[tuple], ax: float, ay: float, ux: float, uy: float, *, fresh: bool
) -> List[tuple]:
    """検査してから返す計画 `[(sketch, kind, values), ...]`(対象と同じ順序。書き込みより前に全部検査する)。"""
    planned: List[tuple] = []
    for sketch_id, entity_id in normalized:
        sketch = get_sketch(doc, sketch_id)
        entity = get_entity(sketch, entity_id)
        current = parameters.resolve_all(doc, entity.params)
        values = dict(current)
        values.update(entity_kind_ops(entity.kind).mirror(current, ax, ay, ux, uy))
        # 元が持っているキーだけで比べる(`angle` を持たない時代のログから再生した長方形は
        # `values` のほうがキーが1つ多く、素の辞書比較では常に不一致になる)。
        if overlaps_original(values, current, fresh=fresh):
            # 対象が軸の上に乗っている。元と重なる複製は作らない(領域検出が同じ辺を2本見る)。
            raise ValueError(
                f"mirror_sketch_entities: 複製 {entity_id!r} が元と完全に重なる"
                "(対象が鏡映軸の上に乗っている)"
            )
        require_valid_params(entity.kind, values)
        planned.append((sketch, entity.kind, values))
    return planned


def _create_mirrors(
    doc: Document,
    normalized: Sequence[tuple],
    ax: float,
    ay: float,
    ux: float,
    uy: float,
    new_ids: Sequence[str],
    *,
    fresh: bool,
) -> List[str]:
    """検査済みの計画から複製を作って追加し、作った entity_id の並びを返す(do も redo/replay もここを通る)。"""
    plan = _mirrored_plan(doc, normalized, ax, ay, ux, uy, fresh=fresh)
    if len(new_ids) != len(plan):
        raise ValueError(
            f"mirror_sketch_entities: {len(plan)} copies planned but "
            f"{len(new_ids)} ids recorded"
        )
    return add_entities(doc, [(sketch, new_id, kind, values) for (sketch, kind, values), new_id in zip(plan, new_ids)])


def mirror_sketch_entities(doc: Document, targets: Sequence[dict], axis: Any) -> Operation:
    """選択のスケッチ図形を、既存の線分 `axis` を軸に鏡映複製する(常に複製)。

    軸自身が対象に混ざっていてもよく、軸に使った図形は対象から黙って外す。失敗したときは
    文書にもログにも何も残さない。
    """
    normalized = _normalize_targets(doc, targets)
    axis_sketch, axis_entity, ax, ay, ux, uy = _axis_line(doc, axis)
    normalized = [item for item in normalized if item != (axis_sketch, axis_entity)]
    if not normalized:
        raise ValueError(
            "mirror_sketch_entities: 対象が鏡映軸自身しか無い"
            "(軸に使った図形は対象から自動的に外れるので、他に対象が要る)"
        )
    new_ids = [new_entity_id() for _ in normalized]
    created = _create_mirrors(doc, normalized, ax, ay, ux, uy, new_ids, fresh=True)

    return doc.log.append(
        VERB_MIRROR_SKETCH_ENTITIES,
        {
            "targets": [{"sketch": s, "entity": e} for s, e in normalized],
            "axis_p0": [ax, ay],
            "axis_dir": [ux, uy],
            "created": created,
        },
    )


def _do_mirror_sketch_entities(document: Document, operation: Operation) -> None:
    """redo / replay: 記録された対象・軸で鏡映し直す(ログには触らない)。"""
    args = operation.args
    ax, ay = (float(value) for value in args["axis_p0"])
    ux, uy = (float(value) for value in args["axis_dir"])
    normalized = [(item["sketch"], item["entity"]) for item in args["targets"]]
    _create_mirrors(document, normalized, ax, ay, ux, uy, list(args.get("created") or []), fresh=False)


def _undo_mirror_sketch_entities(document: Document, operation: Operation) -> None:
    """undo: 作った複製を消す(元の図形・軸は触らない)。"""
    remove_entities(document, operation.args.get("created") or [])


register_verb(
    VERB_MIRROR_SKETCH_ENTITIES,
    do=_do_mirror_sketch_entities,
    undo=_undo_mirror_sketch_entities,
)


# --------------------------------------------------------------------------------------
# 入口(`duocad/entry.py`) —— 対象を選択から取り、上の verb を掛け、応答に載せるものを返す
# --------------------------------------------------------------------------------------
@entry(VERB_MIRROR_SKETCH_ENTITIES)
def _enter_mirror_sketch_entities(document: Document, axis: Any) -> dict:
    targets = selection.require_sketch_entities(document)
    sketch_ids = sorted({str(item["sketch"]) for item in targets})
    args = mirror_sketch_entities(document, targets, axis).args
    return {
        "axis_p0": args["axis_p0"],
        "axis_dir": args["axis_dir"],
        "created": list(args["created"]),
        "sketch_ids": sketch_ids,
    }
