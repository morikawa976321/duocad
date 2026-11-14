"""面のオフセットで端の壁の外面と内面を一緒に選んだときの「端ごと引き伸ばす」(docs/FACE_EDIT.md「端ごと引き伸ばす」)。

題材は上が閉じて下が開いた箱(外形 40×30×20、壁と天板の厚さ 2)。+X の壁の外面 x=40 と内面 x=38 を動かす。
x=20 での断面は、外形 30×20 から内側 26×18 を引いた 132 mm²。
"""
from __future__ import annotations

import math

import pytest
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakeWire, BRepBuilderAPI_Transform
from OCP.BRepFilletAPI import BRepFilletAPI_MakeFillet
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder, BRepPrimAPI_MakePrism, BRepPrimAPI_MakeWedge
from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain
from OCP.gp import gp_Ax1, gp_Ax2, gp_Dir, gp_Elips, gp_Pnt, gp_Trsf, gp_Vec

import duocad.verbs as verbs
from duocad import kernel_face_edit, kernel_face_stretch, query
from duocad.document import Document
from duocad.verbs.add_solid import add_solid_body
from document_support import kinds_of, replay_through_json

from duocad import (
    brep_topology as _brep_topology,
)


SECTION = 30.0 * 20.0 - 26.0 * 18.0


def _box(x0, y0, z0, x1, y1, z1):
    return BRepPrimAPI_MakeBox(gp_Pnt(x0, y0, z0), gp_Pnt(x1, y1, z1)).Shape()


def _unified(shape):
    unifier = ShapeUpgrade_UnifySameDomain(shape, True, True, False)
    unifier.Build()
    return unifier.Shape()


def _shell(outer):
    return _unified(BRepAlgoAPI_Cut(outer, _box(2, 2, -1, 38, 28, 18)).Shape())


def _rounded_outer(radius):
    """外形の縦の4辺を `radius` で丸めた箱(丸めは壁の厚さ 2 より大きい)。"""
    outer = _box(0, 0, 0, 40, 30, 20)
    maker = BRepFilletAPI_MakeFillet(outer)
    for edge in query.describe_edges(outer):
        start, end = edge["start"], edge["end"]
        if abs(start[0] - end[0]) < 1e-9 and abs(start[1] - end[1]) < 1e-9:
            maker.Add(radius, _brep_topology.edge_of(outer, edge["index"]))
    maker.Build()
    return maker.Shape()


def _sharp():
    return _shell(_box(0, 0, 0, 40, 30, 20))


def _rounded():
    return _shell(_rounded_outer(4.0))


def _wall_ends_low():
    """+X・+Y の壁が z=8 から上だけにある丸めた箱。+X と −Y の外面の間の角の丸めは z<8 で −Y の壁の端まで続き、
    支えの面が3枚になる(丸め追従では断られる形)。"""
    return _unified(BRepAlgoAPI_Cut(_rounded(), _box(2, 2, -1, 41, 31, 8)).Shape())


def _document(shape):
    doc = Document()
    return doc, add_solid_body(doc, shape, "box")


def _plane(doc, body_id, axis, value, sign):
    found = [
        face["index"]
        for face in query.describe_faces(doc.get_body(body_id).shape)
        if face["kind"] == "plane" and abs(face["center"][axis] - value) < 1e-6 and abs(face["normal"][axis] - sign) < 1e-9
    ]
    assert len(found) == 1, found
    return found[0]


def _wall(doc, body_id):
    return [_plane(doc, body_id, 0, 40.0, 1.0), _plane(doc, body_id, 0, 38.0, -1.0)]


def _x_planes(doc, body_id):
    return sorted(
        round(face["center"][0], 6)
        for face in query.describe_faces(doc.get_body(body_id).shape)
        if face["kind"] == "plane" and abs(abs(face["normal"][0]) - 1.0) < 1e-9
    )


