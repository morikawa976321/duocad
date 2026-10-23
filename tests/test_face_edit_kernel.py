"""面の削除・オフセットの幾何(`duocad/kernel_face_edit.py`)が、受入条件より細かい場面で正しく通し・断るか。

落ちたら、丸めの半径を取り違える、断る理由が利用者に伝わらない、関門が壊れた形を通す、のどれかを意味する。
"""
from __future__ import annotations

import math

import pytest
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCone, BRepPrimAPI_MakeCylinder
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt
from OCP.TopAbs import TopAbs_ShapeEnum
from OCP.TopoDS import TopoDS

from duocad import kernel_face_edit, query
from duocad.mesh_geometry import compound, indexed_map

W, D, H = 40.0, 30.0, 20.0


def _edge_at(shape, y: float, z: float):
    """x 方向に走る、y・z が指定の値の辺。"""
    (index,) = [
        edge["index"]
        for edge in query.describe_edges(shape)
        if all(abs(p[1] - y) < 1e-6 and abs(p[2] - z) < 1e-6 for p in (edge["start"], edge["end"]))
    ]
    return TopoDS.Edge_s(indexed_map(shape, TopAbs_ShapeEnum.TopAbs_EDGE).FindKey(index))


def _face_facing(shape, normal: tuple) -> int:
    (index,) = [f["index"] for f in query.describe_faces(shape) if f["kind"] == "plane" and f["normal"] == list(normal)]
    return index


def _radii(shape) -> list:
    from OCP.BRepAdaptor import BRepAdaptor_Surface

    faces = indexed_map(shape, TopAbs_ShapeEnum.TopAbs_FACE)
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
    with pytest.raises(ValueError, match="平面・円筒・球・トーラス"):
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


def test_the_structural_gate_refuses_two_solids():
    two = compound([BRepPrimAPI_MakeBox(1.0, 1.0, 1.0).Shape(), BRepPrimAPI_MakeBox(gp_Pnt(5, 0, 0), 1.0, 1.0, 1.0).Shape()])
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
    edges = indexed_map(shape, TopAbs_ShapeEnum.TopAbs_EDGE)
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
