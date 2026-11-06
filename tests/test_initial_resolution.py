"""画面の見え方が届く前の粗さ(文書の外接対角から決める)の契約。"""
from __future__ import annotations

import math

import pytest

from duocad import resolution
from duocad.server import Session

RADIUS = 300.0
HEIGHT = 400.0
DIAGONAL = math.sqrt((2 * RADIUS) ** 2 * 2 + HEIGHT**2)


@pytest.fixture
def big_cylinder() -> tuple:
    session = Session(0.2)
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_circle(sketch_id, [0.0, 0.0], 2 * RADIUS)
    session.clear_selection()
    session.modify_selection([{"sketch": sketch_id, "index": 1}], [])
    session.extrude(HEIGHT, mode="new")
    session.clear_selection()
    return session, session.document.bodies[-1].id


def test_before_the_view_arrives_the_body_is_measured_at_the_whole_view_roughness(big_cylinder) -> None:
    """未着の間の粗さが固定の 0.2mm に戻っていたら、大きな文書の最初の配信が細かすぎる測り方になる。"""
    session, body_id = big_cylinder
    expected = resolution.document_resolution(DIAGONAL)
    assert expected[0] > 0.2 * 2
    assert session.geometry(body_id)["deviation"] == pytest.approx(expected[0])
    assert session.view.view_angular_deflection == pytest.approx(expected[1])


def test_the_first_view_matching_the_document_roughness_does_not_remeasure(big_cylinder) -> None:
    """全体表示の見え方が最初の粗さと同じなら、届いても測り直さない(版が上がらない)。"""
    session, body_id = big_cylinder
    revision = session.view.body_revision(body_id)
    recipe = session.view.body_recipe(body_id)
    session.set_view_resolution(DIAGONAL / resolution.COARSE_REFERENCE_PIXELS, DIAGONAL)
    assert session.view.body_recipe(body_id) == recipe
    assert session.view.body_revision(body_id) == revision


def test_an_empty_document_keeps_the_base_roughness() -> None:
    """空の文書には外接箱が無いので、土台の粗さのまま。"""
    session = Session(0.2)
    assert session.view.view_deviation == pytest.approx(0.2)
