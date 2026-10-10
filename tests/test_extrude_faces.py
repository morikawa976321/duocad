"""verb `extrude` が選択の**面群**(同一平面・同一ボディ)を押し出せること(join / cut / new)。"""
from __future__ import annotations

import json
import math

import pytest
from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.gp import gp_Pnt

from duocad import kernel, kernel_extrude, query
from duocad.document import Body, Document
from duocad.region import region_ref
from duocad.sketch import add_rectangle, sketch_create
from duocad.verbs import extrude, extrude_faces
from duocad.mcp_server import DocumentTools, McpServer
from document_support import replay_through_json
from mcp_support import call
from session_support import select, session_with_box

REL = 1.0e-9


def _box(dx: float, dy: float, dz: float, origin=(0.0, 0.0, 0.0)):
    return BRepPrimAPI_MakeBox(gp_Pnt(*origin), dx, dy, dz).Shape()


def _two_towers():
    """上面が z=10 で離れて2枚ある形(60x20 の板 z0..5 の両端に 10x20 の柱 z5..10)。体積 6000 + 2000。"""
    plate = _box(60.0, 20.0, 5.0)
    left = _box(10.0, 20.0, 5.0, (0.0, 0.0, 5.0))
    right = _box(10.0, 20.0, 5.0, (50.0, 0.0, 5.0))
    shape = BRepAlgoAPI_Fuse(BRepAlgoAPI_Fuse(plate, left).Shape(), right).Shape()
    return shape


def _doc(shape, body_id: str = "b") -> Document:
    document = Document()
    document.add_body(Body(id=body_id, name=body_id, kind="solid", shape=shape))
    return document


def _faces_facing(shape, nx: float, ny: float, nz: float, z: float | None = None) -> list:
    """外向き法線が (nx,ny,nz) の平面の添字(`z` を渡すとその高さのものだけ)。"""
    found = []
    for face in query.describe_faces(shape):
        normal = face["normal"]
        if normal[0] * nx + normal[1] * ny + normal[2] * nz < 0.999:
            continue
        if z is not None and abs(face["center"][2] - z) > 1e-6:
            continue
        found.append(int(face["index"]))
    return found


def _refs(body_id: str, indices) -> list:
    return [{"body": body_id, "kind": "face", "index": index} for index in indices]


def _state(document: Document) -> tuple:
    return (
        json.dumps(document.log.to_json(), sort_keys=True),
        [(body.id, kernel.volume(body.shape), kernel.face_count(body.shape)) for body in document.bodies],
    )


def _top(shape) -> int:
    (index,) = _faces_facing(shape, 0, 0, 1, z=kernel.bounding_box(shape)["max"][2])
    return index


def test_join_on_a_box_top_adds_volume_and_cut_with_negative_distance_removes_it():
    """面の join(正)/cut(負)が効かない。"""
    document = _doc(_box(20.0, 20.0, 20.0))
    top = _top(document.get_body("b").shape)

    extrude_faces(document, _refs("b", [top]), 10.0, mode="join")
    assert math.isclose(kernel.volume(document.get_body("b").shape), 12000.0, rel_tol=REL)

    top = _top(document.get_body("b").shape)
    extrude_faces(document, _refs("b", [top]), -10.0, mode="cut")
    assert math.isclose(kernel.volume(document.get_body("b").shape), 8000.0, rel_tol=REL)


def test_new_makes_a_separate_pillar_and_leaves_the_source_body():
    """面の new が元のボディを変えるか、柱以外を作る。"""
    document = _doc(_box(20.0, 20.0, 20.0))
    top = _top(document.get_body("b").shape)

    operation = extrude_faces(document, _refs("b", [top]), 5.0, mode="new")

    assert len(document.bodies) == 2
    assert math.isclose(kernel.volume(document.get_body("b").shape), 8000.0, rel_tol=REL)
    pillar = document.get_body(operation.args["body_id"])
    assert operation.args["body_id"] != "b"
    assert math.isclose(kernel.volume(pillar.shape), 400.0 * 5.0, rel_tol=REL)


@pytest.mark.parametrize("distance, mode, expected", [(10.0, "join", 12000.0), (-7.0, "cut", 5200.0)])
def test_one_face_join_and_cut_keep_the_box_a_box(distance, mode, expected):
    """1面の join(正)/cut(負)の体積・面数・妥当性は、従来の `extrude_face` の結果(20mm立方体の上面)と一致する。"""
    shape = _box(20.0, 20.0, 20.0)
    result = kernel_extrude.extrude_faces(shape, [_top(shape)], distance, mode, target_shape=shape)

    assert math.isclose(result["volume_after"], expected, rel_tol=REL)
    assert kernel.face_count(result["shape"]) == 6
    assert BRepCheck_Analyzer(result["shape"]).IsValid()


