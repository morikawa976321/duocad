"""スケッチの吸着先(`get_sketch_guides`)—— スケッチの平面と、見えているボディが交わる・接する所を (u,v) の図形と点で返す。"""
from __future__ import annotations

import math

import pytest
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

from duocad import kernel_project, query, sketch
from duocad.document import Body
from duocad.server import Session
from session_support import browser as _browser

#: 30×20×10 の箱(角 (10, 20, 5))に、半径 4 の貫通穴(中心 (25, 30))と、上面から深さ 5 の半径 2 の止まり穴(中心 (15, 25))。
BOX = (10.0, 20.0, 5.0, 30.0, 20.0, 10.0)
THROUGH = (25.0, 30.0, 4.0)
BLIND = (15.0, 25.0, 2.0)


def _holed_box():
    x, y, z, dx, dy, dz = BOX
    shape = BRepPrimAPI_MakeBox(gp_Pnt(x, y, z), dx, dy, dz).Shape()
    cx, cy, r = THROUGH
    shape = BRepAlgoAPI_Cut(shape, BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(cx, cy, 0.0), gp_Dir(0, 0, 1)), r, 30.0).Shape()).Shape()
    bx, by, br = BLIND
    blind = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(bx, by, z + dz / 2.0), gp_Dir(0, 0, 1)), br, 30.0).Shape()
    return BRepAlgoAPI_Cut(shape, blind).Shape()


@pytest.fixture()
def session() -> Session:
    made = Session(0.2)
    made.document.add_body(Body(id="part", name="part", kind="solid", shape=_holed_box()))
    return made


def _top_face(session: Session) -> int:
    top = BOX[2] + BOX[5]
    faces = query.describe_faces(session.document.get_body("part").shape)
    return next(f["index"] for f in faces if f["kind"] == "plane" and f["normal"][2] > 0.99 and abs(f["center"][2] - top) < 1e-6)


def _kinds(entities: list) -> dict:
    counts: dict = {}
    for entity in entities:
        counts[entity["kind"]] = counts.get(entity["kind"], 0) + 1
    return counts


def _circles(entities: list) -> list:
    return sorted(
        (round(e["params"]["cx"], 9), round(e["params"]["cy"], 9), round(e["params"]["diameter"], 9))
        for e in entities
        if e["kind"] == "circle"
    )


def _rounded(points: list) -> set:
    return {(round(u, 9) + 0.0, round(v, 9) + 0.0) for u, v in points}


def test_a_sketch_on_the_top_face_snaps_to_its_outline_and_both_hole_edges(session: Session) -> None:
    """上面に貼ったスケッチの吸着先は、上面の外周4本と2つの穴の縁、点は外周の角。落ちたら平面に乗る面の縁を拾っていない。"""
    sketch_id = sketch.sketch_create(session.document, sketch.face_plane("part", _top_face(session)))
    guides = session.sketch_guides(sketch_id)
    assert guides["errors"] == []
    assert _kinds(guides["entities"]) == {"line": 4, "circle": 2}
    # 面に貼ったスケッチの (u,v) は面の枠なので、世界座標の中心は枠を通して比べる
    frame = sketch.get_sketch(session.document, sketch_id).plane["frame"]
    centers = sorted(
        tuple(round(frame["origin"][axis] + u * frame["u"][axis] + v * frame["v"][axis], 6) for axis in range(2))
        for u, v, _diameter in _circles(guides["entities"])
    )
    assert centers == sorted([(THROUGH[0], THROUGH[1]), (BLIND[0], BLIND[1])])
    assert sorted(d for _u, _v, d in _circles(guides["entities"])) == pytest.approx([2 * BLIND[2], 2 * THROUGH[2]])
    corners = {
        tuple(round(frame["origin"][axis] + u * frame["u"][axis] + v * frame["v"][axis], 6) for axis in range(2))
        for u, v in guides["points"]
    }
    x, y, _, dx, dy, _ = BOX
    assert {(x, y), (x + dx, y), (x + dx, y + dy), (x, y + dy)} <= corners


