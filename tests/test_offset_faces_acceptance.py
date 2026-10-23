"""面のオフセット `offset_faces`(#183)の受入条件(docs/FACE_EDIT.md「受入条件」)。

落ちたら、受入条件のどれが満たされていないかを意味する。

期待値の出どころ(Git 3c3fb655 の tools/face_edit_probe で実測、2026-10-18): 40×30×20 の箱の上面 ±5 → 30000 / 18000 mm³。
面取り 5 の箱の上面 +3 → 断面 30×23 から 8×8 の三角を引いて ×40 = 26320 mm³。
丸め r5 の箱の上面 +3 → 40×30×23 から丸めの欠け (25 − 25π/4)×40 を引いた 27385.40 mm³(丸めが付いてくる形)。
"""
from __future__ import annotations

import json
import math

import pytest
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain

import duocad.verbs as verbs
from duocad import catalog, query, selection
from duocad.document import Body, Document
from duocad.entry import entry_for
from duocad.verbs import chamfer, fillet
from document_support import kinds_of, new_box, new_box_with_hole, replay_through_json, shape_ref
from mcp_support import call
from session_support import session_with_box

from duocad import (
    brep_topology as _brep_topology,
)


W, D, H = 40.0, 30.0, 20.0


def _offset_faces(doc, faces, distance, body_id):
    return verbs.offset_faces(doc, faces, distance, body_id=body_id)


def _box(doc: Document) -> str:
    return new_box(doc, W, D, H)


def _plane_at(doc: Document, body_id: str, axis: int, value: float) -> int:
    found = [
        face["index"]
        for face in query.describe_faces(doc.get_body(body_id).shape)
        if face["kind"] == "plane" and abs(face["center"][axis] - value) < 1e-6 and abs(abs(face["normal"][axis]) - 1) < 1e-9
    ]
    assert len(found) == 1, found
    return found[0]


def _top_front_edge(doc: Document, body_id: str) -> int:
    found = [
        edge["index"]
        for edge in query.describe_edges(doc.get_body(body_id).shape)
        if edge["kind"] == "line"
        and all(abs(p[1]) < 1e-6 and abs(p[2] - H) < 1e-6 for p in (edge["start"], edge["end"]))
    ]
    assert len(found) == 1, found
    return found[0]


def _bbox(doc: Document, body_id: str) -> list:
    points = [vertex["point"] for vertex in query.describe_vertices(doc.get_body(body_id).shape)]
    return [min(p[axis] for p in points) for axis in range(3)] + [max(p[axis] for p in points) for axis in range(3)]


def _state(doc: Document) -> tuple:
    return (
        json.dumps(doc.log.to_json(), sort_keys=True),
        tuple((body.id, body.shape, query.volume(body.shape), _brep_topology.face_count(body.shape)) for body in doc.bodies),
    )


def _volume(doc: Document, body_id: str) -> float:
    return query.volume(doc.get_body(body_id).shape)


def _hole(doc: Document, body_id: str) -> int:
    (index,) = [f["index"] for f in query.describe_faces(doc.get_body(body_id).shape) if f["kind"] == "cylinder_full"]
    return index


def _l_block(doc: Document) -> str:
    """40×30×10 の上に 20×30×10 を載せた段(踏み面 z=10 は x∈[20,40]、蹴上げ x=20)。"""
    fused = BRepAlgoAPI_Fuse(
        BRepPrimAPI_MakeBox(40.0, 30.0, 10.0).Shape(), BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 10), 20.0, 30.0, 10.0).Shape()
    ).Shape()
    unifier = ShapeUpgrade_UnifySameDomain(fused, True, True, False)
    unifier.Build()
    doc.add_body(Body(id="step", name="step", kind="solid", shape=unifier.Shape()))
    assert _brep_topology.face_count(doc.get_body("step").shape) == 8
    return "step"


# ---------------------------------------------------------------- Issue の受入条件(案)
@pytest.mark.parametrize("distance", [5.0, -5.0])
def test_moving_a_box_face_out_or_in_keeps_a_box_with_new_size(distance):
    """直方体の1面を外側・内側へ動かすと、隣の4面が伸縮して直方体のまま寸法が変わる。"""
    doc = Document()
    body_id = _box(doc)

    _offset_faces(doc, [_plane_at(doc, body_id, 2, H)], distance, body_id)

    assert _volume(doc, body_id) == pytest.approx(W * D * (H + distance), rel=1e-9)
    assert kinds_of(doc, body_id) == {"plane": 6}
    assert _bbox(doc, body_id) == pytest.approx([0, 0, 0, W, D, H + distance])


@pytest.mark.parametrize("distance, radius", [(1.0, 3.0), (-1.0, 5.0)])
def test_moving_a_hole_face_changes_the_hole_diameter(distance, radius):
    """円筒穴の面を動かすと、穴の直径が変わる(正=外向き法線=穴の軸へ向かうので穴が細る)。"""
    doc = Document()
    body_id = new_box_with_hole(doc, 20.0, 4.0)

    _offset_faces(doc, [_hole(doc, body_id)], distance, body_id)

    assert _volume(doc, body_id) == pytest.approx(8000.0 - math.pi * radius**2 * 20.0, rel=1e-9)
    assert kinds_of(doc, body_id) == {"plane": 6, "cylinder_full": 1}


