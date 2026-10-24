"""辺の曲率半径を超える半径のフィレット(issue #207、`kernel_tight`)。楕円柱の上縁を、先端の曲率半径より大きい半径で丸めると、
Fusion 360 と同じく、上面は両端の尖ったレンズ形、長い側は球が転がる面、先端は隣の面となめらかにつながる帽子の面になる。
"""
from __future__ import annotations

import math

import pytest
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakeWire
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepClass3d import BRepClass3d_SolidClassifier
from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
from OCP.BRepGProp import BRepGProp
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder, BRepPrimAPI_MakePrism, BRepPrimAPI_MakeSphere
from OCP.GProp import GProp_GProps
from OCP.GeomAPI import GeomAPI_Interpolate
from OCP.GeomAbs import GeomAbs_SurfaceType
from OCP.TColgp import TColgp_HArray1OfPnt
from OCP.TopAbs import TopAbs_IN, TopAbs_OUT
from OCP.TopoDS import TopoDS
from OCP.gp import gp_Ax2, gp_Dir, gp_Elips, gp_Pnt, gp_Vec

from duocad import kernel, query
from duocad.document import Document
from duocad.region import region_ref
from duocad.sketch import add_ellipse, base_plane, sketch_create
from duocad.verbs import extrude, fillet
from document_support import assert_matches_replay

from duocad import (
    brep_geometry as _brep_geometry,
    brep_topology as _brep_topology,
)


def _elliptic_post(a: float, b: float, height: float):
    """半軸 `a`・`b`、高さ `height` の楕円柱(先端の曲率半径は b²/a)。"""
    outline = BRepBuilderAPI_MakeWire(BRepBuilderAPI_MakeEdge(gp_Elips(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), a, b)).Edge()).Wire()
    return BRepPrimAPI_MakePrism(BRepBuilderAPI_MakeFace(outline).Face(), gp_Vec(0, 0, height)).Shape()


def _peanut_post(height: float):
    """長さ 12、くびれの幅 2(両端の膨らみの幅 6、先端の曲率半径 0.75)の落花生形の柱。"""
    points = TColgp_HArray1OfPnt(1, 120)
    for i in range(120):
        t = 2.0 * math.pi * i / 120
        points.SetValue(i + 1, gp_Pnt(6.0 * math.cos(t), math.sin(t) * (1.0 + 2.0 * math.cos(t) ** 2), 0.0))
    interpolation = GeomAPI_Interpolate(points, True, 1e-9)
    interpolation.Perform()
    outline = BRepBuilderAPI_MakeWire(BRepBuilderAPI_MakeEdge(interpolation.Curve()).Edge()).Wire()
    return BRepPrimAPI_MakePrism(BRepBuilderAPI_MakeFace(outline).Face(), gp_Vec(0, 0, height)).Shape()


def _top_edges(shape, height: float) -> list:
    return [
        edge["index"] for edge in query.describe_edges(shape)
        if abs(edge["start"][2] - height) < 1e-9 and abs(edge["end"][2] - height) < 1e-9
    ]


def _native_done(shape, edges: list, radius: float) -> bool:
    maker = BRepFilletAPI_MakeFillet(shape)
    for index in edges:
        maker.Add(radius, TopoDS.Edge_s(_brep_topology.edge_of(shape, index)))
    maker.Build()
    return maker.IsDone()


def _faces_of_type(shape, kind) -> list:
    return [
        _brep_topology.face_of(shape, index) for index in range(1, _brep_topology.face_count(shape) + 1)
        if BRepAdaptor_Surface(TopoDS.Face_s(_brep_topology.face_of(shape, index))).GetType() == kind
    ]


def _area(face) -> float:
    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face, props)
    return props.Mass()


def _lens_area(a: float, b: float, radius: float, steps: int = 100000) -> float:
    """楕円(半軸 a・b)を内へ `radius` ずらした曲線のうち、x 軸上の自己交差より内側の2本の枝で囲む面積(グリーンの定理)。
    自己交差は、ずらした点が x 軸を横切る t(N = radius·a/b、N = √(b²cos²t + a²sin²t))。"""
    t_cross = math.asin(math.sqrt(((radius * a / b) ** 2 - b * b) / (a * a - b * b)))

    def point(t):
        n = math.hypot(b * math.cos(t), a * math.sin(t))
        return a * math.cos(t) - radius * b * math.cos(t) / n, b * math.sin(t) - radius * a * math.sin(t) / n

    total = 0.0
    for t0, t1 in ((t_cross, math.pi - t_cross), (math.pi + t_cross, 2.0 * math.pi - t_cross)):
        previous = point(t0)
        for i in range(1, steps + 1):
            current = point(t0 + (t1 - t0) * i / steps)
            total += previous[0] * current[1] - current[0] * previous[1]
            previous = current
    return total / 2.0


