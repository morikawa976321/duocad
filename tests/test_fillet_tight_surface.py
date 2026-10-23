"""曲面の上面とテーパー壁でも、接点・断面の半径・由来を保って丸まる。"""
import math

import numpy as np
import pytest
from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Cut
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_Transform
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder, BRepPrimAPI_MakePrism, BRepPrimAPI_MakeSphere
from OCP.Geom import Geom_BezierSurface
from OCP.GeomAbs import GeomAbs_SurfaceType
from OCP.GeomConvert import GeomConvert
from OCP.TColgp import TColgp_Array2OfPnt
from OCP.TopAbs import TopAbs_IN, TopAbs_OUT
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS
from OCP.gp import gp_Ax1, gp_Ax2, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec

from duocad import kernel, kernel_tight, kernel_tight_surface
from duocad import query
from duocad.document import Document
from duocad.region import region_ref
from duocad.sketch import add_ellipse, base_plane, sketch_create
from duocad.verbs import extrude, fillet
from duocad.verbs.add_solid import add_solid_body
from document_support import replay_through_json
from test_fillet_tight import _elliptic_post, _state


def _dome(post=None, spline=False, center=-20):
    post = _elliptic_post(6, 2, 8) if post is None else post
    if spline:
        poles = TColgp_Array2OfPnt(1, 3, 1, 3)
        for i, x in enumerate((-7, 0, 7), 1):
            for j, y in enumerate((-3, 0, 3), 1):
                poles.SetValue(i, j, gp_Pnt(x, y, 6 + (0.98 if i == 2 else -0.98)
                                           + (0.45 if j == 2 else -0.45)))
        surface = GeomConvert.SurfaceToBSplineSurface_s(Geom_BezierSurface(poles))
        tool = BRepPrimAPI_MakePrism(BRepBuilderAPI_MakeFace(surface, 1e-8).Face(), gp_Vec(0, 0, -10)).Shape()
    else:
        tool = BRepPrimAPI_MakeSphere(gp_Pnt(0, 0, center), 26).Shape()
    return BRepAlgoAPI_Common(post, tool).Shape()


def _taper_document(angle):
    doc = Document()
    sketch = sketch_create(doc, base_plane("XY"))
    add_ellipse(doc, sketch, (0, 0), 12, 4)
    body_id = extrude(doc, [region_ref(sketch, 1)], 5, taper=angle, mode="new").args["body_id"]
    return doc, body_id


def _outline(shape, curved=False):
    candidates = [face for face in kernel_tight._faces(shape)
                  if (BRepAdaptor_Surface(TopoDS.Face_s(face)).GetType() in
                      (GeomAbs_SurfaceType.GeomAbs_Sphere, GeomAbs_SurfaceType.GeomAbs_BSplineSurface)) == curved]
    spheres = [face for face in candidates if BRepAdaptor_Surface(TopoDS.Face_s(face)).GetType() == GeomAbs_SurfaceType.GeomAbs_Sphere]
    top = ((spheres or candidates)[0] if curved else max(
        (face for face in candidates if BRepAdaptor_Surface(TopoDS.Face_s(face)).GetType() == GeomAbs_SurfaceType.GeomAbs_Plane),
        key=lambda face: kernel_tight._a_point_on(face).Z()))
    boundary = [edge for edge in kernel_tight._edges(top)
                if not BRep_Tool.IsClosed_s(edge, TopoDS.Face_s(top)) and not BRep_Tool.Degenerated_s(edge)]
    return [i for i in range(1, kernel.edge_count(shape) + 1)
            if any(kernel.edge_of(shape, i).IsSame(edge) for edge in boundary)]


def _check_provenance(before, rounded):
    provenance = rounded["provenance"]
    assert provenance["deleted"] == []
    assert len(provenance["new_faces"]) == 4
    for old, successors in provenance["face_map"].items():
        assert len(successors) == 1
        assert BRepAdaptor_Surface(TopoDS.Face_s(kernel.face_of(before, int(old)))).GetType() == BRepAdaptor_Surface(
            TopoDS.Face_s(kernel.face_of(rounded["shape"], successors[0]))).GetType()