def _reach_below(doc, body_id, height):
    """高さ `height` より下にある頂点の x の最大(−Y の壁の下の端がどこまで届くか)。"""
    return max(vertex["point"][0] for vertex in query.describe_vertices(doc.get_body(body_id).shape) if vertex["point"][2] < height - 1e-6)


def _volume(doc, body_id):
    return query.volume(doc.get_body(body_id).shape)


@pytest.mark.parametrize("build", [_sharp, _rounded], ids=["sharp", "rounded"])
@pytest.mark.parametrize("distance", [5.0, -3.0])
def test_moving_both_faces_of_an_end_wall_moves_the_wall_and_stretches_the_box(build, distance):
    """落ちたら、端の壁の外面と内面を一緒に選んでも壁ごと平行移動しない(両側へ厚くなる・面が増える・丸めが崩れる)。"""
    doc, body_id = _document(build())
    before, kinds = _volume(doc, body_id), kinds_of(doc, body_id)

    operation = verbs.offset_faces(doc, _wall(doc, body_id), distance, body_id=body_id)

    assert operation.args["stretch"] is True
    assert _x_planes(doc, body_id) == pytest.approx([0.0, 2.0, 38.0 + distance, 40.0 + distance])
    assert _volume(doc, body_id) == pytest.approx(before + SECTION * distance, rel=1e-9)
    assert kinds_of(doc, body_id) == kinds
    radii = sorted(face["radius"] for face in query.describe_faces(doc.get_body(body_id).shape) if face["kind"] == "cylinder")
    assert radii == [4.0] * len(radii)


def test_a_corner_round_running_past_the_wall_end_is_stretched_in_one_call():
    """落ちたら、丸め追従で断られる形(角の丸めの支えが3枚)を、外面と内面を選んだ1回で作れない。手作業の正解と同じく
    z<8 にある −Y の壁の端も一緒に延びる。"""
    doc, body_id = _document(_wall_ends_low())
    outer, inner = _wall(doc, body_id)
    with pytest.raises(ValueError, match=rf"面 {inner} も一緒に選ぶと、端ごと引き伸ばせる"):
        verbs.offset_faces(doc, [outer], 5.0, body_id=body_id)
    before, count = _volume(doc, body_id), _brep_topology.face_count(doc.get_body(body_id).shape)
    low_before = _reach_below(doc, body_id, 8.0)

    operation = verbs.offset_faces(doc, [outer, inner], 5.0, body_id=body_id)

    assert operation.args["stretch"] is True
    assert _volume(doc, body_id) == pytest.approx(before + 5.0 * (2.0 * 20.0 + 2.0 * 12.0 + 26.0 * 2.0), rel=1e-9)
    assert _brep_topology.face_count(doc.get_body(body_id).shape) == count
    assert _reach_below(doc, body_id, 8.0) == pytest.approx(low_before + 5.0)


def test_shrinking_more_than_the_prismatic_span_is_refused_and_changes_nothing():
    """落ちたら、切り口を置ける区間(x=2〜38)の長さを超えて縮める指定で、形を壊すか黙って別の形を作る。"""
    doc, body_id = _document(_sharp())
    shape, log = doc.get_body(body_id).shape, doc.log.to_json()

    with pytest.raises(ValueError, match="縮める量が、切り口を置ける区間の長さ 36 mm を超える"):
        verbs.offset_faces(doc, _wall(doc, body_id), -36.5, body_id=body_id)

    assert doc.get_body(body_id).shape is shape
    assert doc.log.to_json() == log


def test_a_stretch_is_logged_undoable_redoable_and_replays_the_same():
    """落ちたら、引き伸ばしがログ1行・undo 1回にならないか、開き直したときに同じ形にならない。"""
    doc, body_id = _document(_rounded())
    before = _volume(doc, body_id)
    entries = len(doc.log.entries)

    verbs.offset_faces(doc, _wall(doc, body_id), 5.0, body_id=body_id)

    assert len(doc.log.entries) == entries + 1
    after = _volume(doc, body_id)
    doc.log.undo(doc)
    assert _volume(doc, body_id) == pytest.approx(before, rel=1e-12)
    doc.log.redo(doc)
    assert _volume(doc, body_id) == pytest.approx(after, rel=1e-12)
    reopened = replay_through_json(doc)
    assert _volume(reopened, body_id) == pytest.approx(after, rel=1e-12)
    assert _x_planes(reopened, body_id) == _x_planes(doc, body_id)


