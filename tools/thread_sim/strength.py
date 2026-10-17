"""印刷したボルトとナットの組の、軸方向の引張に対する強さ(軸対称 FEA)。

組み付けの座標(profiles の約束と同じ): ナットは z ∈ [0, L_nut]、座面は下面 z=0 の外側の円環で u_z=0。
ボルトは下端 z=-L_below を u_z=-Δ で引き下げる(変位制御)。ボルトが下へ引かれるので、ボルトの山の下向きの面
(軸鉛直・頭を下にして刷ると張り出しになる面)がナットの山の上向きの面に当たる。

山の断面は、印刷の模擬から取った**位相ごとの平均の山形**(θ で平均したもの)をリング状の山として使う
(astra の指摘: 層の段を軸対称に持ち込むと全周につながる切欠きになるので、段つきは粗さの感度にだけ使う)。

破壊は4つの判定を並べる(どれか1つを真とせず、食い違いは「順位未確定」の材料にする):
1. 正味断面: ボルトの芯の層間引張、ボルトの山の付け根のせん断、ナットの山の付け根のせん断(荷重は全山で一様と仮定)。
2. FEA の局所応力を臨界距離(TCD の面積法: 半径 R_A の円内で応力テンソルを平均)で評価した異方性の破壊指数。
3. 接触圧(圧潰)。
4. 乗り越えの警報: 接触点の半径方向の相対変位が掛かり代の半分を超える(線形小変形なので警報止まり)。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from scipy.sparse import csr_matrix
from scipy.spatial import cKDTree

from .fea import Body, ContactPair, ContactSystem, Dirichlet, Material
from .mesh import mesh_polygon


@dataclass(frozen=True)
class PrintedPLA:
    """軸鉛直に刷った PLA。層の法線 = z。値は文献の幅の中ほど(出典と幅は docs/THREAD_SIM.md)。単位 MPa・mm。"""

    E_p: float = 3200.0  # 層の面内
    E_z: float = 2900.0  # 層の法線
    nu_p: float = 0.33
    nu_zp: float = 0.33
    X_t: float = 50.0  # ビードに沿う引張(周方向 θ)
    Y_t: float = 40.0  # 層の面内でビードどうしを剥がす引張(半径 r)
    Z_t: float = 30.0  # 層間の引張(軸 z)
    S_il: float = 25.0  # 層間・ビード間のせん断
    C: float = 70.0  # 圧縮(圧潰)
    L: float = 0.5  # TCD の臨界距離。面積法の半径 R_A = 1.32·L(Taylor)

    def material(self) -> Material:
        G = self.E_p / (2 * (1 + self.nu_p))
        return Material(self.E_p, self.E_z, self.nu_p, self.nu_zp, G)


@dataclass
class Joint:
    bolt_r: callable  # z → ボルトの表面の半径(組み付けの座標、位相の山形を z で評価したもの)
    nut_r: callable  # z → ナットの穴の半径
    pitch: float
    L_nut: float
    R_out: float  # ナットの外半径
    r_bear: float  # 座面の内半径(これより外の下面を u_z=0)
    L_below: float = None
    L_above: float = None
    dz: float = 0.01
    h_surf: float = 0.03
    h_core: float = 0.35

    def __post_init__(self):
        self.L_below = 2 * self.pitch if self.L_below is None else self.L_below
        self.L_above = 1 * self.pitch if self.L_above is None else self.L_above


def _simplify(z, r, tol=1e-4):
    """ほぼ一直線に並ぶ頂点を間引く(Douglas–Peucker)。"""
    pts = np.column_stack([r, z])
    keep = np.zeros(len(pts), bool)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        if b <= a + 1:
            continue
        p, q = pts[a], pts[b]
        d = q - p
        n = np.hypot(*d)
        seg = pts[a + 1:b]
        dist = np.abs(d[0] * (seg[:, 1] - p[1]) - d[1] * (seg[:, 0] - p[0])) / max(n, 1e-15)
        i = int(np.argmax(dist))
        if dist[i] > tol:
            keep[a + 1 + i] = True
            stack += [(a, a + 1 + i), (a + 1 + i, b)]
    return z[keep], r[keep]


def _bolt_poly(j: Joint):
    z0, z1 = -j.L_below, j.L_nut + j.L_above
    z = np.arange(z0, z1 + 1e-9, j.dz)
    z, r = _simplify(z, j.bolt_r(z))
    surf = np.column_stack([r, z])
    poly = np.vstack([[0.0, z0], surf, [0.0, z1]])
    n = len(poly)
    # 辺番号: 0 = 下面、1..n-3 = 表面、n-2 = 上面、n-1 = 軸
    return poly, dict(bottom={0}, surface=set(range(1, n - 2)), top={n - 2}, axis={n - 1})


def _nut_poly(j: Joint):
    z = np.arange(0.0, j.L_nut + 1e-9, j.dz)
    z, r = _simplify(z, j.nut_r(z))
    bore = np.column_stack([r, z])[::-1]  # 上から下へ
    poly = np.vstack([[bore[-1, 0], 0.0], [j.r_bear, 0.0], [j.R_out, 0.0], [j.R_out, j.L_nut], bore[:-1]])
    n = len(poly)
    # 0 = 下面の内側(自由)、1 = 座面、2 = 外周、3 = 上面、4..n-1 = 穴の面
    return poly, dict(bottom_free={0}, bearing={1}, outer={2}, top={3}, surface=set(range(4, n)))


def _size_fn(surface_pts, h_surf, h_core, grow=0.25):
    tree = cKDTree(surface_pts)

    def f(p):
        d, _ = tree.query(p)
        return h_surf + grow * d

    return f


def nominal_offset(j: Joint, n: int = 4000):
    """ナットの穴の山形を軸方向に t ずらしたとき、ボルトと重ならない t の範囲 [t_lo, t_hi](重なりは r の比較で見る)。
    ボルトを下へ引く(=ナットが相対的に上へ動いて荷重面が閉じる)ので、荷重のかかる配置は t_hi。"""
    P = j.pitch
    z = np.linspace(0, P, n, endpoint=False)
    rb = j.bolt_r(z)
    ts = np.linspace(-P / 2, P / 2, 2001)
    ok = np.array([np.all(rb < j.nut_r(z - t)) for t in ts])
    if not ok.any():
        raise ValueError("どの軸方向の位置でもボルトとナットが重なる(隙間が足りない)")
    i = int(np.argmax(ok))
    best = None
    run_lo = None
    for k in range(len(ts)):
        if ok[k] and run_lo is None:
            run_lo = k
        if (not ok[k] or k == len(ts) - 1) and run_lo is not None:
            hi = k if ok[k] else k - 1
            if best is None or hi - run_lo > best[1] - best[0]:
                best = (run_lo, hi)
            run_lo = None
    return ts[best[0]], ts[best[1]]


@dataclass
class Result:
    F: dict  # 判定ごとの破断荷重(N)
    mode: str
    detail: dict = field(default_factory=dict)


def analyze(j: Joint, m: PrintedPLA, n_steps: int = 6, delta_max: float = None, verbose=False) -> Result:
    t_lo, t_hi = nominal_offset(j)
    shift = t_hi - 1e-4
    nut_r0 = j.nut_r
    jj = Joint(j.bolt_r, lambda z: nut_r0(z - shift), j.pitch, j.L_nut, j.R_out, j.r_bear, j.L_below, j.L_above,
               j.dz, j.h_surf, j.h_core)
    bpoly, bid = _bolt_poly(jj)
    npoly, nid = _nut_poly(jj)
    surf_pts = np.vstack([bpoly[1:-1], npoly[4:]])
    size = _size_fn(surf_pts, j.h_surf, j.h_core)
    bmesh = mesh_polygon(bpoly, size, j.h_surf * 0.5, j.h_core)
    nmesh = mesh_polygon(npoly, size, j.h_surf * 0.5, j.h_core)
    mat = m.material()
    bolt = Body(bmesh, mat, [Dirichlet(bid["bottom"], uz=-1.0)])
    nut = Body(nmesh, mat, [Dirichlet(nid["bearing"], uz=0.0)])
    system = ContactSystem([bolt, nut], [ContactPair(0, bid["surface"], 1, nid["surface"])], search_distance=0.15)
    # 1回の解で荷重のおおよその剛性を取り、破壊までの範囲を走査する
    probe = 1e-3
    r = system.solve(probe)
    F_probe = _axial_force(bolt, r.bodies[0], bid["bottom"])
    tcd = _TcdAverager([bolt, nut], [r.bodies[0], r.bodies[1]], m.L * 1.32, j, exclude=dict(
        bolt=lambda p: p[:, 1] < -j.L_below + 1.5 * j.pitch, nut=lambda p: np.hypot(p[:, 0] - j.r_bear, p[:, 1]) < 0.3))
    fi_probe, _, _ = tcd.index(r, m)
    # 破壊指数は応力の2次なので、だいたい Δ ∝ 1/√FI。そこを中心に走査する(単調とは限らないので二分探索はしない)
    d_est = probe / math.sqrt(max(fi_probe, 1e-30))
    deltas = np.linspace(0.25, 1.6, n_steps) * d_est if delta_max is None else np.linspace(delta_max / n_steps, delta_max, n_steps)
    rows = []
    for d in deltas:
        rr = system.solve(float(d))
        F = _axial_force(bolt, rr.bodies[0], bid["bottom"])
        fi, where, mode = tcd.index(rr, m)
        rows.append((float(d), F, fi, where, mode, rr))
    # FI=1 を線形補間で(FI の平方根は荷重にほぼ比例)
    F_tcd, mode_tcd, where_tcd = None, None, None
    for (d0, F0, fi0, *_), (d1, F1, fi1, w1, mo1, _) in zip(rows, rows[1:]):
        if fi0 < 1 <= fi1:
            a = (1 - math.sqrt(fi0)) / (math.sqrt(fi1) - math.sqrt(fi0))
            F_tcd, mode_tcd, where_tcd = F0 + a * (F1 - F0), mo1, w1
            break
    if F_tcd is None and rows[0][2] >= 1:
        F_tcd, mode_tcd, where_tcd = rows[0][1] / math.sqrt(rows[0][2]), rows[0][4], rows[0][3]
    if F_tcd is None:
        F_tcd, mode_tcd, where_tcd = rows[-1][1] / math.sqrt(rows[-1][2]), rows[-1][4], rows[-1][3]
    # 荷重分担(最後の段で、1ピッチごとの接触力の和)
    last = rows[-1][5]
    share = _thread_share(last, j)
    net = _net_section(jj, m)
    pressure = _contact_pressure(system, last)
    F_crush = rows[-1][1] * m.C / max(pressure, 1e-9)
    Fs = dict(tcd=F_tcd, **net, crush=F_crush)
    mode = min(Fs, key=Fs.get)
    detail = dict(tcd_mode=mode_tcd, tcd_where=where_tcd, share=share, offset=(t_lo, t_hi), nodes=(len(bmesh.nodes), len(nmesh.nodes)),
                  rows=[(d, F, fi, w, mo) for d, F, fi, w, mo, _ in rows], max_pressure_at_last=pressure,
                  min_angle=(float(bmesh.min_angles().min()), float(nmesh.min_angles().min())))
    if verbose:
        print(detail)
    return Result(Fs, mode, detail)


def _axial_force(body: Body, res, edges):
    nodes = body.mesh.boundary_nodes(edges)
    return float(-res.reaction[nodes, 1].sum())


def _thread_share(res: "ContactResult", j: Joint):
    lam = res.multipliers
    z = res.points[:, 1]
    n = int(math.ceil(j.L_nut / j.pitch))
    bins = np.clip(np.floor(z / j.pitch).astype(int), 0, n - 1)
    # 接触力はリングの力(法線方向)。軸方向の成分で分担を見る
    fz = lam * np.abs(res.normals[:, 1])
    tot = np.bincount(bins, weights=fz, minlength=n)
    return (tot / max(tot.sum(), 1e-30)).tolist()


def _contact_pressure(system: ContactSystem, res) -> float:
    """接触力(リングの力)を、接触点の間隔と 2πr で割った最大の面圧(MPa)。"""
    lam = res.multipliers
    act = lam > lam.max() * 1e-6
    if not act.any():
        return 0.0
    p = res.points
    order = np.argsort(p[act, 1])
    pts = p[act][order]
    seg = np.hypot(np.gradient(pts[:, 0]), np.gradient(pts[:, 1]))
    seg = np.maximum(seg, 1e-3)
    press = lam[act][order] / (2 * math.pi * np.maximum(pts[:, 0], 1e-6) * seg)
    # 両方向の制約で同じ接触が2回数えられるので半分にする
    return float(np.percentile(press, 99) / 2)


def _net_section(j: Joint, m: PrintedPLA) -> dict:
    """正味断面の破断荷重(荷重は全山で一様と仮定)。"""
    P = j.pitch
    z = np.linspace(0, P, 4000, endpoint=False)
    rb, rn = j.bolt_r(z), j.nut_r(z)
    r_root_b = float(rb.min())  # ボルトの谷
    r_crest_n = float(rn.min())  # ナットの山頂(穴の最小半径)
    r_crest_b = float(rb.max())
    # ボルトの山の付け根: ナットの山頂の半径の円筒でボルトの山を切る面。材料は r_crest_n > ... ではなく
    # ボルトの材料(rb > r)が占める軸方向の割合
    frac_b = float((rb > r_crest_n).mean())
    frac_n = float((rn < r_crest_b).mean())
    n_threads = j.L_nut / P
    A_core = math.pi * r_root_b ** 2
    A_sb = 2 * math.pi * r_crest_n * P * frac_b * n_threads
    A_sn = 2 * math.pi * r_crest_b * P * frac_n * n_threads
    return dict(core=m.Z_t * A_core, strip_bolt=m.S_il * A_sb, strip_nut=m.S_il * A_sn)


class _TcdAverager:
    """面積法の TCD: 各中心点のまわり半径 R の円内(同じ物体の材料だけ)で応力テンソルを面積で平均する。"""

    def __init__(self, bodies, results, R, j: Joint, exclude):
        self.R = R
        self.mats = []
        self.centers = []
        for name, body, res in zip(("bolt", "nut"), bodies, results):
            pos = res.position.reshape(-1, 2)
            area = res.area.reshape(-1)
            tree = cKDTree(pos)
            # 中心は表面から R 以内のガウス点を間引いたもの + 芯の中央の列
            cand = pos[::3]
            ex = exclude[name](cand)
            cand = cand[~ex]
            nb = tree.query_ball_point(cand, R)
            rows, cols, vals = [], [], []
            for i, ids in enumerate(nb):
                w = area[ids]
                rows += [i] * len(ids)
                cols += ids
                vals += (w / w.sum()).tolist()
            W = csr_matrix((vals, (rows, cols)), shape=(len(cand), len(pos)))
            self.mats.append(W)
            self.centers.append(cand)

    def index(self, contact_result, m: PrintedPLA):
        best, where, mode = -1.0, None, None
        for k, (name, W, cen, res) in enumerate(zip(("bolt", "nut"), self.mats, self.centers, contact_result.bodies)):
            s = W @ res.stress.reshape(-1, 4)
            sr, sz, st, trz = s.T
            pos = lambda x: np.maximum(x, 0.0)
            fi = dict(
                interlayer=(pos(sz) / m.Z_t) ** 2 + (trz / m.S_il) ** 2,
                interbead=(pos(sr) / m.Y_t) ** 2 + (trz / m.S_il) ** 2,
                hoop=(pos(st) / m.X_t) ** 2,
            )
            c = 0.5 * (sr + sz)
            rad = np.hypot(0.5 * (sr - sz), trz)
            smin = np.minimum(c - rad, st)
            fi["crush"] = (pos(-smin) / m.C) ** 2
            for md, v in fi.items():
                i = int(np.argmax(v))
                if v[i] > best:
                    best, where, mode = float(v[i]), (name, float(cen[i, 0]), float(cen[i, 1])), md
        return best, where, mode
