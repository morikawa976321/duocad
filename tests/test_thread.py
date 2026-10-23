"""ねじ(`thread`)の契約。山形・隙間の表は docs/THREAD_SIM.md 6.10、形の作り方は docs/THREAD.md「形の作り方」。

落ちたら: ねじの形が壊れた(体積の門・BRepCheck・三角形化)か、隙間の既定・上下限・警告の決まりが変わった。
"""
from __future__ import annotations

import numpy as np
import pytest

from duocad import kernel, kernel_thread, query, selection
from duocad.document import Document
from duocad.entry import entry_for
from duocad.server import Session
from duocad.region import region_ref
from duocad.sketch import add_circle, base_plane, sketch_create
from duocad.verbs import chamfer, extrude, thread
from document_support import new_box, new_box_with_hole, replay_through_json
from test_chamfer import new_cylinder


def _cylinder_face(doc: Document, body_id: str) -> int:
    face_map = kernel._face_map(doc.get_body(body_id).shape)
    found = [i for i in range(1, face_map.Extent() + 1) if kernel.classify_face(face_map.FindKey(i)) == "cylinder_full"]
    assert len(found) == 1, found
    return found[0]


def _plane_face(doc: Document, body_id: str) -> int:
    face_map = kernel._face_map(doc.get_body(body_id).shape)
    return next(i for i in range(1, face_map.Extent() + 1) if kernel.classify_face(face_map.FindKey(i)) == "plane")


def _hole(doc: Document) -> tuple:
    """20mm の立方体に直径 6 の貫通穴。"""
    body_id = new_box_with_hole(doc, 20.0, 3.0)
    return body_id, _cylinder_face(doc, body_id)


def _assert_sound(shape) -> None:
    mesh = kernel.tessellate(shape, 0.01, kernel.DEFAULT_ANGULAR_DEFLECTION)
    assert mesh["untriangulated"] == []


# ---------------------------------------------------------------- 雌(穴)・雄(軸)
def test_a_hole_gets_a_female_thread_with_the_default_clearance():
    """穴は雌ねじ。隙間の空欄は推奨の真ん中(45°台形 0.12)、ピッチは径から(D6 → 1.5)。ねじ山が穴へ張り出すぶん体積が増える。"""
    doc = Document()
    body_id, face = _hole(doc)
    volume_before = query.volume(doc.get_body(body_id).shape)

    operation = thread(doc, face, "trapezoid45", body_id=body_id)

    assert operation.args == {"face": face, "profile": "trapezoid45", "clearance": 0.12, "pitch": 1.5, "body_id": body_id}
    assert operation.provenance["female"] is True
    assert face in operation.provenance["deleted"]
    shape = doc.get_body(body_id).shape
    assert query.volume(shape) > volume_before
    _assert_sound(shape)
    doc.log.to_json()


def test_a_shaft_gets_a_male_thread_with_zero_clearance_and_chamfered_tips():
    """軸は雄ねじで、既定の隙間は 0(隙間はナット側)。両端が円板の軸は両方の先を面取りする。"""
    doc = Document()
    body_id = new_cylinder(doc, radius=3.0, height=10.0)
    face = _cylinder_face(doc, body_id)
    volume_before = query.volume(doc.get_body(body_id).shape)

    operation = thread(doc, face, "trapezoid45", body_id=body_id)

    assert operation.args["clearance"] == 0.0
    assert operation.provenance["female"] is False
    shape = doc.get_body(body_id).shape
    assert query.volume(shape) < volume_before
    _assert_sound(shape)


@pytest.mark.parametrize("profile", sorted(kernel_thread.PROFILES))
def test_a_d3_shaft_gets_a_male_thread(profile):
    """D3 は面取りした先の半径が 1mm 未満(1.5 − 0.6)。先の円錐を作る回転体が細い軸で壊れないこと(両端とも先)。"""
    doc = Document()
    body_id = new_cylinder(doc, radius=1.5, height=8.0)
    operation = thread(doc, _cylinder_face(doc, body_id), profile, body_id=body_id)
    assert operation.args["pitch"] == pytest.approx(1.2)
    _assert_sound(doc.get_body(body_id).shape)