def _state(shape, x: float, y: float, z: float):
    return BRepClass3d_SolidClassifier(shape, gp_Pnt(x, y, z), 1e-7).State()


@pytest.mark.parametrize("a, b, height, radius", [(6.0, 2.0, 5.0, 1.0), (30.0, 12.0, 20.0, 8.0)])
def test_an_elliptic_top_rounded_past_its_tip_curvature_gets_a_lens_top_and_two_caps(a, b, height, radius):
    """楕円柱の上縁を先端の曲率半径(b²/a)より大きい半径で丸めると、OCCT は断るが、丸まる。面は上面(レンズ形)・長い側の
    丸め 2・先端の帽子 2・壁・底の 7 枚。上面の広さは、輪郭を内へ R ずらして自己交差を切り落とした形の面積(式から積分した値。
    OCCT の平面のオフセットは 30×12・R=8 で 1.5% ずれるので使わない)と一致し、壁は深さ R で終わる。"""
    shape = _elliptic_post(a, b, height)
    edges = _top_edges(shape, height)
    assert radius > b * b / a and not _native_done(shape, edges, radius), "前提: OCCT はこの半径を断る"

    rounded = kernel.fillet_edges(shape, {index: radius for index in edges})

    result = rounded["shape"]
    assert BRepCheck_Analyzer(result).IsValid()
    assert _brep_topology.face_count(result) == 7
    planes = _faces_of_type(result, GeomAbs_SurfaceType.GeomAbs_Plane)
    lens = max(planes, key=lambda face: BRepAdaptor_Surface(TopoDS.Face_s(face)).Plane().Location().Z())
    assert _area(lens) == pytest.approx(_lens_area(a, b, radius), rel=1e-6)
    walls = [face for face in (_brep_topology.face_of(result, i) for i in range(1, _brep_topology.face_count(result) + 1))
             if BRepAdaptor_Surface(TopoDS.Face_s(face)).GetType() == GeomAbs_SurfaceType.GeomAbs_SurfaceOfExtrusion]
    assert len(walls) == 1
    from OCP.BRepBndLib import BRepBndLib
    from OCP.Bnd import Bnd_Box

    box = Bnd_Box()
    BRepBndLib.AddOptimal_s(walls[0], box, False, False)
    assert box.Get()[5] == pytest.approx(height - radius, abs=1e-6), "壁が深さ R で終わらない"
    # 長い側の真ん中(輪郭の点 (0, b))の断面は、中心 (0, b − R, h − R)・半径 R の四分円
    for scale, expected in ((0.99, TopAbs_IN), (1.01, TopAbs_OUT)):
        reach = radius * scale * math.sqrt(0.5)
        assert _state(result, 0.0, b - radius + reach, height - radius + reach) == expected


def test_rounding_past_the_tip_curvature_reports_which_faces_became_which():
    """由来: 壁は深さ R までの壁、上面はレンズ形の上面、底は底。丸めの面 2 と帽子 2 は新しい面で、消えた面は無い。"""
    shape = _elliptic_post(6.0, 2.0, 5.0)
    edges = _top_edges(shape, 5.0)

    rounded = kernel.fillet_edges(shape, {index: 1.0 for index in edges})

    provenance = rounded["provenance"]
    assert provenance["deleted"] == []
    assert len(provenance["new_faces"]) == 4
    for old_index, new_indices in provenance["face_map"].items():
        assert len(new_indices) == 1
        old = BRepAdaptor_Surface(TopoDS.Face_s(_brep_topology.face_of(shape, int(old_index)))).GetType()
        new = BRepAdaptor_Surface(TopoDS.Face_s(_brep_topology.face_of(rounded["shape"], new_indices[0]))).GetType()
        assert old == new, (old_index, old, new)