def test_moving_a_face_next_to_a_slant_extends_the_slant_without_a_gap():
    """斜面(面取り)に接する平面を動かすと、斜面が延長されて隙間なく閉じる。"""
    doc = Document()
    body_id = _box(doc)
    chamfer(doc, [_top_front_edge(doc, body_id)], 5.0, body_id=body_id)

    _offset_faces(doc, [_plane_at(doc, body_id, 2, H)], 3.0, body_id)

    assert _volume(doc, body_id) == pytest.approx((30.0 * 23.0 - 8.0 * 8.0 / 2) * W, rel=1e-9)
    assert kinds_of(doc, body_id) == {"plane": 7}


@pytest.mark.parametrize(
    "build, pick, distance",
    [
        ("box", "top", -25.0),
        ("box", "top", -20.0),
        ("hole", "hole", 4.0),
        ("hole", "hole", -6.5),
    ],
    ids=["below-the-bottom", "zero-thickness", "hole-radius-zero", "hole-breaks-the-wall"],
)
def test_impossible_distances_raise_and_leave_document_and_log_unchanged(build, pick, distance):
    """形が成り立たない距離はエラーになり、文書もログも変わらない。"""
    doc = Document()
    body_id = _box(doc) if build == "box" else new_box_with_hole(doc, 20.0, 4.0)
    face = _plane_at(doc, body_id, 2, H) if pick == "top" else _hole(doc, body_id)
    before = _state(doc)

    with pytest.raises(ValueError):
        _offset_faces(doc, [face], distance, body_id)

    assert _state(doc) == before


def test_replaying_the_log_gives_the_same_shape():
    """記録したログを再生すると同じ形になる(面の並びまで同じ)。"""
    doc = Document()
    body_id = new_box_with_hole(doc, 20.0, 4.0)
    _offset_faces(doc, [_hole(doc, body_id)], -1.0, body_id)

    restored = replay_through_json(doc)

    assert query.describe_faces(restored.get_body(body_id).shape) == query.describe_faces(doc.get_body(body_id).shape)


# ---------------------------------------------------------------- Issue の要件から足したもの
def test_a_face_that_would_vanish_is_refused():
    """動かした結果、隣の面が消える(段の踏み面の幅が0になる)ときは例外にし、文書もログも変えない。
    MakeOffset は面積0の踏み面を位相に残したまま有効な形として返す(docs/FACE_EDIT.md で実測)。"""
    doc = Document()
    body_id = _l_block(doc)
    riser = _plane_at(doc, body_id, 0, 20.0)
    before = _state(doc)

    with pytest.raises(ValueError):
        _offset_faces(doc, [riser], 20.0, body_id)

    assert _state(doc) == before


def test_a_fillet_next_to_the_moved_face_follows_it():
    """フィレットを含む形の寸法直し: 丸めに接する面を動かすと、丸めは同じ半径のまま付いてきて、選んでいない平面はずれない。
    MakeOffset だけでは、接線でつながる面(丸めとその先の前面)まで同じ距離ずらした形を有効な形として返す(実測)。
    由来では、旧い丸めは `deleted`、作り直した丸めは `new_faces`(旧と新を対応させる根拠を持たない)。"""
    doc = Document()
    body_id = _box(doc)
    fillet(doc, [_top_front_edge(doc, body_id)], 5.0, body_id=body_id)
    (old_round,) = [f["index"] for f in query.describe_faces(doc.get_body(body_id).shape) if f["kind"] == "cylinder_partial"]

    operation = _offset_faces(doc, [_plane_at(doc, body_id, 2, H)], 3.0, body_id)

    shape = doc.get_body(body_id).shape
    assert query.volume(shape) == pytest.approx(W * D * (H + 3.0) - (25.0 - 25.0 * math.pi / 4) * W, rel=1e-9)
    assert kinds_of(doc, body_id) == {"plane": 6, "cylinder_partial": 1}
    (new_round,) = [f["index"] for f in query.describe_faces(shape) if f["kind"] == "cylinder_partial"]
    assert BRepAdaptor_Surface(_brep_topology.face_of(shape, new_round)).Cylinder().Radius() == pytest.approx(5.0, abs=1e-9)
    for axis, value in ((1, 0.0), (1, D), (0, 0.0), (0, W), (2, 0.0), (2, H + 3.0)):
        _plane_at(doc, body_id, axis, value)  # 前面は y=0 のまま、上面は z=23
    assert operation.provenance["deleted"] == [old_round]
    assert operation.provenance["new_faces"] == [new_round]


