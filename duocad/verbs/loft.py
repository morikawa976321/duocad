"""verb `loft` — 別々の平面にある断面(スケッチの**領域**)を、並べた順になめらかにつないで立体を作る(ロフト)。

押し出し(`extrude`)・回転体(`revolve`)の兄弟で、領域の解決・行き先の決め方・`join` / `cut` / `new` の意味と
既定は押し出しと同じ。

- `sections` … 断面の領域の並び(順番どおりにつなぐ)。断面ごとに別の平面に置く。穴のある領域は拒む
- `start_point` / `end_point` … 省略可。スケッチの図形の点(`{"sketch", "entity", "point"}`。線分の端点・円の中心など)
  を指すと、最初の断面の前・最後の断面の後をその点へ収束させる。断面1つと点1つでもよい
- `shifts` … 省略可。断面ごとの整数。自動で揃えた始点を頂点 k 個ぶん進める(負は戻す。頂点の無い円・楕円は
  90° 単位)。自動の対応が意図と違う(ねじれた・潰れた)ときの逃げ道

ログには断面と点を参照のまま載せ、再生のたびに引き直す(寸法を後から変えるとロフトも追従する)。

失敗(断面が足りない・同じ平面に並んだ・穴がある・潰れた・自己交差した・ブーリアン失敗)は例外を送出し、
documentにもログにも変更を残さない。
"""
from __future__ import annotations

from typing import Any, List, Optional, Sequence

from .. import kernel_extrude, kernel_sketch, parameters, selection
from ..document import BodyId, Document, new_body_id
from ..entry import entry
from ..oplog import Operation, register_verb
from ..region import RegionRef
from ..sketch import entity_point_pair, get_entity, get_sketch
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

VERB_LOFT = "loft"


def loft(
    doc: Document,
    sections: Sequence[RegionRef],
    mode: str = "join",
    body_id: Optional[BodyId] = None,
    start_point: Any = None,
    end_point: Any = None,
    shifts: Optional[Sequence[int]] = None,
) -> Operation:
    if mode not in MODES:
        raise ValueError(f"unknown loft mode: {mode!r} (one of {list(MODES)})")
    refs = list(sections)
    if not refs:
        raise ValueError("loft requires at least one section")
    start = _normalized_point(start_point, "start_point")
    end = _normalized_point(end_point, "end_point")
    recorded_shifts = _normalized_shifts(shifts, len(refs))

    target_body, resolved_body_id = _resolve_target(doc, mode, body_id, what=VERB_LOFT)
    result = _loft_shape(
        doc, refs, start, end, recorded_shifts, mode, None if target_body is None else target_body.shape
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
        VERB_LOFT,
        {
            "sections": [ref.to_dict() for ref in refs],
            "start_point": start,
            "end_point": end,
            "shifts": recorded_shifts,
            "mode": mode,
            "body_id": resolved_body_id,
            **numbered,
        },
    )
    operation.provenance = result["provenance"]
    remember(doc, resolved_body_id, operation.seq, None if created_new else previous_shape)
    return operation


def _normalized_point(point: Any, label: str) -> Optional[dict]:
    """点の指定をログに載せる形へ(`None` か `{"sketch", "entity", "point"}`)。形が違えば ValueError。"""
    if point is None:
        return None
    if isinstance(point, dict) and point.get("sketch") and point.get("entity") and point.get("point"):
        return {"sketch": str(point["sketch"]), "entity": str(point["entity"]), "point": str(point["point"])}
    raise ValueError(
        f'{label} must be a sketch point like {{"sketch": "<sketch_id>", "entity": "<entity_id>", "point": "x0"}}, '
        f"got {point!r}"
    )


def _normalized_shifts(shifts: Optional[Sequence[int]], count: int) -> List[int]:
    if shifts is None:
        return [0] * count
    values = list(shifts)
    if len(values) != count:
        raise ValueError(f"shifts must have one entry per section ({count}), got {len(values)}: {values!r}")
    out = []
    for value in values:
        if isinstance(value, bool) or int(value) != value:
            raise ValueError(f"shifts must be integers, got {values!r}")
        out.append(int(value))
    return out