def _outer_cylinder_face(doc: Document, body_id: str) -> int:
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.TopoDS import TopoDS

    face_map = kernel._face_map(doc.get_body(body_id).shape)
    found = [i for i in range(1, face_map.Extent() + 1) if kernel.classify_face(face_map.FindKey(i)) == "cylinder_full"]
    return max(found, key=lambda i: BRepAdaptor_Surface(TopoDS.Face_s(face_map.FindKey(i))).Cylinder().Radius())


def _bolt(doc: Document, diameter: float) -> str:
    """12×12×3 の頭の上面に立てた軸(根元は頭の平面、先は円板)。"""
    body_id = new_box(doc, 12.0, 12.0, 3.0)
    sketch_id = sketch_create(doc, base_plane("XY"))
    add_circle(doc, sketch_id, (6.0, 6.0), diameter)
    extrude(doc, [region_ref(sketch_id, 1)], 3.0 + 2 * diameter, mode="join", body_id=body_id)
    return body_id


def _hollow_shaft(doc: Document, bore_diameter: float) -> str:
    body_id = new_cylinder(doc, radius=3.0, height=12.0)
    sketch_id = sketch_create(doc, base_plane("XY"))
    add_circle(doc, sketch_id, (0.0, 0.0), bore_diameter)
    extrude(doc, [region_ref(sketch_id, 1)], 12.0, mode="cut", body_id=body_id)
    return body_id


@pytest.mark.parametrize("profile", sorted(kernel_thread.PROFILES))
@pytest.mark.parametrize("diameter", [3.0, 10.0])
def test_a_bolt_standing_on_a_head_is_sound(profile, diameter):
    """根元が頭の平面の軸。ねじの切り口が頭の上面に載ると 30° でその面が三角形に分割できず、D10 では門で落ちた(根元は谷より細い首で始める)。"""
    doc = Document()
    body_id = _bolt(doc, diameter)
    volume_before = query.volume(doc.get_body(body_id).shape)
    thread(doc, _cylinder_face(doc, body_id), profile, body_id=body_id)
    shape = doc.get_body(body_id).shape
    assert query.volume(shape) < volume_before
    _assert_sound(shape)


@pytest.mark.parametrize("profile", sorted(kernel_thread.PROFILES))
def test_a_hollow_shaft_keeps_its_bore(profile):
    """軸の中の同軸の穴(直径 1.2)は、ねじを切った後も穴のまま(円柱を足し引きする作り方で埋めない)。"""
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    from OCP.TopAbs import TopAbs_State
    from OCP.gp import gp_Pnt

    doc = Document()
    body_id = _hollow_shaft(doc, 1.2)
    thread(doc, _outer_cylinder_face(doc, body_id), profile, body_id=body_id)
    shape = doc.get_body(body_id).shape
    assert BRepClass3d_SolidClassifier(shape, gp_Pnt(0.0, 0.0, 6.0), 1e-6).State() == TopAbs_State.TopAbs_OUT
    _assert_sound(shape)


def test_a_bore_just_inside_the_chamfered_tips_is_kept():
    """D6 P1.5 の軸の先の面取りの半径は 2.25。直径 4.44(半径 2.22)の穴はその内側なので、断らずに穴を残す。"""
    doc = Document()
    body_id = _hollow_shaft(doc, 4.44)
    thread(doc, _outer_cylinder_face(doc, body_id), "trapezoid45", body_id=body_id)
    _assert_sound(doc.get_body(body_id).shape)


