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
    mu: float = 0.3  # 印刷面どうしの摩擦係数(Santana 2021 の 0.12〜0.38 の中ほど)。FEA は摩擦なしで、締め付けの判定だけに使う
    # 既定の印刷(壁2周・zig-zag 充填15%)の構造: 表面から t_wall までは中実の壁、その内側は疎な充填。
    # 充填は層ごとに向きが変わり層間は線の交点でしか接しないので、剛性も層間の強さもごく小さい(AI の仮置き)
    walls: bool = True
    t_wall: float = 0.83  # 外壁 0.42 + 内壁 0.45 − 重なり h(1−π/4)
    shell_bottom: float = 0.6  # 下面の中実層 3層
    shell_top: float = 1.0  # 上面の中実層 5層
    infill_E: float = 0.06  # 充填の剛性(中実比)
    infill_Z: float = 0.03  # 充填の層間の引張・せん断の強さ(中実比)

    def material(self) -> Material:
        G = self.E_p / (2 * (1 + self.nu_p))
        return Material(self.E_p, self.E_z, self.nu_p, self.nu_zp, G)

    def infill_material(self) -> Material:
        f = self.infill_E
        G = self.E_p / (2 * (1 + self.nu_p))
        return Material(f * self.E_p, f * self.E_z, self.nu_p, self.nu_zp, f * G)


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
    h_surf: float = 0.045  # 0.045〜0.02 で破断荷重が 1N も変わらない(実測、M6 相当の2例)
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
    # 多角形は z を dz 刻みで取るので、軸に直角に近い面は dz の幅の斜面になり、t_hi ちょうどでは数µm 食い込む(実測)。
    # dz だけ下げて置く(荷重の最初の dz ぶんで閉じる)。遊びが dz より狭いときは区間の中央まで
    shift = t_hi - min(j.dz, 0.5 * (t_hi - t_lo))
    nut_r0 = j.nut_r
    jj = Joint(j.bolt_r, lambda z: nut_r0(z - shift), j.pitch, j.L_nut, j.R_out, j.r_bear, j.L_below, j.L_above,
               j.dz, j.h_surf, j.h_core)
    bpoly, bid = _bolt_poly(jj)
    npoly, nid = _nut_poly(jj)
    surf_pts = np.vstack([bpoly[1:-1], npoly[4:]])
    size = _size_fn(surf_pts, j.h_surf, j.h_core)
    bmesh = _mesh_or_dump(bpoly, size, j, "bolt")
    nmesh = _mesh_or_dump(npoly, size, j, "nut")
    mats = [m.material(), m.infill_material()]
    bwall = _wall_rule(jj, m, "bolt")
    nwall = _wall_rule(jj, m, "nut")
    bg = np.where(bwall(bmesh.nodes[bmesh.tris[:, :3]].mean(axis=1)), 0, 1)
    ng = np.where(nwall(nmesh.nodes[nmesh.tris[:, :3]].mean(axis=1)), 0, 1)
    bolt = Body(bmesh, mats, [Dirichlet(bid["bottom"], uz=-1.0)], groups=bg)
    nut = Body(nmesh, mats, [Dirichlet(nid["bearing"], uz=0.0)], groups=ng)
    def solve_all(two_pass, search):
        system = ContactSystem([bolt, nut], [ContactPair(0, bid["surface"], 1, nid["surface"])], search_distance=search,
                               allow_penetration=True, max_penetration=0.05, max_candidates=6000,
                               two_pass=two_pass)
        # 節点から最寄りの辺へ測る隙間は、軸に直角に近い棚の角で数µm 負になる(重なっていなくても。実測で最小 -0.004mm)。
        # 10µm までは「最初から接している」として 0 に丸め、件数と量を残す。それより深いものは本当の重なりとして断る
        pen = system.g0 < 0
        if pen.any() and system.g0.min() < -0.01:
            raise ValueError(f"初期の重なり {system.g0.min():.4f}mm")
        pen_info = (int(pen.sum()), float(system.g0.min()) if pen.any() else 0.0)
        system.g0 = np.maximum(system.g0, 0.0)
        # 走査の起点: 置き方の初期の隙間(最大 dz)を閉じて接触が起きるまで試しの変位を倍々にする
        # (0.001mm では接触が無く、そこから推定した走査が極端な変位になっていた。Codex のレビュー)
        def solve_at(d):
            rr = system.solve(float(d))
            return rr, _axial_force(bolt, rr.bodies[0], bid["bottom"])

        d1 = 0.002
        while True:
            r1, F1 = solve_at(d1)
            if F1 > 1e-6 and (r1.multipliers > 0).any():
                break
            d1 *= 2
            if d1 > 1.0:
                raise ValueError("試しの変位を 1mm にしても接触しない")
        tcd = _TcdAverager([bpoly, npoly], [bolt, nut], [r1.bodies[0], r1.bodies[1]], [m.L * 1.32], walls=[bwall, nwall], exclude=dict(
            bolt=lambda p: p[:, 1] < -j.L_below + 1.5 * j.pitch, nut=lambda p: np.hypot(p[:, 0] - j.r_bear, p[:, 1]) < 0.3))
        d2 = 2 * d1
        r2, F2 = solve_at(d2)
        k_ax = max((F2 - F1) / (d2 - d1), 1e-12)  # 接触が閉じた後の軸方向の剛性
        d_close = d1 - F1 / k_ax  # 接触が閉じる変位
        fi2, _, _ = tcd.index(r2, m)
        # 接触が閉じた後は応力 ∝ 荷重で、破壊指数は応力の2次。だいたい F_fail ≈ F2/√FI2。その前後を走査し、FI=1 を挟むまで広げる
        if delta_max is None:
            F_est = F2 / math.sqrt(max(fi2, 1e-30))
            span = F_est / k_ax
            deltas = list(d_close + np.linspace(0.4, 1.6, n_steps) * span)
        else:
            deltas = list(np.linspace(delta_max / n_steps, delta_max, n_steps))
        rows = []
        for d in deltas:
            rr, F = solve_at(d)
            fi, where, mode = tcd.index(rr, m)
            rows.append((float(d), F, fi, where, mode, rr))
        for _ in range(4):
            if rows[0][2] >= 1:  # 最初の段で既に壊れている: 手前へ広げる
                d = d_close + 0.5 * (rows[0][0] - d_close)
                rr, F = solve_at(d)
                fi, where, mode = tcd.index(rr, m)
                rows.insert(0, (float(d), F, fi, where, mode, rr))
            elif rows[-1][2] < 1:  # 最後の段でもまだ壊れない: 先へ広げる
                d = d_close + 1.5 * (rows[-1][0] - d_close)
                rr, F = solve_at(d)
                fi, where, mode = tcd.index(rr, m)
                rows.append((float(d), F, fi, where, mode, rr))
            else:
                break
        return system, pen_info, tcd, rows

    # 両方向の接触の制約がほぼ重複すると、LCP が収束しないことがある(実測 436 設計中 2 件)。そのときは片方向で解き直す
    # 遊びが狭い(固い端)と荷重の掛からない側の斜面も探索距離に入り、接触の候補が上限 6000 を越える(実測: 台形45 D8 P2 normal)。
    # 荷重面の初期の隙間は dz 程度なので、探索距離を縮めて解き直す。遊びがほぼ 0 だと 0.03 でも越える(同、掛かり代 0.70)ので、最後は片方向にする
    def attempt(two_pass):
        for search in (0.15, 0.07, 0.03):
            try:
                return solve_all(two_pass, search) + (search, two_pass)
            except MemoryError:
                if search == 0.03:
                    if two_pass:
                        return attempt(False)
                    raise

    try:
        system, pen_info, tcd, rows, search_used, two_pass_used = attempt(True)
    except RuntimeError:
        system, pen_info, tcd, rows, search_used, two_pass_used = attempt(False)
    lcp_fallback = not two_pass_used
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
    pressure = _contact_pressure(system, last, halve=not lcp_fallback)
    # 平均化しない接触圧が C に届く荷重は、局所の降伏の始まりの目安(メッシュに依存する)。破断の判定には入れない
    F_yield_contact = rows[-1][1] * m.C / max(pressure, 1e-9)
    Fs = dict(tcd=F_tcd, **net)
    beta_eff, slip = _contact_angle_and_slip(system, last, jj)
    # 引張で保持できる荷重の判定は下限側(リングの FEA と山のせん断)で取る。芯の上限 core_slice と締め付けは別に持つ
    mode = min(("tcd", "strip_bolt", "strip_nut"), key=Fs.get)
    Fs["tighten"] = tighten_force(jj.bolt_r(np.linspace(0, j.pitch, 4000, endpoint=False)), j.pitch, beta_eff, F_tcd, m)
    # 乗り越えの目安: 破断荷重まで線形に伸ばしたときの、接触点での雄と雌の半径方向の相対変位 ÷ 掛かり代
    F_last = rows[-1][1]
    engagement = float(jj.bolt_r(np.linspace(0, j.pitch, 2000)).max() - jj.nut_r(np.linspace(0, j.pitch, 2000)).min())
    jump_ratio = slip * (Fs[mode] / max(F_last, 1e-9)) / max(engagement, 1e-9)
    detail = dict(lcp_fallback=lcp_fallback, search_distance=search_used, beta_eff=beta_eff, jump_ratio=jump_ratio, initial_penetration=pen_info, F_yield_contact=F_yield_contact, tcd_mode=mode_tcd, tcd_where=where_tcd, share=share, offset=(t_lo, t_hi), nodes=(len(bmesh.nodes), len(nmesh.nodes)),
                  rows=[(d, F, fi, w, mo) for d, F, fi, w, mo, _ in rows], max_pressure_at_last=pressure,
                  min_angle=(float(bmesh.min_angles().min()), float(nmesh.min_angles().min())))
    if verbose:
        print(detail)
    return Result(Fs, mode, detail)


