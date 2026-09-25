"""**2項目のあいだを測る**(issue #10)—— `duocad/measure.py` と、その口(MCP / WebSocket)。

固定する契約:

1. 点・直線・平面の6通りの組で、距離(平行なら)か角度(平行でなければ)が返る。
   **直線と平面は無限に延ばして測る**(有限の辺・面の最短距離ではない)。
2. 直線でない辺・平面でない面・ボディ・スケッチの図形は `between_error` で断る
   (例外ではない —— 各項目の寸法は読めたままにする)。
3. `between` が付くのは**ちょうど2項目**のときだけ。
4. MCP の `measure` と WebSocket の `measure` は同じ計算を通り、WSの応答は
   要求した接続にだけ返る(`client_op` を添えて)。
"""
from __future__ import annotations

import json
import math

import pytest
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder

from duocad import kernel, query
from duocad.document import Body
from duocad.measure import PARALLEL_SIN_TOLERANCE, Line, Plane, Point, between
from duocad.mcp_server import DocumentTools, McpServer
from duocad.server import Session
from session_support import browser

BOX_X, BOX_Y, BOX_Z = 30.0, 30.0, 40.0


# --------------------------------------------------------------------------------------
# 道具
# --------------------------------------------------------------------------------------
@pytest.fixture()
def session() -> Session:
    """30×30×40 の直方体(id "box")と、半径10・高さ20の円柱(id "cyl")を持つセッション。"""
    made = Session(None, 0.2)
    made.document.add_body(
        Body(id="box", name="box", kind="solid", shape=BRepPrimAPI_MakeBox(BOX_X, BOX_Y, BOX_Z).Shape())
    )
    made.document.add_body(
        Body(id="cyl", name="cyl", kind="solid", shape=BRepPrimAPI_MakeCylinder(10.0, 20.0).Shape())
    )
    return made


def _shape(session: Session, body_id: str):
    return next(body for body in session.document.bodies if body.id == body_id).shape


def _close(a, b, tol=1e-6) -> bool:
    return all(math.isclose(float(x), float(y), abs_tol=tol) for x, y in zip(a, b))


def vertex_at(shape, point) -> int:
    """その座標にある頂点の添字(`kernel.vertex_positions` の体系)。"""
    return next(item["index"] for item in kernel.vertex_positions(shape) if _close(item["point"], point))


def edge_between(shape, p, q) -> int:
    """端点が {p, q} の辺の添字。"""
    for edge in query.describe_edges(shape):
        ends = (edge["start"], edge["end"])
        if (_close(ends[0], p) and _close(ends[1], q)) or (_close(ends[0], q) and _close(ends[1], p)):
            return edge["index"]
    raise AssertionError(f"no edge between {p} and {q}")


def face_with_normal(shape, normal) -> int:
    return next(face["index"] for face in query.describe_faces(shape) if _close(face["normal"], normal))


def select(session: Session, refs: list) -> None:
    session.clear_selection()
    session.modify_selection(refs, [])


def ref(kind: str, index: int, body: str = "box") -> dict:
    return {"body": body, "kind": kind, "index": index}


# --------------------------------------------------------------------------------------
# 1. 純粋な幾何(`between`)
# --------------------------------------------------------------------------------------
def test_point_point_reports_distance_and_delta():
    result = between(Point((0.0, 0.0, 0.0)), Point((3.0, 4.0, 12.0)))
    assert result["kind"] == "point-point"
    assert result["distance"] == pytest.approx(13.0)
    assert result["delta"] == [3.0, 4.0, 12.0]
    assert result["from"] == [0.0, 0.0, 0.0] and result["to"] == [3.0, 4.0, 12.0]
    assert result["parallel"] is None and result["angle_deg"] is None


def test_point_line_uses_the_infinite_line():
    """辺の外側に落ちる垂線の足でも、無限直線として測る(足は `to`)。"""
    line = Line(point=(0.0, 0.0, 0.0), direction=(1.0, 0.0, 0.0))
    result = between(Point((100.0, 5.0, 0.0)), line)
    assert result["kind"] == "point-line"
    assert result["distance"] == pytest.approx(5.0)
    assert result["to"] == pytest.approx([100.0, 0.0, 0.0])


def test_the_pair_name_is_normalized_by_kind_not_by_selection_order():
    """平面→点の順で渡しても `point-plane`(from が点・to が足)。"""
    plane = Plane(point=(0.0, 0.0, 0.0), normal=(0.0, 0.0, 1.0))
    result = between(plane, Point((1.0, 2.0, -7.0)))
    assert result["kind"] == "point-plane"
    assert result["distance"] == pytest.approx(7.0)
    assert result["from"] == [1.0, 2.0, -7.0] and result["to"] == pytest.approx([1.0, 2.0, 0.0])


