"""ブーリアンの `keep_tool`: ツールボディを文書に残す。既定(引数なし)は今までどおり消す。"""
from __future__ import annotations

import math

from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.gp import gp_Pnt

from duocad import query
from duocad.document import Body, new_body_id
from duocad.oplog import Operation
from duocad.server import Session
from duocad.verbs.boolean_body import _do_boolean_body
from session_support import select as _select


def _box(size: float, ox: float = 0.0) -> object:
    return BRepPrimAPI_MakeBox(gp_Pnt(ox, 0.0, 0.0), size, size, size).Shape()


def _setup(session: Session) -> tuple[str, str]:
    target_id, tool_id = new_body_id(), new_body_id()
    session.document.add_body(Body(id=target_id, name="target", kind="solid", shape=_box(20.0)))
    session.document.add_body(Body(id=tool_id, name="tool", kind="solid", shape=_box(7.0, 2.0)))
    _select(session, [{"body": tool_id, "kind": "face", "index": 1}])
    return target_id, tool_id


def _ids(session: Session) -> list[str]:
    return [body.id for body in session.document.bodies]


def test_keep_tool_cuts_and_leaves_the_tool_body(session: Session) -> None:
    """落ちたら、keep_tool を付けてもツールのボディが消える。"""
    target_id, tool_id = _setup(session)
    result = session.boolean_body({"body": target_id, "kind": "face", "index": 1}, "cut", keep_tool=True)
    assert result["removed_bodies"] == []
    assert _ids(session) == [target_id, tool_id]
    assert math.isclose(query.volume(session.document.get_body(target_id).shape), 20.0**3 - 7.0**3, rel_tol=1e-6)
    assert math.isclose(query.volume(session.document.get_body(tool_id).shape), 7.0**3, rel_tol=1e-9)


def test_without_keep_tool_the_tool_body_is_still_removed(session: Session) -> None:
    target_id, tool_id = _setup(session)
    result = session.boolean_body({"body": target_id, "kind": "face", "index": 1}, "cut")
    assert result["removed_bodies"] == [tool_id]
    assert _ids(session) == [target_id]


def test_undo_and_redo_with_keep_tool_leave_the_tool_alone(session: Session) -> None:
    target_id, tool_id = _setup(session)
    session.boolean_body({"body": target_id, "kind": "face", "index": 1}, "cut", keep_tool=True)
    session.undo()
    assert _ids(session) == [target_id, tool_id]
    assert math.isclose(query.volume(session.document.get_body(target_id).shape), 20.0**3, rel_tol=1e-9)
    session.redo()
    assert _ids(session) == [target_id, tool_id]
    assert math.isclose(query.volume(session.document.get_body(target_id).shape), 20.0**3 - 7.0**3, rel_tol=1e-6)


def test_the_log_carries_keep_tool_only_when_it_is_true(session: Session) -> None:
    target_id, tool_id = _setup(session)
    session.boolean_body({"body": target_id, "kind": "face", "index": 1}, "cut", keep_tool=True)
    assert session.document.log.entries[-1].args["keep_tool"] is True
    session.undo()
    _select(session, [{"body": tool_id, "kind": "face", "index": 1}])
    session.boolean_body({"body": target_id, "kind": "face", "index": 1}, "cut")
    assert "keep_tool" not in session.document.log.entries[-1].args


def test_replaying_a_log_without_keep_tool_removes_the_tool(session: Session) -> None:
    """落ちたら、引数の無い保存済みの文書の再生で、ツールが残る。"""
    target_id, tool_id = _setup(session)
    _do_boolean_body(
        session.document,
        Operation(seq=1, verb="boolean_body", args={"target_body_id": target_id, "tool_bodies": [tool_id], "mode": "cut"}),
    )
    assert _ids(session) == [target_id]


def test_replaying_a_log_with_keep_tool_keeps_the_tool(session: Session) -> None:
    target_id, tool_id = _setup(session)
    _do_boolean_body(
        session.document,
        Operation(
            seq=1,
            verb="boolean_body",
            args={"target_body_id": target_id, "tool_bodies": [tool_id], "mode": "cut", "keep_tool": True},
        ),
    )
    assert _ids(session) == [target_id, tool_id]
