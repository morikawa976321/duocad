"""複数の面を押したときの行き先(`kernel_extrude.extrude_faces` の `after_face_indices`)。"""
from __future__ import annotations

import math

import pytest
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon, BRepBuilderAPI_MakeWire
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.gp import gp_Ax2, gp_Circ, gp_Dir, gp_Pnt

from duocad import brep_geometry, brep_topology, kernel_extrude

UP = (0.0, 0.0, 1.0)


def _rectangle(x: float, y: float, width: float, height: float):
    corners = [gp_Pnt(x, y, 0.0), gp_Pnt(x + width, y, 0.0), gp_Pnt(x + width, y + height, 0.0), gp_Pnt(x, y + height, 0.0)]
    return BRepBuilderAPI_MakeFace(BRepBuilderAPI_MakePolygon(*corners, True).Wire(), True).Face()


def _circle_wire(x: float, y: float, radius: float):
    edge = BRepBuilderAPI_MakeEdge(gp_Circ(gp_Ax2(gp_Pnt(x, y, 0.0), gp_Dir(0, 0, 1)), radius)).Edge()
    return BRepBuilderAPI_MakeWire(edge).Wire()


def _regions(count: int) -> list:
    """長方形・円・穴あきの領域をピッチ 12 で並べる(どの2つも 2 mm 以上離れる)。"""
    faces = []
    for i in range(count):
        x, y = (i % 5) * 12.0, (i // 5) * 12.0
        kind = i % 3
        if kind == 0:
            face = _rectangle(x, y, 10.0, 6.0 + i % 4)
        elif kind == 1:
            face = BRepBuilderAPI_MakeFace(_circle_wire(x + 5.0, y + 5.0, 4.0), True).Face()
        else:
            maker = BRepBuilderAPI_MakeFace(BRepBuilderAPI_MakePolygon(
                gp_Pnt(x, y, 0), gp_Pnt(x + 10, y, 0), gp_Pnt(x + 10, y + 10, 0), gp_Pnt(x, y + 10, 0), True).Wire(), True)
            hole = _circle_wire(x + 5.0, y + 5.0, 2.0)
            hole.Reverse()
            maker.Add(hole)
            face = maker.Face()
        faces.append(face)
    return faces


def _bosses_and_tops():
    """板に離れた柱を立てた形と、その柱の上面の添字。"""
    plate = BRepPrimAPI_MakeBox(gp_Pnt(-5.0, -5.0, -8.0), 80.0, 60.0, 8.0).Shape()
    base = kernel_extrude.extrude_regions(_regions(9), 3.0, "join", UP, target_shape=plate)["shape"]
    faces = brep_topology._face_map(base)
    tops = [
        index for index in range(1, faces.Extent() + 1)
        if brep_geometry.classify_face(faces.FindKey(index)) == "plane"
        and brep_geometry._face_normal(faces.FindKey(index))[2] > 0.999
        and math.isclose(brep_geometry._face_props(faces.FindKey(index))[1][2], 3.0, abs_tol=1e-9)
    ]
    return base, tops


def test_each_pushed_face_finds_its_own_lid_in_selection_order():
    """落ちたら、複数の面を押したときの行き先(`after_face_indices`)が選んだ順の面のふたを指していない。"""
    base, tops = _bosses_and_tops()
    faces = brep_topology._face_map(base)
    picked = [tops[i] for i in (4, 0, 7, 2)]
    expected = [brep_geometry._face_props(faces.FindKey(index))[1] for index in picked]

    result = kernel_extrude.extrude_faces(base, picked, 1.0, "join", target_shape=base)

    lids = brep_topology._face_map(result["shape"])
    found = result["after_face_indices"]
    assert len(set(found)) == len(picked)
    for index, (x, y, z) in zip(found, expected):
        assert brep_geometry._face_normal(lids.FindKey(index))[2] > 0.999
        assert brep_geometry._face_props(lids.FindKey(index))[1] == pytest.approx((x, y, z + 1.0), abs=1e-9)
