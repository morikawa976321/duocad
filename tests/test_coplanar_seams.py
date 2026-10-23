"""隣り合う柱・ボディを束ねたときに同一平面の継ぎ目が残らないこと(issue #141)。落ちたら、
継ぎ目が残ってまたぐ辺を丸められないか、統合しない経路まで統合して保存済みログの番号がずれる。"""
from __future__ import annotations

import math

import pytest
from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.gp import gp_Pnt

from duocad import kernel, query
from duocad.document import Body, Document
from duocad.region import region_ref, regions
from duocad.sketch import add_line, add_rectangle, sketch_create
from duocad.verbs import extrude, extrude_faces
from duocad.verbs.boolean_body import boolean_body

from duocad import (
    brep_topology as _brep_topology,
)

SIDE = 10.0
HEIGHT = 5.0
MERGED_VOLUME = 2 * SIDE * SIDE * HEIGHT


def _box(x: float):
    return BRepPrimAPI_MakeBox(gp_Pnt(x, 0.0, 0.0), SIDE, SIDE, HEIGHT).Shape()


def _seamed_box():
    """統合せずに束ねた 20×10×5(実測 10面)。"""
    fused = BRepAlgoAPI_Fuse(_box(0.0), _box(SIDE))
    fused.Build()
    return fused.Shape()


def _counts(shape) -> tuple:
    return _brep_topology.face_count(shape), _brep_topology.edge_count(shape)


def _two_squares_sketch(document: Document) -> str:
    sketch_id = sketch_create(document, "XY")
    add_rectangle(document, sketch_id, (0.0, 0.0), SIDE, SIDE)
    add_rectangle(document, sketch_id, (SIDE, 0.0), SIDE, SIDE)
    return sketch_id


def _face_index(shape, normal: tuple, centroid: tuple) -> int:
    for face in kernel.tessellate(shape)["faces"]:
        if all(math.isclose(a, b, abs_tol=1e-6) for a, b in zip(face["normal"], normal)) and all(
            math.isclose(a, b, abs_tol=1e-6) for a, b in zip(face["centroid"], centroid)
        ):
            return face["index"]
    raise AssertionError(f"no face with normal {normal} at {centroid}")


def test_extruding_two_adjacent_regions_as_new_gives_one_box():
    """落ちたら `extrude_regions` が柱を束ねた後に統合していない。"""
    document = Document()
    sketch_id = _two_squares_sketch(document)
    found = regions(document, sketch_id)
    assert len(found) == 2

    operation = extrude(document, [region_ref(sketch_id, region.index) for region in found], HEIGHT, mode="new")

    body = document.active_body()
    assert body is not None
    assert _counts(body.shape) == (6, 12)
    assert math.isclose(query.volume(body.shape), MERGED_VOLUME, rel_tol=1e-9)
    assert operation.provenance["unified"] is True
    assert operation.provenance["face_map"] == {}
    assert operation.provenance["new_faces"] == [1, 2, 3, 4, 5, 6]


def test_joining_a_region_next_to_a_body_merges_the_faces_and_maps_them():
    """join の継ぎ目も消え、由来は旧ボディの面を統合後の面へ張る(継ぎ目の面だけが消える)。"""
    document = Document()
    document.add_body(Body(id="box", name="box", kind="solid", shape=_box(0.0)))
    old_top = _face_index(document.get_body("box").shape, (0, 0, 1), (5, 5, HEIGHT))
    old_seam = _face_index(document.get_body("box").shape, (1, 0, 0), (SIDE, 5, HEIGHT / 2))
    sketch_id = sketch_create(document, "XY")
    add_rectangle(document, sketch_id, (SIDE, 0.0), SIDE, SIDE)
    found = regions(document, sketch_id)

    operation = extrude(document, [region_ref(sketch_id, found[0].index)], HEIGHT, mode="join", body_id="box")

    shape = document.get_body("box").shape
    assert _counts(shape) == (6, 12)
    provenance = operation.provenance
    assert provenance["unified"] is True
    assert provenance["deleted"] == [old_seam]
    assert provenance["face_map"][str(old_top)] == [_face_index(shape, (0, 0, 1), (10, 5, HEIGHT))]


