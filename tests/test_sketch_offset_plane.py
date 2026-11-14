"""オフセット平面へのスケッチ作成(issue #3)。"""
from __future__ import annotations

import math

import pytest

from duocad import kernel_sketch, query, region, selection
from duocad.document import Document
from duocad.sketch import (
    BASE_PLANES,
    add_circle,
    base_plane,
    face_plane,
    offset_plane,
    resolve_plane,
    sketch_create,
    sketch_plane,
)
from document_support import all_edges, assert_same_frame, new_box, replay_through_json
from session_support import box as session_box, select

from duocad import (
    brep_geometry as _brep_geometry,
    brep_topology as _brep_topology,
)

BOX = 20.0
#: 直方体の上面(z=BOX・法線+Z)の面添字。`new_box` の直後は必ずこれになる(実測)。
TOP_FACE_INDEX = 6


@pytest.mark.parametrize("name", BASE_PLANES)
def test_an_offset_from_each_base_plane_translates_along_that_planes_own_normal(name):
    """3基準平面それぞれで、平行移動する向きがその平面自身の法線であること(XYだけでは
    たまたま normal=[0,0,1] という特別な形に隠れて他2平面の取り違えを検出できない)。
    """
    document = Document()
    sketch_id = sketch_create(document, offset_plane(base_plane(name), 7.0))

    frame = region.plane_frame(document, sketch_id)
    base_frame = kernel_sketch.sketch_plane_frame(base_plane(name))
    assert frame["origin"] == pytest.approx(
        [base_frame["origin"][i] + base_frame["normal"][i] * 7.0 for i in range(3)]
    )
    assert frame["u"] == pytest.approx(base_frame["u"])
    assert frame["v"] == pytest.approx(base_frame["v"])
    assert frame["normal"] == pytest.approx(base_frame["normal"])


def test_a_zero_distance_coincides_with_the_base_plane():
    """0は「基準平面と同じ位置になる」だけで、禁止する理由が無い(UI側の `reject` と同じ判断)。"""
    document = Document()
    sketch_id = sketch_create(document, offset_plane(base_plane("XY"), 0.0))
    assert region.plane_frame(document, sketch_id) == kernel_sketch.sketch_plane_frame(
        base_plane("XY")
    )


def test_a_later_fillet_does_not_move_an_offset_from_face_sketch():
    """オフセット元の面が丸められて添字の指す先が球面へずれても、offset平面は動かない。"""
    document = Document()
    body_id = new_box(document, BOX)
    sketch_id = sketch_create(
        document, offset_plane(face_plane(body_id, TOP_FACE_INDEX), 3.0)
    )
    add_circle(document, sketch_id, (0.0, 0.0), 8.0)
    frame_before = region.plane_frame(document, sketch_id)

    from duocad.verbs import fillet

    fillet(document, all_edges(document, body_id), 3.0, body_id=body_id)

    # 前提の確認: 添字6はもう平面ではない(旧実装ならここでスケッチごと落ちていた)
    shape = document.get_body(body_id).shape
    assert _brep_geometry.classify_face(_brep_topology.face_of(shape, TOP_FACE_INDEX)) != "plane"

    assert_same_frame(region.plane_frame(document, sketch_id), frame_before)
    assert [item.area for item in region.regions(document, sketch_id)] == pytest.approx(
        [math.pi * 16.0]
    )


def test_an_offset_from_every_face_including_reversed_ones_keeps_the_faces_uv_axes():
    """6面全部(反転面を含む)でoffsetがv軸の符号を壊さないこと。

    `_ax3_from_frame` は反転面の `v` を保存し直さないと平面内で図形が鏡像になる罠を持つ
    (`tests/test_sketch_plane_freeze.py` 参照)。offsetはbaseの `frame` をそのまま
    平行移動するだけなので理屈の上では自動的に安全なはずだが、それを実装から独立に
    6面すべて(反転面3枚を含む実測済みの構成)で確かめる。
    """
    document = Document()
    body_id = new_box(document, BOX)
    shape = document.get_body(body_id).shape
    faces = query.describe_faces(shape)
    assert len(faces) == 6

    for face in faces:
        expected_face_frame = kernel_sketch.face_frame(shape, face["index"])
        sketch_id = sketch_create(
            document, offset_plane(face_plane(body_id, face["index"]), 2.0)
        )
        frame = region.plane_frame(document, sketch_id)
        assert frame["u"] == pytest.approx(expected_face_frame["u"])
        assert frame["v"] == pytest.approx(expected_face_frame["v"])
        assert frame["origin"] == pytest.approx(
            [
                expected_face_frame["origin"][i]
                + expected_face_frame["normal"][i] * 2.0
                for i in range(3)
            ]
        )


def test_offsetting_an_offset_plane_is_rejected():
    document = Document()
    once = offset_plane(base_plane("XY"), 5.0)
    with pytest.raises(ValueError):
        resolve_plane(document, offset_plane(once, 5.0))


def test_the_frame_is_kept_out_of_the_oplog():
    """ログには姿勢を載せない(貼る先の面の枠も落とす)。落ちたら、焼き付けた枠がログへ漏れている。"""
    document = Document()
    body_id = new_box(document, BOX)
    sketch_create(document, offset_plane(face_plane(body_id, TOP_FACE_INDEX), 5.0))

    logged = document.log.entries[-1]
    assert logged.verb == "sketch_create"
    assert "frame" not in logged.args["plane"]
    assert logged.args["plane"]["base"] == {
        "kind": "face", "body": body_id, "index": TOP_FACE_INDEX, "provenance": None
    }
    assert logged.args["plane"]["distance"] == 5.0