def test_parallel_lines_report_their_separation():
    a = Line(point=(0.0, 0.0, 0.0), direction=(0.0, 0.0, 1.0))
    b = Line(point=(3.0, 4.0, 99.0), direction=(0.0, 0.0, -1.0))  # 逆向きでも平行
    result = between(a, b)
    assert result["kind"] == "line-line" and result["parallel"] is True
    assert result["distance"] == pytest.approx(5.0)
    assert result["to"] == pytest.approx([3.0, 4.0, 0.0])


def test_skew_lines_report_the_angle_instead_of_a_distance():
    a = Line(point=(0.0, 0.0, 0.0), direction=(1.0, 0.0, 0.0))
    b = Line(point=(0.0, 0.0, 5.0), direction=(1.0, 1.0, 0.0))
    result = between(a, b)
    assert result["parallel"] is False
    assert result["angle_deg"] == pytest.approx(45.0)
    assert result["distance"] is None and result["from"] is None and result["to"] is None


def test_line_plane_reports_distance_when_parallel_and_angle_otherwise():
    plane = Plane(point=(0.0, 0.0, 0.0), normal=(0.0, 0.0, 1.0))
    flat = between(Line(point=(1.0, 1.0, 8.0), direction=(1.0, 0.0, 0.0)), plane)
    assert flat["kind"] == "line-plane" and flat["parallel"] is True
    assert flat["distance"] == pytest.approx(8.0)
    tilted = between(Line(point=(0.0, 0.0, 0.0), direction=(1.0, 0.0, 1.0)), plane)
    assert tilted["parallel"] is False and tilted["angle_deg"] == pytest.approx(45.0)
    upright = between(Line(point=(0.0, 0.0, 0.0), direction=(0.0, 0.0, 1.0)), plane)
    assert upright["angle_deg"] == pytest.approx(90.0)


def test_plane_plane_reports_distance_when_parallel_and_angle_otherwise():
    a = Plane(point=(0.0, 0.0, 0.0), normal=(0.0, 0.0, 1.0))
    b = Plane(point=(50.0, 50.0, 12.0), normal=(0.0, 0.0, -1.0))  # 重なっていなくても間隔が答え
    result = between(a, b)
    assert result["parallel"] is True and result["distance"] == pytest.approx(12.0)
    c = Plane(point=(0.0, 0.0, 0.0), normal=(1.0, 0.0, 0.0))
    assert between(a, c)["angle_deg"] == pytest.approx(90.0)


def test_the_parallel_tolerance_is_tighter_than_the_predicate_one():
    """述語の `PARALLEL_TOLERANCE`(≈2.6度)で平行扱いされる傾きは、測定では角度になる。

    落ちたら、2度傾いた面どうしに「距離」が出る(その距離は場所によって違う値なので嘘になる)。
    """
    tilt = math.radians(2.0)
    a = Plane(point=(0.0, 0.0, 0.0), normal=(0.0, 0.0, 1.0))
    b = Plane(point=(0.0, 0.0, 10.0), normal=(math.sin(tilt), 0.0, math.cos(tilt)))
    result = between(a, b)
    assert result["parallel"] is False
    assert result["angle_deg"] == pytest.approx(2.0)
    assert PARALLEL_SIN_TOLERANCE < 1e-3


# --------------------------------------------------------------------------------------
# 2. 実物のB-repから(`Session.measure`)
# --------------------------------------------------------------------------------------
def test_two_vertices_of_the_box(session: Session):
    shape = _shape(session, "box")
    select(session, [ref("vertex", vertex_at(shape, (0, 0, 0))), ref("vertex", vertex_at(shape, (30, 30, 40)))])
    result = session.measure()
    assert result["count"] == 2 and result["between_error"] is None
    assert result["between"]["distance"] == pytest.approx(math.sqrt(30**2 + 30**2 + 40**2))
    assert result["between"]["delta"] == pytest.approx([30.0, 30.0, 40.0])


def test_a_vertex_and_a_straight_edge(session: Session):
    shape = _shape(session, "box")
    edge = edge_between(shape, (30, 30, 0), (30, 30, 40))  # Zに平行な辺
    select(session, [ref("vertex", vertex_at(shape, (0, 0, 0))), ref("edge", edge)])
    result = session.measure()["between"]
    assert result["kind"] == "point-line"
    assert result["distance"] == pytest.approx(math.hypot(30, 30))
    assert result["to"] == pytest.approx([30.0, 30.0, 0.0])


def test_a_vertex_and_a_planar_face(session: Session):
    shape = _shape(session, "box")
    select(session, [ref("face", face_with_normal(shape, (0, 0, 1))), ref("vertex", vertex_at(shape, (0, 0, 0)))])
    result = session.measure()["between"]
    assert result["kind"] == "point-plane"
    assert result["distance"] == pytest.approx(BOX_Z)
    assert result["to"] == pytest.approx([0.0, 0.0, BOX_Z])