def test_a_bore_reaching_past_the_chamfered_tips_is_refused():
    """直径 4.6(半径 2.3)の穴は先の面取りの半径 2.25 より太く、切ると壁が抜けるので断る。"""
    doc = Document()
    body_id = _hollow_shaft(doc, 4.6)
    _assert_refused_without_change(doc, body_id, _outer_cylinder_face(doc, body_id), "trapezoid45", None, "軸の中の穴")


def test_a_shaft_too_short_to_keep_a_full_thread_is_refused():
    """D6 P1.5 の軸の両端の面取りは 0.75 ずつ。長さ 2 では山頂の残る長さがピッチに足りない(ねじ山が円錐に削られて消える)。"""
    doc = Document()
    body_id = new_cylinder(doc, radius=3.0, height=2.0)
    _assert_refused_without_change(doc, body_id, _cylinder_face(doc, body_id), "trapezoid45", None, "軸が短すぎる")


def test_a_shoulder_widening_towards_the_root_is_not_taken_for_a_chamfered_tip():
    """根元へ太くなる円錐の肩の上の軸は切れる(先へ細る円錐 = 面取り済みの先だけを断る)。"""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCone, BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

    shoulder = BRepPrimAPI_MakeCone(5.0, 3.0, 2.0).Shape()
    shaft = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0.0, 0.0, 2.0), gp_Dir(0.0, 0.0, 1.0)), 3.0, 8.0).Shape()
    shape = BRepAlgoAPI_Fuse(shoulder, shaft).Shape()
    face_map = kernel._face_map(shape)
    face = next(i for i in range(1, face_map.Extent() + 1) if kernel.classify_face(face_map.FindKey(i)) == "cylinder_full")
    assert kernel_thread.describe_target(shape, face)["ends"]["low"]["kind"] == "shoulder"
    result = kernel_thread.thread_face(shape, face, "trapezoid45")
    _assert_sound(result["shape"])


def test_the_semicircle_profile_builds_on_a_hole():
    doc = Document()
    body_id, face = _hole(doc)
    operation = thread(doc, face, "semicircle", body_id=body_id)
    assert operation.args["clearance"] == 0.12
    _assert_sound(doc.get_body(body_id).shape)


def test_thread_is_logged_undoable_and_replays():
    doc = Document()
    body_id, face = _hole(doc)
    volume_before = query.volume(doc.get_body(body_id).shape)
    thread(doc, face, "trapezoid30", 0.2, body_id=body_id)
    volume_after = query.volume(doc.get_body(body_id).shape)

    doc.log.undo(doc)
    assert query.volume(doc.get_body(body_id).shape) == pytest.approx(volume_before)
    doc.log.redo(doc)
    assert query.volume(doc.get_body(body_id).shape) == pytest.approx(volume_after)
    replayed = replay_through_json(doc)
    assert query.volume(replayed.get_body(body_id).shape) == pytest.approx(volume_after)


# ---------------------------------------------------------------- 断るもの(文書もログも変えない)
def _assert_refused_without_change(doc, body_id, face, profile, clearance, match):
    shape_before = doc.get_body(body_id).shape
    log_before = len(doc.log.entries)
    with pytest.raises(ValueError, match=match):
        thread(doc, face, profile, clearance, body_id=body_id)
    assert doc.get_body(body_id).shape is shape_before
    assert len(doc.log.entries) == log_before


@pytest.mark.parametrize(
    "profile,clearance,match",
    [
        ("iso", None, "profile"),
        ("trapezoid45", -0.01, "0 以上"),
        ("trapezoid45", 0.48, "上限"),  # D6 は P1.5、上限 0.4P − 0.125 = 0.475
        ("semicircle", 0.27, "上限"),  # 0.24P − 0.1 = 0.26
    ],
)
def test_bad_arguments_are_refused_without_change(profile, clearance, match):
    doc = Document()
    body_id, face = _hole(doc)
    _assert_refused_without_change(doc, body_id, face, profile, clearance, match)


def test_a_flat_face_is_refused():
    doc = Document()
    body_id, _ = _hole(doc)
    _assert_refused_without_change(doc, body_id, _plane_face(doc, body_id), "trapezoid45", None, "全周の円筒面")


