"""領域の中や縁に行き止まりの線があっても、領域を押し出せる(`kernel_sketch.sketch_regions`)。"""
from __future__ import annotations

import pytest

from duocad import query, verbs
from duocad.document import Document
from duocad.region import region_ref, regions
from duocad.sketch import add_circle, add_line, add_rectangle, sketch_create

SIDE = 10.0
HEIGHT = 5.0


def _square(document: Document) -> str:
    sketch_id = sketch_create(document, "XY")
    add_rectangle(document, sketch_id, (0.0, 0.0), SIDE, SIDE)
    return sketch_id


def _extruded_volume(document: Document, sketch_id: str, index: int, taper: float = 0.0) -> float:
    ref = region_ref(sketch_id, index)
    before = len(document.bodies)
    kwargs = {"taper": taper} if taper else {}
    verbs.extrude(document, [ref], HEIGHT, mode="new", **kwargs)
    assert len(document.bodies) == before + 1
    return query.volume(document.bodies[-1].shape)


@pytest.mark.parametrize(
    "dangling",
    [
        ((0.0, 5.0), (5.0, 5.0)),
        ((3.0, 5.0), (5.0, 5.0)),
    ],
    ids=["from-edge", "floating"],
)
def test_a_dangling_line_inside_a_region_does_not_stop_the_extrusion(dangling) -> None:
    """落ちたら、領域の中に行き止まりの線が1本あるだけで、領域は出るのに押し出しが BRepCheck で失敗する。"""
    document = Document()
    sketch_id = _square(document)
    add_line(document, sketch_id, *dangling)
    found = regions(document, sketch_id)
    assert [round(item.area, 6) for item in found] == [SIDE * SIDE]
    assert _extruded_volume(document, sketch_id, 1) == pytest.approx(SIDE * SIDE * HEIGHT)


def test_a_dangling_line_inside_a_hole_region_keeps_both_regions_extrudable() -> None:
    """落ちたら、穴の縁から生える線のある板で、環も穴の内側も押し出せない。"""
    document = Document()
    sketch_id = sketch_create(document, "XY")
    add_rectangle(document, sketch_id, (0.0, 0.0), 20.0, 20.0)
    add_circle(document, sketch_id, (10.0, 10.0), 12.0)
    add_line(document, sketch_id, (10.0, 10.0), (13.0, 10.0))
    add_line(document, sketch_id, (0.0, 3.0), (2.0, 3.0))
    found = regions(document, sketch_id)
    assert len(found) == 2
    ring, disc = found[0].area, found[1].area
    assert _extruded_volume(document, sketch_id, 1) == pytest.approx(ring * HEIGHT)
    assert _extruded_volume(document, sketch_id, 2) == pytest.approx(disc * HEIGHT)


def test_a_dangling_line_does_not_stop_a_tapered_extrusion() -> None:
    """落ちたら、行き止まりの線のある領域のテーパー付き押し出しが失敗する。"""
    document = Document()
    sketch_id = _square(document)
    add_line(document, sketch_id, (0.0, 5.0), (5.0, 5.0))
    assert _extruded_volume(document, sketch_id, 1, taper=3.0) > 0
