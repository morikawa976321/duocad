"""曲面の上面とテーパー壁でも、接点・断面の半径・由来を保って丸まる。"""
import math
import time

import numpy as np
import pytest
from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Cut
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakeWire, BRepBuilderAPI_Transform
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
from OCP.BRepGProp import BRepGProp
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder, BRepPrimAPI_MakePrism, BRepPrimAPI_MakeSphere
from OCP.Geom import Geom_BezierSurface
from OCP.GC import GC_MakeArcOfCircle
from OCP.GeomAbs import GeomAbs_SurfaceType
from OCP.GeomConvert import GeomConvert
from OCP.GProp import GProp_GProps
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
from test_fillet_tight import _elliptic_post, _lens_area, _native_done, _state


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


def _taper_document(angle, height=5, rotation=0):
    doc = Document()
    sketch = sketch_create(doc, base_plane("XY"))
    add_ellipse(doc, sketch, (0, 0), 12, 4, angle=rotation)
    body_id = extrude(doc, [region_ref(sketch, 1)], height, taper=angle, mode="new").args["body_id"]
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


def _spherical_lens_area(radius, nodes=256):
    """楕円の内側平行体の極半径から、球面上のレンズ面積を独立に積分する。"""
    abscissae, weights = np.polynomial.legendre.leggauss(nodes)
    phi = math.pi / 4 * (abscissae + 1)
    crossing = math.asin(math.sqrt(((radius * 6 / 2) ** 2 - 4) / 32))
    low, high = np.full(nodes, crossing), np.full(nodes, math.pi / 2)
    for _ in range(60):
        t = (low + high) / 2
        n = np.hypot(2 * np.cos(t), 6 * np.sin(t))
        x, y = 6 * np.cos(t) - radius * 2 * np.cos(t) / n, 2 * np.sin(t) - radius * 6 * np.sin(t) / n
        before = np.arctan2(y, x) < phi
        low, high = np.where(before, t, low), np.where(before, high, t)
    rho = np.hypot(x, y) * 26 / (26 - radius)
    return math.pi * np.dot(weights, 26 * rho ** 2 / (26 + np.sqrt(26 ** 2 - rho ** 2)))


def _precise_area(face):
    """自由曲線で切った面の面積を、精度指定の積分で測る。"""
    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face, props, 1e-9)
    return props.Mass()


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
    lens = next(face for face in kernel_tight._faces(result)
                if BRepAdaptor_Surface(TopoDS.Face_s(face)).GetType() == GeomAbs_SurfaceType.GeomAbs_Sphere)
    assert _spherical_lens_area(radius, 128) == pytest.approx(_spherical_lens_area(radius), abs=1e-9)
    assert _precise_area(lens) == pytest.approx(_spherical_lens_area(radius), abs=2e-6)
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


@pytest.mark.parametrize("angle,radius", [(-5, 0.8), (-5, 1), (-5, 1.65), (5, 1.8), (5, 2.2)])
def test_tapered_lens_area_matches_the_independent_parallel_ellipse_integral(angle, radius):
    """実装の接点や道を使わず、底の楕円と傾斜角から上面のレンズ面積を求める。"""
    doc, body = _taper_document(angle)
    shape = doc.get_body(body).shape
    result = kernel.fillet_edges(shape, {edge: radius for edge in _outline(shape)})["shape"]
    lens = max((face for face in kernel_tight._faces(result)
                if BRepAdaptor_Surface(TopoDS.Face_s(face)).GetType() == GeomAbs_SurfaceType.GeomAbs_Plane),
               key=lambda face: kernel_tight._a_point_on(face).Z())
    theta = math.radians(angle)
    offset = radius / math.cos(theta) - (5 - radius) * math.tan(theta)
    assert _precise_area(lens) == pytest.approx(_lens_area(6, 2, offset), abs=1e-4)


@pytest.mark.parametrize("kind", ["taper", "dome"])
def test_a_thin_body_uses_a_cut_just_below_the_contacts(kind):
    """高さが 2R 程度でも、壁の接点が底から離れていれば丸められる。"""
    shape = _dome(center=-23) if kind == "dome" else _taper_document(-5, 2.5)[0].bodies[0].shape
    radius = 1.5 if kind == "dome" else 1.4
    assert not _native_done(shape, _outline(shape, kind == "dome"), radius)
    rounded = kernel.fillet_edges(shape, {edge: radius for edge in _outline(shape, kind == "dome")})
    assert BRepCheck_Analyzer(rounded["shape"]).IsValid()
    assert 0 < rounded["volume_after"] < rounded["volume_before"]
    _check_provenance(shape, rounded)


def test_contacts_reaching_the_bottom_are_refused_with_a_reason():
    """接点が底を突き抜ける部品は、底に届く可能性を文面に載せて断る。"""
    shape = _taper_document(-5, 1)[0].bodies[0].shape
    with pytest.raises(ValueError, match="曲率半径を超える丸めもできない: .*bottom"):
        kernel.fillet_edges(shape, {edge: 1.4 for edge in _outline(shape)})


