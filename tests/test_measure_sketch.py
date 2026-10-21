"""**スケッチの点・線分・領域を測る**(issue #201)—— 点同士・点と線・点と面がスケッチの世界座標で測れる。"""
from __future__ import annotations

import math

import pytest
from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.gp import gp_Ax1, gp_Dir, gp_Pnt, gp_Trsf

from duocad import query, region
from duocad.document import Body
from duocad.server import Session
from duocad.sketch import add_circle, add_line, add_point, add_rectangle, face_plane, sketch_create

BOX = 30.0


@pytest.fixture()
def session() -> Session:
    """30 mm 角の立方体(id "box")を持つセッション。"""
    made = Session(0.2)
    made.document.add_body(Body(id="box", name="box", kind="solid", shape=BRepPrimAPI_MakeBox(BOX, BOX, BOX).Shape()))
    return made


def select(session: Session, refs: list) -> None:
    session.clear_selection()
    session.modify_selection(refs, [])


def point_ref(sketch_id: str, entity_id: str, point: str = "cx") -> dict:
    return {"sketch": sketch_id, "entity": entity_id, "point": point}


def entity_ref(sketch_id: str, entity_id: str) -> dict:
    return {"sketch": sketch_id, "entity": entity_id, "point": None}


def top_face(session: Session) -> int:
    shape = session.document.get_body("box").shape
    return next(face["index"] for face in query.describe_faces(shape) if face["normal"][2] > 0.99)


def test_two_sketch_points_report_their_world_distance(session: Session):
    sketch_id = sketch_create(session.document, "XY")
    a = add_point(session.document, sketch_id, (1.0, 2.0))
    b = add_point(session.document, sketch_id, (4.0, 6.0))
    select(session, [point_ref(sketch_id, a), point_ref(sketch_id, b)])
    result = session.measure()
    assert result["between_error"] is None
    assert result["between"]["kind"] == "point-point"
    assert result["between"]["distance"] == pytest.approx(5.0)
    assert result["items"][0]["world"] == pytest.approx([1.0, 2.0, 0.0])


def test_a_point_and_a_sketch_line_use_the_infinite_line():
    """線分 (0,0)–(1,0) と点 (2,1) は 1 mm(延長した直線への垂線)。端点までの √2 ではない。"""
    session = Session(0.2)
    sketch_id = sketch_create(session.document, "XY")
    line = add_line(session.document, sketch_id, (0.0, 0.0), (1.0, 0.0))
    point = add_point(session.document, sketch_id, (2.0, 1.0))
    select(session, [entity_ref(sketch_id, line), point_ref(sketch_id, point)])
    result = session.measure()
    assert result["between"]["kind"] == "point-line"
    assert result["between"]["distance"] == pytest.approx(1.0)
    assert result["between"]["to"] == pytest.approx([2.0, 0.0, 0.0])
    assert result["items"][0]["start"] == pytest.approx([0.0, 0.0, 0.0])
    assert result["items"][0]["length"] == pytest.approx(1.0)


def test_the_end_point_of_a_line_and_the_center_of_a_circle_are_points():
    session = Session(0.2)
    sketch_id = sketch_create(session.document, "XY")
    line = add_line(session.document, sketch_id, (0.0, 0.0), (3.0, 0.0))
    circle = add_circle(session.document, sketch_id, (3.0, 4.0), 2.0)
    select(session, [point_ref(sketch_id, line, "x0"), point_ref(sketch_id, circle, "cx")])
    assert session.measure()["between"]["distance"] == pytest.approx(5.0)


def test_a_sketch_point_and_a_solid_face_and_vertex(session: Session):
    """スケッチの点からソリッドの平らな面・頂点まで(スケッチは z=40 のオフセット平面)。"""
    sketch_id = sketch_create(
        session.document, {"kind": "offset", "base": {"kind": "base", "name": "XY"}, "distance": 40.0}
    )
    point = add_point(session.document, sketch_id, (5.0, 5.0))
    select(session, [point_ref(sketch_id, point), {"body": "box", "kind": "face", "index": top_face(session)}])
    result = session.measure()
    assert result["between"]["kind"] == "point-plane"
    assert result["between"]["distance"] == pytest.approx(40.0 - BOX)
    assert result["items"][0]["world"] == pytest.approx([5.0, 5.0, 40.0])


