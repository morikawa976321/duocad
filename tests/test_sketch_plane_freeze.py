"""面に貼ったスケッチの姿勢は**作成時に固定**され、その後の形状変更に動かされない。"""
from __future__ import annotations

import math

import pytest

from duocad import kernel, kernel_sketch, parameters, query, region
from duocad.document import Document
from duocad.region import region_ref
from duocad.sketch import (
    add_circle,
    add_rectangle,
    face_plane,
    get_sketch,
    sketch_create,
)
from duocad.verbs import extrude
from document_support import all_edges, assert_same_frame, new_box, replay_through_json

BOX = 20.0
#: 直方体の上面(z=BOX・法線+Z)の面添字。`new_box` の直後は必ずこれになる(実測)。
TOP_FACE_INDEX = 6
#: +X側の側面の面添字(`new_box` の直後)。幅(`d3`)を変えると位置が動く面。
PLUS_X_FACE_INDEX = 3


def live_frame(doc: Document, body_id: str, face_index: int) -> dict:
    """**その瞬間の** `body.shape` の面から枠を取る(凍結前の古い解決の再現)。"""
    return kernel_sketch.face_frame(doc.get_body(body_id).shape, face_index)


def top_plane_indices(doc: Document, body_id: str, z: float) -> list:
    """高さ `z` にある上向きの平面の面添字(消滅したかを実物で見るため)。"""
    found = []
    for face in query.describe_faces(doc.get_body(body_id).shape):
        if face["kind"] != "plane" or face["normal"][2] < 0.99:
            continue
        if abs(face["center"][2] - z) < 1.0e-9:
            found.append(face["index"])
    return found


def test_a_later_fillet_does_not_move_a_face_sketch_even_when_the_index_now_lands_on_a_sphere():
    """2026-08-30の事故そのもの。**添字の指す先が球面へずれてもスケッチは動かない**。"""
    document = Document()
    body_id = new_box(document, BOX)
    sketch_id = sketch_create(document, face_plane(body_id, TOP_FACE_INDEX))
    add_circle(document, sketch_id, (0.0, 0.0), 8.0)
    frame_before = region.plane_frame(document, sketch_id)

    from duocad.verbs import fillet

    fillet(document, all_edges(document, body_id), 3.0, body_id=body_id)

    # 前提の確認: 添字6はもう平面ではない(＝古い実装ならここで落ちていた)
    shape = document.get_body(body_id).shape
    assert kernel.classify_face(kernel.face_of(shape, TOP_FACE_INDEX)) != "plane"
    with pytest.raises(ValueError):
        kernel_sketch.face_frame(shape, TOP_FACE_INDEX)

    assert_same_frame(region.plane_frame(document, sketch_id), frame_before)
    assert [item.area for item in region.regions(document, sketch_id)] == pytest.approx(
        [math.pi * 16.0]
    )


def test_a_face_sketch_survives_the_referenced_face_disappearing_completely():
    """参照元の面が**完全に消滅**してもスケッチは元の高さ・向きで残る(Fusion実機の挙動)。

    **フィレットで面積をちょうど0にする経路は使えない**: OCCTのフィレットは半径が限界値
    (20mm立方体の上面4辺なら r=10)ちょうどだと `IsDone=False` で失敗する(実測。
    `tests/test_fillet.py` のdocstringも同じ限界を記録している)。限界未満では常に微小な
    平面が残るので、「面積が完全に0」を作れるのは切削の側である。
    """
    document = Document()
    body_id = new_box(document, BOX)
    kept = sketch_create(document, face_plane(body_id, TOP_FACE_INDEX))
    add_circle(document, kept, (0.0, 0.0), 8.0)
    frame_before = region.plane_frame(document, kept)
    assert top_plane_indices(document, body_id, BOX) == [TOP_FACE_INDEX]

    cutter = sketch_create(document, face_plane(body_id, TOP_FACE_INDEX))
    # 上面より大きい長方形。スケッチ平面の原点は面の中央付近なので (u,v) を中心対称に取る。
    add_rectangle(document, cutter, (-15.0, -15.0), 30.0, 30.0)
    extrude(document, [region_ref(cutter, 1)], -5.0, mode="cut", body_id=body_id)

    assert top_plane_indices(document, body_id, BOX) == [], "上面が残っている(前提が崩れた)"
    assert kernel.volume(document.get_body(body_id).shape) == pytest.approx(
        BOX * BOX * (BOX - 5.0)
    )
    # 添字6は今や+X側の側面。凍結前は**例外も出さずに**スケッチがそこへ移っていた。
    assert live_frame(document, body_id, TOP_FACE_INDEX)["normal"] == pytest.approx(
        [1.0, 0.0, 0.0]
    )

    assert_same_frame(region.plane_frame(document, kept), frame_before)
    assert region.plane_frame(document, kept)["origin"][2] == pytest.approx(BOX)
    assert [item.area for item in region.regions(document, kept)] == pytest.approx(
        [math.pi * 16.0]
    )