def test_a_log_round_trip_reproduces_the_same_frame():
    document = Document()
    body_id = new_box(document, BOX)
    sketch_id = sketch_create(
        document, offset_plane(face_plane(body_id, TOP_FACE_INDEX), 4.0)
    )
    add_circle(document, sketch_id, (0.0, 0.0), 4.0)
    expected = region.plane_frame(document, sketch_id)

    restored = replay_through_json(document)

    assert_same_frame(region.plane_frame(restored, sketch_id), expected)
    assert [item.area for item in region.regions(restored, sketch_id)] == pytest.approx(
        [math.pi * 4.0]
    )


def test_an_offset_from_an_offset_sketch_stacks_on_that_sketchs_own_frame():
    """落ちたら、オフセット平面のスケッチを基準にしたオフセットが、そのスケッチの平面から離れない。"""
    document = Document()
    body_id = new_box(document, BOX)
    first = sketch_create(document, offset_plane(face_plane(body_id, TOP_FACE_INDEX), 3.0))
    first_frame = region.plane_frame(document, first)

    second = sketch_create(document, offset_plane(sketch_plane(first), 4.0))

    frame = region.plane_frame(document, second)
    assert frame["origin"] == pytest.approx(
        [first_frame["origin"][i] + first_frame["normal"][i] * 4.0 for i in range(3)]
    )
    for key in ("u", "v", "normal"):
        assert frame[key] == pytest.approx(first_frame[key])
    assert_same_frame(region.plane_frame(replay_through_json(document), second), frame)
    assert document.log.entries[-1].args["plane"]["base"] == {"kind": "sketch", "sketch": first, "provenance": None}


def test_a_sketch_plane_does_not_follow_a_later_fillet_of_the_face_under_it():
    """落ちたら、面に貼ったスケッチを基準にした平面が、面の添字を辿り直して後の編集に動かされる。"""
    document = Document()
    body_id = new_box(document, BOX)
    on_face = sketch_create(document, face_plane(body_id, TOP_FACE_INDEX))
    from duocad.verbs import fillet

    fillet(document, all_edges(document, body_id), 3.0, body_id=body_id)
    expected = region.plane_frame(document, on_face)

    stacked = sketch_create(document, offset_plane(sketch_plane(on_face), 2.0))

    assert region.plane_frame(document, stacked)["origin"] == pytest.approx(
        [expected["origin"][i] + expected["normal"][i] * 2.0 for i in range(3)]
    )


def test_an_unknown_sketch_is_refused_as_a_plane():
    document = Document()
    with pytest.raises(KeyError):
        resolve_plane(document, offset_plane(sketch_plane("sketch-missing"), 1.0))


@pytest.mark.parametrize("pick", ["region", "entity"])
def test_a_selected_sketch_is_the_base_of_an_offset_sketch(session, pick):
    """落ちたら、スケッチの領域・図形を選んでオフセット平面を作っても、そのスケッチの平面を基準にしない。"""
    base = session.sketch_create_offset(distance=5.0, base_plane="XZ")["sketch_id"]
    entity = session.add_rectangle(base, [0.0, 0.0], 4.0, 4.0)["entity_id"]
    ref = {"sketch": base, "index": 1} if pick == "region" else {"sketch": base, "entity": entity}
    select(session, [ref])

    created = session.sketch_create_offset(distance=2.0)

    expected = session.sketch_create_offset(distance=7.0, base_plane="XZ")
    assert_same_frame(created["frame"], expected["frame"])


def test_items_of_one_sketch_count_as_one_plane(session):
    """落ちたら、同じスケッチの領域と線を両方選んだだけで「2つ選ばれている」と断られる。"""
    base = session.sketch_create("XY")["sketch_id"]
    entity = session.add_rectangle(base, [0.0, 0.0], 4.0, 4.0)["entity_id"]
    select(session, [{"sketch": base, "index": 1}, {"sketch": base, "entity": entity}])

    created = session.sketch_create_offset(distance=1.0)

    assert created["frame"]["origin"] == pytest.approx([0.0, 0.0, 1.0])


def test_a_sketch_and_a_face_together_are_ambiguous(session):
    """落ちたら、スケッチと面を両方選んだのに、どちらかを基準に黙って作られる。"""
    body, sketch_id = session_box(session)
    select(session, [{"sketch": sketch_id, "index": 1}, {"body": body.id, "kind": "face", "index": 1}])
    with pytest.raises(selection.SelectionAmbiguous):
        session.sketch_create_offset(distance=1.0)


def test_two_sketches_are_ambiguous(session):
    first = session.sketch_create("XY")["sketch_id"]
    session.add_rectangle(first, [0.0, 0.0], 4.0, 4.0)
    second = session.sketch_create("XZ")["sketch_id"]
    session.add_rectangle(second, [0.0, 0.0], 4.0, 4.0)
    select(session, [{"sketch": first, "index": 1}, {"sketch": second, "index": 1}])
    with pytest.raises(selection.SelectionAmbiguous):
        session.sketch_create_offset(distance=1.0)