def test_two_parallel_edges_and_two_skew_edges(session: Session):
    shape = _shape(session, "box")
    a = edge_between(shape, (0, 0, 0), (0, 0, 40))
    b = edge_between(shape, (30, 0, 0), (30, 0, 40))
    select(session, [ref("edge", a), ref("edge", b)])
    parallel = session.measure()["between"]
    assert parallel["parallel"] is True and parallel["distance"] == pytest.approx(BOX_X)
    c = edge_between(shape, (0, 0, 0), (30, 0, 0))
    select(session, [ref("edge", a), ref("edge", c)])
    skew = session.measure()["between"]
    assert skew["parallel"] is False and skew["angle_deg"] == pytest.approx(90.0)


def test_two_parallel_faces_and_two_perpendicular_faces(session: Session):
    shape = _shape(session, "box")
    top = face_with_normal(shape, (0, 0, 1))
    bottom = face_with_normal(shape, (0, 0, -1))
    select(session, [ref("face", top), ref("face", bottom)])
    parallel = session.measure()["between"]
    assert parallel["kind"] == "plane-plane" and parallel["parallel"] is True
    assert parallel["distance"] == pytest.approx(BOX_Z)
    side = face_with_normal(shape, (1, 0, 0))
    select(session, [ref("face", top), ref("face", side)])
    assert session.measure()["between"]["angle_deg"] == pytest.approx(90.0)


def test_an_edge_parallel_to_a_face(session: Session):
    shape = _shape(session, "box")
    edge = edge_between(shape, (0, 0, 0), (30, 0, 0))  # 底面の辺
    top = face_with_normal(shape, (0, 0, 1))
    select(session, [ref("edge", edge), ref("face", top)])
    result = session.measure()["between"]
    assert result["kind"] == "line-plane" and result["parallel"] is True
    assert result["distance"] == pytest.approx(BOX_Z)


def test_a_curved_edge_or_face_is_refused_without_losing_the_item_values(session: Session):
    """円柱の円い辺・円筒面は `between_error`。**各項目の寸法は返ったまま**(例外にしない)。"""
    cyl = _shape(session, "cyl")
    circle = next(edge["index"] for edge in query.describe_edges(cyl) if edge["kind"] == "circle")
    select(session, [ref("edge", circle, "cyl"), ref("vertex", vertex_at(_shape(session, "box"), (0, 0, 0)))])
    result = session.measure()
    assert result["between"] is None
    assert "直線ではない" in result["between_error"]
    assert result["items"][0]["edge_kind"] == "circle" and result["items"][0]["length"] > 0

    side = next(face["index"] for face in query.describe_faces(cyl) if face["kind"] != "plane")
    select(session, [ref("face", side, "cyl"), ref("face", face_with_normal(_shape(session, "box"), (0, 0, 1)))])
    result = session.measure()
    assert result["between"] is None
    assert "平面ではない" in result["between_error"]


def test_a_body_reference_is_refused(session: Session):
    shape = _shape(session, "box")
    select(session, [{"body": "cyl", "kind": "body", "index": None}, ref("vertex", vertex_at(shape, (0, 0, 0)))])
    result = session.measure()
    assert result["between"] is None and "ボディ" in result["between_error"]
    assert result["items"][0]["kind"] == "body" and result["items"][0]["volume"] > 0


def test_between_is_reported_only_for_exactly_two_items(session: Session):
    shape = _shape(session, "box")
    select(session, [ref("vertex", vertex_at(shape, (0, 0, 0)))])
    assert "between" not in session.measure()
    select(session, [ref("vertex", index) for index in (1, 2, 3)])
    three = session.measure()
    assert three["count"] == 3 and "between" not in three
    session.clear_selection()
    empty = session.measure()
    assert empty["count"] == 0 and "between" not in empty


# --------------------------------------------------------------------------------------
# 3. 口(MCP / WebSocket)
# --------------------------------------------------------------------------------------
def test_the_mcp_tool_returns_the_same_between_as_the_session(session: Session):
    shape = _shape(session, "box")
    server = McpServer(DocumentTools(session))
    top = face_with_normal(shape, (0, 0, 1))
    bottom = face_with_normal(shape, (0, 0, -1))
    server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "propose_selection", "arguments": {"refs": [ref("face", top), ref("face", bottom)]}},
        }
    )
    response = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "measure"}})
    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload["origin"] == "proposed"  # 提案のままで測れる
    assert payload["between"] == session.measure()["between"]
    assert payload["between"]["distance"] == pytest.approx(BOX_Z)


def test_the_websocket_answers_only_the_asking_connection_with_the_client_op(session: Session):
    shape = _shape(session, "box")
    select(session, [ref("vertex", vertex_at(shape, (0, 0, 0))), ref("vertex", vertex_at(shape, (30, 0, 0)))])

    _, clients = browser(session, lambda send: send({"cmd": "measure", "client_op": 77}), initial_push=False)
    answer = clients[0].payloads[-1]
    assert answer["type"] == "measure" and answer["client_op"] == 77
    assert answer["between"]["distance"] == pytest.approx(BOX_X)
    assert clients[1].payloads == [], "測定は全接続へ配らない(要求した接続だけ)"
    assert session.can_undo is False, "測定は操作ログに載らない"
