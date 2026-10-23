"""干渉チェックのメッシュ経路(`duocad/interference_mesh.py`)。落ちたら、メッシュを含む組の区分か近似の扱いが崩れている。"""
from __future__ import annotations

import numpy as np
import pytest

from duocad import interference_mesh as im

_CUBE_FACES = np.array([
    [0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4],
    [1, 2, 6], [1, 6, 5], [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7],
])


def cube(x, y, z, dx, dy, dz):
    corners = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0], [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]], dtype=float)
    return im.manifold_of(corners * [dx, dy, dz] + [x, y, z], _CUBE_FACES)


def pair(a, b, search=1.0, deviation=None):
    return im.mesh_pair(a, b, search, deviation)


def test_the_overlap_has_its_centre_and_box():
    result = pair(cube(0, 0, 0, 10, 10, 10), cube(5, 0, 0, 10, 10, 10))
    assert result["center"] == pytest.approx([7.5, 5, 5])
    assert result["bbox"] == {"min": pytest.approx([5, 0, 0]), "max": pytest.approx([10, 10, 10])}


def test_overlapping_cubes_report_the_volume_and_a_thickness():
    result = pair(cube(0, 0, 0, 10, 10, 10), cube(5, 0, 0, 10, 10, 10))
    assert result["kind"] == "overlap"
    assert result["volume"] == pytest.approx(500.0, rel=1e-9)
    assert result["thickness"] == pytest.approx(2.5, rel=1e-6)
    assert result["obb_min"] == pytest.approx(5.0, rel=1e-6)


def test_a_thin_flat_overlap_is_thin_by_both_measures():
    result = pair(cube(0, 0, 0, 10, 10, 10), cube(9.9995, 0, 0, 10, 10, 10))
    assert result["kind"] == "overlap"
    assert result["thickness"] <= 1e-3 and result["obb_min"] <= 1e-3


def test_face_touching_cubes_are_contact_and_gapped_cubes_are_none_with_a_distance():
    a = cube(0, 0, 0, 10, 10, 10)
    assert pair(a, cube(10, 0, 0, 10, 10, 10))["kind"] == "contact"
    gapped = pair(a, cube(10.1, 0, 0, 10, 10, 10))
    assert gapped["kind"] == "none" and gapped["distance"] == pytest.approx(0.1, abs=1e-9)
    assert not gapped["distance_lower_bound"]


def test_a_gap_beyond_the_search_width_is_flagged_as_a_lower_bound():
    result = pair(cube(0, 0, 0, 10, 10, 10), cube(30, 0, 0, 10, 10, 10), search=1.0)
    assert result["kind"] == "none" and result["distance_lower_bound"] and result["distance"] == pytest.approx(1.0)


def test_the_overlap_render_payload_is_a_triangle_soup():
    result = pair(cube(0, 0, 0, 10, 10, 10), cube(5, 0, 0, 10, 10, 10), deviation=0.1)
    solid = result["render"]["solid"]
    assert len(solid["indices"]) % 3 == 0 and len(solid["indices"]) >= 36 and len(solid["positions"]) % 3 == 0


def test_a_nonmanifold_input_is_rejected_with_a_reason():
    with pytest.raises(ValueError):
        im.manifold_of(np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype=float), np.array([[0, 1, 2]]))
