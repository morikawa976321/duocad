"""オフセット平面へのスケッチ作成(issue #3)。"""
from __future__ import annotations

import math

import pytest

from duocad import kernel, kernel_sketch, query, region
from duocad.document import Document
from duocad.sketch import (
    BASE_PLANES,
    add_circle,
    base_plane,
    face_plane,
    offset_plane,
    resolve_plane,
    sketch_create,
)
from document_support import all_edges, assert_same_frame, new_box, replay_through_json

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


def test_an_offset_from_a_face_stacks_on_top_of_the_frozen_face_frame():
    """面からのオフセットは、まず面参照そのものを凍結してから平行移動する。"""
    document = Document()
    body_id = new_box(document, BOX)
    face_frame = kernel_sketch.face_frame(document.get_body(body_id).shape, TOP_FACE_INDEX)

    sketch_id = sketch_create(
        document, offset_plane(face_plane(body_id, TOP_FACE_INDEX), 3.0)
    )

    frame = region.plane_frame(document, sketch_id)
    assert frame["origin"] == pytest.approx(
        [face_frame["origin"][i] + face_frame["normal"][i] * 3.0 for i in range(3)]
    )
    assert frame["normal"] == pytest.approx(face_frame["normal"])


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
    assert kernel.classify_face(kernel.face_of(shape, TOP_FACE_INDEX)) != "plane"

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


def test_sketch_create_offset_logs_its_own_verb_name():
    """`verb=` を渡すと、ログへ積む名前だけが変わる(do/undoの実体は共有)。"""
    from duocad.sketch import VERB_SKETCH_CREATE_OFFSET

    document = Document()
    sketch_create(
        document, offset_plane(base_plane("XY"), 5.0), verb=VERB_SKETCH_CREATE_OFFSET
    )
    assert document.log.entries[-1].verb == "sketch_create_offset"


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