def test_a_point_and_a_region_measure_the_height_above_the_sketch_plane(session: Session):
    """領域はそのスケッチの平面。領域の外の点でも平面までの高さを返す。"""
    base = sketch_create(session.document, "XY")
    add_rectangle(session.document, base, (0.0, 0.0), 10.0, 10.0)
    other = sketch_create(session.document, {"kind": "offset", "base": {"kind": "base", "name": "XY"}, "distance": 12.5})
    point = add_point(session.document, other, (50.0, 50.0))
    select(session, [point_ref(other, point), region.region_ref(base, 1).to_dict()])
    result = session.measure()
    assert result["between"]["kind"] == "point-plane"
    assert result["between"]["distance"] == pytest.approx(12.5)


def test_two_regions_on_parallel_sketches_report_their_spacing():
    session = Session(0.2)
    low = sketch_create(session.document, "XY")
    add_rectangle(session.document, low, (0.0, 0.0), 10.0, 10.0)
    high = sketch_create(session.document, {"kind": "offset", "base": {"kind": "base", "name": "XY"}, "distance": 7.0})
    add_rectangle(session.document, high, (0.0, 0.0), 10.0, 10.0)
    select(session, [region.region_ref(low, 1).to_dict(), region.region_ref(high, 1).to_dict()])
    result = session.measure()
    assert result["between"]["kind"] == "plane-plane"
    assert result["between"]["parallel"] is True and result["between"]["distance"] == pytest.approx(7.0)


def test_a_sketch_line_and_a_solid_edge_on_a_vertical_plane(session: Session):
    """XZ 平面のスケッチ(u=x, v=z 以外の向きもありうるので枠から世界座標へ)の線分と、立方体の縦の辺。"""
    sketch_id = sketch_create(session.document, "XZ")
    line = add_line(session.document, sketch_id, (0.0, 0.0), (0.0, 10.0))
    select(session, [entity_ref(sketch_id, line)])
    item = session.measure()["items"][0]
    start, end = item["start"], item["end"]
    frame = region.plane_frame(session.document, sketch_id)
    assert start == pytest.approx(frame["origin"])
    assert end == pytest.approx([frame["origin"][i] + 10.0 * frame["v"][i] for i in range(3)])
    assert abs(start[1]) < 1e-12 and abs(end[1]) < 1e-12, "XZ 平面の線分が y=0 から外れた"


def test_a_sketch_on_a_tilted_face_maps_points_into_that_face():
    """45度傾けた立方体の面に貼ったスケッチの点は、その面の平面に乗る(点-平面の距離が 0)。"""
    session = Session(0.2)
    turn = gp_Trsf()
    turn.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(1, 0, 0)), math.radians(45.0))
    shape = BRepBuilderAPI_Transform(BRepPrimAPI_MakeBox(BOX, BOX, BOX).Shape(), turn, True).Shape()
    session.document.add_body(Body(id="tilted", name="tilted", kind="solid", shape=shape))
    tilted = next(
        face["index"]
        for face in query.describe_faces(shape)
        if face["kind"] == "plane" and abs(face["normal"][0]) < 1e-9 and 0.1 < abs(face["normal"][2]) < 0.9
    )
    sketch_id = sketch_create(session.document, face_plane("tilted", tilted))
    point = add_point(session.document, sketch_id, (3.0, 4.0))
    select(session, [point_ref(sketch_id, point), {"body": "tilted", "kind": "face", "index": tilted}])
    assert session.measure()["between"]["distance"] == pytest.approx(0.0, abs=1e-9)
    origin = add_point(session.document, sketch_id, (0.0, 0.0))
    select(session, [point_ref(sketch_id, point), point_ref(sketch_id, origin)])
    assert session.measure()["between"]["distance"] == pytest.approx(5.0), "スケッチの (u, v) の長さが世界で保たれない"


def test_a_whole_circle_or_rectangle_is_refused_with_its_values_kept():
    session = Session(0.2)
    sketch_id = sketch_create(session.document, "XY")
    circle = add_circle(session.document, sketch_id, (0.0, 0.0), 4.0)
    rectangle = add_rectangle(session.document, sketch_id, (10.0, 0.0), 5.0, 5.0)
    select(session, [entity_ref(sketch_id, circle), entity_ref(sketch_id, rectangle)])
    result = session.measure()
    assert result["between"] is None and "線分だけ" in result["between_error"]
    assert result["items"][0]["params"]["diameter"] == pytest.approx(4.0)