def test_a_log_saved_before_stretching_existed_replays_as_a_plain_offset():
    """落ちたら、引き伸ばしの印の無い古いログ(外面と内面がそれぞれ外へ出た)を開くと、形が変わる。"""
    doc, body_id = _document(_sharp())
    operation = verbs.offset_faces(doc, _wall(doc, body_id), 5.0, body_id=body_id)
    del operation.args["stretch"]

    reopened = replay_through_json(doc)

    assert _x_planes(reopened, body_id) == pytest.approx([0.0, 2.0, 33.0, 45.0])


def test_slot_walls_facing_each_other_still_move_along_their_own_normals():
    """落ちたら、向かい合う溝の両壁を選んだ今のオフセット(溝が細くなる)が、引き伸ばしに化ける。"""
    doc, body_id = _document(_unified(BRepAlgoAPI_Cut(_box(0, 0, 0, 40, 30, 20), _box(15, -1, 10, 25, 31, 21)).Shape()))
    walls = [_plane(doc, body_id, 0, 15.0, 1.0), _plane(doc, body_id, 0, 25.0, -1.0)]

    operation = verbs.offset_faces(doc, walls, 1.0, body_id=body_id)

    assert "stretch" not in operation.args
    assert _x_planes(doc, body_id) == pytest.approx([0.0, 16.0, 24.0, 40.0])


def test_slot_walls_are_not_an_end_wall_even_when_nothing_flat_lies_beyond():
    """落ちたら、先端が丸く(先に選んでいない平面が無く)ても、溝の両壁(間が空いている)を端の壁と取り違えて溝ごと動かす。"""
    rounded_end = BRepAlgoAPI_Fuse(
        _box(0, 0, 0, 30, 30, 20), BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(30, 15, 0), gp_Dir(0, 0, 1)), 15, 20).Shape()
    ).Shape()
    doc, body_id = _document(_unified(BRepAlgoAPI_Cut(_unified(rounded_end), _box(15, -1, 10, 25, 31, 21)).Shape()))
    walls = [_plane(doc, body_id, 0, 15.0, 1.0), _plane(doc, body_id, 0, 25.0, -1.0)]

    operation = verbs.offset_faces(doc, walls, 1.0, body_id=body_id)

    assert "stretch" not in operation.args
    assert _x_planes(doc, body_id) == pytest.approx([0.0, 16.0, 24.0])


def test_an_end_wall_slightly_off_the_axes_is_stretched():
    """落ちたら、座標軸からわずかに傾いた向き(0.001°)で、切り口の位置の測り方が食い違い、継ぎ目をまとめられずに断る。"""
    turn = gp_Trsf()
    turn.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), math.radians(0.001))
    shape = BRepBuilderAPI_Transform(_sharp(), turn, True).Shape()
    faces = query.describe_faces(shape)
    wall = [
        next(f["index"] for f in faces if f["kind"] == "plane" and abs(f["center"][0] - x) < 0.01 and f["normal"][0] * sign > 0.999)
        for x, sign in ((40.0, 1.0), (38.0, -1.0))
    ]

    final, _ = kernel_face_stretch.stretch(shape, wall, 5.0)

    assert query.volume(final) == pytest.approx(query.volume(shape) + SECTION * 5.0, rel=1e-9)
    assert _brep_topology.face_count(final) == _brep_topology.face_count(shape)