def _point_in_space(doc: Document, point: dict) -> List[float]:
    """スケッチの図形の点を3D座標へ解決する。"""
    sketch = get_sketch(doc, point["sketch"])
    entity = get_entity(sketch, point["entity"])
    x_key, y_key = entity_point_pair(entity.kind, point["point"])
    values = parameters.resolve_all(doc, entity.params)
    u, v = float(values[x_key]), float(values[y_key])
    frame = kernel_sketch.sketch_plane_frame(sketch.plane)
    return [frame["origin"][i] + u * frame["u"][i] + v * frame["v"][i] for i in range(3)]


def _loft_shape(
    doc: Document,
    refs: Sequence[RegionRef],
    start: Optional[dict],
    end: Optional[dict],
    shifts: Sequence[int],
    mode: str,
    target_shape: Any,
) -> dict:
    """断面と点を解決してロフトを作り、相手へ join / cut する(文書には触らない)。"""
    sections = []
    for ref in refs:
        faces, normal = _resolve_faces_and_normal(doc, [ref], what=VERB_LOFT)
        sections.append((faces[0], normal))
    return kernel_extrude.loft_regions(
        sections,
        mode,
        target_shape=target_shape,
        start_point=None if start is None else _point_in_space(doc, start),
        end_point=None if end is None else _point_in_space(doc, end),
        shifts=shifts,
    )


# --------------------------------------------------------------------------------------
# verbの登録
# --------------------------------------------------------------------------------------
def _do_loft(document: Document, operation: Operation) -> None:
    """redo / replay 時の再適用。**ログには触らない**(触ると二重記録になる)。"""
    args = operation.args
    body_id = args["body_id"]
    mode = args["mode"]
    refs = [RegionRef.from_dict(item) for item in args["sections"]]
    shape_args = (refs, args.get("start_point"), args.get("end_point"), list(args.get("shifts") or [0] * len(refs)), mode)

    existing = next((candidate for candidate in document.bodies if candidate.id == body_id), None)
    if mode == "new" and existing is not None:
        raise ValueError(f"cannot replay new: body {body_id!r} already exists")
    if existing is None and mode == "cut":
        raise ValueError(f"cannot replay {mode} without target body {body_id!r}")
    if mode == "new" or existing is None:
        result = _loft_shape(document, *shape_args, None)
        _add_new_body(document, body_id, result["shape"], _recorded_name(document, args))
        remember(document, body_id, operation.seq, None)
    else:
        target = _editable_target(document, body_id, what=VERB_LOFT)
        previous_shape = target.shape
        result = _loft_shape(document, *shape_args, previous_shape)
        target.shape = result["shape"]
        remember(document, body_id, operation.seq, previous_shape)
    if operation.provenance is None:
        operation.provenance = result["provenance"]


def _undo_loft(document: Document, operation: Operation) -> None:
    """undo: ロフトの前の形状に戻す(新しく作ったボディなら取り除く)。"""
    body_id = operation.args["body_id"]
    previous = recall(document, body_id, operation.seq)
    if previous is None:
        document.remove_body(body_id)
        return
    document.get_body(body_id).shape = previous


register_verb(VERB_LOFT, do=_do_loft, undo=_undo_loft)


# --------------------------------------------------------------------------------------
# 入口(`duocad/entry.py`) —— 断面を選択から(選んだ順に)取り、上の verb を掛け、応答に載せるものを返す
# --------------------------------------------------------------------------------------
@entry(VERB_LOFT)
def _enter_loft(
    document: Document,
    mode: str = "join",
    start_point: Optional[dict] = None,
    end_point: Optional[dict] = None,
    shifts: Optional[List[int]] = None,
    body_id: Optional[BodyId] = None,
) -> dict:
    """選択の領域を、選んだ順に断面としてつなぐ。点と shifts は選択ではなく引数。"""
    refs = [RegionRef.from_dict(item) for item in selection.require_selected(document, selection.REGION_KIND)]
    operation = loft(
        document,
        refs,
        mode=str(mode),
        body_id=body_id,
        start_point=start_point,
        end_point=end_point,
        shifts=shifts,
    )
    changed = operation.args["body_id"]
    return {
        "body_id": changed,
        "sections": len(refs),
        "mode": str(mode),
        "changed_bodies": [changed],
    }
