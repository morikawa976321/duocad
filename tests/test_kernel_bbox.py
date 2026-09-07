"""`kernel.bounding_box` が返す寸法を、三角形分割の bbox を独立の基準として突き合わせる。

落ちたとき: bbox の取り方(`BRepBndLib` の `Add` / `AddOptimal`)か、三角形分割の粗さが
変わっている。
"""

from __future__ import annotations

import math

import pytest
from build123d import Box, Cone, Cylinder
from OCP.BRepPrimAPI import BRepPrimAPI_MakeTorus

from duocad import kernel

DEVIATION = kernel.DEFAULT_DEVIATION


def _torus_segment():
    """`BRepPrimAPI_MakeTorus` の第3引数は掃引角。

    箱・円柱・平面では `BRepBndLib` の `Add` と `AddOptimal` が一致するので、bbox の取り方を
    見るにはトリムされた曲面の題材が要る。
    """
    return BRepPrimAPI_MakeTorus(100.0, 10.0, math.pi / 6).Shape()


SHAPES = {
    "box": lambda: Box(40, 20, 10).wrapped,
    "cylinder": lambda: Cylinder(10, 30).wrapped,
    "cone": lambda: Cone(10, 0, 20).wrapped,
    "torus_segment": _torus_segment,
}

UNTRIMMED = ["box", "cylinder", "cone"]


def _triangulation_box(shape, deviation: float) -> tuple[list[float], list[float]]:
    """三角形の節点から作った [min, max]。節点は必ず真の面の上にあるので真の寸法を超えない。

    `payload["bbox"]` を使わないのは意図的で、そちらの作り方が変わった日にこの検査が
    黙って循環参照になるため。
    """
    payload = kernel.tessellate(shape, deviation=deviation)
    lows = [math.inf] * 3
    highs = [-math.inf] * 3
    for face in payload["faces"]:
        positions = face["positions"]
        for offset in range(0, len(positions), 3):
            for axis in range(3):
                value = positions[offset + axis]
                lows[axis] = min(lows[axis], value)
                highs[axis] = max(highs[axis], value)
    return lows, highs


def _outward_mm(shape, deviation: float = DEVIATION) -> list[float]:
    """`bounding_box` の6面が三角形分割の箱より**どれだけ外側にあるか**[mm]。負なら内側。"""
    box = kernel.bounding_box(shape)
    lows, highs = _triangulation_box(shape, deviation)
    return [lows[axis] - box["min"][axis] for axis in range(3)] + [
        box["max"][axis] - highs[axis] for axis in range(3)
    ]


@pytest.mark.parametrize("name", list(SHAPES))
def test_the_bounding_box_contains_every_triangle_vertex(name: str) -> None:
    """落ちたら: bbox が形状の一部を切り落としている(カメラ合わせも格子も外側を失う)。"""
    outward = _outward_mm(SHAPES[name]())
    assert min(outward) >= -1e-9, f"{name}: 最も内側の面が {min(outward):+.6f}mm"


@pytest.mark.parametrize("name", UNTRIMMED)
def test_the_bounding_box_is_as_tight_as_the_triangulation_without_trimmed_curved_faces(
    name: str,
) -> None:
    """落ちたら: トリムされた曲面が無い形でも bbox が膨らんでいる(実測の膨らみは0.013mm)。"""
    outward = _outward_mm(SHAPES[name]())
    assert max(outward) <= DEVIATION, f"{name}: 最大の膨らみ {max(outward):.6f}mm"


@pytest.mark.xfail(
    strict=True,
    reason=(
        "`bounding_box` は `BRepBndLib::Add` を使っており、面のトリムを見ずに下地の"
        "サーフェスで囲む。トーラス片の bbox は実寸より 9.06mm 大きい(2026-09-07実測)。"
        "`AddOptimal_s` へ替えるとこの検査は成立し、strict=True によって XPASS で落ちる —— "
        "そのときは期待値ではなくこの marker のほうを外すこと。"
    ),
)
def test_the_bounding_box_is_as_tight_as_the_triangulation_for_a_trimmed_torus_segment() -> None:
    """落ちたら(XPASS): bbox がトリムを見るようになった。上の marker を外して固定すること。"""
    outward = _outward_mm(_torus_segment())
    assert max(outward) <= DEVIATION, f"最大の膨らみ {max(outward):.6f}mm"
