"""verb を `Document` へ直に当てるテストが共有する題材と検算(`Session` を通さない)。"""
from __future__ import annotations

import math
from uuid import uuid4

from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

from duocad import kernel, kernel_sketch, query
from duocad.document import Body, Document
from duocad.oplog import OpLog, verb_handler
from duocad.region import region_ref, regions
from duocad.sketch import add_circle, add_rectangle, base_plane, sketch_create
from duocad.verbs import extrude

#: `box_shape()` の既定の寸法[mm]。3辺が違うので、述語や辺の取り違えがたまたま一致して緑になる余地が減る。
BOX_X, BOX_Y, BOX_Z = 30.0, 30.0, 40.0


def box_shape(dx: float = BOX_X, dy: float = BOX_Y, dz: float = BOX_Z):
    """原点に角を置いた直方体(面6・辺12・頂点8)。"""
    return BRepPrimAPI_MakeBox(dx, dy, dz).Shape()


def document_with_box() -> Document:
    """`id="box"` の `box_shape()` を1つ持つ文書。"""
    document = Document()
    document.add_body(Body(id="box", name="box", kind="solid", shape=box_shape()))
    return document


def new_box(doc: Document, width: float = 20.0, depth: float | None = None, height: float | None = None) -> str:
    """スケッチ(長方形)→押し出しで直方体を1つ作り body_id を返す。`depth`/`height` の既定は `width`。"""
    sketch_id = sketch_create(doc, base_plane("XY"))
    add_rectangle(doc, sketch_id, (0.0, 0.0), width, width if depth is None else depth)
    return extrude(doc, [region_ref(sketch_id, 1)], width if height is None else height, mode="join").args["body_id"]


def new_box_with_hole(doc: Document, size: float = 20.0, radius: float = 4.0) -> str:
    """`new_box` の中央に半径 `radius` の貫通穴を切った立方体(円筒面1・平面6)。"""
    body_id = new_box(doc, size)
    sketch_id = sketch_create(doc, base_plane("XY"))
    add_circle(doc, sketch_id, (size / 2, size / 2), 2 * radius)
    extrude(doc, [region_ref(sketch_id, 1)], size, mode="cut", body_id=body_id)
    return body_id


def second_body(document: Document) -> str:
    """10×10×5 を `mode="new"` でもう1つ作り body_id を返す(undo すると `document.remove_body` で
    完全に消える経路。`duocad/verbs/extrude.py` の `_undo_extrude`)。"""
    sketch_id = sketch_create(document, "XY")
    add_rectangle(document, sketch_id, (0.0, 0.0), 10.0, 10.0)
    boss = regions(document, sketch_id)
    return extrude(document, [region_ref(sketch_id, boss[0].index)], 5.0, mode="new").args["body_id"]


def replay_through_json(document: Document, into: Document | None = None) -> Document:
    """操作ログを JSON へ往復させ、**束ねてから再生**した文書を返す(製品が開くときと同じ順。
    `Session._replace_document` / `OpLog.replay`)。束ねずに再生すると `set_parameter` を含むログは
    `ValueError` で止まる(`duocad/parameters.py`)。`into` は再生の土台(既定は空の文書)。"""
    restored = Document() if into is None else into
    restored.log = OpLog.from_json(document.log.to_json())
    restored.log.replay(restored)
    return restored


def document_state(doc: Document) -> dict:
    """undo 直後の文書と、同じログを再生した文書を `==` で比べるための写し。
    active ボディ・ボディの並び・名前つきの値の表・図形と反復の `params`(名前つきの値への参照のまま)。"""
    active = doc.active_body()
    return {
        "active": None if active is None else active.id,
        "bodies": [(body.id, body.name) for body in doc.bodies],
        "parameters": dict(doc.parameters),
        "sketches": [
            (
                sketch.id,
                sketch.name,
                [(entity.id, entity.kind, dict(entity.params)) for entity in sketch.entities],
                [(pattern.id, dict(pattern.params)) for pattern in sketch.patterns],
            )
            for sketch in doc.sketches
        ],
    }


def assert_matches_replay(doc: Document, into: Document | None = None) -> None:
    """生きている文書の状態が、同じ操作ログを JSON へ往復させて頭から再生した文書の状態と(座標は bit 単位で)一致する。
    undo は「その操作が無かった文書」へ戻すので、undo の直後に呼ぶ。"""
    assert document_state(doc) == document_state(replay_through_json(doc, into))


def kinds_of(doc: Document, body_id: str) -> dict:
    """面の種別ごとの枚数(0枚の種別は落とす)。"""
    described = query.describe_shape(doc.get_body(body_id).shape)
    return {kind: count for kind, count in described["faces_by_type"].items() if count}


def edges_of_kind(doc: Document, body_id: str, kind: str) -> list:
    return [edge["index"] for edge in query.describe_edges(doc.get_body(body_id).shape) if edge["kind"] == kind]


def all_edges(doc: Document, body_id: str) -> list:
    return [edge["index"] for edge in query.describe_edges(doc.get_body(body_id).shape)]


def assert_same_frame(actual: dict, expected: dict) -> None:
    """スケッチ平面の座標系(原点・u・v・法線)が 1e-9 まで一致する。"""
    for key in ("origin", "u", "v", "normal"):
        assert len(actual[key]) == 3
        for got, want in zip(actual[key], expected[key]):
            assert math.isclose(got, want, rel_tol=0.0, abs_tol=1.0e-9), (
                f"{key}: {actual[key]} != {expected[key]}"
            )


def drill_all(document: Document, sketch_id: str, depth: float = 20.0) -> None:
    """スケッチの全領域を `depth` だけ切り取る(既定は厚さ10mmの板を貫く深さ)。"""
    refs = [region_ref(sketch_id, region.index) for region in regions(document, sketch_id)]
    extrude(document, refs, depth, mode="cut")


def hole_axes(shape) -> list:
    """全周円筒面の重心 (x, y)。重心は軸上にあるので穴の位置として使える。"""
    return sorted(
        (round(face["centroid"][0], 6), round(face["centroid"][1], 6))
        for face in kernel.tessellate(shape)["faces"]
        if face["kind"] == "cylinder_full"
    )


def corner_set(params: dict) -> list:
    """長方形の4隅を順序に依らない集合として比べられる形へ。"""
    return sorted((round(u, 9), round(v, 9)) for u, v in kernel_sketch.rectangle_corners(params))


def install_live_pattern(document: Document, verb: str, sketch_id: str, entities: list, **values) -> str:
    """反復を旧形式(スケッチに `Pattern` を置き、描くたびに展開する)で入れ、PatternId を返す。今の verb はこの形を
    作らないので、保存済みの古い文書に残る形を確かめる検査だけが使う。行は保存ログの旧形式と同じ
    (矩形は `dx, dy, nx, ny`、円形は `center, count, total_angle, rotate_copies`)。"""
    pattern_id = f"pattern-{uuid4().hex[:12]}"
    operation = document.log.append(
        verb, {"sketch_id": sketch_id, "pattern_id": pattern_id, "entities": list(entities), **values}
    )
    verb_handler(verb).do(document, operation)
    return pattern_id
