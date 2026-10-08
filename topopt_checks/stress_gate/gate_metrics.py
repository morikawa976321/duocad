"""母数に依らない応力の合否判定の候補式(研究用。製品コードは変えない)。

全関数は要素ごとの von Mises [MPa](格子と同じ形の配列)と材料マスクを受け取る。
「母数」は材料セルの総数のこと。ここの式はどれも、応力の低い材料セルを足し引きしても値が変わらない。
"""
from __future__ import annotations

import numpy as np
from scipy import ndimage, signal


def percentile_current(vm: np.ndarray, mask: np.ndarray, q: float = 99.5) -> float:
    """製品の現行(材料セル全体の q% 点)。比較用。"""
    return float(np.percentile(vm[mask], q))


def top_volume_value(vm: np.ndarray, mask: np.ndarray, cell_mm3: float, volume_mm3: float) -> float:
    """固定物理体積の上位値 Q_V: 「応力がこれを超える材料の体積が V」となる応力。

    V がセル体積の整数倍でないときは隣り合う2つの順位の値を線形に補間する(格子を変えたときに値が
    階段状に跳ねないように)。V が材料全体より大きければ材料の最小値を返す。
    """
    vals = np.sort(vm[mask])[::-1]
    k = volume_mm3 / cell_mm3  # 上位 k セルぶん
    if k <= 1.0:
        return float(vals[0])
    if k >= vals.size:
        return float(vals[-1])
    lo = int(np.floor(k)) - 1  # k セル目の0始まり添字
    frac = k - np.floor(k)
    if frac == 0.0:
        return float(vals[lo])
    return float(vals[lo] + (vals[lo + 1] - vals[lo]) * frac)


def exceed_volume(vm: np.ndarray, mask: np.ndarray, cell_mm3: float, level_mpa: float) -> float:
    return float(np.count_nonzero(mask & (vm > level_mpa)) * cell_mm3)


def exceed_integral(vm: np.ndarray, mask: np.ndarray, cell_mm3: float, level_mpa: float) -> float:
    """M5b と同じ Σ(σ−level)+·体積 [MPa·mm³]。"""
    over = np.where(mask, vm - level_mpa, 0.0)
    return float(np.clip(over, 0.0, None).sum() * cell_mm3)


def ball_kernel(radius_mm: float, spacing_mm: float) -> np.ndarray:
    r = radius_mm / spacing_mm
    n = int(np.floor(r + 1e-9))
    ax = np.arange(-n, n + 1)
    x, y, z = np.meshgrid(ax, ax, ax, indexing="ij")
    return (x * x + y * y + z * z <= r * r + 1e-9).astype(float)


def ball_mean_field(vm: np.ndarray, mask: np.ndarray, radius_mm: float, spacing_mm: float) -> np.ndarray:
    """材料セルだけで取った、中心から半径 R 内の平均応力の場(材料外は 0)。"""
    ker = ball_kernel(radius_mm, spacing_mm)
    m = mask.astype(float)
    num = signal.fftconvolve(np.where(mask, vm, 0.0), ker, mode="same")
    den = signal.fftconvolve(m, ker, mode="same")
    out = np.zeros_like(vm, dtype=float)
    ok = mask & (den > 0.5)
    out[ok] = num[ok] / den[ok]
    return out


def ball_mean_max(vm: np.ndarray, mask: np.ndarray, radius_mm: float, spacing_mm: float) -> float:
    return float(ball_mean_field(vm, mask, radius_mm, spacing_mm)[mask].max())


def distance_to_boxes(grid, boxes) -> np.ndarray:
    """各セル中心から、ボックス群(ワールド座標の AABB)までの最短距離 [mm]。"""
    xx, yy, zz = np.meshgrid(grid.centers_1d(0), grid.centers_1d(1), grid.centers_1d(2), indexing="ij")
    pts = np.stack([xx, yy, zz], axis=-1).reshape(-1, 3)
    d = np.full(pts.shape[0], np.inf)
    for lo, hi in boxes:
        closest = np.clip(pts, np.asarray(lo, float), np.asarray(hi, float))
        d = np.minimum(d, np.linalg.norm(pts - closest, axis=-1))
    return d.reshape(xx.shape)