@pytest.mark.parametrize("distance, mode, expected", [(3.0, "join", 6000.0 + 2000.0 + 2 * 200.0 * 3.0), (-3.0, "cut", 8000.0 - 2 * 200.0 * 3.0)])
def test_two_separate_coplanar_faces_are_extruded_together(distance, mode, expected):
    """離れた同一平面の2面を1回で押し出せない、または体積が合わない。"""
    document = _doc(_two_towers())
    tops = _faces_facing(document.get_body("b").shape, 0, 0, 1, z=10.0)
    assert len(tops) == 2

    extrude_faces(document, _refs("b", tops), distance, mode=mode)

    assert math.isclose(kernel.volume(document.get_body("b").shape), expected, rel_tol=REL)


def test_extruding_coplanar_faces_does_not_leave_split_side_faces():
    """同一平面の統合が効いておらず、押すたびに側面が分裂して増える(面数は 20mm立方体の上面+10 で 6)。"""
    document = _doc(_box(20.0, 20.0, 20.0))
    extrude_faces(document, _refs("b", [_top(document.get_body("b").shape)]), 10.0, mode="join")
    assert kernel.face_count(document.get_body("b").shape) == 6


def _rejects_without_a_trace(document: Document, call) -> str:
    before = _state(document)
    with pytest.raises(ValueError) as caught:
        call()
    assert _state(document) == before, "失敗したのに文書かログが変わった"
    return str(caught.value)


def test_errors_leave_document_and_log_untouched():
    """検査の拒否が文書・ログを変えるか、原因(何番目の面のどの条件か)を言わない。"""
    document = _doc(_two_towers())
    shape = document.get_body("b").shape
    top_z10 = _faces_facing(shape, 0, 0, 1, z=10.0)
    top_z5 = _faces_facing(shape, 0, 0, 1, z=5.0)
    bottom = _faces_facing(shape, 0, 0, -1)
    side = _faces_facing(shape, 1, 0, 0)
    assert top_z5 and bottom and side

    message = _rejects_without_a_trace(
        document, lambda: extrude_faces(document, _refs("b", [top_z10[0], top_z5[0]]), 1.0)
    )
    assert "#2" in message and "同一平面" in message

    message = _rejects_without_a_trace(
        document, lambda: extrude_faces(document, _refs("b", [top_z10[0], bottom[0]]), 1.0)
    )
    assert "#2" in message and "逆向き" in message

    message = _rejects_without_a_trace(
        document, lambda: extrude_faces(document, _refs("b", [top_z10[0], side[0]]), 1.0)
    )
    assert "#2" in message and "平行でない" in message

    message = _rejects_without_a_trace(
        document, lambda: extrude_faces(document, _refs("b", top_z10), 1.0, mode="cut")
    )
    assert "負の距離" in message

    message = _rejects_without_a_trace(
        document, lambda: extrude_faces(document, _refs("b", top_z10), 1.0, mode="new", body_id="b")
    )
    assert "body_id" in message

    message = _rejects_without_a_trace(
        document, lambda: extrude_faces(document, _refs("b", top_z10), -1.0, mode="join")
    )
    assert "did not change" in message

    message = _rejects_without_a_trace(
        document, lambda: extrude_faces(document, _refs("b", [top_z10[0], top_z10[0]]), 1.0)
    )
    assert "distinct" in message


def test_faces_of_different_bodies_and_curved_faces_are_rejected():
    """別ボディの面・曲面を黙って押し出す。"""
    document = _doc(_box(20.0, 20.0, 20.0))
    other = Body(id="c", name="c", kind="solid", shape=_box(20.0, 20.0, 20.0, (50.0, 0.0, 0.0)))
    document.add_body(other)
    top_b = _top(document.get_body("b").shape)
    top_c = _top(other.shape)

    message = _rejects_without_a_trace(
        document,
        lambda: extrude_faces(document, [*_refs("b", [top_b]), *_refs("c", [top_c])], 1.0),
    )
    assert "#2" in message and "別のボディ" in message

    cylinder = Document()
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder

    cylinder.add_body(Body(id="y", name="y", kind="solid", shape=BRepPrimAPI_MakeCylinder(5.0, 10.0).Shape()))
    curved = next(f["index"] for f in query.describe_faces(cylinder.get_body("y").shape) if f["kind"] != "plane")
    message = _rejects_without_a_trace(
        cylinder, lambda: extrude_faces(cylinder, _refs("y", [curved]), 1.0)
    )
    assert "平面" in message


