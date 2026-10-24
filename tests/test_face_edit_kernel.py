"""面の削除・オフセットの幾何(`duocad/kernel_face_edit.py`)が、受入条件より細かい場面で正しく通し・断るか。

落ちたら、丸めの半径を取り違える、断る理由が利用者に伝わらない、関門が壊れた形を通す、のどれかを意味する。
"""
from __future__ import annotations

import math

import pytest
from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakeWire
from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
from OCP.BRepOffsetAPI import BRepOffsetAPI_MakeThickSolid
from OCP.BRepPrimAPI import (
    BRepPrimAPI_MakeBox,
    BRepPrimAPI_MakeCone,
    BRepPrimAPI_MakeCylinder,
    BRepPrimAPI_MakePrism,
    BRepPrimAPI_MakeRevol,
)
from OCP.BRepTools import BRepTools, BRepTools_ReShape
from OCP.Geom import Geom_BezierCurve, Geom_OffsetSurface, Geom_RectangularTrimmedSurface
from OCP.GeomAbs import GeomAbs_CurveType
from OCP.gp import gp_Ax1, gp_Ax2, gp_Dir, gp_Elips, gp_Pnt, gp_Vec
from OCP.TColgp import TColgp_Array1OfPnt
from OCP.TopAbs import TopAbs_ShapeEnum
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS
from OCP.TopTools import TopTools_ListOfShape

from duocad import kernel_face_edit, query

from duocad import (
    brep_topology as _brep_topology,
)


W, D, H = 40.0, 30.0, 20.0


def _edge_at(shape, y: float, z: float):
    """x 方向に走る、y・z が指定の値の辺。"""
    (index,) = [
        edge["index"]
        for edge in query.describe_edges(shape)
        if all(abs(p[1] - y) < 1e-6 and abs(p[2] - z) < 1e-6 for p in (edge["start"], edge["end"]))
    ]
    return TopoDS.Edge_s(_brep_topology.indexed_map(shape, TopAbs_ShapeEnum.TopAbs_EDGE).FindKey(index))


def _face_facing(shape, normal: tuple) -> int:
    (index,) = [f["index"] for f in query.describe_faces(shape) if f["kind"] == "plane" and f["normal"] == list(normal)]
    return index


def _radii(shape) -> list:
    from OCP.BRepAdaptor import BRepAdaptor_Surface

    faces = _brep_topology.indexed_map(shape, TopAbs_ShapeEnum.TopAbs_FACE)
    return sorted(
        round(BRepAdaptor_Surface(TopoDS.Face_s(faces.FindKey(f["index"]))).Cylinder().Radius(), 9)
        for f in query.describe_faces(shape)
        if f["kind"].startswith("cylinder")
    )


def test_blends_of_different_radii_follow_with_their_own_radius():
    """前 r5・後 r3 の丸めに挟まれた上面を上げると、どちらの丸めも自分の半径のまま付いてくる。"""
    box = BRepPrimAPI_MakeBox(W, D, H).Shape()
    maker = BRepFilletAPI_MakeFillet(box)
    maker.Add(5.0, _edge_at(box, 0.0, H))
    maker.Add(3.0, _edge_at(box, D, H))
    maker.Build()
    shape = maker.Shape()
    result = kernel_face_edit.offset_faces(shape, [_face_facing(shape, (0.0, 0.0, 1.0))], 2.0)
    assert _radii(result["shape"]) == [3.0, 5.0]
    lost = sum((r * r - math.pi * r * r / 4) * W for r in (5.0, 3.0))
    assert query.volume(result["shape"]) == pytest.approx(W * D * (H + 2.0) - lost, abs=1e-6)


def test_moving_a_blend_itself_is_refused_with_a_reason():
    box = BRepPrimAPI_MakeBox(W, D, H).Shape()
    maker = BRepFilletAPI_MakeFillet(box)
    maker.Add(5.0, _edge_at(box, 0.0, H))
    maker.Build()
    shape = maker.Shape()
    (rounded,) = [f["index"] for f in query.describe_faces(shape) if f["kind"].startswith("cylinder")]
    with pytest.raises(ValueError, match="丸めの面そのものは動かせない"):
        kernel_face_edit.offset_faces(shape, [rounded], 1.0)


def test_moving_a_cone_is_refused_with_a_reason():
    cone = BRepPrimAPI_MakeCone(10.0, 5.0, 10.0).Shape()
    (side,) = [f["index"] for f in query.describe_faces(cone) if f["kind"].startswith("cone")]
    with pytest.raises(ValueError, match="平面・円筒・球・トーラスと、押し出し・回転・オフセットの面だけ"):
        kernel_face_edit.offset_faces(cone, [side], 1.0)


