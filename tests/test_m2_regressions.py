"""M2の回帰テスト — **契約のテストが通っていても壊れていた経路**。"""
from __future__ import annotations

import math
from pathlib import Path

import pytest

from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

from duocad import kernel, query
from duocad.document import Body, Document
from document_support import assert_finite_json, replay_through_json
from duocad.region import region_ref, regions
from duocad.sketch import add_circle, add_rectangle, face_plane, sketch_create
from duocad.verbs import extrude
from duocad.verbs.import_editable import import_step

BOX_EDGE = 20.0
BOX_VOLUME = BOX_EDGE**3


def box_document() -> Document:
    """20mm立方体のソリッドを1つ持つ文書。"""
    document = Document()
    document.add_body(
        Body(
            id="box",
            name="box",
            kind="solid",
            shape=BRepPrimAPI_MakeBox(BOX_EDGE, BOX_EDGE, BOX_EDGE).Shape(),
        )
    )
    return document


def face_index_by_normal_z(shape, sign: float) -> int:
    """法線のZ成分が `sign` 向きの面の添字(上面 / 底面を選ぶ)。"""
    for face in kernel.tessellate(shape)["faces"]:
        if face["normal"][2] * sign > 0.999:
            return face["index"]
    raise AssertionError("該当する面が無い")


@pytest.mark.parametrize("sign", [1.0, -1.0], ids=["top_face", "bottom_face"])
def test_sketch_on_a_face_can_be_extruded_and_replayed(sign):
    """面に貼ったスケッチが、領域検出 → 押し出し → 再生まで通る。"""
    document = box_document()
    index = face_index_by_normal_z(document.active_body().shape, sign)

    sketch_id = sketch_create(document, face_plane("box", index))
    add_rectangle(document, sketch_id, (0.0, 0.0), 5.0, 5.0)
    found = regions(document, sketch_id)

    assert len(found) == 1
    assert math.isclose(found[0].area, 25.0, rel_tol=1e-9)

    extrude(document, [region_ref(sketch_id, found[0].index)], 4.0, mode="join")

    volume = query.volume(document.active_body().shape)
    assert math.isclose(volume, BOX_VOLUME + 100.0, rel_tol=1e-9)

    # 再生: 立方体はログの外で作った参照物なので、同じ土台を持つ文書へ再生する
    replayed = replay_through_json(document, into=box_document())
    assert math.isclose(query.volume(replayed.active_body().shape), volume, rel_tol=1e-9)


def test_extrude_rejects_regions_from_two_different_sketches():
    """別平面のスケッチの領域を混ぜたら ValueError(体積0の退化ソリッドを黙って作らない)。

    実測: XY平面とYZ平面の10×10を同時に押し出すと、例外なしで体積500
    (期待1000)の形状ができ、退化した面が2枚残っていた。
    """
    document = Document()
    first = sketch_create(document, "XY")
    add_rectangle(document, first, (0.0, 0.0), 10.0, 10.0)
    second = sketch_create(document, "YZ")
    add_rectangle(document, second, (0.0, 0.0), 10.0, 10.0)
    refs = [
        region_ref(first, regions(document, first)[0].index),
        region_ref(second, regions(document, second)[0].index),
    ]
    entries_before = len(document.log.entries)

    with pytest.raises(ValueError):
        extrude(document, refs, 5.0, mode="join")

    assert document.bodies == []
    assert len(document.log.entries) == entries_before


@pytest.mark.parametrize(
    "add",
    [
        lambda doc, sk: add_circle(doc, sk, (0.0, 0.0), 2 * (float("nan"))),
        lambda doc, sk: add_circle(doc, sk, (0.0, 0.0), 0.0),
        lambda doc, sk: add_circle(doc, sk, (0.0, 0.0), -10.0),
        lambda doc, sk: add_rectangle(doc, sk, (0.0, 0.0), 0.0, 5.0),
        lambda doc, sk: add_rectangle(doc, sk, (float("inf"), 0.0), 5.0, 5.0),
    ],
    ids=["nan_radius", "zero_radius", "negative_radius", "zero_width", "inf_corner"],
)
def test_broken_numbers_never_enter_the_sketch(add):
    """使えない数値はその場で ValueError。スケッチにもログにも残さない(§6予防6)。

    残すと後戻りできない: M2に図形の削除verbは無いので、壊れた図形が入ったスケッチは
    undo以外に直す手段が無い。NaNの場合はさらに悪く、**同じスケッチ上の正常な領域まで
    黙って消え**(バウンディングボックスがNaNになる)、`to_json()` が裸のNaNを吐いて
    JSONとして壊れる(実測)。
    """
    document = Document()
    sketch_id = sketch_create(document, "XY")
    add_rectangle(document, sketch_id, (0.0, 0.0), 40.0, 30.0)
    entries_before = len(document.log.entries)

    with pytest.raises(ValueError):
        add(document, sketch_id)

    assert len(document.log.entries) == entries_before
    assert len(regions(document, sketch_id)) == 1, "正常な領域が巻き添えで消えている"
    assert_finite_json(document.log.to_json())


def _single_solid_step(tmp_path) -> Path:
    """20×10×5(体積1000)のソリッド1個だけのSTEPを作って返す。"""
    step_path = tmp_path / "reference.step"
    kernel.write_step(BRepPrimAPI_MakeBox(20.0, 10.0, 5.0).Shape(), str(step_path))
    return step_path


def test_join_right_after_importing_a_step_does_not_fail(tmp_path):
    """STEPを読んだ直後のアクティブが編集可能で、そこへ `join` が通る。落ちたら最初の板が作れない。"""
    document = Document()
    body_ids = import_step(document, _single_solid_step(tmp_path))

    active = document.active_body()
    assert active is not None and active.id == body_ids[-1]
    assert document.is_editable(active.id)

    sketch_id = sketch_create(document, "XY")
    add_rectangle(document, sketch_id, (0.0, 0.0), 20.0, 10.0)
    found = regions(document, sketch_id)
    assert len(found) == 1

    extrude(document, [region_ref(sketch_id, found[0].index)], 10.0, mode="join")

    assert math.isclose(
        query.volume(document.get_body(body_ids[0]).shape), 2000.0, rel_tol=1e-9
    )
