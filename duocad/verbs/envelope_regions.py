"""verb `envelope_regions` — 選択した複数の領域を、へこみに沿って包む**単一の閉じた領域線**をスケッチに描く。

結果 = D(offset)( D(-radius)( D(radius)(選択領域の和集合) ) )。D(r) は半径 r の円との
ミンコフスキー和(負は縮小)。前半の D(-radius)∘D(radius) はクロージングで、幅 2*radius 未満の
隙間とへこみを埋め、それより広いへこみには沿う(凸包ではない)。`offset` は正で外へ・負で内へ。
幾何は OCCT の2Dオフセット(角は円弧)で正確に取り、出力は**線分と円弧(全周は円)だけ**
(`kernel_sketch.envelope_faces`)。

- 結果の面がちょうど1枚のときだけ成功する。内側に穴を持つ1枚も単一とみなす。2枚以上は分裂
  として、何個に分かれたかを添えて ValueError。**必要な radius・offset の最小値は求めない**。
- 空になったら(offset < 0 が大きすぎる)別の文で ValueError。
- 描き足す先は選択領域が属する**同じスケッチ**で、元の領域の線は残す。複数のスケッチにまたがる
  選択は ValueError(領域はスケッチごとに別平面で、包む平面が決まらない)。
- **ログには解決済みの図形の数値を載せる**。redo / replay は領域を引き直さず、記録した図形を
  記録した ID でそのまま足す(領域の添字は後の編集で指す先が変わりうるため。`mirror_sketch` と同じ)。

**OCP / build123d を直接importしない**。
"""
from __future__ import annotations

import math
from typing import List, Sequence, Tuple

from .. import kernel_sketch
from ..document import Document
from ..oplog import Operation, register_verb
from .. import selection
from ..entry import entry
from ..region import RegionRef, _regions_detailed
from ..sketch import (
    add_entities,
    get_sketch,
    new_entity_id,
    remove_entities,
    require_valid_params,
)

VERB_ENVELOPE_REGIONS = "envelope_regions"


def envelope_pieces(
    doc: Document,
    regions: Sequence[RegionRef],
    radius: float = 0.0,
    offset: float = 0.0,
) -> Tuple[str, List]:
    """`regions` を包んだ面の並びと、それが属するスケッチIDを返す。文書は変えない。

    面の数は判定しない(分裂・消失の判定は呼び出し側。プレビューは分裂した形も見せる)。
    """
    radius, offset = float(radius), float(offset)
    if not (math.isfinite(radius) and math.isfinite(offset)):
        raise ValueError(f"envelope_regions: radius と offset は有限の数値(radius={radius!r}, offset={offset!r})")
    if radius < 0.0:
        raise ValueError(f"envelope_regions: radius は 0 以上(追従半径。got {radius!r})")
    refs = list(regions)
    if not refs:
        raise ValueError("envelope_regions requires at least one region")
    sketch_ids = sorted({ref.sketch for ref in refs})
    if len(sketch_ids) > 1:
        raise ValueError(
            f"envelope_regions takes regions of a single sketch, got {sketch_ids!r} "
            "(選択が複数のスケッチにまたがっている。包む領域は同じスケッチのものだけ)"
        )
    sketch_id = sketch_ids[0]

    detailed = _regions_detailed(doc, sketch_id)
    faces: List = []
    for index in sorted({ref.index for ref in refs}):
        match = next((item for item in detailed if item["index"] == index), None)
        if match is None:
            raise ValueError(f"region index out of range: {index} (sketch {sketch_id!r})")
        faces.append(match["face"])

    sketch = get_sketch(doc, sketch_id)
    return sketch_id, kernel_sketch.envelope_faces(sketch.plane, faces, radius, offset)


def envelope_regions(
    doc: Document,
    regions: Sequence[RegionRef],
    radius: float = 0.0,
    offset: float = 0.0,
) -> Operation:
    """`regions`(1つのスケッチの領域)を包んだ単一の領域線を、同じスケッチへ足す。

    失敗したときは文書にもログにも何も残さない。
    """
    radius, offset = float(radius), float(offset)
    refs = list(regions)
    sketch_id, pieces = envelope_pieces(doc, refs, radius, offset)
    if not pieces:
        raise ValueError("オフセットで領域が消えます。d を 0 に近づけてください")
    if len(pieces) > 1:
        raise ValueError(f"{len(pieces)} 個に分かれます。R か d を大きくしてください")

    sketch = get_sketch(doc, sketch_id)
    entities = kernel_sketch.face_boundary_entities(sketch.plane, pieces[0])
    for entity in entities:
        require_valid_params(entity["kind"], entity["params"])
    created = add_entities(
        doc,
        [(sketch, new_entity_id(), entity["kind"], dict(entity["params"])) for entity in entities],
    )

    return doc.log.append(
        VERB_ENVELOPE_REGIONS,
        {
            "sketch_id": sketch_id,
            "regions": [ref.to_dict() for ref in refs],
            "radius": radius,
            "offset": offset,
            "entities": [{"kind": e["kind"], "params": dict(e["params"])} for e in entities],
            "created": created,
        },
    )


def _do_envelope_regions(document: Document, operation: Operation) -> None:
    """redo / replay: 記録された図形を、記録されたIDで足し直す(ログには触らない)。"""
    args = operation.args
    sketch = get_sketch(document, args["sketch_id"])
    add_entities(
        document,
        [
            (sketch, entity_id, entity["kind"], dict(entity["params"]))
            for entity_id, entity in zip(args["created"], args["entities"])
        ],
    )


def _undo_envelope_regions(document: Document, operation: Operation) -> None:
    """undo: 足した図形だけを消す(元の領域の線は触らない)。"""
    remove_entities(document, operation.args["created"], sketch_id=operation.args["sketch_id"])


register_verb(
    VERB_ENVELOPE_REGIONS,
    do=_do_envelope_regions,
    undo=_undo_envelope_regions,
)


# --------------------------------------------------------------------------------------
# 入口(`duocad/entry.py`) —— 対象を確定選択から取り、上の verb を掛け、応答に載せるものを返す
# --------------------------------------------------------------------------------------
def selected_regions(document: Document) -> List[RegionRef]:
    """確定選択の領域(確定とプレビューが同じ取り方をする)。"""
    return [RegionRef.from_dict(item) for item in selection.require_confirmed(document, "region")]


@entry(VERB_ENVELOPE_REGIONS)
def _enter_envelope_regions(document: Document, radius: float = 0.0, offset: float = 0.0) -> dict:
    args = envelope_regions(document, selected_regions(document), float(radius), float(offset)).args
    return {
        "radius": args["radius"],
        "offset": args["offset"],
        "created": list(args["created"]),
        "sketch_ids": [args["sketch_id"]],
    }