def _full_round_plate(doc: Document) -> str:
    """40×10×15 の板の上を半円筒(r5)にした全丸め。上の曲面は両側の面に接線でつながり、外す(延ばして閉じる)ことができない。"""
    plate = BRepPrimAPI_MakeBox(40.0, 10.0, 15.0).Shape()
    rod = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 5, 15), gp_Dir(1, 0, 0)), 5.0, 40.0).Shape()
    unifier = ShapeUpgrade_UnifySameDomain(BRepAlgoAPI_Fuse(plate, rod).Shape(), True, True, False)
    unifier.Build()
    doc.add_body(Body(id="round", name="round", kind="solid", shape=unifier.Shape()))
    return "round"


def test_a_tangent_neighbour_that_cannot_follow_is_refused():
    """丸めとして付いてこさせられない接線の隣(外せない全丸め)なら、接線でつながる面ごと動かして通さずに例外にし、文書もログも変えない。"""
    doc = Document()
    body_id = _full_round_plate(doc)
    before = _state(doc)

    with pytest.raises(ValueError):
        _offset_faces(doc, [_plane_at(doc, body_id, 1, 0.0)], 1.0, body_id)

    assert _state(doc) == before


def test_two_faces_move_together():
    doc = Document()
    body_id = _box(doc)

    _offset_faces(doc, [_plane_at(doc, body_id, 2, H), _plane_at(doc, body_id, 1, 0.0)], 3.0, body_id)

    assert _bbox(doc, body_id) == pytest.approx([0, -3.0, 0, W, D, H + 3.0])
    assert kinds_of(doc, body_id) == {"plane": 6}


def test_offset_faces_is_logged_undoable_and_redoable():
    doc = Document()
    body_id = _box(doc)
    top = _plane_at(doc, body_id, 2, H)
    entries_before = len(doc.log.entries)

    operation = _offset_faces(doc, [top], 5.0, body_id)

    assert operation.verb == "offset_faces"
    assert operation.args == {"faces": [top], "distance": 5.0, "body_id": body_id}
    assert len(doc.log.entries) == entries_before + 1
    doc.log.undo(doc)
    assert _volume(doc, body_id) == pytest.approx(W * D * H, rel=1e-9)
    doc.log.redo(doc)
    assert _volume(doc, body_id) == pytest.approx(W * D * (H + 5.0), rel=1e-9)


def test_provenance_maps_every_face_one_to_one():
    """面の由来の記録に載る。位相を変えないので、旧面はすべて新しい面1枚に対応し、消えた面も新しい面も無い。"""
    doc = Document()
    body_id = new_box_with_hole(doc, 20.0, 4.0)
    count = _brep_topology.face_count(doc.get_body(body_id).shape)
    before = {face["index"]: face for face in query.describe_faces(doc.get_body(body_id).shape)}

    operation = _offset_faces(doc, [_hole(doc, body_id)], -1.0, body_id)

    provenance = operation.provenance
    assert provenance["deleted"] == []
    assert provenance["new_faces"] == []
    assert sorted(new[0] for new in provenance["face_map"].values()) == list(range(1, count + 1))
    # 並べ替えでも通らないように、対応先の面の種類と向き(穴は穴のまま)まで見る
    after = {face["index"]: face for face in query.describe_faces(doc.get_body(body_id).shape)}
    for old, (new,) in provenance["face_map"].items():
        assert after[new]["kind"] == before[int(old)]["kind"]
        if before[int(old)]["kind"] == "plane":
            assert after[new]["normal"] == pytest.approx(before[int(old)]["normal"], abs=1e-9)
    json.dumps(doc.log.to_json())


def test_the_entry_takes_the_faces_from_the_selection():
    doc = Document()
    body_id = _box(doc)
    selection.set_selection(doc, refs=[shape_ref(doc, body_id, "face", _plane_at(doc, body_id, 2, H))])

    result = entry_for("offset_faces")(doc, distance=5.0)

    assert result == {"body_id": body_id, "changed_bodies": [body_id]}
    assert _volume(doc, body_id) == pytest.approx(W * D * (H + 5.0), rel=1e-9)


def test_ai_can_call_it_over_mcp():
    """AI(MCP)からも呼べる。面は選択から取り、引数は距離だけ(寸法の印つき)。"""
    from duocad.mcp_server import DocumentTools, McpServer

    descriptor = next(d for d in catalog.tool_descriptors() if d["name"] == "offset_faces")
    assert descriptor["inputSchema"].get("required", []) == ["distance"]
    assert catalog.dimension_params("offset_faces") == ("distance",)
    session = session_with_box()
    top = _plane_at(session.document, "box", 2, 40.0)
    server = McpServer(DocumentTools(session))
    call(server, "set_selection", {"refs": [{"body": "box", "kind": "face", "index": top}]})

    payload = call(server, "offset_faces", {"distance": 5.0})

    assert payload["verb"] == "offset_faces"
    assert query.volume(session.document.get_body("box").shape) == pytest.approx(30.0 * 30.0 * 45.0, rel=1e-9)
