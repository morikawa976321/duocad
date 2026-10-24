"""干渉チェックのカーネル(`duocad/kernel_interference.py`)。落ちたら、組の関係か最短距離を誤って読んでいる。"""
from __future__ import annotations

import math

import pytest

from duocad import kernel_interference as ki
from interference_support import box, cylinder, nested_boxes, plate_with_hole, rotated

from duocad import (
    brep_geometry as _brep_geometry,
    brep_topology as _brep_topology,
)


def relation(a, b):
    return ki.pair_relation(a, b)


A = box(0, 0, 0, 10, 10, 10)


def test_half_overlapping_boxes_share_a_solid_with_the_exact_volume():
    result = relation(A, box(5, 0, 0, 10, 10, 10))
    assert result["kind"] == "overlap"
    assert result["volume"] == pytest.approx(500.0, rel=1e-9)
    assert result["thickness"] == pytest.approx(2.5, rel=1e-9)


def test_a_box_inside_a_box_is_an_overlap():
    result = relation(A, box(2, 2, 2, 3, 3, 3))
    assert result["kind"] == "overlap" and result["volume"] == pytest.approx(27.0, rel=1e-9)


@pytest.mark.parametrize(
    "other, kind, extra",
    [
        (box(10, 0, 0, 10, 10, 10), "face", {"area": 100.0}),
        (box(10, 3, 4, 10, 10, 10), "face", {"area": 42.0}),
        (box(10, 10, 0, 10, 10, 10), "edge", {}),
        (box(10, 10, 10, 10, 10, 10), "vertex", {}),
    ],
)
def test_touching_boxes_share_a_face_an_edge_or_a_vertex(other, kind, extra):
    result = relation(A, other)
    assert result["kind"] == kind
    for key, value in extra.items():
        assert result[key] == pytest.approx(value, rel=1e-9)


def test_a_gap_larger_than_the_tolerance_shares_nothing():
    assert relation(A, box(10.1, 0, 0, 10, 10, 10))["kind"] == "none"
    assert relation(A, box(10.00001, 0, 0, 10, 10, 10))["kind"] == "none"


def test_the_relation_does_not_depend_on_the_argument_order():
    other = box(5, 0, 0, 10, 10, 10)
    forward, backward = relation(A, other), relation(other, A)
    assert forward["kind"] == backward["kind"] == "overlap"
    assert forward["volume"] == pytest.approx(backward["volume"], rel=1e-12)
    assert forward["thickness"] == pytest.approx(backward["thickness"], rel=1e-12)


def test_the_inputs_are_not_modified_by_the_fuse():
    from OCP.BRep import BRep_Tool

    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_VERTEX

    def tolerances(shape):
        return [
            (BRep_Tool.MaxTolerance_s(shape, kind), len(_brep_topology.subshapes(shape, kind)))
            for kind in (TopAbs_VERTEX, TopAbs_EDGE, TopAbs_FACE)
        ]

    other = box(5, 0, 0, 10, 10, 10)
    before = tolerances(A), tolerances(other)
    relation(A, other)
    assert (tolerances(A), tolerances(other)) == before


def test_a_thin_flat_overlap_is_measured_thin():
    result = relation(A, box(9.999, 0, 0, 10, 10, 10))
    assert result["kind"] == "overlap"
    assert result["thickness"] == pytest.approx(0.001, rel=1e-3)
    assert result["obb_min"] == pytest.approx(0.001, rel=1e-3)


def test_a_pin_pressed_into_a_hole_is_a_thick_overlap_even_when_the_wall_is_thin():
    result = relation(plate_with_hole(5.0), cylinder(0, 0, -5, 5.05, 20))
    assert result["kind"] == "overlap"
    assert result["thickness"] == pytest.approx(0.05, rel=0.02)


def test_a_thin_flat_overlap_joined_to_a_deep_pin_is_not_thin_by_the_oriented_box():
    """200×200 の面が 1e-6 重なり、半径1・深さ5のピンが刺さる形。2V/A だけなら薄いが、有向境界箱の最小辺が厚いので tiny にしない根拠になる。"""
    base = box(0, 0, 0, 200, 200, 10)
    lid = _fuse(box(0, 0, 9.999999, 200, 200, 5), cylinder(100, 100, 5, 1.0, 6))
    result = relation(base, lid)
    assert result["kind"] == "overlap"
    assert result["thickness"] <= 1e-3 < result["obb_min"]


def _fuse(a, b):
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse

    operation = BRepAlgoAPI_Fuse(a, b)
    operation.Build()
    return operation.Shape()


