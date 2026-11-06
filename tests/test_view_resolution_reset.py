"""文書を差し替えたときの、ズーム連動の目標粗さの扱い。"""
from __future__ import annotations

import pytest

from duocad.server import Session

DEVIATION = 0.2
SIDE = 20.0


def _box_session() -> tuple:
    session = Session(DEVIATION)
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_circle(sketch_id, [0.0, 0.0], SIDE)
    session.clear_selection()
    session.modify_selection([{"sketch": sketch_id, "index": 1}], [])
    session.extrude(SIDE, mode="new")
    session.clear_selection()
    return session, session.document.bodies[-1].id


def test_replacing_the_document_forgets_the_zoom_of_the_previous_one() -> None:
    """落ちたら、開き直した文書の最初の配信が前の文書のズームの粗さで測られる。"""
    session, _ = _box_session()
    session.set_view_resolution(0.002, SIDE, {"x": [1]})
    assert session.view.view_deviation < DEVIATION

    session.new_document()

    assert session.view.view_deviation == pytest.approx(DEVIATION)
    assert session.view._view_resolution is None
    assert session.view._view_coarse_resolution is None
    assert session.view._view_floored is False
    # 同じ値を送り直せば、捨てた後なので「変わった」として受け取る(画面の送り直しが届く)。
    assert session.set_view_resolution(0.002, SIDE) is True
