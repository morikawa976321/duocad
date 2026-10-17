"""印刷後の雄と雌がはまるかの判定。

雄と雌は同じピッチの螺旋運動で相対的に動くので、雄の位相 s の点は、ねじ込むあいだに雌の同じ位相の点と出会う
(軸方向の遊び δ のぶん位相がずれる)。だから位相ごとの包絡 E_bolt(s)=最大、E_nut(s)=最小を比べ、
ある δ で全 s について E_bolt(s) ≤ E_nut(s+δ) なら、剛体としてはまる**十分条件**になる(同軸を仮定。
実際には出会わない欠陥の組まで重ねるので、落ちても固着とは限らない。落ちたものは `direct_interference` で見る)。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .printsim import Printed, phase_stats


@dataclass
class Fit:
    margin: np.ndarray  # 各 δ での min_s(E_nut(s+δ) - E_bolt(s))(mm、半径)
    delta: np.ndarray  # δ の格子(mm、軸方向)
    best_margin: float  # max_δ margin。負なら、半径方向にこれだけ隙間を足せば包絡が通る
    play: float  # margin ≥ 0 の δ の連続区間のうち最長の幅(軸方向の遊び)
    engagement: float  # 平均の山形どうしの半径方向の掛かり(雄の山頂 − 雌の山頂)
    bolt_mean: np.ndarray
    nut_mean: np.ndarray
    s: np.ndarray


def _longest_true_run_circular(mask):
    if mask.all():
        return len(mask)
    if not mask.any():
        return 0
    m = np.concatenate([mask, mask])
    best = run = 0
    for v in m:
        run = run + 1 if v else 0
        best = max(best, run)
    return min(best, len(mask))


def evaluate(bolt: Printed, nut: Printed, n_s: int = 400, z_range=None) -> Fit:
    P = bolt.part.profile.pitch
    s, bmx, _, bmean, bc = phase_stats(bolt, n_s, z_range)
    _, _, nmn, nmean, nc = phase_stats(nut, n_s, z_range)
    if (bc == 0).any() or (nc == 0).any():
        raise ValueError("位相の格子に点の無い箱がある(n_s を減らすか範囲を広げる)")
    # δ を箱単位で回す: E_nut(s+δ) = roll(nmn, -shift)
    shifts = np.arange(n_s)
    margin = np.array([np.min(np.roll(nmn, -sh) - bmx) for sh in shifts])
    delta = shifts * P / n_s
    delta = np.where(delta > P / 2, delta - P, delta)
    best = float(margin.max())
    play = _longest_true_run_circular(margin >= 0) * P / n_s
    engagement = float(np.nanmax(bmean) - np.nanmin(nmean))
    return Fit(margin, delta, best, play, engagement, bmean, nmean, s)


def window(fit: Fit, clearance: float, e_req: float):
    """設計の隙間 clearance で刷ったときの Fit から、隙間の窓 [c_lo, c_hi] を出す。
    雌を半径方向に Δc 動かすと margin も掛かり代も Δc だけ動く(山形が半径方向に平行移動するだけで、
    印刷の模擬の結果もほぼ平行移動する。`check_linearity` で確かめる)。"""
    c_lo = clearance - fit.best_margin
    c_hi = clearance + fit.engagement - e_req
    return c_lo, c_hi, c_hi - c_lo


def _helical_grid(pp: Printed, n_s: int, dpsi: float, take_max: bool, z_range=None):
    """場を螺旋の座標 (s, ψ) の格子へ集める。ψ は螺旋に沿った角(z = s + kψ)。"""
    ta, za, s = pp.assembly_points()
    P = pp.part.profile.pitch
    k = P / (2 * math.pi)
    F = pp.F
    m = np.isfinite(F)
    if z_range is not None:
        m &= (za >= z_range[0]) & (za <= z_range[1])
    psi = (za[m] - s[m]) / k
    ib = np.minimum((s[m] / P * n_s).astype(int), n_s - 1)
    jb = np.floor(psi / dpsi).astype(int)
    j0 = int(jb.min())
    jb -= j0
    G = np.full((n_s, int(jb.max()) + 1), -np.inf if take_max else np.inf)
    (np.maximum if take_max else np.minimum).at(G, (ib, jb), F[m])
    return G, j0


def direct_interference(bolt: Printed, nut: Printed, n_s: int = 80, dpsi_deg: float = 3.0,
                        delta_span: float | None = None, n_delta: int = 41, bolt_range=None, nut_range=None):
    """回転角 φ と挿入量を保った干渉の計算。雌は軸方向に δ だけ浮けるとする(同軸)。

    返り値: 各 φ での min_δ(max 干渉)の列と、その最大(経路の中で最も干渉する位置で、δ を最良にしてもなお残る干渉。
    負なら最悪の位置でもその量だけ隙間が余る)。包絡の best_margin の符号を反転したものと比べて、包絡の保守性を測る。
    """
    P = bolt.part.profile.pitch
    k = P / (2 * math.pi)
    dpsi = math.radians(dpsi_deg)
    B, jb0 = _helical_grid(bolt, n_s, dpsi, True, bolt_range)
    N, jn0 = _helical_grid(nut, n_s, dpsi, False, nut_range)
    nb, nn = B.shape[1], N.shape[1]
    if nn > nb:
        raise ValueError("雄の範囲が雌より短い")
    span = P if delta_span is None else delta_span
    shifts_s = np.unique(np.round(np.linspace(-span / 2, span / 2, n_delta) / P * n_s).astype(int))
    worst = []
    for off in range(0, nb - nn + 1):  # 雌の ψ の位置(ねじ込みの途中の各位置)
        Bw = B[:, off:off + nn]
        best = np.inf
        for sh in shifts_s:
            Nn = np.roll(N, sh, axis=0)
            d = Bw - Nn
            ok = np.isfinite(d)
            if ok.any():
                best = min(best, float(d[ok].max()))
        worst.append(best)
    worst = np.array(worst)
    return worst, float(worst.max())