def test_extrude_takes_its_direction_from_the_frozen_frame():
    """押し出しの向きも凍結した枠から取る(ライブの面から引き直さない)。"""
    document = Document()
    body_id = new_box(document, BOX)
    sketch_id = sketch_create(document, face_plane(body_id, TOP_FACE_INDEX))
    add_circle(document, sketch_id, (0.0, 0.0), 8.0)

    from duocad.verbs import fillet

    fillet(document, all_edges(document, body_id), 3.0, body_id=body_id)
    volume_before = kernel.volume(document.get_body(body_id).shape)

    extrude(document, [region_ref(sketch_id, 1)], 5.0, mode="join", body_id=body_id)

    volume_after = kernel.volume(document.get_body(body_id).shape)
    assert volume_after > volume_before, "上向きに積めていない(法線が別の面へ移った)"


def test_a_face_sketch_follows_a_parameter_edit_that_precedes_it():
    """`set_parameter` は履歴を頭から再生するので、スケッチはその時点の形状で解き直される。"""
    document = Document()
    body_id = new_box(document, BOX)
    sketch_id = sketch_create(document, face_plane(body_id, PLUS_X_FACE_INDEX))
    add_circle(document, sketch_id, (0.0, 0.0), 6.0)
    before = region.plane_frame(document, sketch_id)
    assert before["origin"][0] == pytest.approx(BOX)

    parameters.set_parameter(document, "d3", 30.0)

    after = region.plane_frame(document, sketch_id)
    assert after["origin"][0] == pytest.approx(30.0)
    assert after["normal"] == pytest.approx([1.0, 0.0, 0.0])
    assert after["u"] == pytest.approx(before["u"])
    assert after["v"] == pytest.approx(before["v"])
    assert [item.area for item in region.regions(document, sketch_id)] == pytest.approx(
        [math.pi * 9.0]
    )


def test_the_frozen_frame_equals_the_live_resolution_at_creation_time_for_every_face():
    """凍結した枠は「作成時にライブで解いた枠」と**厳密に一致**する。

    **反転面(`TopAbs_REVERSED`)が要点**。`_ax3_from_face` は反転面に `ZReverse()` を当てるが、
    これは Direction だけを裏返して X/Y を据え置く(＝左手系になる)。一方
    `gp_Ax3(location, direction, xdirection)` は必ず右手系を作る。origin/normal/u だけを
    保存して組み直すと **v の符号だけが反転し、図形が平面内で鏡像になる**(OCCTのこの挙動は
    2026-08-30に実測)。だから枠は v も保存し、必要なら `YReverse()` で戻す。
    直方体の6面には反転面が3枚あるので、この検査で両方の系を通る。
    """
    document = Document()
    body_id = new_box(document, BOX)
    faces = query.describe_faces(document.get_body(body_id).shape)
    assert len(faces) == 6

    reversed_faces = 0
    for face in faces:
        expected = live_frame(document, body_id, face["index"])
        sketch_id = sketch_create(document, face_plane(body_id, face["index"]))
        assert_same_frame(region.plane_frame(document, sketch_id), expected)
        # 右手系/左手系の両方を実際に通ったことを数える(u × v が normal と同じ向きか)
        cross_z = (
            expected["u"][1] * expected["v"][2] - expected["u"][2] * expected["v"][1],
            expected["u"][2] * expected["v"][0] - expected["u"][0] * expected["v"][2],
            expected["u"][0] * expected["v"][1] - expected["u"][1] * expected["v"][0],
        )
        if sum(a * b for a, b in zip(cross_z, expected["normal"])) < 0:
            reversed_faces += 1
    # 押し出しで作った直方体は6面中5面が反転面(実測)。**両方の系を実際に通ったこと**だけを
    # 固定する —— 枚数そのものはOCCTのブーリアンの都合で変わりうる。
    assert 0 < reversed_faces < 6, (
        f"右手系と左手系の両方を通っていない(反転面 {reversed_faces}/6)"
    )