def test_rounding_past_the_tip_curvature_is_logged_undone_and_replayed():
    """スケッチの楕円を押し出した柱の上縁でも、ログ・undo・redo・JSON からの再生で同じ形になる。"""
    doc = Document()
    sketch_id = sketch_create(doc, base_plane("XY"))
    add_ellipse(doc, sketch_id, (0.0, 0.0), 12.0, 4.0)
    body_id = extrude(doc, [region_ref(sketch_id, 1)], 5.0, mode="new").args["body_id"]
    shape = doc.get_body(body_id).shape
    before = query.volume(shape)
    edges = _top_edges(shape, 5.0)

    fillet(doc, edges, 1.0, body_id=body_id)
    after = query.volume(doc.get_body(body_id).shape)
    assert after < before

    doc.log.undo(doc)
    assert query.volume(doc.get_body(body_id).shape) == pytest.approx(before)
    doc.log.redo(doc)
    assert query.volume(doc.get_body(body_id).shape) == pytest.approx(after)
    assert_matches_replay(doc)


def test_a_radius_within_the_tip_curvature_stays_occt_s_own():
    """先端の曲率半径以下の半径は、OCCT の `MakeFillet` の形のまま(この作り方は使わない)。"""
    shape = _elliptic_post(6.0, 2.0, 5.0)
    edges = _top_edges(shape, 5.0)
    maker = BRepFilletAPI_MakeFillet(shape)
    for index in edges:
        maker.Add(0.5, TopoDS.Edge_s(_brep_topology.edge_of(shape, index)))
    maker.Build()

    rounded = kernel.fillet_edges(shape, {index: 0.5 for index in edges})

    assert rounded["volume_after"] == pytest.approx(_brep_geometry.volume(maker.Shape()), rel=1e-12)
    assert _brep_topology.face_count(rounded["shape"]) == _brep_topology.face_count(maker.Shape())


def test_a_top_face_with_a_hole_is_refused_with_the_reason():
    """上面に穴がある楕円柱は、この作り方では丸めない(穴の上まで冠で塞いでしまう)。OCCT の失敗の理由で断る。"""
    post = _elliptic_post(6.0, 2.0, 5.0)
    hole = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, 0, -1), gp_Dir(0, 0, 1)), 0.4, 10.0).Shape()
    shape = kernel.boolean_bodies(post, [hole], "cut")["shape"]
    edges = [index for index in _top_edges(shape, 5.0)
             if query.describe_edges(shape)[index - 1]["start"][0] > 1.0 or query.describe_edges(shape)[index - 1]["end"][0] > 1.0]

    with pytest.raises(ValueError, match="IsDone is false"):
        kernel.fillet_edges(shape, {index: 1.0 for index in edges})


def test_a_post_lower_than_the_radius_is_refused():
    """高さ 0.6 の楕円柱の上縁を半径 1 で丸めると、深さ R が底を突き抜けるので、この作り方では丸めない。"""
    shape = _elliptic_post(6.0, 2.0, 0.6)
    edges = _top_edges(shape, 0.6)

    with pytest.raises(ValueError):
        kernel.fillet_edges(shape, {index: 1.0 for index in edges})


@pytest.mark.parametrize("radius", [0.6667, 0.67, 1.95])
def test_radii_near_the_ends_of_the_range_are_rounded(radius):
    """楕円 6×2 の上縁を、先端の曲率半径(0.66667)をわずかに超える半径と、上面の半幅(2)にほぼ届く半径で丸めても、7 面の
    有効な立体になり、上面の広さが式と合う。0.6667 の燕尾は標本の間隔より小さく、折れ線では交わらない。0.67 は交点の枝の角が
    浅い(交互に寄せる詰め方は収束しなかった)。1.95 は上面が幅 0.1 の細いレンズ(丸めの面の下の縁が壁から離れて縫えなかった)。"""
    shape = _elliptic_post(6.0, 2.0, 5.0)
    edges = _top_edges(shape, 5.0)
    assert not _native_done(shape, edges, radius), "前提: OCCT はこの半径を断る"

    result = kernel.fillet_edges(shape, {index: radius for index in edges})["shape"]

    assert BRepCheck_Analyzer(result).IsValid()
    assert _brep_topology.face_count(result) == 7
    lens = max(_faces_of_type(result, GeomAbs_SurfaceType.GeomAbs_Plane),
               key=lambda face: BRepAdaptor_Surface(TopoDS.Face_s(face)).Plane().Location().Z())
    assert _area(lens) == pytest.approx(_lens_area(6.0, 2.0, radius), rel=1e-5)


