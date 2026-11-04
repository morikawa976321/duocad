"""離れた柱をブーリアンを掛けずに束ねる近道(`kernel_extrude._fused_solids`)が、柱を順に fuse した結果と同じ形を返すこと。
落ちたら、近道の並びか許容差の補正が OCCT の fuse とずれ、離れた領域を選んだ保存済みのログで面・辺の番号が変わる。"""
from __future__ import annotations

import json
import math

import pytest
from OCP.BRep import BRep_Builder, BRep_Tool
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon, BRepBuilderAPI_MakeWire
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakePrism
from OCP.BRepTools import BRepTools
from OCP.TopAbs import TopAbs_VERTEX
from OCP.TopoDS import TopoDS
from OCP.gp import gp_Ax2, gp_Circ, gp_Dir, gp_Pnt, gp_Vec

from duocad import brep_geometry, brep_operations, brep_topology, kernel_extrude

UP = (0.0, 0.0, 1.0)


def _rectangle(x: float, y: float, width: float, height: float, z: float = 0.0):
    corners = [gp_Pnt(x, y, z), gp_Pnt(x + width, y, z), gp_Pnt(x + width, y + height, z), gp_Pnt(x, y + height, z)]
    return BRepBuilderAPI_MakeFace(BRepBuilderAPI_MakePolygon(*corners, True).Wire(), True).Face()


def _circle_wire(x: float, y: float, radius: float, z: float = 0.0):
    edge = BRepBuilderAPI_MakeEdge(gp_Circ(gp_Ax2(gp_Pnt(x, y, z), gp_Dir(0, 0, 1)), radius)).Edge()
    return BRepBuilderAPI_MakeWire(edge).Wire()