def test_the_frame_is_kept_out_of_the_oplog():
    """ログには**素の参照だけ**を載せる。焼き付けた枠を入れると再生で古い姿勢が復活する。"""
    document = Document()
    body_id = new_box(document, BOX)
    sketch_id = sketch_create(document, face_plane(body_id, TOP_FACE_INDEX))

    logged = [
        entry for entry in document.log.entries if entry.verb == "sketch_create"
    ][-1]
    assert set(logged.args["plane"]) == {"kind", "body", "index", "provenance"}
    assert "frame" in get_sketch(document, sketch_id).plane


def test_a_log_round_trip_reproduces_the_same_frames():
    """保存(JSON)→再生でスケッチの枠が同じに戻る(undo/redoと保存の互換)。"""
    document = Document()
    body_id = new_box(document, BOX)
    sketch_id = sketch_create(document, face_plane(body_id, TOP_FACE_INDEX))
    add_circle(document, sketch_id, (0.0, 0.0), 8.0)

    from duocad.verbs import fillet

    fillet(document, all_edges(document, body_id), 3.0, body_id=body_id)
    expected = region.plane_frame(document, sketch_id)

    restored = replay_through_json(document)

    assert_same_frame(region.plane_frame(restored, sketch_id), expected)
    assert [item.area for item in region.regions(restored, sketch_id)] == pytest.approx(
        [math.pi * 16.0]
    )


def test_undo_and_redo_of_the_fillet_leave_the_sketch_pose_alone():
    """フィレットの取り消し・やり直しでスケッチの姿勢が揺れない。"""
    document = Document()
    body_id = new_box(document, BOX)
    sketch_id = sketch_create(document, face_plane(body_id, TOP_FACE_INDEX))
    add_circle(document, sketch_id, (0.0, 0.0), 8.0)
    expected = region.plane_frame(document, sketch_id)

    from duocad.verbs import fillet

    fillet(document, all_edges(document, body_id), 3.0, body_id=body_id)
    assert_same_frame(region.plane_frame(document, sketch_id), expected)

    document.log.undo(document)
    assert_same_frame(region.plane_frame(document, sketch_id), expected)

    document.log.redo(document)
    assert_same_frame(region.plane_frame(document, sketch_id), expected)


def test_a_face_plane_without_a_frame_is_rejected():
    """枠を持たない面参照は解けない(姿勢は作成時に焼き付けた枠が唯一の正)。"""
    bare = {"kind": "face", "body": "b", "index": TOP_FACE_INDEX, "provenance": None}
    with pytest.raises(ValueError):
        kernel_sketch.sketch_plane_frame(bare)


