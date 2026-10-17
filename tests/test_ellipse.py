"""楕円の契約(`add_ellipse`)。設計は docs/ELLIPSE.md。

`params` は `cx` / `cy` / `width` / `height` / `angle`。`width` は `angle` の向きの直径、`height` はそれに直交する向きの直径で、
どちらが長くてもよい(`gp_Elips` は長半径を先に要求するので、カーネルが入れ替えて渡す)。
"""
from __future__ import annotations

import math

import pytest

from duocad import kernel, kernel_sketch
from duocad.document import Document
from duocad.region import region_ref, regions
from duocad.sketch import (
    add_arc,
    add_ellipse,
    add_line,
    face_plane,
    entity_kind_ops,
    entity_params,
    sketch_create,
)
from duocad.verbs import extrude
from document_support import new_box, replay_through_json


def _ellipse(width: float, height: float, angle: float = 0.0):
    doc = Document()
    sketch_id = sketch_create(doc, "XY")
    entity_id = add_ellipse(doc, sketch_id, (3.0, -2.0), width, height, angle)
    return doc, sketch_id, entity_id


@pytest.mark.parametrize("width,height", [(20.0, 8.0), (8.0, 20.0), (12.0, 12.0)])
def test_an_ellipse_region_has_the_ellipse_area_whichever_side_is_longer(width, height):
    """落ちたら、縦長・真円の楕円で辺が作れないか、別の大きさの面になっている(`gp_Elips` の長短の罠)。"""
    doc, sketch_id, _ = _ellipse(width, height, 30.0)
    found = regions(doc, sketch_id)
    assert len(found) == 1
    assert found[0].area == pytest.approx(math.pi * width * height / 4.0, rel=1e-9)


def test_the_angle_turns_the_width_axis_counterclockwise():
    """落ちたら、`angle` の向きか、縦長のときの 90° の足し方が画面(`entityKinds.ts`)と食い違う。"""
    for width, height in ((20.0, 8.0), (8.0, 20.0)):
        doc, sketch_id, _ = _ellipse(width, height, 30.0)
        # angle の向きに、2つの半径のあいだの長さだけ中心から両側へ線を引く。その向きの直径が width なら、
        # width が長いときは線が楕円の内側で止まって割らず、短いときは突き抜けて2つに割る。
        direction = (math.cos(math.radians(30.0)), math.sin(math.radians(30.0)))
        reach = (width + height) / 4.0
        add_line(
            doc,
            sketch_id,
            (3.0 - reach * direction[0], -2.0 - reach * direction[1]),
            (3.0 + reach * direction[0], -2.0 + reach * direction[1]),
        )
        expected = 1 if width > height else 2
        assert len(regions(doc, sketch_id)) == expected, f"{width}x{height}: width 側の軸が angle の向きに無い"
    # 縦の直径 8・横の直径 20 を 90° 立てると、外接箱は u に ±4、v に ±10。
    box = kernel_sketch._bbox_ellipse({"cx": 0.0, "cy": 0.0, "width": 20.0, "height": 8.0, "angle": 90.0})
    assert box == pytest.approx((-4.0, 4.0, -10.0, 10.0))


def test_extruding_an_ellipse_gives_area_times_distance():
    doc, sketch_id, _ = _ellipse(20.0, 8.0, 15.0)
    extrude(doc, [region_ref(sketch_id, 1)], 5.0, mode="new")
    assert kernel.volume(doc.bodies[0].shape) == pytest.approx(math.pi * 10.0 * 4.0 * 5.0, rel=1e-6)


def test_the_perimeter_matches_the_integral():
    """落ちたら、`Length_s` の既定の粗い許容差に戻っている(直径 20×10 で +0.13%)。"""
    perimeter = kernel_sketch.ellipse_perimeter({"cx": 0.0, "cy": 0.0, "width": 20.0, "height": 10.0, "angle": 40.0})
    steps = 200_000
    integral = sum(
        math.hypot(10.0 * math.sin(2 * math.pi * (i + 0.5) / steps), 5.0 * math.cos(2 * math.pi * (i + 0.5) / steps))
        for i in range(steps)
    ) * 2 * math.pi / steps
    assert perimeter == pytest.approx(integral, rel=1e-9)


def test_rotating_and_mirroring_turn_the_axis():
    """落ちたら、回した・映した楕円の向きが元のまま残る(`rotate_copies` / 鏡映の `2φ - angle`)。"""
    ops = entity_kind_ops("ellipse")
    params = {"cx": 10.0, "cy": 0.0, "width": 6.0, "height": 2.0, "angle": 20.0}
    turned = ops.rotate(params, 0.0, 0.0, 90.0, True)
    assert (turned["cx"], turned["cy"], turned["angle"]) == pytest.approx((0.0, 10.0, 110.0))
    kept = ops.rotate(params, 0.0, 0.0, 90.0, False)
    assert kept["angle"] == 20.0
    # Y軸(x=0)で映すと向きは 180 - 20。
    mirrored = ops.mirror(params, 0.0, 0.0, 0.0, 1.0)
    assert (mirrored["cx"], mirrored["cy"], mirrored["angle"]) == pytest.approx((-10.0, 0.0, 160.0))
    assert (mirrored["width"], mirrored["height"]) == (6.0, 2.0)


@pytest.mark.parametrize("width,height", [(0.0, 5.0), (5.0, -1.0)])
def test_a_flat_ellipse_is_refused_and_leaves_nothing(width, height):
    doc = Document()
    sketch_id = sketch_create(doc, "XY")
    with pytest.raises(ValueError):
        add_ellipse(doc, sketch_id, (0.0, 0.0), width, height)
    assert [entry.verb for entry in doc.log.entries] == ["sketch_create"]


def test_an_ellipse_survives_save_and_replay():
    doc, sketch_id, entity_id = _ellipse(9.0, 4.0, 33.0)
    restored = replay_through_json(doc)
    assert entity_params(restored, sketch_id, entity_id) == entity_params(doc, sketch_id, entity_id)


@pytest.mark.parametrize("face_index", range(1, 7))
def test_ellipses_and_arcs_keep_their_turn_on_every_face_of_a_box(face_index):
    """落ちたら、面に貼ったスケッチ(左手系の枠がある)で楕円の向き・円弧の回り方が画面と逆になる
    (`kernel_sketch._curve_axis`。立方体の6面のうち5面が左手系)。"""
    doc = Document()
    body_id = new_box(doc, 40.0)
    plane = face_plane(body_id, face_index)

    ellipse_sketch = sketch_create(doc, plane)
    add_ellipse(doc, ellipse_sketch, (0.0, 0.0), 20.0, 8.0, 30.0)
    direction = (math.cos(math.radians(30.0)), math.sin(math.radians(30.0)))
    add_line(doc, ellipse_sketch, (-7.0 * direction[0], -7.0 * direction[1]), (7.0 * direction[0], 7.0 * direction[1]))
    assert len(regions(doc, ellipse_sketch)) == 1, "横の直径の軸が angle の向きに無い(線が楕円を突き抜けた)"

    arc_sketch = sketch_create(doc, plane)
    add_arc(doc, arc_sketch, (0.0, 0.0), 5.0, 0.0, 90.0)
    add_line(doc, arc_sketch, (5.0, 0.0), (0.0, 0.0))
    add_line(doc, arc_sketch, (0.0, 0.0), (0.0, 5.0))
    found = regions(doc, arc_sketch)
    assert [round(region.area, 6) for region in found] == [round(math.pi * 25.0 / 4.0, 6)], "四分円が閉じない(円弧が逆回り)"
