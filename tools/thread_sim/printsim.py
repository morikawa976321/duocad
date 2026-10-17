"""軸を鉛直にして刷ったねじの、印刷後の表面の模擬。

1層ごとに:
1. 層の中央の高さで断面を取る(スライサーと同じ)。ねじの断面は原点まわりの星形の曲線 R(θ) になる。
2. 外壁のビードは幅 w の円板を断面の内側で転がした跡なので、印刷される外形は断面の開放演算(半径 w/2)。
   雌は穴の側から見て閉包演算になる。円板の包絡を極座標のまま厳密に計算する(`_dilate` / `_erode`)。
3. 前の層との支持: 今の層の外形の各点から、前の層の外形までの XY 平面での符号付き距離 d(外へ出たら正)。
   支持率 φ = clip(1 - d/w, 0, 1)。Orca の張り出しの区分(25/50/75/100%)と同じ量。
4. 垂れ: φ < φok の点では、張り出した部分が δ だけ下へ垂れる(係数は printer.Printer)。
5. ビードの側面は半径 h/2 の半円(高さ z での外形は、層の外形を XY の法線方向へ h/2-√((h/2)²-(z-zc)²) だけ縮めたもの)。

出力は表面の半径の場 F[θ, z](雄は材料の外縁、雌は穴の縁)。組み付けの座標での位相 s = (z - kθ) mod P へ写して、
包絡と平均の山形を取る。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy.spatial import cKDTree

from .printer import Printer
from .profiles import Profile

N_THETA = 1024
SUB = 10  # 1層を何段の高さで表すか


@dataclass
class Part:
    kind: str  # "bolt" | "nut"
    profile: Profile  # 組み付けの座標での表面の山形
    z0: float  # 模擬する範囲(組み付けの座標)
    z1: float
    flipped: bool = False  # True: 組み付けの向きから上下を返して刷る
    bed_gap: float = 3.0  # ベッドから模擬する範囲の下端までの高さ(印刷の座標。層の位相を決める)

    def print_profile_fn(self):
        """印刷の座標での位相 s' から半径を返す関数。z = z0 + z' - gap(返さない)、z = z1 + gap - z'・θ = -θ'(返す)。"""
        P = self.profile.pitch
        if not self.flipped:
            off = self.z0 - self.bed_gap
            return lambda sp: self.profile(np.mod(sp + off, P))
        off = self.z1 + self.bed_gap
        return lambda sp: self.profile(np.mod(off - sp, P))

    def to_assembly(self, theta_p, z_p):
        if not self.flipped:
            return theta_p, self.z0 + z_p - self.bed_gap
        return -theta_p, self.z1 + self.bed_gap - z_p


# ---------- 星形の領域の円板による膨張・侵食(極座標のまま) ----------

def _window(R, rho):
    n = len(R)
    dth = 2 * math.pi / n
    rmin = max(float(R.min()), 1e-6)
    a = math.asin(min(1.0, rho / rmin)) if rho < rmin else math.pi / 2
    return int(math.ceil(a / dth)) + 2


def _ray_disc(R, rho, far: bool):
    """各方向 θ_j の半直線と、境界点 p_i を中心とする半径 rho の円との交点(far=True で遠い側、False で近い側)。
    交わらない組は除く。返り値は各 j での max(遠い側)/min(近い側)。"""
    n = len(R)
    th = np.arange(n) * 2 * math.pi / n
    m = _window(R, rho)
    offs = np.arange(-m, m + 1)
    idx = (np.arange(n)[:, None] + offs[None, :]) % n
    phi = th[:, None] - th[idx]  # 方向 j から見た点 i の角度差
    rp = R[idx]
    u_dot_p = rp * np.cos(phi)
    disc = rho * rho - rp * rp + u_dot_p * u_dot_p
    ok = (disc >= 0) & (u_dot_p > 0)
    sq = np.sqrt(np.where(ok, disc, 0.0))
    if far:
        t = np.where(ok, u_dot_p + sq, -np.inf)
        return t.max(axis=1)
    t = np.where(ok, u_dot_p - sq, np.inf)
    return t.min(axis=1)


def dilate(R, rho):
    if rho <= 0:
        return R.copy()
    return np.maximum(R, _ray_disc(R, rho, far=True))


def erode(R, rho):
    if rho <= 0:
        return R.copy()
    return np.minimum(R, _ray_disc(R, rho, far=False))


def opening(R, rho):
    return dilate(erode(R, rho), rho)


def closing(R, rho):
    return erode(dilate(R, rho), rho)


def normal_offset(R, d):
    """曲線を外向きの法線方向へ d(配列可、負で内へ)動かして、同じ θ の格子へ引き直す。"""
    n = len(R)
    th = np.arange(n) * 2 * math.pi / n
    x, y = R * np.cos(th), R * np.sin(th)
    tx = np.roll(x, -1) - np.roll(x, 1)
    ty = np.roll(y, -1) - np.roll(y, 1)
    L = np.hypot(tx, ty)
    nx, ny = ty / L, -tx / L  # 反時計回りの曲線の外向き法線
    x2, y2 = x + d * nx, y + d * ny
    th2 = np.unwrap(np.arctan2(y2, x2))
    r2 = np.hypot(x2, y2)
    # 引き直し: 角度が単調でない区間(小さなループ)は、遠い/近い側を取る代わりに中央値で潰さず、
    # 単調化してから補間する(d が曲率半径より小さい範囲で使う)
    order = np.argsort(th2)
    th2, r2 = th2[order], r2[order]
    th2 = th2 - 2 * math.pi * math.floor(th2[0] / (2 * math.pi))
    return np.interp(th, th2, r2, period=2 * math.pi)


def signed_distance_out(Rq, Rref, outward: bool):
    """曲線 Rq の各点から曲線 Rref への XY の距離。outward=True なら Rref の外(半径が大きい側)にある点を正にする。"""
    n = len(Rq)
    th = np.arange(n) * 2 * math.pi / n
    ref = np.column_stack([Rref * np.cos(th), Rref * np.sin(th)])
    # 線分の上の最近点まで取るため、参照曲線を4倍に細かくする
    k = 4
    thf = np.arange(n * k) * 2 * math.pi / (n * k)
    Rf = np.interp(thf, th, Rref, period=2 * math.pi)
    ref = np.column_stack([Rf * np.cos(thf), Rf * np.sin(thf)])
    q = np.column_stack([Rq * np.cos(th), Rq * np.sin(th)])
    dist, _ = cKDTree(ref).query(q)
    outside = Rq > Rref if outward else Rq < Rref
    return np.where(outside, dist, -dist)


# ---------- 1部品の印刷 ----------

@dataclass
class Printed:
    part: Part
    printer: Printer
    theta: np.ndarray  # 印刷の座標の θ'
    z: np.ndarray  # 印刷の座標の z'(場の高さ)
    F: np.ndarray  # (n_theta, n_z) 表面の半径
    layer_z: np.ndarray  # 各層の中央の z'
    contour: np.ndarray  # (n_layer, n_theta) 印刷される外形(層の中央)
    overhang: np.ndarray  # (n_layer, n_theta) 前の層からの張り出し d(mm、正が張り出し)
    support: np.ndarray  # 支持率 φ
    sag: np.ndarray  # 垂れ δ

    def assembly_points(self):
        """場の各点の組み付けの座標 (θ, z, s)。"""
        th = np.broadcast_to(self.theta[:, None], self.F.shape)
        zz = np.broadcast_to(self.z[None, :], self.F.shape)
        ta, za = self.part.to_assembly(th, zz)
        P = self.part.profile.pitch
        k = P / (2 * math.pi)
        s = np.mod(za - k * ta, P)
        return ta, za, s


def _sag_amount(phi, pr: Printer):
    x = np.clip((pr.sag_phi_ok - phi) / pr.sag_phi_ok, 0.0, None)
    return pr.layer * pr.sag_amp * x ** pr.sag_exp


def simulate(part: Part, pr: Printer, n_theta: int = N_THETA, seed: int = 0) -> Printed:
    h = pr.layer
    P = part.profile.pitch
    k = P / (2 * math.pi)
    is_bolt = part.kind == "bolt"
    length = part.z1 - part.z0
    g = part.print_profile_fn()
    theta = np.arange(n_theta) * 2 * math.pi / n_theta
    # 模擬範囲にかかる層(印刷の座標)。最初の層だけ高さが違う場合は first_layer(既定は同じ 0.2)
    zp_lo, zp_hi = part.bed_gap, part.bed_gap + length
    k0 = int(math.floor(zp_lo / h)) - 1
    k1 = int(math.ceil(zp_hi / h)) + 1
    ks = np.arange(max(k0, 0), k1)
    layer_mid = (ks + 0.5) * h
    rng = np.random.default_rng(seed)
    contour = np.empty((len(ks), n_theta))
    rho = pr.outer_wall / 2
    seam_th = 0.0
    for i, zc in enumerate(layer_mid):
        R = g(zc - k * theta)
        R = R + (pr.contour_bias if is_bolt else pr.hole_bias)
        if pr.layer_noise > 0:
            R = R + rng.normal(0.0, pr.layer_noise)
        if pr.seam_bump > 0:
            half = pr.seam_width / 2 / max(float(R.mean()), 1e-6)
            dth = np.angle(np.exp(1j * (theta - seam_th)))
            bump = pr.seam_bump * np.where(np.abs(dth) < half, 0.5 * (1 + np.cos(math.pi * dth / half)), 0.0)
            R = R + (bump if is_bolt else -bump)
        contour[i] = opening(R, rho) if is_bolt else closing(R, rho)
    # 張り出しと支持
    over = np.zeros_like(contour)
    for i in range(1, len(ks)):
        over[i] = signed_distance_out(contour[i], contour[i - 1], outward=is_bolt)
    support = np.clip(1.0 - over / pr.outer_wall, 0.0, 1.0)
    support[0] = 1.0
    sag = np.where(over > 0, _sag_amount(support, pr), 0.0)
    # 表面の場: 各層を SUB 段で。ビードの側面の丸みは法線方向の縮み
    dz = h / SUB
    z_field = np.arange(int(round(zp_lo / dz)), int(round(zp_hi / dz))) * dz + dz / 2
    F = np.full((n_theta, len(z_field)), -np.inf if is_bolt else np.inf)
    half = h / 2
    for i, kk in enumerate(ks):
        zc = layer_mid[i]
        sel = np.nonzero((z_field >= kk * h) & (z_field < (kk + 1) * h))[0]
        for j in sel:
            t = (z_field[j] - zc) / half
            shrink = half * (1 - math.sqrt(max(0.0, 1 - t * t)))
            # 雄は材料を、雌は穴を広げる向き(=雌の材料の侵食)に、円板で厳密に縮める
            Rz = erode(contour[i], shrink) if is_bolt else dilate(contour[i], shrink)
            F[:, j] = np.maximum(F[:, j], Rz) if is_bolt else np.minimum(F[:, j], Rz)
        # 垂れた縁: 張り出した点のビードを δ だけ下げた形を、層の下側へ足す
        if i > 0 and sag[i].max() > 0:
            dmax = float(sag[i].max())
            below = np.nonzero((z_field >= kk * h - dmax - half) & (z_field < kk * h))[0]
            for j in below:
                t = (z_field[j] - (zc - sag[i])) / half  # θ ごとに中心が違う
                ok = (np.abs(t) <= 1) & (sag[i] > 0)
                if not ok.any():
                    continue
                shrink = half * (1 - np.sqrt(np.clip(1 - t * t, 0.0, None)))
                lip = contour[i] - shrink if is_bolt else contour[i] + shrink
                if is_bolt:
                    F[ok, j] = np.maximum(F[ok, j], lip[ok])
                else:
                    F[ok, j] = np.minimum(F[ok, j], lip[ok])
    return Printed(part, pr, theta, z_field, F, layer_mid, contour, over, support, sag)


# ---------- 位相 s ごとの包絡と平均 ----------

def phase_stats(pp: Printed, n_s: int = 400, z_range=None):
    """組み付けの座標の位相 s の格子ごとの (最大, 最小, 平均) の半径。z_range(組み付けの座標)で端を除ける。"""
    ta, za, s = pp.assembly_points()
    F = pp.F
    m = np.isfinite(F)
    if z_range is not None:
        m &= (za >= z_range[0]) & (za <= z_range[1])
    P = pp.part.profile.pitch
    b = np.minimum((s[m] / P * n_s).astype(int), n_s - 1)
    v = F[m]
    mx = np.full(n_s, -np.inf)
    mn = np.full(n_s, np.inf)
    np.maximum.at(mx, b, v)
    np.minimum.at(mn, b, v)
    sm = np.bincount(b, weights=v, minlength=n_s)
    cnt = np.bincount(b, minlength=n_s)
    mean = np.where(cnt > 0, sm / np.maximum(cnt, 1), np.nan)
    s_grid = (np.arange(n_s) + 0.5) * P / n_s
    return s_grid, mx, mn, mean, cnt


def overhang_stats(pp: Printed, z_range=None):
    """外壁の張り出しの区分ごとの割合(Orca の 1/4〜4/4 と同じく、支持の欠けた割合で分ける)と垂れ。"""
    ta, za = pp.part.to_assembly(pp.theta[None, :], pp.layer_z[:, None])
    m = np.ones_like(pp.support, bool)
    m[0] = False
    if z_range is not None:
        m &= (za >= z_range[0]) & (za <= z_range[1])
    phi = pp.support[m]
    lack = 1 - phi
    return dict(
        n=int(m.sum()),
        lack_ge_25=float((lack >= 0.25).mean()),
        lack_ge_50=float((lack >= 0.50).mean()),
        lack_ge_75=float((lack >= 0.75).mean()),
        lack_100=float((lack >= 0.999).mean()),
        support_min=float(phi.min()),
        overhang_max=float(pp.overhang[m].max()),
        sag_mean=float(pp.sag[m].mean()),
        sag_max=float(pp.sag[m].max()),
    )
