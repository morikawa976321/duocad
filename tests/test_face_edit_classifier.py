"""面の削除の関門(残した面の範囲)が使う、面ごとに作って点ごとに使い回す分類(`kernel_face_edit._classifier` ほか)。"""
from __future__ import annotations

import math

import pytest
from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Curve2d, BRepAdaptor_Surface
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
from OCP.BRepClass import BRepClass_FaceClassifier
from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
from OCP.BRepTools import BRepTools
from OCP.GeomAbs import GeomAbs_CurveType
from OCP.TopAbs import TopAbs_ShapeEnum
from OCP.TopExp import TopExp
from OCP.TopoDS import TopoDS
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt, gp_Pnt2d

from duocad import kernel_face_edit
from duocad import brep_topology as _brep_topology


@pytest.fixture(scope="module")
def plate():
    """穴 5×3 の板。穴の上の縁を r0.8 で丸める(多くの辺を持つ平面・円筒・トーラスがそろう)。"""
    shape = BRepPrimAPI_MakeBox(gp_Pnt(-25, -15, 0), 50, 30, 6).Shape()
    for i in range(5):
        for j in range(3):
            hole = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(-20 + 10 * i, -10 + 10 * j, -1), gp_Dir(0, 0, 1)), 2.5, 8).Shape()
            shape = BRepAlgoAPI_Cut(shape, hole).Shape()
    maker = BRepFilletAPI_MakeFillet(shape)
    edges = _brep_topology.indexed_map(shape, TopAbs_ShapeEnum.TopAbs_EDGE)
    for k in range(1, edges.Extent() + 1):
        edge = TopoDS.Edge_s(edges.FindKey(k))
        ends = [BRep_Tool.Pnt_s(v).Z() for v in (TopExp.FirstVertex_s(edge), TopExp.LastVertex_s(edge))]
        if all(abs(z - 6) < 1e-9 for z in ends) and BRepAdaptor_Curve(edge).GetType() == GeomAbs_CurveType.GeomAbs_Circle:
            maker.Add(0.8, edge)
    maker.Build()
    return maker.Shape()


def _faces(shape) -> list:
    faces = _brep_topology._face_map(shape)
    return [_brep_topology._face_at(faces, index) for index in range(1, faces.Extent() + 1)]


def _probes(face) -> list:
    """面の UV の範囲より少し広い格子と、辺の上・辺から少しずらした点。"""
    u1, u2, v1, v2 = BRepTools.UVBounds_s(face)
    points = [
        (u1 + (u2 - u1) * (i / 8 - 0.05), v1 + (v2 - v1) * (j / 8 - 0.05)) for i in range(10) for j in range(10)
    ]
    for edge in _brep_topology._face_edit_edges(face):
        curve = BRepAdaptor_Curve2d(edge, face)
        first, last = curve.FirstParameter(), curve.LastParameter()
        for t in (0.0, 0.37, 1.0):
            on = curve.Value(first + t * (last - first))
            for d in (0.0, 5e-8, 2e-7, 1e-4):
                points += [(on.X() + d, on.Y()), (on.X(), on.Y() - d)]
    return points


def test_a_classifier_reused_across_points_answers_like_one_made_per_point(plate):
    """落ちたら、面ごとに使い回す分類が点ごとに作る分類と違う位置を返す(削除の関門の合否が変わり、断る・通す形が変わる)。"""
    kinds = set()
    for face in _faces(plate):
        kinds.add(kernel_face_edit._surface_type(face))
        probes = _probes(face)
        expected = [BRepClass_FaceClassifier(face, gp_Pnt2d(u, v), 1e-7).State() for u, v in probes]
        classify = kernel_face_edit._classifier(face)
        assert [classify(u, v) for u, v in probes] == expected
        assert [classify(u, v) for u, v in reversed(probes)] == expected[::-1]
    assert {"plane", "cylinder", "torus"} <= kinds


def test_a_projection_off_by_whole_turns_is_brought_back_into_the_face(plate):
    """落ちたら、周期面で一周ずれた射影の角度を面の範囲へ戻せず、面の内側の点を「外」と判定する(正しい削除を断る)。"""
    torus = next(face for face in _faces(plate) if kernel_face_edit._surface_type(face) == "torus")
    u1, u2, v1, v2 = BRepTools.UVBounds_s(torus)
    adaptor = BRepAdaptor_Surface(torus)
    into_period = kernel_face_edit._face_period(torus)
    u, v = (u1 + u2) / 2, (v1 + v2) / 2
    for turns in (-2, -1, 1, 3):
        back_u, back_v = into_period(u + turns * adaptor.UPeriod(), v)
        assert back_u == pytest.approx(u, abs=1e-9) and u1 <= back_u < u1 + adaptor.UPeriod()
        assert back_v == pytest.approx(v, abs=1e-9)
    on_face = kernel_face_edit._point_on_face(torus)
    point = adaptor.Value(u, v)
    assert on_face((point.X(), point.Y(), point.Z()))
    assert not on_face((point.X(), point.Y(), point.Z() + 0.5))
    assert math.isclose(adaptor.UPeriod(), 2 * math.pi)
