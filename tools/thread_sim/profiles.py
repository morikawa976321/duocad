"""ねじ山形の族。雄ねじの表面を1ピッチ分の折れ線 r = f(s)(s は軸方向の位相 0..P)で持つ。

座標の約束(この包の全部で共通):
- 組み付けた状態の座標で、軸は z。右ねじ。表面は r = f((z - kθ) mod P)、k = P/2π。
- 雄の材料は r < f、雌(ナット)の材料は r > g。
- 雄の f が z とともに増える区間は**下向きの面**(雄を軸鉛直・頭を下にして刷ると張り出しになる斜面)。
  雌の g が z とともに減る区間が雌の下向きの面。
- β は斜面の水平からの角(ISO 30°、山の角度 90° の山なら 45°、軸に直角な棚は 0°)。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

N_DENSE = 2400  # 1ピッチを等間隔に引き直す点数。0.5mm ピッチで 0.2µm、5mm ピッチで 2µm 刻み


@dataclass(frozen=True)
class Profile:
    """1ピッチ分の山形。s は 0..pitch で単調(非減少)、r[0] == r[-1]。"""

    s: np.ndarray
    r: np.ndarray
    pitch: float
    name: str
    params: dict = field(default_factory=dict)

    def __call__(self, s):
        return np.interp(np.mod(s, self.pitch), self.s, self.r)

    @property
    def r_max(self) -> float:
        return float(self.r.max())

    @property
    def r_min(self) -> float:
        return float(self.r.min())

    @property
    def depth(self) -> float:
        return self.r_max - self.r_min

    def dense(self, n: int = N_DENSE):
        s = np.linspace(0.0, self.pitch, n, endpoint=False)
        return s, self(s)

    def shifted(self, dr: float, name: str | None = None) -> "Profile":
        """半径方向に dr 動かす(雌の隙間 +c、雄の隙間 -c)。"""
        return Profile(self.s, self.r + dr, self.pitch, name or self.name, dict(self.params))

    def offset_normal(self, c: float, name: str | None = None) -> "Profile":
        """山形の曲線を法線方向に c だけ動かす(c>0 で外へ=雄の材料を円板 c で膨らませた境界)。
        f_c(s) = max_{|t|≤c} f(s+t) + sqrt(c²-t²)。c<0 はその逆(侵食)。"""
        s, r = self.dense()
        ds = self.pitch / len(s)
        m = int(math.ceil(abs(c) / ds))
        t = np.arange(-m, m + 1) * ds
        keep = np.abs(t) <= abs(c)
        t = t[keep]
        cap = np.sqrt(np.maximum(c * c - t * t, 0.0))
        idx = (np.arange(len(s))[:, None] + np.round(t / ds).astype(int)[None, :]) % len(s)
        if c >= 0:
            out = (r[idx] + cap[None, :]).max(axis=1)
        else:
            out = (r[idx] - cap[None, :]).min(axis=1)
        return Profile(np.append(s, self.pitch), np.append(out, out[0]), self.pitch, name or self.name, dict(self.params))

    def flank_angles(self):
        """下向き・上向きの斜面の、面積で重みを付けない最小の β(度)。区間ごとの傾きから。"""
        ds = np.diff(self.s)
        dr = np.diff(self.r)
        ok = ds > 1e-12
        beta = np.degrees(np.arctan2(ds[ok], np.abs(dr[ok])))  # 水平(dr 方向)からの角
        lo = beta[dr[ok] > 1e-9]
        up = beta[dr[ok] < -1e-9]
        return (float(lo.min()) if len(lo) else 90.0, float(up.min()) if len(up) else 90.0)


def _close(pts, pitch, name, params):
    s = np.array([p[0] for p in pts], float)
    r = np.array([p[1] for p in pts], float)
    if abs(s[0]) > 1e-12 or abs(s[-1] - pitch) > 1e-9 or abs(r[0] - r[-1]) > 1e-9:
        raise ValueError(f"{name}: 1ピッチで閉じていない")
    if np.any(np.diff(s) < -1e-12):
        raise ValueError(f"{name}: s が単調でない(張り出しの逆を含む山形は r=f(s) で書けない)")
    return Profile(s, r, pitch, name, params)


def iso(d: float, pitch: float) -> Profile:
    """ISO 68-1 の基準山形(60度、山頂の平坦 P/8、谷の平坦 P/4)。山頂の半径が d/2。"""
    h = 5 * (math.sqrt(3) / 2 * pitch) / 8
    r_maj, r_min = d / 2, d / 2 - h
    f = 5 * pitch / 16
    pts = [(0.0, r_min), (pitch / 8, r_min), (pitch / 8 + f, r_maj), (pitch / 8 + f + pitch / 8, r_maj),
           (pitch / 8 + 2 * f + pitch / 8, r_min), (pitch, r_min)]
    return _close(pts, pitch, f"ISO M{d:g}x{pitch:g}", dict(family="iso", d=d, pitch=pitch))


def trapezoid(d: float, pitch: float, depth: float, beta_lo: float, beta_up: float,
              crest_share: float = 0.5, name: str | None = None) -> Profile:
    """非対称の台形。β_lo は下向きの斜面、β_up は上向きの斜面(度、水平から)。
    斜面の軸方向の幅 depth·tanβ を引いた残りを、山頂の平坦と谷の平坦に crest_share : 1-crest_share で配る。"""
    w_lo = depth * math.tan(math.radians(beta_lo))
    w_up = depth * math.tan(math.radians(beta_up))
    flats = pitch - w_lo - w_up
    if flats < -1e-12:
        raise ValueError(f"斜面だけでピッチを超える(深さ {depth} が深すぎる)")
    crest, root = flats * crest_share, flats * (1 - crest_share)
    r_maj, r_min = d / 2, d / 2 - depth
    a = root / 2
    pts = [(0.0, r_min), (a, r_min), (a + w_lo, r_maj), (a + w_lo + crest, r_maj),
           (a + w_lo + crest + w_up, r_min), (pitch, r_min)]
    nm = name or f"台形 β{beta_lo:g}/{beta_up:g} d{d:g} P{pitch:g} h{depth:g}"
    return _close(pts, pitch, nm, dict(family="trapezoid", d=d, pitch=pitch, depth=depth, beta_lo=beta_lo,
                                        beta_up=beta_up, crest_share=crest_share))


def coil(d: float, pitch: float, rho: float, name: str | None = None) -> Profile:
    """平らな谷と半径 rho の半円の山(Fusion のコイルの円断面を芯の面に中心を置いた形)。山頂の半径が d/2。"""
    if pitch < 2 * rho - 1e-12:
        raise ValueError("ピッチが半円の直径より小さい")
    r_core = d / 2 - rho
    c = pitch / 2
    n = 96
    pts = [(0.0, r_core), (c - rho, r_core)]
    for i in range(1, n):
        a = math.pi - math.pi * i / n
        pts.append((c + rho * math.cos(a), r_core + rho * math.sin(a)))
    pts += [(c + rho, r_core), (pitch, r_core)]
    nm = name or f"半円 d{d:g} P{pitch:g} ρ{rho:g}"
    return _close(pts, pitch, nm, dict(family="coil", d=d, pitch=pitch, rho=rho))


def fillet(profile: Profile, crest_radius: float, root_radius: float, name: str | None = None) -> Profile:
    """折れ線の角を丸める。山頂側の凸の角に crest_radius、谷側の凹の角に root_radius。
    丸めは曲線の形として(s, r 平面で)行い、等間隔に引き直して返す。"""
    s, r = profile.dense()
    P = profile.pitch
    out = r.copy()
    # 凸の角を丸める = 雄の材料を半径 ρ で開く(侵食→膨張)。凹の角 = 閉じる(膨張→侵食)。
    if crest_radius > 0:
        out = Profile(np.append(s, P), np.append(out, out[0]), P, "").offset_normal(-crest_radius).offset_normal(crest_radius).dense()[1]
    if root_radius > 0:
        out = Profile(np.append(s, P), np.append(out, out[0]), P, "").offset_normal(root_radius).offset_normal(-root_radius).dense()[1]
    params = dict(profile.params, crest_radius=crest_radius, root_radius=root_radius)
    return Profile(np.append(s, P), np.append(out, out[0]), P, name or f"{profile.name} 丸めR{crest_radius:g}/{root_radius:g}", params)


def nut_of(bolt: Profile, clearance: float, mode: str = "radial") -> Profile:
    """雄の山形から、隙間 clearance(半径、雌雄の和)の雌の山形を作る。
    radial: 半径方向にずらす(duocad の THREAD.md の定義)。normal: 法線方向に膨らませる(山形の曲線の等距離線)。"""
    if mode == "radial":
        return bolt.shifted(clearance, f"雌({bolt.name}, c{clearance:g})")
    if mode == "normal":
        return bolt.offset_normal(clearance, f"雌({bolt.name}, 法線c{clearance:g})")
    raise ValueError(mode)
