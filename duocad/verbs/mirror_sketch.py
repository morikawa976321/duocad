"""verb `mirror_sketch_entities` — 確定選択のスケッチ図形を、既存の線分1本を軸に鏡映複製する。

**常に複製を作る**(`move_sketch_entities` と違い in-place反転の口は無い —— 本人の依頼は
「鏡像反転コピーを作る」の1点だけ)。対象は**確定選択のスケッチ図形そのもの**
(`move_sketch_entities` と違い、点単位は扱わない —— 鏡映は図形全体の形を変える操作であり、
寸法編集ではないため)。

鏡映軸は**既存の線分1本**への参照 `{"sketch": sketch_id, "entity": entity_id}`。
**対象の確定選択とは別引数で受ける**(`move_body.axis_edge` と同じ理由 —— 対象の選択集合に
軸の線分を混ぜると、それが「鏡映される側」か「軸」かをコアが区別できない)。軸自身が対象の
選択に混ざっていてもよい —— **軸に使った図形は対象から黙って外す**(直線は軸まわりに
常に自分自身へ映るので、鏡映しても「元と完全に重なる複製」にしかならない。軸以外の対象は
そのまま鏡映する)。呼び出し前に軸の2端点の座標(u, v)へ解決し、ログには解決済みの数値
`{"axis_p0", "axis_dir"}` だけを載せる(軸に使った線分が対象に混ざっている場合、複製後に
参照を引き直すと別の線分になるため)。

種別が変わる結果(長方形が軸に対して斜めで、鏡映すると軸平行を保てない場合)は
`move_sketch_entities` の回転と同じ方針(モジュールdocstring参照) —— 黙って線分4本に
化けさせず ValueError。軸が水平・垂直(長方形の辺と平行/垂直)なら型は保たれるので許容。

**OCP / build123d を直接importしない**(幾何は `duocad.sketch` の表越し)。
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Sequence
from uuid import uuid4

from .. import parameters
from ..document import Document
from ..oplog import Operation, register_verb
from ..sketch import Entity, entity_kind_ops, get_entity, get_sketch, require_valid_params

VERB_MIRROR_SKETCH_ENTITIES = "mirror_sketch_entities"


def _normalize_targets(doc: Document, targets: Sequence[dict]) -> List[tuple]:
    """選択の参照を `(sketch_id, entity_id)` の重複なしの並びへ畳む。

    **点は見ない**(モジュールdocstring) —— 鏡映は図形全体を対象にする操作で、
    `move_sketch_entities` のような点単位の選択は無い。存在しないスケッチ・図形は KeyError。
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


def _resolved_params(doc: Document, entity: Entity) -> Dict[str, float]:
    return {key: parameters.resolve(doc, ref) for key, ref in entity.params.items()}


def _axis_line(doc: Document, axis: Any) -> tuple:
    """軸への参照を `(sketch_id, entity_id, ax, ay, ux, uy)` にする(点+単位方向は解決済み)。

    `kind` が `"line"` でなければ ValueError(それ以外の図形は鏡映軸にできない)。
    `sketch_id`/`entity_id` を一緒に返すのは、**軸自身が対象に混ざっていたら弾かず外す**
    (`mirror_sketch_entities` 参照)ための識別に使うためである。
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
    values = _resolved_params(doc, entity)
    ax, ay = float(values["x0"]), float(values["y0"])
    bx, by = float(values["x1"]), float(values["y1"])
    length = math.hypot(bx - ax, by - ay)
    if not length > 0.0:
        raise ValueError("axis line has zero length (鏡映軸の向きが決まらない)")
    return (axis_sketch, axis_entity, ax, ay, (bx - ax) / length, (by - ay) / length)


def _mirrored_plan(
    doc: Document, normalized: Sequence[tuple], ax: float, ay: float, ux: float, uy: float
) -> List[tuple]:
    """検査してから返す計画 `[(sketch, kind, values), ...]`(対象と同じ順序)。

    **書き込みより前に全部検査する** —— 途中で1つでも失敗したら文書に何も残らない。
    """
    planned: List[tuple] = []
    for sketch_id, entity_id in normalized:
        sketch = get_sketch(doc, sketch_id)
        entity = get_entity(sketch, entity_id)
        current = _resolved_params(doc, entity)
        results = entity_kind_ops(entity.kind).mirror(current, ax, ay, ux, uy)
        if len(results) != 1 or str(results[0]["kind"]) != entity.kind:
            kinds = [str(item["kind"]) for item in results]
            raise ValueError(
                f"mirror_sketch_entities: {entity.kind} はこの軸では鏡映できない"
                f"(種別が {kinds} に変わる)。軸を水平・垂直(その図形の辺と平行/垂直)に"
                "するか、線分で描き直してから鏡映すること"
            )
        values = dict(results[0]["params"])
        if values == current:
            # 対象が軸の上に乗っている(鏡映しても動かない)。**元と重なる複製は作らない**
            # (`move_sketch_entities._copy` と同じ理由 —— 領域検出が同じ辺を2本見る)。
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
) -> List[str]:
    """検査済みの計画から複製を作って追加し、作った entity_id の並びを返す。

    **do(初回呼び出し)も redo/replay もここを通る**(`move_sketch_entities._copy` と同じ形)。
    """
    plan = _mirrored_plan(doc, normalized, ax, ay, ux, uy)
    if len(new_ids) != len(plan):
        raise ValueError(
            f"mirror_sketch_entities: {len(plan)} copies planned but "
            f"{len(new_ids)} ids recorded"
        )
    created: List[str] = []
    for (sketch, kind, values), new_id in zip(plan, new_ids):
        if any(existing.id == new_id for existing in sketch.entities):
            raise ValueError(f"entity id already exists: {new_id!r}")
        refs = {key: parameters.register_value(doc, value) for key, value in values.items()}
        sketch.entities.append(Entity(id=new_id, kind=kind, params=refs))
        created.append(new_id)
    return created


def mirror_sketch_entities(doc: Document, targets: Sequence[dict], axis: Any) -> Operation:
    """確定選択のスケッチ図形を、既存の線分 `axis` を軸に鏡映複製する(常に複製)。

    軸自身が対象の選択に混ざっていてもよい —— **軸に使った図形自身は対象から黙って外す**
    (直線は軸まわりに常に自分自身へ映るので、鏡映しても「元と完全に重なる複製」にしか
    ならない。断らずに、軸以外の対象だけを鏡映する)。長方形が軸に対して斜めだと種別を
    保てず ValueError(軸が水平・垂直=長方形の辺と平行/垂直なら通る)。失敗したときは
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
    new_ids = [f"entity-{uuid4().hex[:12]}" for _ in normalized]
    created = _create_mirrors(doc, normalized, ax, ay, ux, uy, new_ids)

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
    _create_mirrors(document, normalized, ax, ay, ux, uy, list(args.get("created") or []))


def _undo_mirror_sketch_entities(document: Document, operation: Operation) -> None:
    """undo: 作った複製を消す(元の図形・軸は触らない)。"""
    created = set(operation.args.get("created") or [])
    for sketch in document.sketches:
        remaining = []
        for entity in sketch.entities:
            if entity.id in created:
                parameters.release_param_refs(document, entity.params)
            else:
                remaining.append(entity)
        sketch.entities = remaining


register_verb(
    VERB_MIRROR_SKETCH_ENTITIES,
    do=_do_mirror_sketch_entities,
    undo=_undo_mirror_sketch_entities,
)