def test_a_blind_hole_needs_its_floor_too():
    """止まり穴は壁だけでは閉じない(断り文は穴を作る面をまとめて選ぶよう促す)。壁と底を選ぶと元の箱に戻る。"""
    box = BRepPrimAPI_MakeBox(W, D, H).Shape()
    drill = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(20.0, 15.0, 10.0), gp_Dir(0, 0, 1)), 4.0, 15.0).Shape()
    shape = BRepAlgoAPI_Cut(box, drill).Shape()
    faces = query.describe_faces(shape)
    (wall,) = [f["index"] for f in faces if f["kind"].startswith("cylinder")]
    (floor,) = [f["index"] for f in faces if f["kind"] == "plane" and abs(f["normal"][2]) > 0.5 and abs(f["center"][2] - 10.0) < 1e-6]
    with pytest.raises(ValueError, match="まとめて選ぶ"):
        kernel_face_edit.delete_faces(shape, [wall])
    result = kernel_face_edit.delete_faces(shape, [wall, floor])
    assert query.volume(result["shape"]) == pytest.approx(W * D * H, abs=1e-6)
    assert sorted(result["provenance"]["deleted"]) == sorted([wall, floor])


def _revolved(profile_edges, angle: float = 2 * math.pi):
    """XZ 面の閉じた輪郭を Z 軸まわりに `angle` 回した立体。"""
    wire = BRepBuilderAPI_MakeWire()
    for edge in profile_edges:
        wire.Add(edge)
    face = BRepBuilderAPI_MakeFace(wire.Wire(), True).Face()
    return BRepPrimAPI_MakeRevol(face, gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), angle).Shape()


def _vase(angle: float = 2 * math.pi):
    """外壁がベジエを回した回転面(OCCT が延ばせない曲面)、上下が平面の器。"""
    points = TColgp_Array1OfPnt(1, 4)
    for i, (x, z) in enumerate([(10, 0), (14, 10), (8, 20), (10, 30)], 1):
        points.SetValue(i, gp_Pnt(x, 0, z))
    corners = [gp_Pnt(10, 0, 30), gp_Pnt(0, 0, 30), gp_Pnt(0, 0, 0), gp_Pnt(10, 0, 0)]
    return _revolved(
        [BRepBuilderAPI_MakeEdge(Geom_BezierCurve(points)).Edge()]
        + [BRepBuilderAPI_MakeEdge(a, b).Edge() for a, b in zip(corners, corners[1:])],
        angle,
    )


def _filleted(shape, edges, radius: float = 1.0):
    maker = BRepFilletAPI_MakeFillet(shape)
    for edge in edges:
        maker.Add(radius, edge)
    maker.Build()
    return maker.Shape()


def _plane_edges(shape):
    """平面の面の辺(回転体では、回転面と平面の境)。"""
    faces = _brep_topology._face_map(shape)
    return [
        edge
        for face in query.describe_faces(shape)
        if face["kind"] == "plane"
        for edge in _brep_topology._face_edit_edges(TopoDS.Face_s(faces.FindKey(face["index"])))
    ]


def _top_rim(shape):
    """上面(z=30)と回転面の境の円弧。"""
    return [
        e
        for e in _plane_edges(shape)
        if BRepAdaptor_Curve(e).GetType() == GeomAbs_CurveType.GeomAbs_Circle
        and all(abs(p[2] - 30.0) < 1e-6 for p in _edge_ends(e))
    ]


def _bspline_faces(shape) -> list:
    return [f["index"] for f in query.describe_faces(shape) if f["kind"] == "bspline"]


def _stray_pcurve_surfaces(shape) -> int:
    """辺の 2D 曲線のうち、どの面の曲面でもない曲面の上のものの数。"""
    faces = _brep_topology._face_map(shape)
    used = [BRep_Tool.Surface_s(TopoDS.Face_s(faces.FindKey(i)), TopLoc_Location()) for i in range(1, faces.Extent() + 1)]
    return sum(
        1
        for edge in _all_edges(shape)
        for representation in edge.TShape().Curves()
        if representation.IsCurveOnSurface() and not any(representation.Surface() == surface for surface in used)
    )


