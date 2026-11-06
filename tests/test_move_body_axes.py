"""`move_body` の並進で、省いた軸は 0。落ちたら dx だけ渡した呼び出しが断られる。"""
from __future__ import annotations

import pytest
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

from duocad import kernel
from duocad.document import Body
from duocad.server import Session
from duocad.verbs import move_body


def _session() -> tuple[Session, str]:
    session = Session(0.2)
    session.document.add_body(Body(id="box", name="box", kind="solid", shape=BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape()))
    return session, "box"


def test_omitted_axes_move_by_zero():
    session, body_id = _session()
    before = kernel.bounding_box(session.document.get_body(body_id).shape)["center"]

    operation = move_body(session.document, [body_id], dx=5.0)

    assert (operation.args["dx"], operation.args["dy"], operation.args["dz"]) == (5.0, 0.0, 0.0)
    after = kernel.bounding_box(session.document.get_body(body_id).shape)["center"]
    assert after == pytest.approx([before[0] + 5.0, before[1], before[2]])


def test_all_axes_omitted_is_still_refused():
    session, body_id = _session()

    with pytest.raises(ValueError, match="at least one"):
        move_body(session.document, [body_id])

    assert session.document.log.entries == []
