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


def test_a_broken_sketch_is_warned_about_once_not_on_every_delivery() -> None:
    """落ちたら、読めないスケッチの警告が配信のたびに積み増される。"""
    session = Session(DEVIATION)
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_circle(sketch_id, [0.0, 0.0], SIDE)
    for name in list(session.document.parameters):
        del session.document.parameters[name]  # 使用中の名前を消した壊れた文書

    session.state_meta()
    first = list(session.warnings)
    assert first, "壊れたスケッチの警告が1件も出ない"
    session.state_meta()
    session.state_meta()
    assert session.warnings == first


def test_the_state_log_does_not_carry_the_brep_text() -> None:
    """落ちたら、add_solid_body の B-rep 全文が状態配信のログに載って配られる。"""
    from duocad import view

    projected = view._log_args_for_state({"body_id": "b1", "name": "x", "brep": "A" * 1000})
    assert "A" * 10 not in str(projected)
    assert projected["body_id"] == "b1"


def _sketch_revision(session: Session, sketch_id: str) -> int:
    return next(item for item in session.state_meta()["sketches"] if item["sketch_id"] == sketch_id)["revision"]


def test_zooming_does_not_bump_the_revision_of_a_straight_only_sketch() -> None:
    """落ちたら、直線だけのスケッチがズームの段が変わるたびに版が上がり、画面が作り直す。"""
    session = Session(DEVIATION)
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_line(sketch_id, [0.0, 0.0], [SIDE, 0.0])
    before = _sketch_revision(session, sketch_id)
    session.set_view_resolution(0.002, SIDE)
    assert _sketch_revision(session, sketch_id) == before


def test_zooming_still_bumps_the_revision_of_a_curved_sketch() -> None:
    """落ちたら、円のスケッチが粗さの変化を画面へ伝えない(円の塗りは粗さで変わる)。"""
    session = Session(DEVIATION)
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_circle(sketch_id, [0.0, 0.0], SIDE)
    before = _sketch_revision(session, sketch_id)
    session.set_view_resolution(0.002, SIDE)
    assert _sketch_revision(session, sketch_id) > before
