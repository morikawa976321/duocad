"""ねじの体積の門が測る増減(`kernel_thread._volume_change`。片方にしか無い面だけを積分する)。

落ちたら: 体積の門の増減が、前後の形を丸ごと積分した差と合わない(壊れたねじを通す・正しいねじを断る)か、
OCCT の Eps 付きの体積の積分の基準点・面ごとの計算が変わって、面ごとに足した値が丸ごとの値と別物になった。
"""
from __future__ import annotations

import math

import pytest
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

from duocad import brep_geometry as _brep_geometry, brep_operations as _brep_operations, kernel_thread
from duocad.document import Document
from duocad.verbs import thread
from test_chamfer import new_cylinder
from test_thread import _cylinder_face, _hole


def _whole(before, after) -> float:
    return _brep_geometry._thread_volume_eps(after) - _brep_geometry._thread_volume_eps(before)


@pytest.mark.parametrize("make", ["hole", "shaft"])
def test_the_gate_measures_the_change_that_integrating_both_shapes_gives(monkeypatch, make):
    """雌(入口の面取り → 埋める → 引く)・雄(切る → 足す)のどちらでも、門の増減は前後を丸ごと積分した差と同じ。"""
    doc = Document()
    if make == "hole":
        body_id, face = _hole(doc)
    else:
        body_id = new_cylinder(doc, radius=3.0, height=10.0)
        face = _cylinder_face(doc, body_id)
    seen: list = []
    real = kernel_thread._volume_change
    monkeypatch.setattr(kernel_thread, "_volume_change", lambda before, after: seen.append((before, after)) or real(before, after))

    thread(doc, face, "trapezoid30", body_id=body_id)

    (before, after), = seen
    assert real(before, after) == pytest.approx(_whole(before, after), abs=1e-6)
    centre = kernel_thread._vertex_mean(after)
    whole = 0.0
    for face in _brep_geometry._faces_of(after):
        whole += kernel_thread._face_volume(face, centre)
    assert whole == _brep_geometry._thread_volume_eps(after)


def test_a_boolean_that_keeps_most_faces_and_one_that_keeps_none():
    """箱から円柱を引いた差(残った面は打ち消す)と、共有する面の無い2つの形の差。同じ形どうしは 0。"""
    box = BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 20, 20, 20).Shape()
    pin = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(10, 10, -1), gp_Dir(0, 0, 1)), 3.0, 22.0).Shape()
    holed = _brep_operations.build_boolean("cut", box, pin).Shape()

    assert kernel_thread._volume_change(box, holed) == pytest.approx(-math.pi * 9.0 * 20.0, rel=1e-9)
    assert kernel_thread._volume_change(box, holed) == pytest.approx(_whole(box, holed), abs=1e-6)
    assert kernel_thread._volume_change(pin, holed) == pytest.approx(_whole(pin, holed), abs=1e-6)
    assert kernel_thread._volume_change(holed, holed) == 0.0