def _mesh_or_dump(poly, size, j: Joint, name):
    """メッシュに失敗したら、再現用に多角形を output/thread_sim/mesh_fail/ へ残して投げ直す。"""
    try:
        return mesh_polygon(poly, size, j.h_surf * 0.5, j.h_core)
    except ValueError:
        import time
        from pathlib import Path
        d = Path(__file__).resolve().parents[2] / "output" / "thread_sim" / "mesh_fail"
        d.mkdir(parents=True, exist_ok=True)
        np.save(d / f"{name}_{time.time_ns()}.npy", poly)
        raise


def _contact_angle_and_slip(system: ContactSystem, res, j: Joint):
    """接触力で重みを付けた実効の斜面角 β(度、水平から)と、接触点での半径方向の相対変位(雌の外向き − 雄の外向き、mm)。"""
    lam = res.multipliers
    act = lam > lam.max() * 1e-6
    if not act.any():
        return float("nan"), 0.0
    n = res.normals[act]
    w = lam[act]
    beta = float(np.degrees(np.arctan2(np.abs(n[:, 0]), np.abs(n[:, 1]))) @ w / w.sum())
    # 接触点の近くの節点の半径方向の変位(雄は内へ縮み、雌は外へ広がると乗り越えに近づく)
    pts = res.points[act]
    du = []
    for body, br in zip(system.bodies, res.bodies):
        tree = cKDTree(body.mesh.nodes)
        _, idx = tree.query(pts)
        du.append(br.displacement[idx, 0])
    rel = (du[1] - du[0]) @ w / w.sum()
    return beta, float(rel)