def test_a_shaft_whose_tip_is_already_chamfered_is_refused():
    """面取りの後にねじを切ると、先が山より太い段になってナットに入らない(docs/THREAD.md「断るもの」)。"""
    doc = Document()
    body_id = new_cylinder(doc, radius=3.0, height=10.0)
    circles = [e["index"] for e in query.describe_edges(doc.get_body(body_id).shape) if e["kind"] == "circle"]
    chamfer(doc, circles[:1], 0.5, body_id=body_id)
    _assert_refused_without_change(doc, body_id, _cylinder_face(doc, body_id), "trapezoid45", None, "面取り")


# ---------------------------------------------------------------- 山形と隙間
@pytest.mark.parametrize("profile", sorted(kernel_thread.PROFILES))
def test_the_nut_profile_always_clears_the_bolt_profile(profile):
    """同じ位相で重ねたとき、雌(隙間 c)の面は雄(隙間 0)の面より外にある = はまる側(法線方向の隙間でも半径方向でも)。"""
    pitch, radius, c = 1.5, 3.0, kernel_thread.default_clearance(profile, True)
    male = kernel_thread.profile_points(profile, radius, pitch, 0.0, female=False)
    female = kernel_thread.profile_points(profile, radius, pitch, c, female=True)
    s = np.linspace(0.0, pitch, 3000, endpoint=False)
    gap = np.interp(s, *zip(*female)) - np.interp(s, *zip(*male))
    assert gap.min() > 0.0


def test_the_clearance_limit_keeps_engagement_and_scales_with_pitch():
    """上限は山の深さ(= 0.4P)に比例する式で、径によらない(THREAD_SIM.md 6.10)。"""
    assert kernel_thread.clearance_limit("trapezoid45", 1.5) == pytest.approx(0.475)
    assert kernel_thread.clearance_limit("semicircle", 2.5) == pytest.approx(0.5)
    assert kernel_thread.default_pitch(3.0) == pytest.approx(1.2)
    assert kernel_thread.default_pitch(10.0) == pytest.approx(2.5)


def test_a_clearance_outside_the_recommended_range_warns_but_does_not_refuse():
    assert kernel_thread.clearance_warning("semicircle", True, 0.12) is None
    assert "推奨" in kernel_thread.clearance_warning("semicircle", True, 0.2)
    assert "ナット側" in kernel_thread.clearance_warning("trapezoid45", False, 0.1)


# ---------------------------------------------------------------- 入口・警告
def test_the_entry_takes_exactly_one_face_from_the_selection():
    doc = Document()
    body_id, face = _hole(doc)
    selection.set_selection(doc, refs=[selection.select_face(doc, body_id, face)])
    result = entry_for("thread")(doc, profile="trapezoid45")
    assert result == {"body_id": body_id, "changed_bodies": [body_id]}


def test_the_entry_refuses_two_faces():
    doc = Document()
    body_id, face = _hole(doc)
    selection.set_selection(
        doc, refs=[selection.select_face(doc, body_id, face), selection.select_face(doc, body_id, _plane_face(doc, body_id))]
    )
    with pytest.raises(selection.SelectionAmbiguous):
        entry_for("thread")(doc, profile="trapezoid45")


def test_the_warning_is_shown_on_apply_but_not_on_preview():
    """推奨の外の隙間は確定のときだけ警告のトーストへ積む(プレビューのたびに積まない)。"""
    session = Session(0.2)
    body_id, face = _hole(session.document)
    session.clear_selection()
    session.modify_selection([{"body": body_id, "kind": "face", "index": face}], [])

    session.preview("thread", {"profile": "trapezoid45", "clearance": 0.3})
    assert session.take_warnings() == []

    result = session.thread("trapezoid45", 0.3)
    assert "推奨" in result["warning"]
    assert any("推奨" in warning for warning in session.take_warnings())