@pytest.mark.parametrize("angle", [2 * math.pi, math.pi])
def test_a_rim_fillet_beside_a_revolved_wall_is_deleted_back_to_the_vase(angle):
    """落ちたら、回転面(延ばせない曲面)の隣の丸めが消せないか、壁が元の回転面に戻らない。半周の器は、周方向を一周
    覆わない回転面でも母線の向きには延ばして閉じられることを見る。"""
    vase = _vase(angle)
    filleted = _filleted(vase, _top_rim(vase))

    result = kernel_face_edit.delete_faces(filleted, _bspline_faces(filleted))["shape"]

    kinds = query.describe_shape(result)["faces_by_type"]
    assert (kinds["other"], kinds["bspline"]) == (1, 0)
    assert query.volume(result) == pytest.approx(query.volume(vase), rel=1e-8)
    assert query.volume(BRepAlgoAPI_Cut(result, vase).Shape()) == pytest.approx(0.0, abs=1e-6)
    assert query.volume(BRepAlgoAPI_Cut(vase, result).Shape()) == pytest.approx(0.0, abs=1e-6)


def _walls(shape) -> list:
    """延ばせない曲面の面。"""
    faces = _brep_topology._face_map(shape)
    return [
        TopoDS.Face_s(faces.FindKey(i))
        for i in range(1, faces.Extent() + 1)
        if BRepAdaptor_Surface(TopoDS.Face_s(faces.FindKey(i))).GetType() in kernel_face_edit._UNEXTENDABLE
    ]


def _trimmed_to_faces(shape):
    """延ばせない曲面の面を、面の UV の範囲の矩形トリムで包んだ曲面の上に張り直した形。"""
    reshape = BRepTools_ReShape()
    for wall in _walls(shape):
        location = TopLoc_Location()
        surface = BRep_Tool.Surface_s(wall, location)
        trimmed = Geom_RectangularTrimmedSurface(surface, *BRepTools.UVBounds_s(wall))
        reshape.Replace(wall, kernel_face_edit._laid_on(wall, trimmed, location))
    return reshape.Apply(shape)


def test_a_rim_fillet_beside_a_trimmed_revolved_wall_is_deleted_back_to_the_vase():
    """落ちたら、矩形トリムで包んだ曲面の壁を、延びた像のままトリムの狭い範囲へ戻している(辺の許容差が 1.16 mm に広がり、
    体積の違う形が関門を通った。実測)。"""
    vase = _vase()
    filleted = _trimmed_to_faces(_filleted(vase, _top_rim(vase)))

    result = kernel_face_edit.delete_faces(filleted, _bspline_faces(filleted))["shape"]

    assert query.volume(result) == pytest.approx(query.volume(vase), rel=1e-8)


def _shelled_ellipse():
    """楕円柱を上面を開けて殻にした形(内壁は押し出し面のオフセット面)。"""
    edge = BRepBuilderAPI_MakeEdge(gp_Elips(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), 11.4, 10.4)).Edge()
    prism = BRepPrimAPI_MakePrism(BRepBuilderAPI_MakeFace(BRepBuilderAPI_MakeWire(edge).Wire()).Face(), gp_Vec(0, 0, 30)).Shape()
    opening = TopTools_ListOfShape()
    opening.Append(_brep_topology._face_map(prism).FindKey(_face_facing(prism, (0.0, 0.0, 1.0))))
    maker = BRepOffsetAPI_MakeThickSolid()
    maker.MakeThickSolidByJoin(prism, opening, -2.4, 1e-6)
    return maker.Shape()


def _with_trimmed_offset_basis(shape):
    """オフセット面の基底を、面の UV の範囲の矩形トリムで包んだ形。"""
    reshape = BRepTools_ReShape()
    for wall in _walls(shape):
        location = TopLoc_Location()
        surface = BRep_Tool.Surface_s(wall, location)
        if isinstance(surface, Geom_OffsetSurface):
            basis = Geom_RectangularTrimmedSurface(surface.BasisSurface(), *BRepTools.UVBounds_s(wall))
            reshape.Replace(wall, kernel_face_edit._laid_on(wall, Geom_OffsetSurface(basis, surface.Offset()), location))
    return reshape.Apply(shape)


def test_a_rim_fillet_beside_an_offset_wall_on_a_trimmed_basis_is_deleted_back_to_the_shell():
    """落ちたら、基底が矩形トリムのオフセット面(殻の内壁)を、狭い基底のまま張り替えるか戻して、正しい削除を断る。"""
    shell = _shelled_ellipse()
    rims = [e for e in _plane_edges(shell) if all(abs(p[2] - 30.0) < 1e-6 for p in _edge_ends(e))]
    filleted = _with_trimmed_offset_basis(_filleted(shell, rims))

    result = kernel_face_edit.delete_faces(filleted, _bspline_faces(filleted))["shape"]

    assert query.volume(result) == pytest.approx(query.volume(shell), rel=1e-8)