def tighten_force(f: np.ndarray, pitch: float, beta_eff: float, F_tension: float, m: PrintedPLA) -> float:
    """締め付けのとき芯が層間で切れる軸力。ねじ山の摩擦トルク T = F·r2·(tanλ + μ/cosβ)(座面の摩擦は頭の側で受けるので芯に載せない)
    によるねじりと引張を (F/F_tension)² + (T/T_allow)² = 1 で組み合わせる。f は1ピッチの雄の山形の半径の標本。

    F_tension は引張だけで壊れる荷重(リングの FEA)。T_allow は壁の帯を薄肉閉断面とみたねじりの強さ 2·Am·t·S に、
    3D の周期模型との比で決めた補正 k = min(1, 1.14 − 0.36·深さ/t_wall) を掛けたもの(D6 の 45° 台形 P1.5/P2.4 と ISO M6 で
    3D/薄肉管 = 0.88・0.72・0.89。深い山ほど壁の帯が波打ってねじりに弱い。docs/THREAD_SIM.md 4章)。"""
    r1, rc = float(f.min()), float(f.max())
    r2 = 0.5 * (r1 + rc)
    lam = math.atan(pitch / (2 * math.pi * r2))
    b = math.radians(beta_eff if math.isfinite(beta_eff) else 30.0)
    K = math.tan(lam) + m.mu / max(math.cos(b), 1e-6)
    if m.walls:
        Am = math.pi * float(np.mean((f - m.t_wall / 2) ** 2))
        k = min(1.0, 1.14 - 0.36 * (rc - r1) / m.t_wall)
        T_allow = k * 2 * Am * m.t_wall * m.S_il
    else:
        T_allow = m.S_il * math.pi * r1 ** 3 / 2
    return 1 / math.hypot(1 / F_tension, r2 * K / T_allow)


def _wall_rule(j: Joint, m: PrintedPLA, which: str):
    """(r, z) が中実の壁(と上下の中実層)に入るかの判定。印刷では壁は XY の断面の輪郭に沿うが、
    ほぼ円の断面なので半径方向に t_wall とみなす。"""
    if not m.walls:
        return lambda p: np.ones(len(p), bool)
    if which == "bolt":
        return lambda p: p[:, 0] > j.bolt_r(p[:, 1]) - m.t_wall
    return lambda p: ((p[:, 0] < j.nut_r(p[:, 1]) + m.t_wall) | (p[:, 0] > j.R_out - m.t_wall)
                      | (p[:, 1] < m.shell_bottom) | (p[:, 1] > j.L_nut - m.shell_top))


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