@pytest.mark.parametrize("distance", [5.0, -3.0])
def test_a_closed_tube_whose_round_walls_cross_the_cut_is_stretched(distance):
    """落ちたら、全周の円筒(閉じ目のある面)が切り口を横切る形で、円筒が切れ端のまま残って断る。"""
    axis = gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(1, 0, 0))
    tube = BRepAlgoAPI_Cut(BRepPrimAPI_MakeCylinder(axis, 10, 40).Shape(), BRepPrimAPI_MakeCylinder(axis, 8, 38).Shape()).Shape()
    doc, body_id = _document(tube)
    before, kinds = _volume(doc, body_id), kinds_of(doc, body_id)

    operation = verbs.offset_faces(doc, _wall(doc, body_id), distance, body_id=body_id)

    assert operation.args["stretch"] is True
    assert _volume(doc, body_id) == pytest.approx(before + math.pi * (10.0**2 - 8.0**2) * distance, rel=1e-9)
    assert kinds_of(doc, body_id) == kinds
    assert _x_planes(doc, body_id) == pytest.approx([0.0, 38.0 + distance, 40.0 + distance])


def _elliptic_rod(major, minor, length):
    """X 方向へ押し出した楕円柱(側面は押し出し面)。"""
    ellipse = gp_Elips(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(1, 0, 0), gp_Dir(0, 1, 0)), major, minor)
    face = BRepBuilderAPI_MakeFace(BRepBuilderAPI_MakeWire(BRepBuilderAPI_MakeEdge(ellipse).Edge()).Wire()).Face()
    return BRepPrimAPI_MakePrism(face, gp_Vec(length, 0, 0), False, False).Shape()


def test_an_elliptic_tube_keeps_the_plain_offset():
    """落ちたら、押し出し面が切り口を横切る形を引き伸ばしに振り分け、統合できずに断る(今のオフセットで作れた形が作れなくなる)。"""
    doc, body_id = _document(BRepAlgoAPI_Cut(_elliptic_rod(12, 10, 40), _elliptic_rod(10, 8, 38)).Shape())
    before = _volume(doc, body_id)

    operation = verbs.offset_faces(doc, _wall(doc, body_id), 1.0, body_id=body_id)

    assert "stretch" not in operation.args
    assert _volume(doc, body_id) == pytest.approx(before + math.pi * (12.0 * 10.0 + 10.0 * 8.0), rel=1e-6)


def test_a_tube_whose_axis_is_slightly_tilted_keeps_the_plain_offset():
    """落ちたら、軸が 0.001° 傾いた円筒を並進で変わらないとみなして引き伸ばしに振り分け、切れ端が同じ円筒にならずに断る
    (今のオフセットで作れた形が作れなくなる)。"""
    angle = math.radians(0.001)
    axis = gp_Ax2(gp_Pnt(-10, 0, 0), gp_Dir(math.cos(angle), math.sin(angle), 0))
    outer = BRepAlgoAPI_Common(BRepPrimAPI_MakeCylinder(axis, 10, 60).Shape(), _box(0, -20, -20, 40, 20, 20)).Shape()
    inner = BRepAlgoAPI_Common(BRepPrimAPI_MakeCylinder(axis, 8, 60).Shape(), _box(-1, -20, -20, 38, 20, 20)).Shape()
    doc, body_id = _document(BRepAlgoAPI_Cut(outer, inner).Shape())

    operation = verbs.offset_faces(doc, _wall(doc, body_id), 1.0, body_id=body_id)

    assert "stretch" not in operation.args
    assert _x_planes(doc, body_id) == pytest.approx([0.0, 37.0, 41.0])