@pytest.mark.parametrize("radius", [1.0, 1.5, 1.95])
def test_dome_has_a_valid_crown_and_an_independently_calculated_radius(radius):
    """球の内側の同心球との交点を神託に、長い側の断面の接点と円弧を確かめる。"""
    shape = _dome()
    rounded = kernel.fillet_edges(shape, {edge: radius for edge in _outline(shape, True)})
    result = rounded["shape"]
    assert BRepCheck_Analyzer(result).IsValid()
    assert len(kernel.split_solids(result)) == 1
    assert kernel.face_count(result) == 7
    _check_provenance(shape, rounded)
    cy = 2 - radius
    cz = -20 + math.sqrt((26 - radius) ** 2 - cy ** 2)
    end = math.atan2(cz + 20, cy)
    for fraction in (0.0, 0.1, 0.5, 0.9, 1.0):
        theta = fraction * end
        for scale, expected in ((0.999, TopAbs_IN), (1.001, TopAbs_OUT)):
            assert _state(result, 0, cy + scale * radius * math.cos(theta), cz + scale * radius * math.sin(theta)) == expected
    assert _state(result, 0, 2 + 1e-3, cz) == TopAbs_OUT
    assert _state(result, 0, 2 - 1e-3, cz) == TopAbs_IN


def test_a_non_spherical_bspline_top_uses_the_same_contact_solver():
    """球面の解析式に依存した実装では、放物面の B スプライン上面を丸められない。"""
    shape = _dome(spline=True)
    rounded = kernel.fillet_edges(shape, {edge: 1.0 for edge in _outline(shape, True)})
    assert BRepCheck_Analyzer(rounded["shape"]).IsValid()
    _check_provenance(shape, rounded)


def test_dome_volume_matches_an_independent_integral():
    """球面と壁の非平面の縁を既定の積分で測ると、約 0.008 mm³ ずれる。"""
    nodes, weights = np.polynomial.legendre.leggauss(128)
    angles = math.pi * (nodes + 1)
    q = 36 * np.cos(angles) ** 2 + 4 * np.sin(angles) ** 2
    expected = 12 * math.pi * np.sum(weights * ((26 ** 3 - (26 ** 2 - q) ** 1.5) / (3 * q) - 10))
    assert kernel.volume(_dome()) == pytest.approx(expected, abs=2e-6)


@pytest.mark.parametrize("angle,radius", [(-5, 0.8), (-5, 1.0), (-5, 1.65), (5, 1.0), (5, 1.8), (5, 2.2)])
def test_tapered_wall_has_the_independently_calculated_non_quarter_circle(angle, radius):
    """元の楕円の支持線と傾斜角から求めた円弧は、90°±5°で上面と壁に接する。"""
    doc, body = _taper_document(angle)
    shape = doc.get_body(body).shape
    rounded = kernel.fillet_edges(shape, {edge: radius for edge in _outline(shape)})
    result = rounded["shape"]
    assert BRepCheck_Analyzer(result).IsValid()
    assert len(kernel.split_solids(result)) == 1
    theta = math.radians(angle)
    cy = 2 + 5 * math.tan(theta) - radius * (1 / math.cos(theta) + math.tan(theta))
    cz = 5 - radius
    for fraction in (0.0, 0.1, 0.5, 0.9, 1.0):
        phi = -theta + fraction * (math.pi / 2 + theta)
        for scale, expected in ((0.999, TopAbs_IN), (1.001, TopAbs_OUT)):
            assert _state(result, 0, cy + scale * radius * math.cos(phi), cz + scale * radius * math.sin(phi)) == expected
    _check_provenance(shape, rounded)


@pytest.mark.parametrize("kind", ["dome", -5, 5])
def test_small_radii_do_not_call_the_extended_builder(kind, monkeypatch):
    """曲率半径以下では、拡張経路を使わず OCCT の結果を返す。"""
    shape = _dome() if kind == "dome" else _taper_document(kind)[0].bodies[0].shape
    edges = _outline(shape, kind == "dome")
    native = BRepFilletAPI_MakeFillet(shape)
    for edge in edges:
        native.Add(0.2, TopoDS.Edge_s(kernel.edge_of(shape, edge)))
    native.Build()
    assert native.IsDone()

    def unexpected(*args):
        pytest.fail("a small radius entered the extended builder")

    monkeypatch.setattr(kernel_tight, "round_tight_loop", unexpected)
    rounded = kernel.fillet_edges(shape, {edge: 0.2 for edge in edges})
    assert rounded["volume_after"] == pytest.approx(kernel.volume(native.Shape()), rel=1e-12)
    assert kernel.face_count(rounded["shape"]) == kernel.face_count(native.Shape())


