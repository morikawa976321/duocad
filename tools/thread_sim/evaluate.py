"""1つの設計を、刷りやすさ・嵌合・強さの指標にまとめる。

設計の隙間 c は雌雄の和。半分ずつ雄を縮め、雌を広げる(mode="radial" は半径方向、"normal" は山形の法線方向)。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

import numpy as np

from . import fit as ft
from . import printsim as ps
from . import profiles as pf
from . import strength as st
from .printer import DEFAULT, Printer


@dataclass(frozen=True)
class Design:
    name: str
    bolt: pf.Profile  # 隙間を付ける前の雄の山形(山頂の半径 = 呼び径/2)
    clearance: float  # 雌雄の和
    mode: str = "radial"
    L_nut: float = 6.0
    R_out: float | None = None  # ナットの外半径(既定は呼び径の 0.9 倍 = 外径 1.8d)
    bolt_flipped: bool = False
    nut_flipped: bool = False
    split: float = 0.5  # 隙間のうち雄に持たせる割合(0 = 雄は呼び寸法のままで雌だけ広げる。試し刷りの形)

    @property
    def d(self) -> float:
        return 2 * self.bolt.r_max

    def parts(self, c: float | None = None):
        c = self.clearance if c is None else c
        cb, cn = self.split * c, (1 - self.split) * c
        if self.mode == "radial":
            return self.bolt.shifted(-cb), self.bolt.shifted(+cn)
        b = self.bolt.offset_normal(-cb) if cb > 0 else self.bolt
        return b, self.bolt.offset_normal(+cn)


@dataclass
class Evaluation:
    design: Design
    printer: Printer
    over_bolt: dict
    over_nut: dict
    margin: float  # 設計の隙間での包絡の余り(半径、正ではまる)
    c_req: float  # 包絡が通る最小の設計の隙間
    engagement: float  # 設計の隙間での平均の山形どうしの掛かり(半径)
    play: float  # 設計の隙間での軸方向の遊び
    depth_kept: float  # 印刷後の雄の平均の山の高さ / 設計の山の高さ
    rms_dev: float  # 印刷後の雄の平均の山形と設計の差の RMS(mm)
    strength: st.Result | None = None
    extra: dict = field(default_factory=dict)

    def window(self, e_req: float):
        """掛かり代 ≥ e_req を上限にした、設計の隙間の窓 [c_lo, c_hi] と幅。掛かり代は隙間に対しほぼ傾き -1 で減る。"""
        c_hi = self.design.clearance + self.engagement - e_req
        return self.c_req, c_hi, c_hi - self.c_req


def _simulate_pair(des: Design, pr: Printer, c: float, seed: int = 0):
    b_prof, n_prof = des.parts(c)
    P = des.bolt.pitch
    L = des.L_nut
    bolt = ps.simulate(ps.Part("bolt", b_prof, -P, L + P, flipped=des.bolt_flipped), pr, seed=seed)
    nut = ps.simulate(ps.Part("nut", n_prof, 0.0, L, flipped=des.nut_flipped, bed_gap=0.37), pr, seed=seed + 1)
    zr = (0.25 * P, L - 0.25 * P)
    return bolt, nut, ft.evaluate(bolt, nut, z_range=zr), zr


def c_required(des: Design, pr: Printer, lo: float = 0.0, hi: float = 1.5, tol: float = 0.005) -> float:
    """包絡が通る最小の隙間(二分法)。radial は線形なので1回で出る。"""
    if des.mode == "radial":
        _, _, f, _ = _simulate_pair(des, pr, des.clearance)
        return des.clearance - f.best_margin
    f_hi = _simulate_pair(des, pr, hi)[2].best_margin
    if f_hi < 0:
        return math.inf
    while hi - lo > tol:
        mid = (lo + hi) / 2
        if _simulate_pair(des, pr, mid)[2].best_margin >= 0:
            hi = mid
        else:
            lo = mid
    return hi


def evaluate(des: Design, pr: Printer = DEFAULT, with_strength: bool = True, mat: st.PrintedPLA | None = None,
             seed: int = 0) -> Evaluation:
    bolt, nut, f, zr = _simulate_pair(des, pr, des.clearance, seed)
    ob = ps.overhang_stats(bolt, z_range=(0, des.L_nut))
    on = ps.overhang_stats(nut, z_range=zr)
    c_req = des.clearance - f.best_margin if des.mode == "radial" else c_required(des, pr)
    # 雄の印刷後の平均の山形と設計
    s, _, _, mb, _ = ps.phase_stats(bolt, len(f.s), z_range=zr)
    b_prof, _ = des.parts()
    design_r = b_prof(s)
    depth_kept = float((np.nanmax(mb) - np.nanmin(mb)) / max(b_prof.depth, 1e-9))
    rms = float(np.sqrt(np.nanmean((mb - design_r) ** 2)))
    ev = Evaluation(des, pr, ob, on, f.best_margin, c_req, f.engagement, f.play, depth_kept, rms)
    ev.extra["bolt_mean"] = (s, mb)
    ev.extra["nut_mean"] = (s, f.nut_mean)
    if with_strength:
        ev.strength = strength_of(ev, mat or st.PrintedPLA())
    return ev


def strength_of(ev: Evaluation, mat: st.PrintedPLA) -> st.Result | None:
    """平均の山形(θ で平均した印刷後の形)をリングの山として軸対称 FEA にかける。"""
    des = ev.design
    P = des.bolt.pitch
    s, mb = ev.extra["bolt_mean"]
    _, mn = ev.extra["nut_mean"]
    if np.isnan(mb).any() or np.isnan(mn).any():
        return None
    ss = np.append(s - P / len(s) / 2, P)
    mb2 = np.append(mb, mb[0])
    mn2 = np.append(mn, mn[0])
    bolt_r = lambda z: np.interp(np.mod(z, P), ss, mb2)
    nut_r = lambda z: np.interp(np.mod(z, P), ss, mn2)
    R_out = des.R_out if des.R_out is not None else 0.9 * des.d
    r_bear = min(0.55 * des.d + 0.2, 0.5 * (des.d / 2 + R_out))
    # 穴が大きく刷れた側(法線方向の隙間が大きい設計の緩い端)では穴の谷が座面の内径を越え、ナットの下面の辺が折り返して
    # メッシュの境界の回復が発散する(実測: 台形45 D10 P4 normal、谷 5.732 > 座面 5.70)。座面は穴の縁から始める
    r_bear = min(max(r_bear, float(mn.max()) + 0.2), R_out - 0.3)
    j = st.Joint(bolt_r, nut_r, P, des.L_nut, R_out, r_bear)
    try:
        return st.analyze(j, mat)
    except (ValueError, MemoryError, RuntimeError) as e:  # メッシュ・接触の候補数・LCP の失敗は、その点の強さが出ないだけにする
        ev.extra["strength_error"] = f"{type(e).__name__}: {e}"
        return None