@pytest.mark.parametrize("distance", [1.0, 5.0, -3.0])
def test_a_hole_running_along_the_move_inside_a_wall_is_stretched(distance):
    """落ちたら、壁に食い込んで動かす向きに走る穴(割ると円筒が切れ端に分かれる)の切れ端どうしを1枚に戻せず断る。"""
    hole = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(-1, 1.5, 6), gp_Dir(1, 0, 0)), 1, 42).Shape()
    doc, body_id = _document(_unified(BRepAlgoAPI_Cut(_sharp(), hole).Shape()))
    before, kinds = _volume(doc, body_id), kinds_of(doc, body_id)
    inside_wall = math.pi - (math.acos(0.5) - 0.5 * math.sqrt(0.75))

    operation = verbs.offset_faces(doc, _wall(doc, body_id), distance, body_id=body_id)

    assert operation.args["stretch"] is True
    assert _volume(doc, body_id) == pytest.approx(before + (SECTION - inside_wall) * distance, rel=1e-9)
    assert kinds_of(doc, body_id) == kinds


@pytest.mark.parametrize("distance", [1.0, -1.0])
def test_a_hole_whose_axis_runs_on_the_inner_wall_is_stretched(distance):
    """落ちたら、軸が内壁の平面の上を走る穴(辺をつなぐ統合が OCCT の例外で止まる形)で、引き伸ばしが例外で失敗する。"""
    hole = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(-1, 2, 10), gp_Dir(1, 0, 0)), 1, 42).Shape()
    doc, body_id = _document(_unified(BRepAlgoAPI_Cut(_sharp(), hole).Shape()))
    before, kinds = _volume(doc, body_id), kinds_of(doc, body_id)

    operation = verbs.offset_faces(doc, _wall(doc, body_id), distance, body_id=body_id)

    assert operation.args["stretch"] is True
    assert _volume(doc, body_id) == pytest.approx(before + (SECTION - math.pi / 2) * distance, rel=1e-9)
    assert kinds_of(doc, body_id) == kinds


def test_stretching_further_than_the_prismatic_span_fills_the_gap_with_several_slabs():
    """落ちたら、伸ばす量が柱の区間(x=2〜38)より長いとき、間を埋めた板の継ぎ目が残るか形が崩れる。"""
    doc, body_id = _document(_sharp())
    before, count = _volume(doc, body_id), _brep_topology.face_count(doc.get_body(body_id).shape)

    verbs.offset_faces(doc, _wall(doc, body_id), 50.0, body_id=body_id)

    assert _volume(doc, body_id) == pytest.approx(before + SECTION * 50.0, rel=1e-9)
    assert _brep_topology.face_count(doc.get_body(body_id).shape) == count
    assert _x_planes(doc, body_id) == pytest.approx([0.0, 2.0, 88.0, 90.0])


def test_faces_that_cannot_be_stretched_are_refused_by_the_kernel():
    """落ちたら、引き伸ばしの前提(平行な平面・切り口を置ける区間・両向き)が崩れた選択を、記録済みの再生が通してしまう。"""
    doc, body_id = _document(_sharp())
    shape = doc.get_body(body_id).shape
    outer, inner = _wall(doc, body_id)
    top = _plane(doc, body_id, 2, 20.0, 1.0)
    with pytest.raises(kernel_face_edit.FaceEditError, match="no prismatic span"):
        kernel_face_stretch.stretch(shape, [outer, top], 5.0)
    with pytest.raises(kernel_face_edit.FaceEditError, match="does not close an end wall"):
        kernel_face_stretch.stretch(shape, [outer], 5.0)
    rounded, rounded_id = _document(_wall_ends_low())
    rounded_outer, rounded_inner = _wall(rounded, rounded_id)
    with pytest.raises(kernel_face_edit.FaceEditError, match=rf"faces \[{rounded_inner}\] beyond the cut .* not selected"):
        kernel_face_stretch.stretch(rounded.get_body(rounded_id).shape, [rounded_outer], 5.0)
    wedge = BRepPrimAPI_MakeWedge(40.0, 30.0, 20.0, 10.0).Shape()
    end = next(face["index"] for face in query.describe_faces(wedge) if face["kind"] == "plane" and face["normal"][1] > 0.99)
    assert kernel_face_stretch.plans(wedge, [end]) == []
    with pytest.raises(kernel_face_edit.FaceEditError, match="no prismatic span"):
        kernel_face_stretch.stretch(wedge, [end], 5.0)