def test_a_result_removing_more_than_the_band_above_the_cut_is_refused(monkeypatch):
    """有効な小立体でも、切断面より上の体積を超えて削る結果は返さない。"""
    shape = _dome()
    top = next(face for face in kernel_tight._faces(shape)
               if BRepAdaptor_Surface(TopoDS.Face_s(face)).GetType() == GeomAbs_SurfaceType.GeomAbs_Sphere)
    tiny = _elliptic_post(1, 0.5, 0.1)
    monkeypatch.setattr(kernel_tight, "_closed_solid", lambda *args: (tiny, None))
    with pytest.raises(kernel_tight_surface.Unsupported, match="removed an implausible volume"):
        kernel_tight_surface.round_surface_loop(shape, top, 1)


def _teardrop_post():
    """半径 3 と 0.5 の円弧を、中心間距離 6 の共通外接線で結んだ柱を返す。"""
    nx, ny = 2.5 / 6, math.sqrt(1 - (2.5 / 6) ** 2)
    rear_up, rear_down = gp_Pnt(3 * nx, 3 * ny, 0), gp_Pnt(3 * nx, -3 * ny, 0)
    tip_up, tip_down = gp_Pnt(6 + 0.5 * nx, 0.5 * ny, 0), gp_Pnt(6 + 0.5 * nx, -0.5 * ny, 0)
    wire = BRepBuilderAPI_MakeWire()
    for edge in (BRepBuilderAPI_MakeEdge(rear_up, tip_up).Edge(),
                 BRepBuilderAPI_MakeEdge(GC_MakeArcOfCircle(tip_up, gp_Pnt(6.5, 0, 0), tip_down).Value()).Edge(),
                 BRepBuilderAPI_MakeEdge(tip_down, rear_down).Edge(),
                 BRepBuilderAPI_MakeEdge(GC_MakeArcOfCircle(rear_down, gp_Pnt(-3, 0, 0), rear_up).Value()).Edge()):
        wire.Add(edge)
    return BRepPrimAPI_MakePrism(BRepBuilderAPI_MakeFace(wire.Wire()).Face(), gp_Vec(0, 0, 8)).Shape()


@pytest.mark.parametrize("curved", [False, True])
def test_line_and_arc_walls_are_accepted_for_a_flat_or_spherical_top(curved):
    """直線と円弧の輪郭の平面壁・円柱壁を、曲面でも平面でも丸める。"""
    post = _teardrop_post()
    shape = _dome(post) if curved else post
    edges = _outline(shape, curved)
    assert _native_done(shape, edges, 1) == (not curved)
    rounded = kernel.fillet_edges(shape, {edge: 1 for edge in edges})
    assert BRepCheck_Analyzer(rounded["shape"]).IsValid()
    assert len(kernel.split_solids(rounded["shape"])) == 1
    assert rounded["provenance"]["deleted"] == []
    if not curved:
        direct = kernel_tight.round_tight_loop(shape, kernel._edge_map(shape), {edge: 1 for edge in edges})
        assert BRepCheck_Analyzer(direct.shape).IsValid()
        assert kernel.face_count(direct.shape) == 8


def _rotate_z(shape, degrees):
    transform = gp_Trsf()
    transform.SetRotation(gp_Ax1(gp_Pnt(), gp_Dir(0, 0, 1)), math.radians(degrees))
    return BRepBuilderAPI_Transform(shape, transform, True).Shape()


def _mirror_yz(shape):
    transform = gp_Trsf()
    transform.SetMirror(gp_Ax2(gp_Pnt(), gp_Dir(1, 0, 0)))
    return BRepBuilderAPI_Transform(shape, transform, True).Shape()


def _round_and_measure_lens(shape, radius, curved, record_property):
    assert BRepCheck_Analyzer(shape).IsValid()
    start = time.perf_counter()
    rounded = kernel.fillet_edges(shape, {edge: radius for edge in _outline(shape, curved)})
    record_property("fillet_seconds", time.perf_counter() - start)
    result = rounded["shape"]
    assert BRepCheck_Analyzer(result).IsValid()
    assert len(kernel.split_solids(result)) == 1
    assert BRepCheck_Analyzer(shape).IsValid()
    assert rounded["provenance"]["deleted"] == []
    assert all(rounded["provenance"]["face_map"].values())
    faces = kernel_tight._faces(result)
    if curved:
        lenses = [face for face in faces
                  if BRepAdaptor_Surface(TopoDS.Face_s(face)).GetType() == GeomAbs_SurfaceType.GeomAbs_Sphere]
        assert len(lenses) == 1
        lens = lenses[0]
    else:
        lens = max((face for face in faces
                    if BRepAdaptor_Surface(TopoDS.Face_s(face)).GetType() == GeomAbs_SurfaceType.GeomAbs_Plane),
                   key=lambda face: kernel_tight._a_point_on(face).Z())
    area = _precise_area(lens)
    record_property("lens_area", area)
    return area


