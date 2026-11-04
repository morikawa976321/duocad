"""スケッチの塗り(領域の三角形)を粗さごとに覚える(`view._SketchFills`)と、検出と塗りの分離(`region.detect` / `region.fill`)。"""
from __future__ import annotations

import pytest

from duocad import kernel, kernel_sketch, region
from duocad.server import Session


@pytest.fixture
def counted(monkeypatch):
    """`kernel.tessellate`(ボディが無いので全部が領域の塗り)と `kernel_sketch.sketch_regions`(検出)を呼んだ回数。"""
    calls = {"tessellate": [], "detect": 0}
    tessellate = kernel.tessellate
    detect = kernel_sketch.sketch_regions

    def counting_tessellate(shape, *args, **kwargs):
        calls["tessellate"].append(shape)
        return tessellate(shape, *args, **kwargs)

    def counting_detect(*args, **kwargs):
        calls["detect"] += 1
        return detect(*args, **kwargs)

    monkeypatch.setattr(kernel, "tessellate", counting_tessellate)
    monkeypatch.setattr(kernel_sketch, "sketch_regions", counting_detect)
    monkeypatch.setattr(region, "_DETECTED", type(region._DETECTED)())
    return calls


def _plate(session: Session) -> str:
    """穴 2 つの板(領域 3)。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_rectangle(sketch_id, [-60.0, -30.0], 120.0, 60.0)
    session.add_circle(sketch_id, [-30.0, 0.0], 40.0)
    session.add_circle(sketch_id, [30.0, 0.0], 40.0)
    return sketch_id


def test_the_fill_never_meshes_the_shared_detected_faces(counted) -> None:
    """落ちたら、塗りが検出の表と共有の面に三角形を書き込み、別のスレッドの押し出しが写す面と取り合う
    (`kernel_sketch.copy_face` の docstring)。"""
    session = Session(0.2)
    sketch_id = _plate(session)
    detection = region.detect(session.document.sketches[-1].plane, region.expanded_entities(session.document, sketch_id))
    region.fill(detection, 0.05)
    shared = [item["face"] for item in next(iter(region._DETECTED.values()))]
    assert len(counted["tessellate"]) == len(shared) == 3
    assert not any(meshed.IsSame(face) for meshed in counted["tessellate"] for face in shared)
