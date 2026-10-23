"""表現の違いを吸収したスケッチ幾何の比較。"""
from __future__ import annotations

import math
from itertools import permutations
from typing import Dict, Optional

from .. import kernel_sketch


def geometry_signature(kind: str, params: Dict[str, float], *, round_digits: Optional[int] = 6) -> tuple:
    """図形の幾何を、表し方の違い(線分の端点の順、長方形の基準の角と向き、楕円の長軸の取り方、円弧の角度の巻き)に
    よらず同じ値にする署名。反復は小数6桁、許容差で比べる複製は丸めずに使う。"""

    def r(value: float) -> float:
        return float(value) if round_digits is None else round(float(value), round_digits) + 0.0  # -0.0 を 0.0 にする

    if kind == "point":
        return ("point", r(params["cx"]), r(params["cy"]))
    if kind == "circle":
        return ("circle", r(params["cx"]), r(params["cy"]), r(params["diameter"]))
    if kind == "line":
        ends = sorted([(r(params["x0"]), r(params["y0"])), (r(params["x1"]), r(params["y1"]))])
        return ("line", *ends)
    if kind == "rectangle":
        return ("rectangle", *sorted((r(x), r(y)) for x, y in kernel_sketch.rectangle_corners(params)))
    if kind == "arc":
        sweep = (float(params["end_deg"]) - float(params["start_deg"])) % 360.0
        if sweep < 1e-9 or sweep > 360.0 - 1e-9:  # 始終の角が同じ(0〜360 度など)円弧は全周で、円と同じ
            return ("circle", r(params["cx"]), r(params["cy"]), r(2.0 * float(params["radius"])))
        return (
            "arc", r(params["cx"]), r(params["cy"]), r(params["radius"]),
            r(params["start_deg"] % 360.0) % 360.0, r(params["end_deg"] % 360.0) % 360.0,
        )
    if kind == "ellipse":
        width, height, angle = float(params["width"]), float(params["height"]), float(params["angle"])
        if abs(width - height) <= 1e-9:
            return ("circle", r(params["cx"]), r(params["cy"]), r(width))
        if width < height:  # 長軸と短軸を入れ替えても同じ楕円(向きは 90 度ずれる)
            width, height, angle = height, width, angle + 90.0
        return ("ellipse", r(params["cx"]), r(params["cy"]), r(width), r(height), r(angle % 180.0) % 180.0)
    if kind == "bezier":
        points = [(r(params[kx]), r(params[ky])) for kx, ky in kernel_sketch.BEZIER_POINT_KEYS]
        return ("bezier", *min(points, points[::-1]))  # 始点と終点を入れ替えても同じ曲線
    raise ValueError(f"unknown sketch entity kind: {kind!r}")


def geometries_overlap(kind: str, left: dict, right: dict, *, tolerance: float) -> bool:
    a = geometry_signature(kind, left, round_digits=None)
    b = geometry_signature(kind, right, round_digits=None)
    if a[0] != b[0]:
        return False

    def close(x, y):
        return math.isclose(x, y, rel_tol=1e-12, abs_tol=tolerance)

    def point_close(p, q):
        return all(close(x, y) for x, y in zip(p, q))

    if a[0] in ("line", "rectangle"):
        return any(all(point_close(p, q) for p, q in zip(a[1:], points)) for points in permutations(b[1:]))
    if a[0] == "bezier":
        return any(all(point_close(p, q) for p, q in zip(a[1:], points)) for points in (b[1:], b[:0:-1]))
    if a[0] in ("arc", "ellipse"):
        angle_start = 4 if a[0] == "arc" else 5
        period = 360.0 if a[0] == "arc" else 180.0
        return all(close(x, y) for x, y in zip(a[1:angle_start], b[1:angle_start])) and all(
            abs(math.remainder(x - y, period)) <= tolerance for x, y in zip(a[angle_start:], b[angle_start:])
        )
    return all(close(x, y) for x, y in zip(a[1:], b[1:]))