def _regions(count: int, z: float = 0.0, loose_vertex: bool = False) -> list:
    """長方形・円・穴あきの領域をピッチ 12 で並べる(どの2つも 2 mm 以上離れる)。"""
    faces = []
    for i in range(count):
        x, y = (i % 5) * 12.0, (i // 5) * 12.0
        kind = i % 3
        if kind == 0:
            face = _rectangle(x, y, 10.0, 6.0 + i % 4, z)
        elif kind == 1:
            face = BRepBuilderAPI_MakeFace(_circle_wire(x + 5.0, y + 5.0, 4.0, z), True).Face()
        else:
            maker = BRepBuilderAPI_MakeFace(BRepBuilderAPI_MakePolygon(
                gp_Pnt(x, y, z), gp_Pnt(x + 10, y, z), gp_Pnt(x + 10, y + 10, z), gp_Pnt(x, y + 10, z), True).Wire(), True)
            hole = _circle_wire(x + 5.0, y + 5.0, 2.0, z)
            hole.Reverse()
            maker.Add(hole)
            face = maker.Face()
        faces.append(face)
    if loose_vertex:
        # 頂点を辺の端から 2e-5 mm ずらし、許容差は 1e-7 のまま(fuse の後処理が許容差を上げる)
        vertex = TopoDS.Vertex_s(brep_topology.indexed_map(faces[0], TopAbs_VERTEX).FindKey(1))
        point = BRep_Tool.Pnt_s(vertex)
        BRep_Builder().UpdateVertex(vertex, gp_Pnt(point.X() + 2e-5, point.Y(), point.Z()), 1e-7)
    return faces


def _plate():
    return BRepPrimAPI_MakeBox(gp_Pnt(-5.0, -5.0, -8.0), 80.0, 60.0, 8.0).Shape()


def _bosses_and_tops():
    """板に離れた柱を立てた形と、その柱の上面の添字。"""
    base = kernel_extrude.extrude_regions(_regions(9), 3.0, "join", UP, target_shape=_plate())["shape"]
    faces = brep_topology._face_map(base)
    tops = [
        index for index in range(1, faces.Extent() + 1)
        if brep_geometry.classify_face(faces.FindKey(index)) == "plane"
        and brep_geometry._face_normal(faces.FindKey(index))[2] > 0.999
        and math.isclose(brep_geometry._face_props(faces.FindKey(index))[1][2], 3.0, abs_tol=1e-9)
    ]
    return base, tops


CASES = {
    "new": lambda: kernel_extrude.extrude_regions(_regions(13), 5.0, "new", UP),
    "join": lambda: kernel_extrude.extrude_regions(_regions(7), 3.0, "join", UP, target_shape=_plate()),
    "cut": lambda: kernel_extrude.extrude_regions(_regions(7), -4.0, "cut", UP, target_shape=_plate()),
    "revolve": lambda: kernel_extrude.revolve_regions(_regions(5), (0.0, -30.0, 0.0), (1.0, 0.0, 0.0), UP, 90.0, "new"),
    "loose vertex": lambda: kernel_extrude.extrude_regions(_regions(4, loose_vertex=True), 5.0, "new", UP),
    "faces": lambda: (lambda base, tops: kernel_extrude.extrude_faces(base, tops, 1.0, "join", target_shape=base))(*_bosses_and_tops()),
}


def _written(shape, path) -> str:
    BRepTools.Write_s(shape, str(path))
    return path.read_text()


def _outcome(result: dict, path) -> tuple:
    return _written(result["shape"], path), json.dumps(result["provenance"], sort_keys=True), result["volume_after"]


@pytest.mark.parametrize("case", sorted(CASES))
def test_apart_solids_give_the_shape_of_fusing_them_one_by_one(case, monkeypatch, tmp_path):
    """落ちたら、離れた柱を組んだ形が順の fuse と BRep の書き出し(並び・許容差まで)・由来・体積のどれかで違う。"""
    assembled = []
    real_union = kernel_extrude._apart_union
    monkeypatch.setattr(kernel_extrude, "_apart_union", lambda solids: assembled.append(len(solids)) or real_union(solids))
    shortcut = _outcome(CASES[case](), tmp_path / "shortcut.brep")
    assert assembled, "離れた柱なのに近道を通らなかった"

    monkeypatch.setattr(kernel_extrude, "_fused_solids", lambda solids, what: brep_operations.fuse_all(solids, what))
    fused = _outcome(CASES[case](), tmp_path / "fused.brep")
    assert shortcut == fused


def test_loose_vertex_needs_the_same_tolerance_correction_as_the_fuse():
    """落ちたら、例の形が許容差の補正を要しなくなり、上のテストの「loose vertex」が補正を確かめていない。"""
    faces = _regions(4, loose_vertex=True)
    vertex = TopoDS.Vertex_s(brep_topology.indexed_map(faces[0], TopAbs_VERTEX).FindKey(1))
    solids = [BRepPrimAPI_MakePrism(face, gp_Vec(0.0, 0.0, 5.0)).Shape() for face in faces]
    kernel_extrude._apart_union(solids)
    assert BRep_Tool.Tolerance_s(vertex) > 1e-5


def test_touching_overlapping_and_compound_inputs_go_through_the_boolean():
    """落ちたら、触れ合う柱・重なる柱・compound の柱まで近道へ入り、ブーリアンが繋ぐはずの形が別々の立体のまま残る。"""
    def prism(face):
        return BRepPrimAPI_MakePrism(face, gp_Vec(0.0, 0.0, 5.0)).Shape()

    touching = [prism(_rectangle(0.0, 0.0, 10.0, 10.0)), prism(_rectangle(10.0, 0.0, 10.0, 10.0))]
    overlapping = [prism(_rectangle(0.0, 0.0, 10.0, 10.0)), prism(_rectangle(5.0, 5.0, 10.0, 10.0))]
    barely_apart = [prism(_rectangle(0.0, 0.0, 10.0, 10.0)), prism(_rectangle(10.0 + 1e-4, 0.0, 10.0, 10.0))]
    in_a_compound = [brep_topology.compound([prism(_rectangle(0.0, 0.0, 10.0, 10.0))]), prism(_rectangle(30.0, 0.0, 10.0, 10.0))]
    for solids in (touching, overlapping, barely_apart, in_a_compound):
        assert not kernel_extrude._apart(solids)
    assert kernel_extrude._apart([prism(_rectangle(0.0, 0.0, 10.0, 10.0)), prism(_rectangle(10.01, 0.0, 10.0, 10.0))])

    joined = kernel_extrude.extrude_regions([_rectangle(0.0, 0.0, 10.0, 10.0), _rectangle(10.0, 0.0, 10.0, 10.0)], 5.0, "new", UP)
    assert brep_topology.face_count(joined["shape"]) == 6