def test_a_base_plane_through_the_body_snaps_to_the_section(session: Session) -> None:
    """立体の途中を通る基準平面(オフセット)の吸着先は、その高さの断面(外周4本と貫通穴の円)。止まり穴は届かないので無い。"""
    sketch_id = session.sketch_create_offset(8.0, "XY")["sketch_id"]
    guides = session.sketch_guides(sketch_id)
    assert _kinds(guides["entities"]) == {"line": 4, "circle": 1}
    assert _circles(guides["entities"]) == [(THROUGH[0], THROUGH[1], 2 * THROUGH[2])]
    x, y, _, dx, dy, _ = BOX
    assert _rounded(guides["points"]) >= {(x, y), (x + dx, y), (x + dx, y + dy), (x, y + dy)}


def test_a_plane_that_misses_the_body_has_no_guides(session: Session) -> None:
    """立体に届かない平面には吸着先が無い(空で返す。断らない)。"""
    sketch_id = session.sketch_create_offset(40.0, "XY")["sketch_id"]
    guides = session.sketch_guides(sketch_id)
    assert guides == {"sketch_id": sketch_id, "entities": [], "points": [], "errors": []}


def test_a_hidden_body_gives_no_guides(session: Session) -> None:
    """隠したボディは吸着先にしない(見えないものへ吸い付かせない)。"""
    sketch_id = session.sketch_create_offset(8.0, "XY")["sketch_id"]
    session.set_visible("part", False)
    assert session.sketch_guides(sketch_id)["entities"] == []


def test_the_guides_follow_a_changed_shape(session: Session) -> None:
    """形が差し替わったら測り直す(覚えた交わりを使い回さない)。落ちたら穴を足した後も古い断面へ吸い付く。"""
    sketch_id = session.sketch_create_offset(8.0, "XY")["sketch_id"]
    assert _kinds(session.sketch_guides(sketch_id)["entities"]) == {"line": 4, "circle": 1}
    body = session.document.get_body("part")
    body.shape = BRepPrimAPI_MakeBox(gp_Pnt(*BOX[:3]), *BOX[3:]).Shape()
    assert _kinds(session.sketch_guides(sketch_id)["entities"]) == {"line": 4}


def test_a_plane_touching_only_a_corner_gives_that_point_alone() -> None:
    """角1点だけで触れる平面は、図形なしで点1つ。落ちたら平面に触れるだけの頂点へ吸着できない。"""
    x, y, z, dx, dy, dz = BOX
    corner = (x + dx, y + dy, z + dz)
    normal = [1 / math.sqrt(3)] * 3
    u = [1 / math.sqrt(2), -1 / math.sqrt(2), 0.0]
    v = [normal[1] * u[2] - normal[2] * u[1], normal[2] * u[0] - normal[0] * u[2], normal[0] * u[1] - normal[1] * u[0]]
    plane = {"kind": "face", "frame": {"origin": list(corner), "u": u, "v": v, "normal": normal}}
    found = kernel_project.section_guides(plane, BRepPrimAPI_MakeBox(gp_Pnt(x, y, z), dx, dy, dz).Shape())
    assert found["entities"] == []
    assert _rounded(found["points"]) == {(0.0, 0.0)}


def test_guides_are_sent_only_to_the_connection_that_asked(session: Session) -> None:
    """`get_sketch_guides` は要求した接続にだけ返り、`request_id` をそのまま返す(`get_edges` と同じ扱い)。"""
    sketch_id = session.sketch_create_offset(8.0, "XY")["sketch_id"]

    async def script(send):
        return await send({"cmd": "get_sketch_guides", "sketch_id": sketch_id, "request_id": 7})

    (answer, clients) = _browser(session, script)
    assert answer["type"] == "sketch_guides"
    assert answer["request_id"] == 7
    assert answer["sketch_id"] == sketch_id
    assert answer["error"] is None
    assert _kinds(answer["entities"]) == {"line": 4, "circle": 1}
    assert clients[1].payloads == [], "吸着先は全接続へ配らない(要求した接続だけ)"


def test_asking_for_an_unknown_sketch_answers_with_an_error_field(session: Session) -> None:
    """知らないスケッチは `{type:'error'}` ではなく `error` 欄で返す。落ちたら裏の要求の失敗で札が出る。"""

    async def script(send):
        return await send({"cmd": "get_sketch_guides", "sketch_id": "sketch-does-not-exist", "request_id": 3})

    (answer, _clients) = _browser(session, script)
    assert answer["type"] == "sketch_guides"
    assert answer["request_id"] == 3
    assert answer["error"]
