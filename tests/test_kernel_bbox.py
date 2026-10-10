"""`kernel.bounding_box` が返す寸法を、三角形分割の bbox を独立の基準として突き合わせる。"""

from __future__ import annotations

import ast
import math
import pathlib

import pytest
from build123d import Box, Cone, Cylinder
from OCP.BRepPrimAPI import BRepPrimAPI_MakeTorus

from duocad import kernel

DEVIATION = kernel.DEFAULT_DEVIATION


def _torus_segment():
    """`BRepPrimAPI_MakeTorus` の第3引数は掃引角。"""
    return BRepPrimAPI_MakeTorus(100.0, 10.0, math.pi / 6).Shape()


SHAPES = {
    "box": lambda: Box(40, 20, 10).wrapped,
    "cylinder": lambda: Cylinder(10, 30).wrapped,
    "cone": lambda: Cone(10, 0, 20).wrapped,
    "torus_segment": _torus_segment,
}

UNTRIMMED = ["box", "cylinder", "cone"]


def _triangulation_box(shape, deviation: float) -> tuple[list[float], list[float]]:
    """三角形の節点から作った [min, max]。節点は必ず真の面の上にあるので真の寸法を超えない。"""
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


def test_the_bounding_box_is_as_tight_as_the_triangulation_for_a_trimmed_torus_segment() -> None:
    """落ちたら: bbox が面のトリムを見なくなっている(トーラス片が実寸より 9.06mm 大きく出る)。"""
    outward = _outward_mm(_torus_segment())
    assert max(outward) <= DEVIATION, f"最大の膨らみ {max(outward):.6f}mm"


# 呼び出し側が `cache=` を渡しているか
#: `cache=` を渡さないと決めた場所。**空にしておくこと** —— ここへ足すのは
#: 「この呼び出しは毎回測り直してよい」という判断なので、理由を添えて明示的に書く。
#: 鍵はソースの相対パス、値は理由。
BOUNDING_BOX_WITHOUT_CACHE: dict[str, str] = {}


def _bounding_box_calls() -> list[tuple[str, int, bool]]:
    """`duocad/` の中の `bounding_box(...)` 呼び出しを `(パス, 行, cacheを渡したか)` で返す。"""
    root = pathlib.Path(__file__).resolve().parent.parent / "duocad"
    found: list[tuple[str, int, bool]] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else func.id
                if isinstance(func, ast.Name)
                else None
            )
            if name != "bounding_box":
                continue
            passes = any(keyword.arg == "cache" for keyword in node.keywords)
            found.append((path.name, node.lineno, passes))
    return found


def test_every_bounding_box_call_passes_a_cache() -> None:
    """落ちたら: `cache=` を渡していない `bounding_box` の呼び出しが増えた。

    **これはソースを読む検査である。** 渡し忘れても**答えは正しいまま**で、変わるのは
    速さだけ(実測: `resolution_limits` が 0.5ms → 145ms)。だから他のどの検査も緑のまま通る。
    `AddOptimal` は繰り返しても速くならない(OCCTが結果を持たない)ので、渡し忘れは
    そのまま利用者の待ち時間になる。
    """
    missing = [
        f"{name}:{line}"
        for name, line, passes in _bounding_box_calls()
        if not passes and name not in BOUNDING_BOX_WITHOUT_CACHE
    ]
    assert not missing, (
        f"`cache=` を渡していない bounding_box の呼び出し: {missing}。"
        "文書を持っているなら `cache=document.bbox_cache` を渡すこと。"
        "毎回測り直してよい理由があるなら `cache=None` と明示するか、"
        "`BOUNDING_BOX_WITHOUT_CACHE` へ理由付きで足すこと"
    )


def test_the_call_scan_actually_finds_the_calls() -> None:
    """落ちたら: 上の検査が**何も見ていない**(走査が壊れて空振りしている)。"""
    calls = _bounding_box_calls()
    assert len(calls) >= 3, f"duocad/ の中に bounding_box の呼び出しが {len(calls)} 件しか無い"
