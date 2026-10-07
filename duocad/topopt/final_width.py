"""完成メッシュ(平滑化・保持/除外の集合演算の後)の最小幅を、中軸上の太さで測る(#117)。

定義は [TOPOPT_FINAL_WIDTH_117.md](../../docs/TOPOPT_FINAL_WIDTH_117.md)「完成形の最小幅の定義」。要点:

- 部材の太さ = 中軸(内側の点のうち、最寄りの表面点が向かい合う2方向にある所)での、その2点の距離。
- 探索のボクセル規則(軸に沿う m 立方の被覆)は丸めた形に使えない —— 直径 d の丸棒に一辺 d の立方は入らず、
  角を丸めただけで太さが足りていても不合格になる。内接球の被覆(opening)も凸の稜・角で必ず欠けるので使わない。
- 凸の稜・角へ伸びる中軸の枝は、最寄りの表面点が成す角が 180° − 内角 になる(直角の稜で 90°)。
  `MEDIAL_MIN_ANGLE_DEG` 未満の枝は部材とみなさない。内角がそれより鋭い稜(刃先)は細い部材として数える。
"""
from __future__ import annotations

import math
from typing import Optional

import numpy as np
from scipy import ndimage

from . import voxelize

# 最寄り表面点が成す角の下限。直角の稜(90°)と、標本化した斜面の階段(実測で120〜150°に出る)を除き、
# 向かい合う面(180°)を残す。内角30°未満の刃先は細い部材として数える。
MEDIAL_MIN_ANGLE_DEG = 150.0
# この標本数以下の太さは測らない。平滑化した板の表面のうねりが、標本化で2標本幅の突起として出る(実測 2026-10-07)。
NOISE_FLOOR_SAMPLES = 2.0
# 標本格子は探索格子の 1/4 を目指し、収まらなければ 1/3・1/2・1 と粗くする。
# 距離変換は 1 標本あたり約 22 バイト(bool + float64 + int32×3)なので、上限で約 350MB。
MAX_SAMPLES = 16_000_000
SUBDIVISIONS = (4, 3, 2, 1)


def _sampling_grid(vertices: np.ndarray, spacing: float, max_samples: int) -> voxelize.Grid:
    lo, hi = vertices.min(axis=0), vertices.max(axis=0)
    for k in SUBDIVISIONS:
        h = spacing / k
        # 外側に2標本ずつ空ける(距離変換が背景を必ず見る)。
        shape = tuple(int(math.ceil((b - a) / h)) + 4 for a, b in zip(lo, hi))
        if k == SUBDIVISIONS[-1] or math.prod(shape) <= max_samples:
            return voxelize.Grid(tuple(lo - 2 * h), h, shape)
    raise AssertionError('unreachable')


def measure(vertices, faces, *, spacing: float, required_mm: float,
            exempt: Optional[tuple] = None, max_samples: int = MAX_SAMPLES) -> dict:
    """完成メッシュの中軸上の太さを測り、`required_mm` を割る所を数える。

    `spacing` は探索格子の要素サイズ(標本格子はその 1/k)。`exempt=(mask, grid)` を渡すと、その格子で
    True のセルに入る中軸点を対象外にする(保持セル。探索の最小幅規則と同じ免除)。戻り値は `measure_samples`。
    """
    vertices = np.asarray(vertices, dtype=float)
    faces = np.asarray(faces)
    grid = _sampling_grid(vertices, float(spacing), max_samples)
    soup = voxelize.TriangleSoup(positions=vertices, triangles=faces,
                                 face_index=np.zeros(len(faces), dtype=np.int64))
    inside = voxelize.occupancy(soup, grid, allow_empty=True)[0]
    return measure_samples(inside, grid, required_mm=required_mm, exempt=exempt)


def measure_voxels(mask: np.ndarray, grid: voxelize.Grid, *, required_mm: float, subdivision: int = 3,
                   exempt: Optional[np.ndarray] = None) -> dict:
    """ボクセル形状(探索格子の0/1マスク)を各軸 `subdivision` 等分して同じ方法で測る。`exempt` は `mask` と同じ格子。"""
    k = int(subdivision)
    fine = np.pad(np.asarray(mask, dtype=bool), 1)
    for axis in range(3):
        fine = np.repeat(fine, k, axis=axis)
    h = float(grid.spacing) / k
    fine_grid = voxelize.Grid(tuple(np.asarray(grid.origin) - grid.spacing), h, fine.shape)
    return measure_samples(fine, fine_grid, required_mm=required_mm,
                           exempt=None if exempt is None else (np.asarray(exempt, dtype=bool), grid))


