"""測定の `total_length` が、辺に加えてスケッチの線分・楕円の長さを足す。"""
from __future__ import annotations

import math

import pytest

from duocad.server import Session
from duocad.sketch import add_ellipse, add_line, add_point, sketch_create


def _select(session: Session, refs: list) -> None:
    session.clear_selection()
    session.modify_selection(refs, [])


def test_total_length_adds_sketch_lines_and_ellipses_and_skips_points():
    """落ちたら、スケッチの線分を選んでも合計の長さが 0 のままになる。"""
    session = Session(0.2)
    sketch_id = sketch_create(session.document, "XY")
    first = add_line(session.document, sketch_id, (0.0, 0.0), (3.0, 4.0))
    second = add_line(session.document, sketch_id, (0.0, 0.0), (0.0, 2.0))
    point = add_point(session.document, sketch_id, (1.0, 1.0))
    ellipse = add_ellipse(session.document, sketch_id, (10.0, 10.0), 4.0, 4.0)
    _select(session, [
        {"sketch": sketch_id, "entity": first, "point": None},
        {"sketch": sketch_id, "entity": second, "point": None},
        {"sketch": sketch_id, "entity": point, "point": "cx"},
        {"sketch": sketch_id, "entity": ellipse, "point": None},
    ])

    result = session.measure()

    assert result["total_length"] == pytest.approx(7.0 + 4.0 * math.pi)
    assert result["total_area"] == 0.0
