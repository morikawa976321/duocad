"""verb を `Document` へ直に当てるテストが共有する題材と検算(`Session` を通さない)。"""
from __future__ import annotations

import math

from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

from duocad import kernel, query
from duocad.document import Body, Document
from duocad.region import region_ref, regions
from duocad.sketch import add_rectangle, base_plane, sketch_create
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
    """スケッチ(長方形)→押し出しで直方体を1つ作り body_id を返す。`depth`/`height` の既定は `width`。

    長方形の名前つきの値は追加順に `d1`=x / `d2`=y / `d3`=width / `d4`=height。
    """
    sketch_id = sketch_create(doc, base_plane("XY"))
    add_rectangle(doc, sketch_id, (0.0, 0.0), width, width if depth is None else depth)
    return extrude(doc, [region_ref(sketch_id, 1)], width if height is None else height, mode="join").args["body_id"]


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
    """長方形の4隅を順序に依らない集合として比べられる形へ。

    **`angle` と基準の角の組は一意ではない**(同じ4隅を張る取り方が複数ある)ので、値そのものを期待値にしない。
    """
    return sorted((round(u, 9), round(v, 9)) for u, v in kernel.rectangle_corners(params))