def measure_samples(inside: np.ndarray, grid: voxelize.Grid, *, required_mm: float,
                    exempt: Optional[tuple] = None) -> dict:
    """標本の内外 `inside`(外周は外であること)から中軸上の太さを測る。

    太さの測定誤差はおおむね標本間隔 h 以内(表面を標本中心で拾うので片側 h/2)。
    **`required_mm − h` を下回る所だけを幅不足とする**(誤差の範囲で警告を出さない)。
    `NOISE_FLOOR_SAMPLES × h` 以下の太さは測らない(表面のうねりと区別できない)。戻り値は JSON 化できる辞書:

        {"required_mm", "sampling_mm", "tolerance_mm", "noise_floor_mm",
         "min_width_mm": float|None,          # 対象外を除く中軸点の太さの最小(中軸点が無ければ None)
         "thinnest_point_mm": [x,y,z]|None,   # その位置(ワールド mm)
         "thin_points": int,                  # 幅不足の中軸点の数(標本の隣接対ごと)
         "medial_points": int, "exempt_points": int,
         "status": "pass" | "thin"}
    """
    h = float(grid.spacing)
    # 各標本の最寄りの背景標本の添字(3, nx, ny, nz)。
    feature = ndimage.distance_transform_edt(inside, return_distances=False, return_indices=True)

    cos_limit = math.cos(math.radians(MEDIAL_MIN_ANGLE_DEG))
    widths, centers = [], []
    for axis in range(3):
        lead = [slice(None)] * 3
        tail = [slice(None)] * 3
        lead[axis], tail[axis] = slice(None, -1), slice(1, None)
        lead, tail = tuple(lead), tuple(tail)
        both = inside[lead] & inside[tail]
        if not both.any():
            continue
        p = np.argwhere(both)
        q = p.copy()
        q[:, axis] += 1
        fp = feature[:, p[:, 0], p[:, 1], p[:, 2]].T.astype(float)
        fq = feature[:, q[:, 0], q[:, 1], q[:, 2]].T.astype(float)
        mid = (p + q) / 2.0
        u, v = fp - mid, fq - mid
        nu, nv = np.linalg.norm(u, axis=1), np.linalg.norm(v, axis=1)
        cos = np.einsum('ij,ij->i', u, v) / np.maximum(nu * nv, 1e-12)
        medial = (cos < cos_limit) & (nu + nv - 1.0 > NOISE_FLOOR_SAMPLES)
        # 最寄りの背景標本の中心は表面の外側へ平均 h/2 ずつ出ているので、両側で 1 標本ぶん引く。
        widths.append((nu[medial] + nv[medial] - 1.0) * h)
        centers.append(np.asarray(grid.origin) + (mid[medial] + 0.5) * h)
    width = np.concatenate(widths) if widths else np.zeros(0)
    center = np.concatenate(centers) if centers else np.zeros((0, 3))

    exempt_points = 0
    if exempt is not None and len(center):
        mask, coarse = exempt
        index = np.floor((center - np.asarray(coarse.origin)) / coarse.spacing).astype(np.int64)
        within = np.all((index >= 0) & (index < np.asarray(mask.shape)), axis=1)
        skip = np.zeros(len(center), dtype=bool)
        skip[within] = mask[tuple(index[within].T)]
        exempt_points = int(skip.sum())
        width, center = width[~skip], center[~skip]

    tolerance = h
    thin = width < float(required_mm) - tolerance
    worst = int(np.argmin(width)) if len(width) else None
    return dict(
        required_mm=float(required_mm), sampling_mm=h, tolerance_mm=tolerance,
        noise_floor_mm=NOISE_FLOOR_SAMPLES * h,
        min_width_mm=None if worst is None else float(width[worst]),
        thinnest_point_mm=None if worst is None else center[worst].tolist(),
        thin_points=int(thin.sum()), medial_points=int(len(width)), exempt_points=exempt_points,
        status='thin' if thin.any() else 'pass',
    )
