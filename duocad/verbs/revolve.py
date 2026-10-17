"""verb `revolve` — スケッチの**領域**を、同じスケッチ平面上の軸のまわりに回して立体を作る(回転体)。

ボディを回して置き直す「動かす」の回転(`move_body`)とは別物。押し出し(`extrude`)の兄弟で、
領域の解決・行き先の決め方・`join` / `cut` / `new` の意味と既定は押し出しと同じ。

軸(`axis`)は次のどれか。どれもスケッチ平面の上にある:
- `{"sketch": sketch_id, "entity": entity_id}` … 領域と**同じスケッチ**の線分(無限に延ばした直線を軸にする)
- `"x"` / `"y"` … スケッチの原点を通る x / y 軸

ログには軸を参照のまま載せ、再生のたびに引き直す(領域の参照と同じく、線分の寸法を後から変えると
回転体も追従する)。

角度 `angle` は度で `0 < |angle| <= 360`。正は軸の向き(線分なら始点→終点、x / y 軸なら正の向き)まわりの
右ねじ、負はその逆。`symmetric` ならスケッチ平面から両側へ半分ずつ回す。

失敗(領域が空・軸が別スケッチ・軸が線分でない・軸が領域を横切る・角度が範囲外・ブーリアン失敗)は
例外を送出し、documentにもログにも変更を残さない。
"""
from __future__ import annotations

import math
from typing import Any, Optional, Sequence

from .. import kernel_extrude, kernel_sketch, parameters, selection
from ..document import BodyId, Document, new_body_id
from ..entry import entry
from ..oplog import Operation, register_verb
from ..region import RegionRef
from ..sketch import get_entity, get_sketch
from ._common import recall, remember
from .extrude import (
    MODES,
    _add_new_body,
    _add_numbered_body,
    _editable_target,
    _recorded_name,
    _resolve_faces_and_normal,
    _resolve_target,
)

VERB_REVOLVE = "revolve"

#: スケッチの軸の名前 → スケッチ座標での向き(どちらも原点を通る)
SKETCH_AXES = {"x": (1.0, 0.0), "y": (0.0, 1.0)}


def revolve(
    doc: Document,
    regions: Sequence[RegionRef],
    axis: Any,
    angle: float = 360.0,
    mode: str = "join",
    body_id: Optional[BodyId] = None,
    symmetric: bool = False,
) -> Operation:
    angle = _checked_angle(angle)
    if mode not in MODES:
        raise ValueError(f"unknown revolve mode: {mode!r} (one of {list(MODES)})")
    refs = list(regions)
    if not refs:
        raise ValueError("revolve requires at least one region")
    axis = _normalized_axis(axis)

    target_body, resolved_body_id = _resolve_target(doc, mode, body_id, what=VERB_REVOLVE)
    result = _revolve_shape(
        doc, refs, axis, angle, mode, bool(symmetric), None if target_body is None else target_body.shape
    )

    created_new = False
    previous_shape: Any = None
    numbered: dict = {}
    if target_body is None:
        resolved_body_id = new_body_id()
        numbered = _add_numbered_body(doc, resolved_body_id, result["shape"])
        created_new = True
    else:
        previous_shape = target_body.shape
        target_body.shape = result["shape"]

    operation = doc.log.append(
        VERB_REVOLVE,
        {
            "regions": [ref.to_dict() for ref in refs],
            "axis": axis,
            "angle": angle,
            "symmetric": bool(symmetric),
            "mode": mode,
            "body_id": resolved_body_id,
            **numbered,
        },
    )
    operation.provenance = result["provenance"]
    remember(doc, resolved_body_id, operation.seq, None if created_new else previous_shape)
    return operation


def _checked_angle(angle: Any) -> float:
    value = float(angle)
    if not math.isfinite(value) or value == 0.0 or abs(value) > 360.0:
        raise ValueError(f"angle must satisfy 0 < |angle| <= 360 degrees (got {angle!r})")
    return value


def _normalized_axis(axis: Any) -> Any:
    """軸の指定をログに載せる形へ(`"x"` / `"y"` か `{"sketch", "entity"}`)。形が違えば ValueError。"""
    if isinstance(axis, str) and axis in SKETCH_AXES:
        return axis
    if isinstance(axis, dict) and "sketch" in axis and "entity" in axis:
        return {"sketch": str(axis["sketch"]), "entity": str(axis["entity"])}
    raise ValueError(
        'axis must be "x", "y", or a line reference like {"sketch": "<sketch_id>", "entity": "<entity_id>"}, '
        f"got {axis!r}"
    )