@pytest.mark.parametrize("kind", ["dome", -5, 5])
def test_extended_fillet_is_logged_undone_redone_and_reopened(kind):
    """開き直しでも形の体積・面数と操作ログの由来が保たれる。"""
    if kind == "dome":
        doc = Document()
        body = add_solid_body(doc, _dome(), "dome")
    else:
        doc, body = _taper_document(kind)
    shape = doc.get_body(body).shape
    before = kernel.volume(shape)
    fillet(doc, _outline(shape, kind == "dome"), 1.0 if kind != 5 else 2.2, body_id=body)
    after = doc.get_body(body).shape
    volume = kernel.volume(after)
    faces = kernel.face_count(after)
    doc.log.undo(doc)
    assert kernel.volume(doc.get_body(body).shape) == pytest.approx(before)
    doc.log.redo(doc)
    assert kernel.volume(doc.get_body(body).shape) == pytest.approx(volume)
    reopened = replay_through_json(doc)
    restored = reopened.get_body(body).shape
    assert BRepCheck_Analyzer(restored).IsValid()
    assert kernel.volume(restored) == pytest.approx(volume, rel=1e-10)
    assert kernel.face_count(restored) == faces


@pytest.mark.parametrize("motion", ["rotate", "invert", "mirror", "located"])
@pytest.mark.parametrize("kind", ["dome", -5, 5])
def test_extended_fillet_does_not_depend_on_world_z(kind, motion):
    """回転・反転・Location があっても、上面と壁に従って丸める。"""
    shape = _dome() if kind == "dome" else _taper_document(kind)[0].bodies[0].shape
    transform = gp_Trsf()
    if motion in ("rotate", "located"):
        transform.SetRotation(gp_Ax1(gp_Pnt(), gp_Dir(1, 2, 3)), 0.7)
    elif motion == "invert":
        transform.SetRotation(gp_Ax1(gp_Pnt(), gp_Dir(1, 0, 0)), math.pi)
    else:
        transform.SetMirror(gp_Ax2(gp_Pnt(), gp_Dir(0, 0, 1)))
    edges = _outline(shape, kind == "dome")
    if motion == "located":
        transform.SetTranslationPart(gp_Vec(11, -4, 9))
        location = TopLoc_Location(transform)
        moved = shape.Moved(location)
        moved_edges = [kernel.edge_of(shape, edge).Moved(location) for edge in edges]
    else:
        mover = BRepBuilderAPI_Transform(shape, transform, True)
        moved = mover.Shape()
        moved_edges = [mover.ModifiedShape(kernel.edge_of(shape, edge)) for edge in edges]
    selected = [i for i in range(1, kernel.edge_count(moved) + 1)
                if any(kernel.edge_of(moved, i).IsSame(edge) for edge in moved_edges)]
    radius = 2.2 if kind == 5 else 1.0
    rounded = kernel.fillet_edges(moved, {edge: radius for edge in selected})
    assert BRepCheck_Analyzer(rounded["shape"]).IsValid()
    assert 0 < rounded["volume_after"] < rounded["volume_before"]
    if kind == "dome":
        cy = 2 - radius
        cz = -20 + math.sqrt((26 - radius) ** 2 - cy ** 2)
        begin, end = 0, math.atan2(cz + 20, cy)
    else:
        theta = math.radians(kind)
        cy = 2 + 5 * math.tan(theta) - radius * (1 / math.cos(theta) + math.tan(theta))
        cz = 5 - radius
        begin, end = -theta, math.pi / 2
    for fraction in (0.1, 0.5, 0.9):
        angle = begin + fraction * (end - begin)
        for scale, expected in ((0.999, TopAbs_IN), (1.001, TopAbs_OUT)):
            point = gp_Pnt(0, cy + radius * scale * math.cos(angle), cz + radius * scale * math.sin(angle)).Transformed(transform)
            assert _state(rounded["shape"], *point.Coord()) == expected