def test_a_single_prism_as_new_is_not_merged():
    """柱1本の new は束ねていないので統合しない。落ちたら、輪郭の分割で割れた側面までまとまり、
    後の操作が記録した面・辺の番号がずれる。"""
    document = Document()
    sketch_id = sketch_create(document, "XY")
    # 下辺を同一直線上の2本に割った長方形 → 側面5枚 + 上下 = 7面(統合すれば 6面)
    for start, end in [((0, 0), (10, 0)), ((10, 0), (20, 0)), ((20, 0), (20, 10)), ((20, 10), (0, 10)), ((0, 10), (0, 0))]:
        add_line(document, sketch_id, start, end)
    found = regions(document, sketch_id)
    assert len(found) == 1

    operation = extrude(document, [region_ref(sketch_id, found[0].index)], HEIGHT, mode="new")

    assert _brep_topology.face_count(document.active_body().shape) == 7
    assert operation.provenance["unified"] is False


def test_union_of_adjacent_bodies_gives_one_box_and_maps_the_target_faces():
    """落ちたら `boolean_bodies` の union が統合していないか、由来を張れていない。"""
    document = Document()
    document.add_body(Body(id="left", name="left", kind="solid", shape=_box(0.0)))
    document.add_body(Body(id="right", name="right", kind="solid", shape=_box(SIDE)))
    old_seam = _face_index(document.get_body("left").shape, (1, 0, 0), (SIDE, 5, HEIGHT / 2))

    operation = boolean_body(document, {"body": "left", "kind": "face", "index": 1}, ["right"], "union")

    shape = document.get_body("left").shape
    assert _counts(shape) == (6, 12)
    assert math.isclose(query.volume(shape), MERGED_VOLUME, rel_tol=1e-9)
    assert operation.provenance["unified"] is True
    assert operation.provenance["deleted"] == [old_seam]
    assert all(operation.provenance["face_map"][key] for key in operation.provenance["face_map"] if key != str(old_seam))


@pytest.mark.parametrize(
    ("mode", "tool", "faces"),
    [
        # 統合すれば cut は 11面、intersect は 6面になる(実測)
        ("cut", BRepPrimAPI_MakeBox(gp_Pnt(1.0, 1.0, 4.0), 2.0, 2.0, 2.0).Shape(), 15),
        ("intersect", BRepPrimAPI_MakeBox(gp_Pnt(-1.0, -1.0, -1.0), 22.0, 12.0, 7.0).Shape(), 10),
    ],
)
def test_cut_and_intersect_leave_the_seams_as_they_are(mode, tool, faces):
    """統合は union だけ。落ちたら cut / intersect でも統合していて、継ぎ目の面の番号が変わる。"""
    result = kernel.boolean_bodies(_seamed_box(), [tool], mode)
    assert result["unified"] is False
    assert _brep_topology.face_count(result["shape"]) == faces


def test_the_long_edge_across_the_former_seam_can_be_filleted():
    """継ぎ目をまたいでいた長さ20の辺が1本になり、丸められる(体積は (1 - π/4)·r²·20 だけ減る)。"""
    shape = kernel.boolean_bodies(_box(0.0), [_box(SIDE)], "union")["shape"]
    long_edges = [edge["index"] for edge in kernel.tessellate_edges(shape) if math.isclose(edge["length"], 2 * SIDE)]
    assert len(long_edges) == 4

    radius = 1.0
    filleted = kernel.fillet_edges(shape, {long_edges[0]: radius})["shape"]

    removed = (1 - math.pi / 4) * radius**2 * 2 * SIDE
    assert math.isclose(query.volume(filleted), MERGED_VOLUME - removed, rel_tol=1e-6)


def test_extruding_two_coplanar_faces_as_new_gives_one_box():
    """`extrude_faces` の new も、柱が複数なら統合する(柱1本の new は従来どおり統合しない)。"""
    document = Document()
    document.add_body(Body(id="split", name="split", kind="solid", shape=_seamed_box()))
    shape = document.get_body("split").shape
    tops = [_face_index(shape, (0, 0, 1), (5, 5, HEIGHT)), _face_index(shape, (0, 0, 1), (15, 5, HEIGHT))]

    operation = extrude_faces(document, [{"body": "split", "index": index} for index in tops], 3.0, mode="new")

    created = next(body for body in document.bodies if body.id != "split")
    assert _counts(created.shape) == (6, 12)
    assert operation.provenance["unified"] is True
