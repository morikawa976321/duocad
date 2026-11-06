"""離れたツールでの結合は成功させたうえで、離れた塊が入った1ボディになったことを警告で知らせる。"""
from __future__ import annotations

import pytest
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.gp import gp_Pnt

from duocad.document import Body, new_body_id
from duocad.server import Session
from session_support import select as _select


def _box(size: float, ox: float = 0.0) -> object:
    return BRepPrimAPI_MakeBox(gp_Pnt(ox, 0.0, 0.0), size, size, size).Shape()


def _union(session: Session, tool_origin: float) -> dict:
    target_id, tool_id = new_body_id(), new_body_id()
    session.document.add_body(Body(id=target_id, name="target", kind="solid", shape=_box(20.0)))
    session.document.add_body(Body(id=tool_id, name="tool", kind="solid", shape=_box(7.0, tool_origin)))
    _select(session, [{"body": tool_id, "kind": "face", "index": 1}])
    return session.boolean_body({"body": target_id, "kind": "face", "index": 1}, "union")


def test_union_with_a_separate_tool_warns_with_the_lump_count(session: Session) -> None:
    """落ちたら、離れた塊が入った1ボディになっても何も知らされない。"""
    result = _union(session, 40.0)
    assert "2個" in result["warning"]
    assert any("2個" in warning for warning in session.take_warnings())


@pytest.mark.parametrize("tool_origin", [10.0, 20.0], ids=["重なる", "面が接する"])
def test_union_that_joins_the_tool_does_not_warn(session: Session, tool_origin: float) -> None:
    result = _union(session, tool_origin)
    assert "warning" not in result
    assert session.take_warnings() == []