def test_laying_an_outline_on_a_surface_it_does_not_lie_on_is_refused():
    """落ちたら、輪郭が乗らない曲面へ張った面を、`ShapeFix_Face` が辺の許容差を広げて通したまま返す。"""
    (wall,) = _walls(_vase())
    location = TopLoc_Location()
    surface = BRep_Tool.Surface_s(wall, location)
    u1, u2, v1, v2 = BRepTools.UVBounds_s(wall)

    with pytest.raises(kernel_face_edit.FaceEditError, match="does not lie on"):
        kernel_face_edit._laid_on(wall, Geom_RectangularTrimmedSurface(surface, u1, u2, v1, (v1 + v2) / 2), location)


def test_the_b_spline_stand_ins_leave_no_curves_on_the_edges():
    """落ちたら、消す間だけ置き換えた B-spline の上の 2D 曲線が辺に残り、保存する BRep が太る。"""
    vase = _vase()
    filleted = _filleted(vase, _top_rim(vase))

    result = kernel_face_edit.delete_faces(filleted, _bspline_faces(filleted))["shape"]

    assert _stray_pcurve_surfaces(result) == 0


def test_a_fillet_at_the_end_of_a_half_revolution_is_still_refused():
    """落ちたら、周方向を一周覆わない回転面を周方向へ延ばしている(B-spline を周方向に開くと、外挿が元の曲面から外れるか、
    延びた両端が交わって重なった面を関門をすり抜けて返した。楕円を 180° 回した形で実測)。"""
    section = BRepBuilderAPI_MakeEdge(gp_Elips(gp_Ax2(gp_Pnt(15, 0, 0), gp_Dir(0, 1, 0)), 6.0, 4.0)).Edge()
    half = _revolved([section], math.pi)
    filleted = _filleted(half, _plane_edges(half))

    with pytest.raises(ValueError, match="refused"):
        kernel_face_edit.delete_faces(filleted, _bspline_faces(filleted))


def test_the_structural_gate_refuses_two_solids():
    two = _brep_topology.compound([BRepPrimAPI_MakeBox(1.0, 1.0, 1.0).Shape(), BRepPrimAPI_MakeBox(gp_Pnt(5, 0, 0), 1.0, 1.0, 1.0).Shape()])
    with pytest.raises(kernel_face_edit.FaceEditError, match="2 solids"):
        kernel_face_edit._structural_gate(two)


def _filleted_box(radius: float, edges_of) -> object:
    box = BRepPrimAPI_MakeBox(W, D, H).Shape()
    maker = BRepFilletAPI_MakeFillet(box)
    for edge in edges_of(box):
        maker.Add(radius, edge)
    maker.Build()
    return maker.Shape()


def _curved(shape) -> list:
    return sorted(f["index"] for f in query.describe_faces(shape) if f["kind"] != "plane")


def _all_edges(shape):
    edges = _brep_topology.indexed_map(shape, TopAbs_ShapeEnum.TopAbs_EDGE)
    return [TopoDS.Edge_s(edges.FindKey(i)) for i in range(1, edges.Extent() + 1)]


def _top_edges(shape):
    return [e for e in _all_edges(shape) if all(abs(p[2] - H) < 1e-6 for p in _edge_ends(e))]


def _edge_ends(edge):
    from OCP.BRep import BRep_Tool
    from OCP.TopExp import TopExp

    return [tuple(BRep_Tool.Pnt_s(v).XYZ().Coord()) for v in (TopExp.FirstVertex_s(edge), TopExp.LastVertex_s(edge))]


def test_one_click_on_a_blend_reaches_the_blends_meeting_it_at_a_corner():
    """上面4辺の丸めは角で折れて会う(接線でない)。1枚から4枚とも取れなければ、画面の「面の削除」が連なりへ広がらない。"""
    shape = _filleted_box(4.0, _top_edges)
    blends = _curved(shape)
    assert len(blends) == 4
    for index in blends:
        assert kernel_face_edit.blend_chain(shape, index) == blends


def test_the_chain_of_a_fully_rounded_box_includes_the_corner_spheres():
    """全辺の丸めは、角の球を通って 12 本 + 球 8 枚の全部。平面(支え)は入らない。"""
    shape = _filleted_box(3.0, _all_edges)
    blends = _curved(shape)
    assert len(blends) == 20
    assert kernel_face_edit.blend_chain(shape, blends[0]) == blends


def test_a_face_that_is_not_a_blend_is_its_own_chain():
    """丸めの支えの平面をクリックしても丸めへ広がらない(上面を消すつもりで丸めまで消えない)。"""
    shape = _filleted_box(4.0, _top_edges)
    top = _face_facing(shape, (0.0, 0.0, 1.0))
    assert kernel_face_edit.blend_chain(shape, top) == [top]