@pytest.fixture(scope="module", params=["teardrop", "ellipse"])
def spherical_seam_reference(request):
    post = _teardrop_post() if request.param == "teardrop" else _elliptic_post(6, 2, 8)
    radius = 1.0 if request.param == "teardrop" else 1.5
    area = _round_and_measure_lens(_dome(post), radius, True, lambda *args: None)
    return post, radius, area


@pytest.mark.parametrize("degrees", [0, 45, 90, 135, 180, 270])
@pytest.mark.parametrize("motion", ["outline_first", "dome_first", "mirrored_dome"])
def test_spherical_lens_does_not_depend_on_surface_seams(spherical_seam_reference, degrees, motion, record_property):
    """球を切る前後の回転・鏡映でも、球面レンズの面積と有効な1立体を保つ。"""
    post, radius, reference = spherical_seam_reference
    if motion == "outline_first":
        shape = _dome(_rotate_z(post, degrees))
    else:
        shape = _dome(post)
        if motion == "mirrored_dome":
            shape = _mirror_yz(shape)
        shape = _rotate_z(shape, degrees)
    area = _round_and_measure_lens(shape, radius, True, record_property)
    assert area == pytest.approx(reference, abs=2e-6)
    if radius == 1.5:
        assert area == pytest.approx(_spherical_lens_area(radius), abs=2e-6)


@pytest.fixture(scope="module", params=[-5, 5])
def tapered_seam_reference(request):
    angle = request.param
    radius = 1.0 if angle == -5 else 2.2
    doc, body = _taper_document(angle)
    area = _round_and_measure_lens(doc.get_body(body).shape, radius, False, lambda *args: None)
    return angle, radius, area


@pytest.mark.parametrize("degrees", [0, 45, 90, 135, 180, 270])
def test_tapered_lens_does_not_depend_on_the_bspline_wall_seam(tapered_seam_reference, degrees, record_property):
    """テーパー前に楕円を回しても、Bスプライン壁の接点で同じレンズを残す。"""
    angle, radius, reference = tapered_seam_reference
    doc, body = _taper_document(angle, rotation=degrees)
    area = _round_and_measure_lens(doc.get_body(body).shape, radius, False, record_property)
    theta = math.radians(angle)
    offset = radius / math.cos(theta) - (5 - radius) * math.tan(theta)
    assert area == pytest.approx(reference, abs=1e-4)
    assert area == pytest.approx(_lens_area(6, 2, offset), abs=1e-4)


@pytest.mark.parametrize("motion", ["rotate", "mirror"])
def test_tapered_wall_seam_survives_transforming_the_solid(tapered_seam_reference, motion, record_property):
    """完成後のテーパー壁を回転・鏡映しても、同じ面積の有効な1立体に丸まる。"""
    angle, radius, reference = tapered_seam_reference
    doc, body = _taper_document(angle)
    shape = doc.get_body(body).shape
    shape = _rotate_z(shape, 135) if motion == "rotate" else _mirror_yz(shape)
    assert _round_and_measure_lens(shape, radius, False, record_property) == pytest.approx(reference, abs=1e-4)


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


@pytest.mark.parametrize("edges", [[1], [1, 3]])
def test_an_open_selection_keeps_the_failure_message_without_a_loop_reason(edges, monkeypatch):
    """輪でない辺の失敗文面に、曲率半径を超える丸めの理由を付けない。"""
    shape = BRepPrimAPI_MakeBox(10, 10, 10).Shape()
    values = {edge: 30 for edge in edges}

    def no_absorption(*args):
        raise ValueError("no absorption")

    monkeypatch.setattr(kernel, "_absorbed_fillet", no_absorption)
    with pytest.raises(ValueError) as actual:
        kernel._blend_edges(shape, values, "fillet")
    assert "曲率半径を超える丸めもできない" not in str(actual.value)

    def unrelated(*args):
        raise kernel_tight.Unsupported("not a loop")

    monkeypatch.setattr(kernel_tight, "round_tight_loop", unrelated)
    with pytest.raises(ValueError) as baseline:
        kernel._blend_edges(shape, values, "fillet")
    assert str(actual.value) == str(baseline.value)


@pytest.mark.parametrize("height", [1.0, 4.0, 5.5])
def test_a_cavity_is_preserved_below_the_cut_and_refused_above_it(height):
    """切断面より上の別の面は断り、下の空洞の殻と由来は保つ。"""
    dome = _dome()
    hole = BRepPrimAPI_MakeSphere(gp_Pnt(0, 0, height), 0.2).Shape()
    shape = BRepAlgoAPI_Cut(dome, hole).Shape()
    edges = _outline(shape, True)
    if height == 5.5:
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