def test_the_render_payload_has_triangles_for_an_overlap_and_a_point_for_a_vertex():
    overlap = ki.pair_relation(A, box(5, 0, 0, 10, 10, 10), deviation=0.1)
    assert overlap["render"]["solid"]["indices"] and len(overlap["render"]["solid"]["positions"]) % 3 == 0
    face = ki.pair_relation(A, box(10, 0, 0, 10, 10, 10), deviation=0.1)
    assert face["render"]["faces"]["indices"]
    edge = ki.pair_relation(A, box(10, 10, 0, 10, 10, 10), deviation=0.1)
    assert len(edge["render"]["edges"]) == 1 and len(edge["render"]["edges"][0]) >= 2
    vertex = ki.pair_relation(A, box(10, 10, 10, 10, 10, 10), deviation=0.1)
    assert vertex["render"]["points"] == [pytest.approx([10, 10, 10])]
    assert "render" not in ki.pair_relation(A, box(5, 0, 0, 10, 10, 10))


# ---------------------------------------------------------------------------------------------- 最短距離
def test_distance_of_separated_boxes_is_the_gap_with_both_points():
    result = ki.min_distance(A, box(10.1, 0, 0, 10, 10, 10))
    assert result["ok"] and result["stage"] == "whole" and not result["upper_bound"]
    assert result["distance"] == pytest.approx(0.1, abs=1e-9)
    assert abs(result["points"][0][0] - result["points"][1][0]) == pytest.approx(0.1, abs=1e-9)


def test_distance_of_a_pin_in_a_hole_is_the_radial_gap_not_the_diameter_difference():
    result = ki.min_distance(plate_with_hole(30.2, size=70.0), rotated(cylinder(0, 0, -5, 30.0, 20), 3.1))
    assert result["ok"]
    assert result["distance"] == pytest.approx(0.2, abs=1e-6)


def test_the_concentric_rounded_core_is_measured_facewise_and_flagged_as_an_upper_bound():
    shell, core = nested_boxes(0.2)
    result = ki.min_distance(shell, core)
    assert result["ok"]
    assert result["distance"] == pytest.approx(0.2, abs=1e-6)
    assert result["upper_bound"] and result["stage"] == "faces"


def test_the_concentric_core_with_a_gap_that_measures_whole_is_not_flagged():
    shell, core = nested_boxes(0.1)
    result = ki.min_distance(shell, core)
    assert result["ok"] and result["distance"] == pytest.approx(0.1, abs=1e-6)


def test_loose_bbox_contains_the_shape_and_box_gap_is_zero_when_they_overlap():
    box_a = _brep_geometry._interference_loose_bbox(A)
    assert all(lo <= 0 and hi >= 10 for lo, hi in zip(*box_a))
    assert ki.box_gap(box_a, _brep_geometry._interference_loose_bbox(box(5, 0, 0, 10, 10, 10))) == 0.0
    assert ki.box_gap(box_a, _brep_geometry._interference_loose_bbox(box(20, 0, 0, 1, 1, 1))) == pytest.approx(10.0, abs=1e-6)


def test_the_brep_round_trip_without_the_validity_check_keeps_the_volume():
    from duocad.kernel_brep import encode_brep

    restored = ki.decode_brep_unchecked(encode_brep(A))
    assert _brep_geometry.volume(restored) == pytest.approx(1000.0)


def test_shared_pieces_with_the_same_hash_are_neither_dropped_nor_swapped():
    """hash が衝突する別の形状の束(偽の型で作る)を、取り落とさず取り違えない。"""

    class Fake:
        def __init__(self, name):
            self.name = name

        def IsSame(self, other):
            return self.name == other.name

    a, b = Fake("a"), Fake("b")
    left = {1: [a, b]}
    right = {1: [Fake("b")], 2: [Fake("c")]}
    assert [x.name for x in ki._shared(left, right)] == ["b"]
    assert ki._shared({1: [a]}, {1: [b]}) == []


def test_the_render_triangles_do_not_touch_the_inputs():
    """描画の三角形を作っても入力の面に三角形分割が書き込まれない。落ちたら、描画するかどうかで後の組の GF の結果が変わる
    (像は入力と面の実体を共有する)。"""
    from OCP.BRep import BRep_Tool
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS


    first, second = box(0, 0, 0, 10, 10, 10), box(2, 2, 2, 6, 6, 6)  # 内側の箱の面がそのまま重なりの立体の面になる
    assert ki.pair_relation(first, second, deviation=0.1)["kind"] == "overlap"
    meshed = [
        face
        for shape in (first, second)
        for face in _brep_topology.subshapes(shape, TopAbs_FACE)
        if BRep_Tool.Triangulation_s(TopoDS.Face_s(face), TopLoc_Location()) is not None
    ]
    assert meshed == []