def test_a_radius_wider_than_the_top_is_refused_with_this_way_s_reason():
    """楕円 6×2 の上縁を半径 2.2(上面の半幅 2 を超える)で丸めると断る。失敗の文面にこの作り方の理由が載る(載らないと、
    はみ出しの吸収の「くさびが丸まらない」だけが見える)。"""
    shape = _elliptic_post(6.0, 2.0, 5.0)
    edges = _top_edges(shape, 5.0)

    with pytest.raises(ValueError, match="曲率半径を超える丸めもできない: .*the radius is wider than the top"):
        kernel.fillet_edges(shape, {index: 2.2 for index in edges})


def test_a_radius_wider_than_the_waist_of_the_top_is_refused_by_this_way():
    """落花生形の柱(くびれの幅 2、先端の曲率半径 0.75)の上縁を半径 1.2 で丸める道は、くびれで球が反対側の壁に食い込むので
    引き受けない。OCCT はこの形を自分で丸めるので、作り方を直接呼ぶ。"""
    from duocad import kernel_tight

    shape = _peanut_post(5.0)
    edges = _top_edges(shape, 5.0)

    with pytest.raises(kernel_tight.Declined, match="cut into the opposite wall"):
        kernel_tight.round_tight_loop(shape, _brep_topology._edge_map(shape), {index: 1.2 for index in edges})


@pytest.mark.parametrize("hole", ["sphere", "box"])
def test_a_cavity_below_the_rounding_is_kept(hole):
    """深さ R より下に空洞(球・箱)がある楕円柱でも、丸めた形に空洞が残り、体積は空洞の無い柱を丸めた形から空洞を引いた値。
    空洞の面は消えない(縫い合わせで外の殻だけを作り直すと、球の空洞は消え、箱の空洞は殻が2つになって断られた)。"""
    plain = _elliptic_post(6.0, 2.0, 5.0)
    rounded_plain = kernel.fillet_edges(plain, {index: 1.0 for index in _top_edges(plain, 5.0)})["volume_after"]
    cavity = (BRepPrimAPI_MakeSphere(gp_Pnt(0, 0, 2), 0.5).Shape() if hole == "sphere"
              else BRepPrimAPI_MakeBox(gp_Pnt(-0.5, -0.5, 1.5), 1.0, 1.0, 1.0).Shape())
    shape = BRepAlgoAPI_Cut(plain, cavity).Shape()

    rounded = kernel.fillet_edges(shape, {index: 1.0 for index in _top_edges(shape, 5.0)})

    assert BRepCheck_Analyzer(rounded["shape"]).IsValid()
    assert _state(rounded["shape"], 0.0, 0.0, 2.0) == TopAbs_OUT
    assert rounded["volume_after"] == pytest.approx(rounded_plain - _brep_geometry.volume(cavity), rel=1e-9)
    assert rounded["provenance"]["deleted"] == []


@pytest.mark.parametrize("corner", [(-0.5, 1.5, 1.5), (-0.5, -0.5, 2.999999)], ids=["notch in the wall", "cavity just below the cut"])
def test_a_box_cut_below_the_rounding_keeps_every_face_traced(corner):
    """深さ R より下で壁に食い込む箱の切り欠きと、上面が深さ R の 1e-6 下にある箱の空洞でも丸まり、元の面はどれも後継を持ち、
    消えた面は無い(縫った立体を `ShapeFix_Solid` に通すと壁が置き換わって由来が切れた。断面を平面の位置だけで探すと、
    空洞の上面も拾って断った)。"""
    plain = _elliptic_post(6.0, 2.0, 5.0)
    shape = BRepAlgoAPI_Cut(plain, BRepPrimAPI_MakeBox(gp_Pnt(*corner), 1.0, 1.0, 1.0).Shape()).Shape()

    rounded = kernel.fillet_edges(shape, {index: 1.0 for index in _top_edges(shape, 5.0)})

    assert BRepCheck_Analyzer(rounded["shape"]).IsValid()
    assert rounded["provenance"]["deleted"] == []
    assert all(rounded["provenance"]["face_map"].values())