@pytest.mark.parametrize(
    "frame",
    [
        pytest.param("not a dict", id="not-a-dict"),
        pytest.param({"origin": [0, 0, 0], "u": [1, 0, 0], "v": [0, 1, 0]}, id="missing-normal"),
        pytest.param(
            {"origin": [0, 0, 0], "u": [1, 0, 0], "v": [0, 1, 0], "normal": [0, 0, 0]},
            id="zero-normal",
        ),
        pytest.param(
            {"origin": [0, 0, 0], "u": [0, 0, 1], "v": [0, 1, 0], "normal": [0, 0, 1]},
            id="u-parallel-to-normal",
        ),
        pytest.param(
            {"origin": [0, 0, 0], "u": [1, 1, 0], "v": [0, 1, 0], "normal": [0, 0, 1]},
            id="u-not-perpendicular",
        ),
        pytest.param(
            {
                "origin": [0, 0, float("nan")],
                "u": [1, 0, 0],
                "v": [0, 1, 0],
                "normal": [0, 0, 1],
            },
            id="nan-origin",
        ),
    ],
)
def test_a_broken_frame_is_rejected_instead_of_silently_mirroring_the_plane(frame):
    """直交していない・非有限・欠けている枠は ValueError。"""
    plane = {"kind": "face", "body": "b", "index": 1, "provenance": None, "frame": frame}
    with pytest.raises(ValueError):
        kernel_sketch.sketch_plane_frame(plane)


def _bodies_to_check():
    """面に貼るスケッチの法線を確かめるボディ: 通常 / 回転体 / 鏡映1回 / 鏡映2回(`(名前, 文書, body_id)`)。"""
    from duocad.region import regions
    from duocad.sketch import add_line
    from duocad.verbs import mirror_body, revolve

    document = Document()
    box_id = new_box(document, BOX)
    once = mirror_body(document, [box_id], base_plane="YZ").args["created"][0]
    twice = mirror_body(document, [once], base_plane="XZ").args["created"][0]

    sketch_id = sketch_create(document, "XY")
    add_rectangle(document, sketch_id, (50.0, 0.0), 10.0, 5.0)
    axis = add_line(document, sketch_id, (50.0, 0.0), (60.0, 0.0))
    revolved = revolve(
        document, [region_ref(sketch_id, regions(document, sketch_id)[0].index)], {"sketch": sketch_id, "entity": axis},
        mode="new",
    ).args["body_id"]
    return document, {"plain": box_id, "revolved": revolved, "mirrored_once": once, "mirrored_twice": twice}


@pytest.mark.parametrize("name", ["plain", "revolved", "mirrored_once", "mirrored_twice"])
def test_a_face_sketch_normal_points_out_of_the_body_on_every_planar_face(name):
    """面に貼ったスケッチの法線(押し出し・オフセットの正の向き)は面の外向き法線と一致する。
    落ちたら、向きの反転を `Orientation()` で決めている(奇数回鏡映したボディの面は平面の Position が左手系で、全面で逆になる)。"""
    document, body_ids = _bodies_to_check()
    shape = document.get_body(body_ids[name]).shape

    checked = 0
    for face in query.describe_faces(shape):
        if face["kind"] != "plane":
            continue
        frame = kernel_sketch.face_frame(shape, face["index"])
        assert frame["normal"] == pytest.approx(face["normal"], abs=1.0e-9), f"{name} face {face['index']}"
        checked += 1
    assert checked >= 2


def test_a_region_on_a_mirrored_bodys_face_extrudes_outward_with_a_positive_distance():
    """鏡映したボディの面の上の領域を正の距離で押し出して join すると、材料が外へ伸びる。
    落ちたら押し出しが面の内側へ向かい、`join did not change the shape` で断られる。"""
    from duocad.verbs import mirror_body

    document = Document()
    box_id = new_box(document, BOX)
    mirrored = mirror_body(document, [box_id], base_plane="YZ").args["created"][0]
    before = query.volume(document.get_body(mirrored).shape)
    top = next(face for face in query.describe_faces(document.get_body(mirrored).shape) if face["normal"][2] > 0.99)

    sketch_id = sketch_create(document, face_plane(mirrored, top["index"]))
    frame = region.plane_frame(document, sketch_id)
    offset = [top["center"][i] - frame["origin"][i] for i in range(3)]
    u = sum(offset[i] * frame["u"][i] for i in range(3))
    v = sum(offset[i] * frame["v"][i] for i in range(3))
    add_rectangle(document, sketch_id, (u - 5.0, v - 5.0), 10.0, 10.0)
    extrude(document, [region_ref(sketch_id, 1)], 5.0, mode="join", body_id=mirrored)

    assert query.volume(document.get_body(mirrored).shape) == pytest.approx(before + 10.0 * 10.0 * 5.0)
