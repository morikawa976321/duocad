"""スケッチ領域の三角形化の最後の手(`kernel.tessellate_plain`)と、取れない領域の警告(`region.fill` / `view._warn_sketch_once`)。

円と、その円を約 1e-4 mm で近似したベジェ弧を重ねたスケッチは、境界検査が解消できない面(ほぼ重なった辺)を作る。
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from duocad import kernel, kernel_sketch, region
from duocad.server import Session

PLANE = {
    "kind": "XY",
    "frame": {"origin": [0.0, 0.0, 0.0], "u": [1.0, 0.0, 0.0], "v": [0.0, 1.0, 0.0], "normal": [0.0, 0.0, 1.0]},
}
RADIUS = 12.125
PHASE = 0.3


def circle_with_beziers(count: int) -> list:
    """半径 `RADIUS` の円と、同じ円を `count` 本の3次ベジェ弧で近似した輪郭。`count` が多いほど近似の誤差(円とのずれ)は小さい。"""
    drawn = [{"kind": "circle", "params": {"cx": 0.0, "cy": 0.0, "diameter": 2 * RADIUS}}]
    step = 2 * math.pi / count
    handle = 4.0 / 3.0 * math.tan(step / 4.0) * RADIUS
    for i in range(count):
        a0, a1 = PHASE + i * step, PHASE + (i + 1) * step
        p0 = (RADIUS * math.cos(a0), RADIUS * math.sin(a0))
        p1 = (RADIUS * math.cos(a1), RADIUS * math.sin(a1))
        drawn.append({
            "kind": "bezier",
            "params": {
                "x0": p0[0], "y0": p0[1],
                "cx1": p0[0] - handle * math.sin(a0), "cy1": p0[1] + handle * math.cos(a0),
                "cx2": p1[0] + handle * math.sin(a1), "cy2": p1[1] - handle * math.cos(a1),
                "x1": p1[0], "y1": p1[1],
            },
        })
    return drawn


def mesh_area(positions: list, indices: list) -> float:
    points = np.asarray(positions, dtype=float).reshape(-1, 3)
    corners = points[np.asarray(indices, dtype=np.int64).reshape(-1, 3)]
    return 0.5 * float(np.linalg.norm(np.cross(corners[:, 1] - corners[:, 0], corners[:, 2] - corners[:, 0]), axis=1).sum())


def test_the_synthetic_sketch_still_defeats_the_boundary_check():
    """落ちたら、OCCT の挙動が変わって下の再現が再現でなくなった。合成のスケッチ(`circle_with_beziers`)を作り直す。"""
    detection = region.detect(PLANE, circle_with_beziers(6))
    (item,) = detection._items
    with pytest.raises(ValueError):
        kernel.tessellate(kernel_sketch.copy_face(item["face"]), 0.2)


@pytest.mark.parametrize("deviation", [2.0, 0.2, 0.01, 0.0005])
def test_a_region_the_boundary_check_drops_is_filled_by_the_plain_mesh(deviation):
    """落ちたら、境界検査が落とした領域が三角形のない領域として残り、警告が出る(画面は領域を塗らない)。"""
    detection = region.detect(PLANE, circle_with_beziers(6))
    errors: list = []

    filled = region.fill(detection, deviation, on_error=lambda index, error: errors.append(index))

    assert errors == []
    (item,) = filled
    assert item["indices"]
    assert mesh_area(item["positions"], item["indices"]) == pytest.approx(item["area"], rel=kernel.PLAIN_AREA_TOLERANCE)


def test_slivers_the_boundary_check_drops_stay_empty_and_are_not_reported():
    """落ちたら、画面に見えない薄い領域の失敗が警告に積まれる、または領域の添字が詰まって押し出しの参照がずれる。"""
    detection = region.detect(PLANE, circle_with_beziers(8))
    errors: list = []

    filled = region.fill(detection, 0.2, on_error=lambda index, error: errors.append(index))

    assert [item["index"] for item in filled] == list(range(1, len(detection._items) + 1))
    empty = [item for item in filled if not item["indices"]]
    assert empty, "薄い領域が三角形になった。合成のスケッチが再現でなくなった"
    assert all(item["area"] < region.UNREPORTED_AREA for item in empty)
    assert errors == []
    assert filled[0]["indices"] and filled[0]["area"] > region.UNREPORTED_AREA


def test_the_plain_mesh_is_refused_unless_its_area_matches_the_face():
    """落ちたら、素の分割が返した誤った三角形(面積が違う)を領域の塗りに採ってしまう。"""
    detection = region.detect(PLANE, circle_with_beziers(8))
    face = detection._items[0]["face"]
    area = detection._items[0]["area"]

    assert kernel.tessellate_plain(kernel_sketch.copy_face(face), 0.2, area) is not None
    assert kernel.tessellate_plain(kernel_sketch.copy_face(face), 0.2, area * 1.5) is None
    assert kernel.tessellate_plain(kernel_sketch.copy_face(face), 0.2, area * 0.5) is None


def test_a_failed_region_is_reported_once_across_resolutions_until_it_recovers(monkeypatch):
    """落ちたら、ズームの段が変わるたびに同じ領域の警告が積まれ直す、または直った後に落ちても知らされない。"""
    session = Session(0.2)
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_circle(sketch_id, [0.0, 0.0], 40.0)
    real = kernel.tessellate

    def broken(face, deviation, *args):
        raise ValueError(f"三角形分割が1面も取れなかった(deviation={deviation})")

    def reported() -> int:
        return sum("領域 1 を三角形にできない" in warning for warning in session.warnings)

    def listed() -> list:
        return next(item for item in session.state_meta()["sketches"] if item["sketch_id"] == sketch_id)["regions"]

    monkeypatch.setattr(kernel, "tessellate", broken)
    monkeypatch.setattr(kernel, "tessellate_plain", lambda *args, **kwargs: None)
    for world_per_pixel in (0.1, 0.002, 0.01):
        session.set_view_resolution(world_per_pixel, 100.0)
        assert not listed()[0]["indices"]
    assert reported() == 1

    monkeypatch.setattr(kernel, "tessellate", real)
    session.set_view_resolution(0.05, 100.0)
    assert listed()[0]["indices"]
    assert reported() == 1

    monkeypatch.setattr(kernel, "tessellate", broken)
    session.set_view_resolution(0.03, 100.0)
    assert not listed()[0]["indices"]
    assert reported() == 2
