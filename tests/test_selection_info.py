"""画面の選択情報の表示(issue #140)の入口 —— 選択1項目のときだけ `measure` の1項目を返す。"""
from __future__ import annotations

import pytest
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder

from duocad import query
from duocad.document import Body
from duocad.server import Session
from duocad.sketch import add_line, base_plane, sketch_create
from session_support import browser


@pytest.fixture()
def session() -> Session:
    """30×30×40 の直方体(id "box")と、半径10・高さ20の円柱(id "cyl")を持つセッション。"""
    made = Session(None, 0.2)
    made.document.add_body(Body(id="box", name="box", kind="solid", shape=BRepPrimAPI_MakeBox(30, 30, 40).Shape()))
    made.document.add_body(
        Body(id="cyl", name="cyl", kind="solid", shape=BRepPrimAPI_MakeCylinder(10.0, 20.0).Shape())
    )
    return made


def select(session: Session, refs: list) -> None:
    session.clear_selection()
    session.modify_selection(refs, [])


def test_only_a_single_item_is_inspected_and_it_matches_measure(session: Session):
    """落ちたら、表示の値が AI の読む `measure` と別の計算になっている(または2項目以上でも測っている)。"""
    session.clear_selection()
    assert session.inspect_selection() is None
    select(session, [{"body": "box", "kind": "face", "index": 1}])
    assert session.inspect_selection() == session.measure()["items"][0]
    select(session, [{"body": "box", "kind": "face", "index": 1}, {"body": "box", "kind": "face", "index": 2}])
    assert session.inspect_selection() is None


def test_measure_reports_surface_kind_circle_radius_and_sketch_line_length(session: Session):
    """落ちたら、表示に出す面の種類・円の半径と中心・線分の長さが `measure` から読めない。"""
    cyl = next(body for body in session.document.bodies if body.id == "cyl").shape
    side = next(face["index"] for face in query.describe_faces(cyl) if face["kind"] != "plane")
    select(session, [{"body": "cyl", "kind": "face", "index": side}])
    assert session.inspect_selection()["surface_kind"] == "cylinder_full"

    circle = next(edge["index"] for edge in query.describe_edges(cyl) if edge["kind"] == "circle")
    select(session, [{"body": "cyl", "kind": "edge", "index": circle}])
    item = session.inspect_selection()
    assert item["radius"] == pytest.approx(10.0)
    assert item["center"][:2] == pytest.approx([0.0, 0.0])

    sketch_id = sketch_create(session.document, base_plane("XY"))
    entity = add_line(session.document, sketch_id, (0.0, 0.0), (3.0, 4.0))
    select(session, [{"sketch": sketch_id, "entity": entity, "point": None}])
    assert session.inspect_selection()["length"] == pytest.approx(5.0)


def test_the_websocket_answers_the_asker_and_returns_failures_in_the_payload(session: Session, monkeypatch):
    """落ちたら、裏の要求の返事が他の接続へ漏れるか、失敗が `{type:'error'}` になって画面にトーストが出る。"""
    select(session, [{"body": "box", "kind": "body", "index": None}])
    _, clients = browser(session, lambda send: send({"cmd": "inspect_selection", "request_id": 5}), initial_push=False)
    answer = clients[0].payloads[-1]
    assert answer["type"] == "selection_info" and answer["request_id"] == 5
    assert answer["item"]["kind"] == "body" and answer["error"] is None
    assert clients[1].payloads == []

    def broken():
        raise ValueError("face 99 out of range")

    monkeypatch.setattr(session, "inspect_selection", broken)
    _, clients = browser(session, lambda send: send({"cmd": "inspect_selection", "request_id": 6}), initial_push=False)
    answer = clients[0].payloads[-1]
    assert answer["type"] == "selection_info" and answer["item"] is None
    assert "face 99" in answer["error"]
