"""面取りの契約。

実測(重要 —— **まっすぐな辺の面取りは円錐にならない**):
"""
from __future__ import annotations

import pytest

from duocad import query
from duocad.document import Document
from duocad.region import region_ref
from duocad.sketch import add_circle, base_plane, sketch_create
from duocad.verbs import chamfer, extrude
from document_support import edges_of_kind, kinds_of, new_box

#: 円筒の円い縁を1つ面取りしたときの実測
CYLINDER_CHAMFER_KINDS = {"plane": 2, "cylinder_full": 1, "cone": 1}


def new_cylinder(doc: Document, radius: float = 10.0, height: float = 5.0) -> str:
    sketch_id = sketch_create(doc, base_plane("XY"))
    add_circle(doc, sketch_id, (0.0, 0.0), 2 * radius)
    return extrude(doc, [region_ref(sketch_id, 1)], height, mode="join").args["body_id"]


def test_chamfering_a_straight_edge_makes_a_plane():
    """箱のまっすぐな辺を面取りすると**平面が1枚増える**(実測: 6面 -> 平面7)。"""
    doc = Document()
    body_id = new_box(doc)
    assert kinds_of(doc, body_id) == {"plane": 6}
    straight = edges_of_kind(doc, body_id, "line")
    assert len(straight) == 12

    chamfer(doc, straight[:1], 2.0, body_id=body_id)

    assert kinds_of(doc, body_id) == {"plane": 7}
    assert "cone" not in kinds_of(doc, body_id)


def test_chamfering_a_circular_edge_makes_a_cone():
    """**円い辺**を面取りすると円錐ができる(実測: 円筒3面 -> 平面2+円筒1+円錐1)。"""
    doc = Document()
    body_id = new_cylinder(doc)
    assert kinds_of(doc, body_id) == {"plane": 2, "cylinder_full": 1}
    circular = edges_of_kind(doc, body_id, "circle")
    assert len(circular) == 2, "円筒の円い縁は上下の2本"

    chamfer(doc, circular[:1], 1.0, body_id=body_id)

    assert kinds_of(doc, body_id) == CYLINDER_CHAMFER_KINDS


def test_chamfer_is_logged_and_undoable():
    doc = Document()
    body_id = new_cylinder(doc)
    kinds_before = kinds_of(doc, body_id)
    volume_before = query.volume(doc.get_body(body_id).shape)
    circular = edges_of_kind(doc, body_id, "circle")

    operation = chamfer(doc, circular[:1], 1.0, body_id=body_id)

    assert operation.verb == "chamfer"
    assert operation.args["edges"] == circular[:1]
    assert operation.args["distance"] == 1.0
    assert operation.args["body_id"] == body_id

    doc.log.undo(doc)
    assert kinds_of(doc, body_id) == kinds_before
    assert query.volume(doc.get_body(body_id).shape) == pytest.approx(volume_before)

    doc.log.redo(doc)
    assert kinds_of(doc, body_id) == CYLINDER_CHAMFER_KINDS


def test_chamfer_records_provenance_through_the_normalizing_adapter():
    """由来は正規化アダプタ経由で埋まる(フィレットと同じ経路)。"""
    doc = Document()
    body_id = new_cylinder(doc)
    circular = edges_of_kind(doc, body_id, "circle")

    operation = chamfer(doc, circular[:1], 1.0, body_id=body_id)

    assert operation.provenance["verb"] == "chamfer"
    # 面取りだけ由来の写像が空でも緑にならない(フィレットは `test_fillet_shifts_face_indices` が中身まで見ている)
    assert operation.provenance["face_map"]
    doc.log.to_json()  # JSON化できない値が混ざっていればここで落ちる