def _contact_pressure(system: ContactSystem, res, halve: bool = True) -> float:
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
    # 両方向の制約で同じ接触が2回数えられるので半分にする(片方向に切り替えたときは割らない)
    return float(np.percentile(press, 99) / (2 if halve else 1))


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
    # 芯: 螺旋の水平断面(どの高さでも同じ形が回るだけ)の面積で、応力が一様とみた上限の見積もり。
    # 壁のモデルでは、断面の輪郭に沿ってカム形に回る壁の帯 + 内側の充填(強さ infill_Z)。
    # 軸対称のリング模型(FEA)は全周が同時に谷になる断面を作るので下限側になる。真の値はその間
    zz = np.linspace(0, P, 4000, endpoint=False)
    f = j.bolt_r(zz)
    if m.walls:
        fin = np.maximum(f - m.t_wall, 0.0)
        A_core = math.pi * (np.mean(f ** 2) - np.mean(fin ** 2)) + m.infill_Z * math.pi * np.mean(fin ** 2)
    else:
        A_core = math.pi * float(np.mean(f ** 2))
    A_sb = 2 * math.pi * r_crest_n * P * frac_b * n_threads
    A_sn = 2 * math.pi * r_crest_b * P * frac_n * n_threads
    return dict(core_slice=m.Z_t * A_core, strip_bolt=m.S_il * A_sb, strip_nut=m.S_il * A_sn)


class _TcdAverager:
    """面積法の TCD: 各点のまわり半径 R の円内(同じ物体の材料だけ)で応力テンソルを面積で平均する。

    物体ごとに r-z の正方格子を張り、ガウス点の応力を最近傍で格子へ写し、材料の指示関数と一緒に円板で畳み込む
    (平均 = conv(σ·χ) / conv(χ))。格子の間隔は R/6 と 0.02mm の小さい方。"""

    def __init__(self, polys, bodies, results, radii, exclude, walls=(None, None)):
        from scipy.signal import fftconvolve  # noqa: F401  (使えることを先に確かめる)
        from .mesh import _inside, _ring
        self.radii = list(radii)
        self.grids = []
        g = min(0.02, min(self.radii) / 6)
        for name, poly, res, wall in zip(("bolt", "nut"), polys, results, walls):
            ring = _ring(poly)
            r0, z0 = ring.min(axis=0)
            r1, z1 = ring.max(axis=0)
            rr = np.arange(r0 + g / 2, r1, g)
            zz = np.arange(z0 + g / 2, z1, g)
            Rg, Zg = np.meshgrid(rr, zz, indexing="ij")
            pts = np.column_stack([Rg.ravel(), Zg.ravel()])
            inside = _inside(pts, [ring])
            if wall is not None:  # 平均は中実の壁の中だけで取る(充填の強さは見込まない)
                inside &= wall(pts)
            mask = inside.reshape(Rg.shape)
            gp = res.position.reshape(-1, 2)
            _, nn = cKDTree(gp).query(pts[mask.ravel()])
            ex = exclude[name](pts[mask.ravel()])
            self.grids.append(dict(name=name, shape=Rg.shape, mask=mask, nn=nn, ex=ex, pts=pts[mask.ravel()], g=g))

    def index(self, contact_result, m: PrintedPLA, R=None):
        from scipy.signal import fftconvolve
        R = self.radii[0] if R is None else R
        best, where, mode = -1.0, None, None
        for G, res in zip(self.grids, contact_result.bodies):
            g = G["g"]
            k = int(math.ceil(R / g))
            yy, xx = np.mgrid[-k:k + 1, -k:k + 1]
            K = ((xx * g) ** 2 + (yy * g) ** 2 <= R * R).astype(float)
            chi = G["mask"].astype(float)
            den = fftconvolve(chi, K, mode="same")[G["mask"]]
            s_gp = res.stress.reshape(-1, 4)[G["nn"]]
            avg = np.empty_like(s_gp)
            for c in range(4):
                F = np.zeros(G["shape"])
                F[G["mask"]] = s_gp[:, c]
                avg[:, c] = fftconvolve(F, K, mode="same")[G["mask"]] / den
            sr, sz, st_, trz = avg.T
            pos = lambda x: np.maximum(x, 0.0)
            fi = dict(
                interlayer=(pos(sz) / m.Z_t) ** 2 + (trz / m.S_il) ** 2,
                interbead=(pos(sr) / m.Y_t) ** 2 + (trz / m.S_il) ** 2,
                hoop=(pos(st_) / m.X_t) ** 2,
            )
            c_ = 0.5 * (sr + sz)
            rad = np.hypot(0.5 * (sr - sz), trz)
            smin = np.minimum(c_ - rad, st_)
            fi["crush"] = (pos(-smin) / m.C) ** 2
            for md, v in fi.items():
                v = np.where(G["ex"], -1.0, v)
                i = int(np.argmax(v))
                if v[i] > best:
                    best, where, mode = float(v[i]), (G["name"], float(G["pts"][i, 0]), float(G["pts"][i, 1])), md
        return best, where, mode
