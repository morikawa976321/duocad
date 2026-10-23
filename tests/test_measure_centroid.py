"""**重心は duocad が測る**(issue #136)—— 面・領域の面積重心とボディの体積重心が `measure` に返る。

AI が公式から重心を計算して取り違える(等脚台形の重心の高さ 160/9 を 200/9 にする)ので、読める値を返す。
"""
from __future__ import annotations

import math

import pytest
from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder

from duocad import query
from duocad.document import Body
from duocad.measure import measure_selection
from duocad.region import plane_frame, region_ref
from duocad.server import Session
from duocad.sketch import add_line, base_plane, sketch_create
from duocad.verbs import extrude

#: 下底80・上底40・高さ40の等脚台形。面積重心の高さは h(a+2b)/(3(a+b)) = 40·160/360 = 160/9。
TRAPEZOID = ((0.0, 0.0), (80.0, 0.0), (60.0, 40.0), (20.0, 40.0))
CENTROID_UV = (40.0, 160.0 / 9.0)
THICKNESS = 10.0


def test_an_oblique_rational_bspline_ellipse_prism_keeps_its_centroid():
    """斜め押し出しの縁の高さを理由に、側面を不正確な適応積分へ切り替えない。"""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakeWire
    from OCP.BRepPrimAPI import BRepPrimAPI_MakePrism
    from OCP.Geom import Geom_Ellipse
    from OCP.GeomConvert import GeomConvert
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt, gp_Vec
    from duocad import kernel

    ellipse = Geom_Ellipse(gp_Ax2(gp_Pnt(), gp_Dir(0, 0, 1)), 6, 2)
    curve = GeomConvert.CurveToBSplineCurve_s(ellipse)
    face = BRepBuilderAPI_MakeFace(BRepBuilderAPI_MakeWire(BRepBuilderAPI_MakeEdge(curve).Edge()).Wire()).Face()
    shape = BRepPrimAPI_MakePrism(face, gp_Vec(5, 0, 5)).Shape()
    assert kernel.centroid(shape) == pytest.approx([2.5, 0, 2.5], abs=0.0042)

#: 原点まわりの単位四面体(巻きは外向き)。体積重心は (1/4, 1/4, 1/4)。
TETRA_POSITIONS = [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
TETRA_INDICES = [0, 2, 1, 0, 1, 3, 0, 3, 2, 1, 2, 3]


def trapezoid_sketch(session: Session, plane: str) -> str:
    sketch_id = sketch_create(session.document, base_plane(plane))
    for start, end in zip(TRAPEZOID, TRAPEZOID[1:] + TRAPEZOID[:1]):
        add_line(session.document, sketch_id, start, end)
    return sketch_id


def measure_one(session: Session, ref: dict) -> dict:
    session.clear_selection()
    session.modify_selection([ref], [])
    return measure_selection(session)["items"][0]


@pytest.mark.parametrize("plane", ["XY", "XZ", "YZ"])
def test_a_region_reports_its_area_centroid_in_sketch_and_world_coordinates(plane: str):
    """落ちたら、押し出す前に重心で位置を決める手段が無い(または (u, v) とワールドの対応がずれた)。"""
    session = Session(0.2)
    sketch_id = trapezoid_sketch(session, plane)

    item = measure_one(session, {"sketch": sketch_id, "index": 1})

    assert item["center_uv"] == pytest.approx(CENTROID_UV)
    frame = plane_frame(session.document, sketch_id)
    expected = [
        frame["origin"][axis] + CENTROID_UV[0] * frame["u"][axis] + CENTROID_UV[1] * frame["v"][axis]
        for axis in range(3)
    ]
    assert item["center"] == pytest.approx(expected)


def test_a_planar_face_and_the_body_report_their_centroids():
    """落ちたら、押し出した台形の上面やボディの重心を AI が自分で計算することになる。"""
    session = Session(0.2)
    sketch_id = trapezoid_sketch(session, "XY")
    extrude(session.document, [region_ref(sketch_id, 1)], THICKNESS)
    body = session.document.bodies[0]
    top = next(
        face["index"]
        for face in query.describe_faces(body.shape)
        if face["kind"] == "plane" and face["normal"][2] > 0.99
    )

    face = measure_one(session, {"body": body.id, "kind": "face", "index": top})
    whole = measure_one(session, {"body": body.id, "kind": "body", "index": None})

    assert face["center"] == pytest.approx([*CENTROID_UV, THICKNESS])
    assert whole["center"] == pytest.approx([*CENTROID_UV, THICKNESS / 2.0])


def test_a_curved_face_reports_its_area_centroid_not_a_point_on_the_surface():
    """円柱の側面の面積重心は軸の上。落ちたら `center` が面の種類で意味を変えている(UV 範囲の中点に戻った)。"""
    session = Session(0.2)
    session.document.add_body(
        Body(id="cyl", name="cyl", kind="solid", shape=BRepPrimAPI_MakeCylinder(10.0, 20.0).Shape())
    )
    side = next(
        face["index"]
        for face in query.describe_faces(session.document.get_body("cyl").shape)
        if face["kind"].startswith("cylinder")
    )

    item = measure_one(session, {"body": "cyl", "kind": "face", "index": side})

    assert item["center"] == pytest.approx([0.0, 0.0, 10.0], abs=1e-9)
    assert math.isclose(item["area"], 2.0 * math.pi * 10.0 * 20.0, rel_tol=1e-9)


def test_a_mesh_body_reports_its_volume_centroid():
    """落ちたら、メッシュボディだけ重心が返らない(B-rep と別の計算経路)。"""
    session = Session(0.2)
    body_id = session.add_mesh_body(TETRA_POSITIONS, TETRA_INDICES, "tetra")["body_id"]

    item = measure_one(session, {"body": body_id, "kind": "body", "index": None})

    assert item["center"] == pytest.approx([0.25, 0.25, 0.25])