def test_taper_combined_with_curved_top_is_refused_with_a_reason():
    """対象外の組み合わせも、曲率半径を超える丸めの理由を失敗文面に載せる。"""
    doc, body = _taper_document(-5)
    shape = _dome(doc.get_body(body).shape, center=-22)
    with pytest.raises(ValueError, match="曲率半径を超える丸めもできない: .*curved top combined with tapered"):
        kernel.fillet_edges(shape, {edge: 1.0 for edge in _outline(shape, True)})


def test_a_curved_top_with_a_hole_is_refused_with_the_hole_reason():
    """外周だけを選んでも、上面の内側の輪を見落とさずに穴の理由を返す。"""
    shape = BRepAlgoAPI_Cut(_dome(), BRepPrimAPI_MakeCylinder(0.2, 10).Shape()).Shape()
    edges = [edge["index"] for edge in query.describe_edges(shape) if edge["length"] > 20 and edge["start"][2] > 1]
    assert len(edges) == 1
    with pytest.raises(ValueError, match="曲率半径を超える丸めもできない: .*top face has holes"):
        kernel.fillet_edges(shape, {edge: 1.0 for edge in edges})


@pytest.mark.parametrize("height", [1.0, 4.0])
def test_a_cavity_is_preserved_below_the_cut_and_refused_above_it(height):
    """切断面より上の別の面は断り、下の空洞の殻と由来は保つ。"""
    dome = _dome()
    hole = BRepPrimAPI_MakeSphere(gp_Pnt(0, 0, height), 0.2).Shape()
    shape = BRepAlgoAPI_Cut(dome, hole).Shape()
    edges = _outline(shape, True)
    if height == 4.0:
        with pytest.raises(ValueError, match="above the cut there must be only the top and its wall band"):
            kernel.fillet_edges(shape, {edge: 1.0 for edge in edges})
    else:
        rounded = kernel.fillet_edges(shape, {edge: 1.0 for edge in edges})
        assert BRepCheck_Analyzer(rounded["shape"]).IsValid()
        assert _state(rounded["shape"], 0, 0, height) == TopAbs_OUT
        plain = kernel.fillet_edges(dome, {edge: 1.0 for edge in _outline(dome, True)})
        assert rounded["volume_after"] == pytest.approx(plain["volume_after"] - kernel.volume(hole), abs=1e-8)
        assert rounded["provenance"]["deleted"] == []
        assert all(rounded["provenance"]["face_map"].values())


def test_a_projected_crossing_with_different_spatial_centers_is_refused(monkeypatch):
    """射影で重なる2点の高さが違う場合、管面を作る前に理由つきで断る。"""
    original = kernel_tight_surface._Contacts.center

    def lifted(self, s, radius):
        point = original(self, s, radius)
        return point.Translated(self.up.Multiplied(0.01 * math.sin(2 * math.pi * s / self.length)))

    monkeypatch.setattr(kernel_tight_surface._Contacts, "center", lifted)
    shape = _dome()
    with pytest.raises(kernel_tight.Declined, match="projected crossing has distinct spatial centers"):
        kernel_tight.round_tight_loop(shape, kernel._edge_map(shape), {edge: 1.0 for edge in _outline(shape, True)})


def test_a_tapered_outline_is_an_offset_curve_and_not_an_ellipse():
    """半軸だけから推定した曲率半径と、実際のテーパー上縁の曲率半径を取り違えない。"""
    doc, body = _taper_document(-5)
    shape = doc.get_body(body).shape
    from OCP.BRepAdaptor import BRepAdaptor_Curve

    edge = BRepAdaptor_Curve(TopoDS.Edge_s(kernel.edge_of(shape, _outline(shape)[0])))
    point, first, second = gp_Pnt(), gp_Vec(), gp_Vec()
    edge.D2(edge.FirstParameter(), point, first, second)
    curvature_radius = first.Magnitude() ** 3 / first.Crossed(second).Magnitude()
    assert 0.22 < curvature_radius < 0.24
    rounded = kernel.fillet_edges(shape, {edge: 0.3 for edge in _outline(shape)})
    assert BRepCheck_Analyzer(rounded["shape"]).IsValid()
