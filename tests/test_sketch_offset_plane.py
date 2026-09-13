"""オフセット平面へのスケッチ作成(issue #3)。

`sketch_create_offset` は `sketch_create` の兄弟verbで、貼る先(基準平面 or 面)から法線
方向へ `distance` だけ離れた平面にスケッチを貼る。実体は `sketch.offset_plane` が組み立てる
PlaneRef(`{"kind":"offset","base":..,"distance":..,"provenance":None}`)を
`sketch.resolve_plane` が姿勢まで解決するだけで、`duocad/kernel.py` の `_ax3_from_plane` が
「`frame` を持つ参照はkindを問わずそれを使う」よう一般化してあるので、面参照(`face_plane`)と
まったく同じ「作成時に1回だけ焼き付けて以後動かさない」規律に自動で乗る
(`tests/test_sketch_plane_freeze.py` と同じ不変条件)。

**verb名を分けている理由**は `sketch.sketch_create` のdocstring参照 ——
`base_plane`(貼る先)に加えて `distance`(how)を持つので、同じverb名に足すと
`sketch_create` 自体の即適用(Enter不要)まで変わってしまう。
"""
from __future__ import annotations

import math

import pytest

from duocad import kernel, region
from duocad.document import Document
from duocad.oplog import OpLog
from duocad.region import region_ref
from duocad.sketch import (
    add_circle,
    add_rectangle,
    base_plane,
    face_plane,
    get_sketch,
    offset_plane,
    resolve_plane,
    sketch_create,
)
from duocad.verbs import extrude

BOX = 20.0
#: 直方体の上面(z=BOX・法線+Z)の面添字。`new_box` の直後は必ずこれになる(実測)。
TOP_FACE_INDEX = 6


def new_box(doc: Document) -> str:
    """スケッチ(長方形)→押し出しで20mm立方体を1つ作り body_id を返す(duocadのverbだけ)。"""
    sketch_id = sketch_create(doc, base_plane("XY"))
    add_rectangle(doc, sketch_id, (0.0, 0.0), BOX, BOX)
    return extrude(doc, [region_ref(sketch_id, 1)], BOX, mode="join").args["body_id"]


def assert_same_frame(actual: dict, expected: dict) -> None:
    for key in ("origin", "u", "v", "normal"):
        for got, want in zip(actual[key], expected[key]):
            assert math.isclose(got, want, rel_tol=0.0, abs_tol=1.0e-9), (
                f"{key}: {actual[key]} != {expected[key]}"
            )


# --------------------------------------------------------------------------------------
# 1. 姿勢の計算 — 法線方向への平行移動だけで、u/v/normalは不変
# --------------------------------------------------------------------------------------
def test_an_offset_from_a_base_plane_translates_the_origin_along_the_normal():
    document = Document()
    sketch_id = sketch_create(document, offset_plane(base_plane("XY"), 5.0))

    frame = region.plane_frame(document, sketch_id)
    xy_frame = kernel.sketch_plane_frame(base_plane("XY"))
    assert frame["origin"] == pytest.approx([0.0, 0.0, 5.0])
    assert frame["u"] == pytest.approx(xy_frame["u"])
    assert frame["v"] == pytest.approx(xy_frame["v"])
    assert frame["normal"] == pytest.approx(xy_frame["normal"])


def test_a_negative_distance_offsets_to_the_opposite_side():
    document = Document()
    sketch_id = sketch_create(document, offset_plane(base_plane("XY"), -5.0))
    assert region.plane_frame(document, sketch_id)["origin"] == pytest.approx(
        [0.0, 0.0, -5.0]
    )


def test_a_zero_distance_coincides_with_the_base_plane():
    """0は「基準平面と同じ位置になる」だけで、禁止する理由が無い(UI側の `reject` と同じ判断)。"""
    document = Document()
    sketch_id = sketch_create(document, offset_plane(base_plane("XY"), 0.0))
    assert region.plane_frame(document, sketch_id) == kernel.sketch_plane_frame(
        base_plane("XY")
    )


def test_an_offset_from_a_face_stacks_on_top_of_the_frozen_face_frame():
    """面からのオフセットは、まず面参照そのものを凍結してから平行移動する。"""
    document = Document()
    body_id = new_box(document)
    face_frame = kernel.face_frame(document.get_body(body_id).shape, TOP_FACE_INDEX)

    sketch_id = sketch_create(
        document, offset_plane(face_plane(body_id, TOP_FACE_INDEX), 3.0)
    )

    frame = region.plane_frame(document, sketch_id)
    assert frame["origin"] == pytest.approx(
        [face_frame["origin"][i] + face_frame["normal"][i] * 3.0 for i in range(3)]
    )
    assert frame["normal"] == pytest.approx(face_frame["normal"])


def test_regions_are_computable_on_an_offset_plane():
    """姿勢だけでなく領域計算そのものが offset 平面でも動く(u,v は基準側と同じ規則)。"""
    document = Document()
    sketch_id = sketch_create(document, offset_plane(base_plane("XY"), 10.0))
    add_circle(document, sketch_id, (0.0, 0.0), 4.0)
    assert [item.area for item in region.regions(document, sketch_id)] == pytest.approx(
        [math.pi * 16.0]
    )


# --------------------------------------------------------------------------------------
# 2. 入れ子(offsetのoffset)は作れない
# --------------------------------------------------------------------------------------
def test_offsetting_an_offset_plane_is_rejected():
    document = Document()
    once = offset_plane(base_plane("XY"), 5.0)
    with pytest.raises(ValueError):
        resolve_plane(document, offset_plane(once, 5.0))


# --------------------------------------------------------------------------------------
# 3. ログ — 姿勢は載せず、素の参照(base/distance)だけを積む
# --------------------------------------------------------------------------------------
def test_the_frame_is_kept_out_of_the_oplog():
    document = Document()
    sketch_create(document, offset_plane(base_plane("XY"), 5.0))

    logged = document.log.entries[-1]
    assert logged.verb == "sketch_create"  # base_plane を渡したときのverb名は変わらない
    assert set(logged.args["plane"]) == {"kind", "base", "distance", "provenance"}
    assert logged.args["plane"]["base"] == {"kind": "base", "name": "XY", "provenance": None}
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
    body_id = new_box(document)
    sketch_id = sketch_create(
        document, offset_plane(face_plane(body_id, TOP_FACE_INDEX), 4.0)
    )
    add_circle(document, sketch_id, (0.0, 0.0), 2.0)
    expected = region.plane_frame(document, sketch_id)

    restored = Document()
    restored.log = OpLog.from_json(document.log.to_json())
    restored.log.replay(restored)

    assert_same_frame(region.plane_frame(restored, sketch_id), expected)
    assert [item.area for item in region.regions(restored, sketch_id)] == pytest.approx(
        [math.pi * 4.0]
    )


def test_undo_and_redo_of_the_sketch_create_itself_reproduce_the_same_frame():
    """undo(=sketch_createの取り消し)→redoで、同じスケッチIDが同じ姿勢で戻る。

    redoは `_do_sketch_create` を再び走らせる、つまり `resolve_plane` の offset 分岐を
    もう一度通る —— そこが壊れていれば距離が二重にずれるか例外になる。
    """
    document = Document()
    sketch_id = sketch_create(document, offset_plane(base_plane("XY"), 5.0))
    expected = region.plane_frame(document, sketch_id)

    document.log.undo(document)
    assert not any(s.id == sketch_id for s in document.sketches)

    document.log.redo(document)
    assert get_sketch(document, sketch_id) is not None
    assert_same_frame(region.plane_frame(document, sketch_id), expected)