def _axis_in_sketch(doc: Document, sketch_id: str, axis: Any) -> tuple:
    """軸をスケッチ座標の `(点, 単位方向)` へ解決する。別スケッチの線分・線分以外は ValueError。"""
    if isinstance(axis, str):
        return (0.0, 0.0), SKETCH_AXES[axis]
    if axis["sketch"] != sketch_id:
        raise ValueError(
            f"revolve axis must be a line of the regions' sketch {sketch_id!r}, got sketch {axis['sketch']!r}"
        )
    entity = get_entity(get_sketch(doc, sketch_id), axis["entity"])
    if entity.kind != "line":
        raise ValueError(f"revolve axis must reference a line entity, got kind={entity.kind!r}")
    values = parameters.resolve_all(doc, entity.params)
    ax, ay = float(values["x0"]), float(values["y0"])
    bx, by = float(values["x1"]), float(values["y1"])
    length = math.hypot(bx - ax, by - ay)
    if not length > 0.0:
        raise ValueError("revolve axis line has zero length (軸の向きが決まらない)")
    return (ax, ay), ((bx - ax) / length, (by - ay) / length)


def _revolve_shape(
    doc: Document,
    refs: Sequence[RegionRef],
    axis: Any,
    angle: float,
    mode: str,
    symmetric: bool,
    target_shape: Any,
) -> dict:
    """領域と軸を解決して回転体を作り、相手へ join / cut する(文書には触らない)。"""
    faces, normal = _resolve_faces_and_normal(doc, refs, what=VERB_REVOLVE)
    sketch_id = refs[0].sketch
    (pu, pv), (du, dv) = _axis_in_sketch(doc, sketch_id, axis)
    # 法線と同じく、作成時に焼き付けた枠から取る(`_resolve_faces_and_normal` の注記)。
    frame = kernel_sketch.sketch_plane_frame(get_sketch(doc, sketch_id).plane)
    origin, u, v = frame["origin"], frame["u"], frame["v"]
    point = [origin[i] + pu * u[i] + pv * v[i] for i in range(3)]
    direction = [du * u[i] + dv * v[i] for i in range(3)]
    return kernel_extrude.revolve_regions(
        faces, point, direction, normal, angle, mode, target_shape=target_shape, symmetric=symmetric
    )


# --------------------------------------------------------------------------------------
# verbの登録
# --------------------------------------------------------------------------------------
def _do_revolve(document: Document, operation: Operation) -> None:
    """redo / replay 時の再適用。**ログには触らない**(触ると二重記録になる)。"""
    args = operation.args
    body_id = args["body_id"]
    mode = args["mode"]
    refs = [RegionRef.from_dict(item) for item in args["regions"]]
    shape_args = (refs, args["axis"], float(args["angle"]), mode, bool(args.get("symmetric", False)))

    existing = next((candidate for candidate in document.bodies if candidate.id == body_id), None)
    if mode == "new" and existing is not None:
        raise ValueError(f"cannot replay new: body {body_id!r} already exists")
    if existing is None and mode == "cut":
        raise ValueError(f"cannot replay {mode} without target body {body_id!r}")
    if mode == "new" or existing is None:
        result = _revolve_shape(document, *shape_args, None)
        _add_new_body(document, body_id, result["shape"], _recorded_name(document, args))
        remember(document, body_id, operation.seq, None)
    else:
        target = _editable_target(document, body_id, what=VERB_REVOLVE)
        previous_shape = target.shape
        result = _revolve_shape(document, *shape_args, previous_shape)
        target.shape = result["shape"]
        remember(document, body_id, operation.seq, previous_shape)
    if operation.provenance is None:
        operation.provenance = result["provenance"]


def _undo_revolve(document: Document, operation: Operation) -> None:
    """undo: 回す前の形状に戻す(新しく作ったボディなら取り除く)。"""
    body_id = operation.args["body_id"]
    previous = recall(document, body_id, operation.seq)
    if previous is None:
        document.remove_body(body_id)
        return
    document.get_body(body_id).shape = previous


register_verb(VERB_REVOLVE, do=_do_revolve, undo=_undo_revolve)


# --------------------------------------------------------------------------------------
# 入口(`duocad/entry.py`) —— 対象を選択から取り、上の verb を掛け、応答に載せるものを返す
# --------------------------------------------------------------------------------------
@entry(VERB_REVOLVE)
def _enter_revolve(
    document: Document,
    axis: Any,
    angle: float = 360.0,
    mode: str = "join",
    symmetric: bool = False,
    body_id: Optional[BodyId] = None,
) -> dict:
    """選択の領域を回す。軸は選択ではなく引数(`mirror_sketch_entities` の軸と同じ別チャンネル)。"""
    refs = [RegionRef.from_dict(item) for item in selection.require_selected(document, selection.REGION_KIND)]
    operation = revolve(
        document, refs, axis, float(angle), mode=str(mode), body_id=body_id, symmetric=bool(symmetric)
    )
    changed = operation.args["body_id"]
    return {
        "body_id": changed,
        "angle": operation.args["angle"],
        "mode": str(mode),
        "changed_bodies": [changed],
    }
