"""構えたコマンドの引数の面・辺の参照は、そのボディの形が変わったときに外れる。落ちたら、形が変わったあとも古い添字が残り、
今の形の別の面を指して確定される。"""
from __future__ import annotations

from duocad.server import Session
from session_support import pad, session_with_box

VERTICAL_EDGES = {"kind": "edge", "where": [{"op": "parallel_to", "axis": "Z"}]}
FACE = {"body": "box", "kind": "face", "index": 1}


def filleted_box_with_mirror_staged(ref: dict) -> Session:
    session = session_with_box()
    session.set_selection(predicate=VERTICAL_EDGES)
    session.fillet(2.0)
    session.begin_command("mirror_body", {"base_plane": "XY"} | ({"plane_face": ref} if ref else {}))
    return session


def staged_args(session: Session) -> dict:
    return session.state_meta()["command"]["args"]


def test_undo_of_a_shape_change_drops_the_picked_face_and_says_so():
    session = filleted_box_with_mirror_staged(FACE)
    assert "plane_face" in staged_args(session)
    revision = session.state_meta()["command"]["revision"]
    applied = session.undo()
    assert "plane_face" not in staged_args(session) and applied["dropped_args"] == ["plane_face"]
    assert session.state_meta()["command"]["revision"] > revision
    assert session.state_meta()["command"]["verb"] == "mirror_body"


def test_redo_also_drops_it():
    session = filleted_box_with_mirror_staged({})
    session.undo()
    session.set_command_args({"plane_face": FACE})
    applied = session.redo()
    assert applied["dropped_args"] == ["plane_face"] and "plane_face" not in staged_args(session)


def test_a_verb_that_reshapes_the_picked_body_drops_it():
    session = filleted_box_with_mirror_staged(FACE)
    session.set_selection(predicate={"kind": "edge", "where": [{"op": "parallel_to", "axis": "X"}]})
    applied = session.chamfer(0.5)
    assert applied["dropped_args"] == ["plane_face"] and "plane_face" not in staged_args(session)


def test_a_face_reference_without_kind_is_dropped_too():
    session = filleted_box_with_mirror_staged({"body": "box", "index": 1})
    assert session.undo()["dropped_args"] == ["plane_face"]


def test_changes_to_other_bodies_and_datum_references_leave_it_alone():
    session = session_with_box()
    pad(session, 10, 10, 10, corner=(100.0, 100.0))
    session.begin_command("mirror_body", {"plane_face": FACE})
    applied = session.undo()
    assert staged_args(session)["plane_face"] == FACE and "dropped_args" not in applied

    session.redo()
    session.set_command_args({"plane_face": {"datum": "XY"}})
    session.undo()
    assert staged_args(session)["plane_face"] == {"datum": "XY"}