def test_a_body_id_sends_the_pillar_to_another_body():
    """行き先 body_id を指定しても、面を持つボディへ足してしまう。"""
    document = _doc(_box(20.0, 20.0, 20.0))
    document.add_body(Body(id="c", name="c", kind="solid", shape=_box(20.0, 20.0, 20.0, (50.0, 0.0, 0.0))))
    top_b = _top(document.get_body("b").shape)

    extrude_faces(document, _refs("b", [top_b]), 5.0, mode="join", body_id="c")

    assert math.isclose(kernel.volume(document.get_body("b").shape), 8000.0, rel_tol=REL)
    assert kernel.volume(document.get_body("c").shape) > 8000.0


@pytest.mark.parametrize("mode, distance", [("join", 6.0), ("cut", -6.0), ("new", 6.0)])
def test_undo_redo_and_replay_give_the_same_shape(mode, distance):
    """面の押し出しの undo / redo / 保存したログの再生が同じ形にならない。"""
    document = _doc(_two_towers())
    tops = _faces_facing(document.get_body("b").shape, 0, 0, 1, z=10.0)
    original = kernel.volume(document.get_body("b").shape)
    operation = extrude_faces(document, _refs("b", tops), distance, mode=mode)
    after = {body.id: kernel.volume(body.shape) for body in document.bodies}

    document.log.undo(document)
    assert [body.id for body in document.bodies] == ["b"]
    assert math.isclose(kernel.volume(document.get_body("b").shape), original, rel_tol=REL)

    document.log.redo(document)
    assert {body.id: kernel.volume(body.shape) for body in document.bodies} == pytest.approx(after, rel=REL)

    restored = replay_through_json(document, into=_doc(_two_towers()))
    assert {body.id: kernel.volume(body.shape) for body in restored.bodies} == pytest.approx(after, rel=REL)
    assert operation.provenance["faces"] == tops


def test_session_extrude_takes_faces_and_rejects_a_mixed_selection():
    """Session.extrude が面の選択を受けない、または領域との混在を通す。プレビューも文書を変えない。"""
    session = session_with_box()
    top = _top(session.document.get_body("box").shape)
    before = kernel.volume(session.document.get_body("box").shape)

    select(session, _refs("box", [top]))
    preview = session.preview("extrude", {"distance": 5.0, "mode": "join"})
    assert preview["bodies"], "面のプレビューが形を返さない"
    assert kernel.volume(session.document.get_body("box").shape) == before
    assert session.document.log.entries == []

    applied = session.extrude(5.0, mode="join")
    assert applied["changed_bodies"] == ["box"]
    assert kernel.volume(session.document.get_body("box").shape) > before

    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_rectangle(sketch_id, [100.0, 100.0], 5.0, 5.0)
    select(session, [*_refs("box", [top]), {"sketch": sketch_id, "index": 1}])
    with pytest.raises(ValueError, match="not both"):
        session.extrude(1.0, mode="join")


def test_mcp_extrude_takes_a_face_selection():
    """MCP の `extrude` が確定した面の選択を押さない(AIが面を押し出せる唯一の入口)。"""
    session = session_with_box()
    top = _top(session.document.get_body("box").shape)
    before = kernel.volume(session.document.get_body("box").shape)
    server = McpServer(DocumentTools(session))
    call(server, "set_selection", {"refs": _refs("box", [top])})

    payload = call(server, "extrude", {"distance": 5.0, "mode": "join"})

    assert payload["verb"] == "extrude"
    assert kernel.volume(session.document.get_body("box").shape) > before


def test_a_region_selection_still_extrudes_as_before():
    """領域の押し出しが面の分岐に巻き込まれて変わる。"""
    document = Document()
    sketch_id = sketch_create(document, "XY")
    add_rectangle(document, sketch_id, (0.0, 0.0), 10.0, 10.0)
    operation = extrude(document, [region_ref(sketch_id, 1)], 5.0, mode="join")
    assert "regions" in operation.args and "faces" not in operation.args
    assert math.isclose(kernel.volume(document.bodies[0].shape), 500.0, rel_tol=REL)
