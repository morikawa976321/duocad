"""3D SIMP トポロジー最適化 (片持ち梁)。"""

from __future__ import annotations

import contextlib
import os
import time
import warnings
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import numpy as np
import scipy.sparse as sp
import scipy.sparse.linalg as spla

# 解いた後に残差で検算するときの上限(相対)。CG は `cg_rtol=1e-9` まで詰め、直接解法は
# 1e-14 程度まで出るので、1e-6 は「収束した解」に対して6桁以上の余裕がある。
# **拘束不足の系を黙って通さないための関門**であって、精度の要求ではない。
SOLVE_RESIDUAL_TOL = 1e-6

# ---------------------------------------------------------------------------
# 性能計測の口
# ---------------------------------------------------------------------------
# `Model.solve` が反復数と各段の秒数を積む先。**None のときは何も積まず、callback も渡さない**
# ので、本番経路の数値は1ビットも変わらない(`check_absolute_volume.py` [4] がそれを見ている)。
#
# **外から `scipy.sparse.linalg.cg` を差し替えて数える方式を採らない。** 解法を差し替えた
# 瞬間に数え口が外れ、反復数が黙って 0 になる —— マルチグリッド前処理を入れるタスクが
# まさにそこを差し替えるので、数え口は解法と同じ場所に置く。
_SOLVE_STATS: dict | None = None


def new_solve_stats() -> dict:
    """`collect_solve_stats` が積む器。鍵を1か所に置くためだけの関数。"""
    return {
        "solves": 0,
        "cg_iterations": 0,
        "cg_iterations_each": [],
        "assemble_seconds": 0.0,
        "slice_seconds": 0.0,
        "solve_seconds": 0.0,
        # `solve_seconds` の内数。マルチグリッドは密度が変わるたびに各段の作用素(RAP)と
        # 最粗格子の LU を組み直すので、その費用だけ別に見えるようにしてある。
        "mg_setup_seconds": 0.0,
        "fallbacks": 0,
        "direct_solves": 0,
    }


@contextlib.contextmanager
def collect_solve_stats() -> Iterator[dict]:
    """この文脈の中で `Model.solve` が積んだ実測を返す。**入れ子にしない**(後勝ちになる)。

    `cg_iterations_each` は solve 1回ごとの反復数。合計や平均だけだと、設計反復が進んで
    密度が二値へ寄るにつれ条件数が悪化する分が見えない。
    """
    global _SOLVE_STATS
    previous = _SOLVE_STATS
    stats = new_solve_stats()
    _SOLVE_STATS = stats
    try:
        yield stats
    finally:
        _SOLVE_STATS = previous


# `volfrac` も絶対量も渡さなかったときの分率。**PRESET の volfrac とは別物**
# (PRESET は片持ち梁の題材、こちらは `optimize` の既定値)。値は歴史的に一致している。
DEFAULT_VOLFRAC = 0.3

# 片持ち梁の基準題材。**受入検査3本がこれを回すので、大きさがそのまま検証の1周の長さになる。**
# 40x16x16(1周およそ11分)から 20x8x8 へ落としてある(2026-09-04)。**反復数は 60 のまま** ——
# 30反復まで落とすと終盤10反復の変動が 2.47% になり、`check_simp.py` (b) の1%を割る。
# 消してはならないが、検査の基準として使い続ける限り寸法と反復数は変えてよい
# (engine `cantilever-preset-is-the-regression-baseline-not-a-demo`)。
# **`nelz` は偶数**(奇数だと `_build_load` が荷重を2節点へ割り、z対称性の検査の意味が変わる)。
PRESET: dict = {
    "nelx": 20,
    "nely": 8,
    "nelz": 8,
    "volfrac": 0.3,
    "penal": 3.0,
    "rmin": 1.5,
    "max_iter": 60,
    "change_tol": 0.01,
    "E": 1.0,
    "nu": 0.3,
    "emin": 1e-9,
    "move": 0.2,
    "solver": "cg",
    "cg_rtol": 1e-9,
}


def element_stiffness(E: float = 1.0, nu: float = 0.3) -> np.ndarray:
    """一辺1の8節点六面体(trilinear hex)の要素剛性行列 (24,24) を返す。"""
    lam_nodes = np.array(
        [[2 * a - 1, 2 * b - 1, 2 * c - 1] for a in (0, 1) for b in (0, 1) for c in (0, 1)],
        float,
    )
    d = np.zeros((6, 6))
    c1 = E / ((1.0 + nu) * (1.0 - 2.0 * nu))
    d[:3, :3] = c1 * (nu * np.ones((3, 3)) + (1.0 - 2.0 * nu) * np.eye(3))
    d[3:, 3:] = c1 * (1.0 - 2.0 * nu) / 2.0 * np.eye(3)
    g = 1.0 / np.sqrt(3.0)
    ke = np.zeros((24, 24))
    for xi in (-g, g):
        for eta in (-g, g):
            for zeta in (-g, g):
                dn = np.empty((8, 3))
                for l in range(8):
                    s = lam_nodes[l]
                    dn[l, 0] = 0.125 * s[0] * (1.0 + s[1] * eta) * (1.0 + s[2] * zeta)
                    dn[l, 1] = 0.125 * (1.0 + s[0] * xi) * s[1] * (1.0 + s[2] * zeta)
                    dn[l, 2] = 0.125 * (1.0 + s[0] * xi) * (1.0 + s[1] * eta) * s[2]
                dndx = dn * 2.0
                det_j = 0.125
                b = np.zeros((6, 24))
                for l in range(8):
                    bx, by, bz = dndx[l]
                    b[0, 3 * l + 0] = bx
                    b[1, 3 * l + 1] = by
                    b[2, 3 * l + 2] = bz
                    b[3, 3 * l + 0] = by
                    b[3, 3 * l + 1] = bx
                    b[4, 3 * l + 1] = bz
                    b[4, 3 * l + 2] = by
                    b[5, 3 * l + 0] = bz
                    b[5, 3 * l + 2] = bx
                ke += b.T @ d @ b * det_j
    return ke


def _build_edof(nelx: int, nely: int, nelz: int) -> np.ndarray:
    """要素DOF写像 (nele, 24) を返す。"""
    nz1 = nelz + 1
    ny1 = nely + 1
    ex, ey, ez = np.meshgrid(
        np.arange(nelx), np.arange(nely), np.arange(nelz), indexing="ij"
    )
    ex = ex.ravel()
    ey = ey.ravel()
    ez = ez.ravel()
    ed = np.empty((ex.size, 24), dtype=np.int32)
    l = 0
    for a in (0, 1):
        for b in (0, 1):
            for c in (0, 1):
                n = (ex + a) * ny1 * nz1 + (ey + b) * nz1 + (ez + c)
                ed[:, 3 * l + 0] = 3 * n
                ed[:, 3 * l + 1] = 3 * n + 1
                ed[:, 3 * l + 2] = 3 * n + 2
                l += 1
    return ed


def _build_load(nelx: int, nely: int, nelz: int, ndof: int) -> np.ndarray:
    """片持ち梁の荷重ベクトルを組み立てる(z方向対称)。"""
    f = np.zeros(ndof)
    ny1 = nely + 1
    nz1 = nelz + 1
    if nelz % 2 == 0:
        k_nodes = [nelz // 2]
        magnitudes = [1.0]
    else:
        k_nodes = [nelz // 2, nelz // 2 + 1]
        magnitudes = [0.5, 0.5]
    for k, mag in zip(k_nodes, magnitudes, strict=True):
        n = nelx * ny1 * nz1 + nely * nz1 + k
        f[3 * n + 1] = -mag
    return f


def _filter_kernel(rmin: float) -> tuple[np.ndarray, int]:
    """密度フィルタのカーネルと半径(整数)を返す。"""
    r = int(np.ceil(rmin)) - 1
    rng = np.arange(-r, r + 1)
    dx, dy, dz = np.meshgrid(rng, rng, rng, indexing="ij")
    dist = np.sqrt(dx**2 + dy**2 + dz**2)
    return np.maximum(0.0, rmin - dist), r


def _filter_conv(field: np.ndarray, kernel: np.ndarray, r: int) -> np.ndarray:
    """3次元シフト加算による線形重み畳み込み。"""
    out = np.zeros_like(field)
    n = 2 * r + 1
    for a in range(n):
        for b in range(n):
            for c in range(n):
                w = kernel[a, b, c]
                if w == 0.0:
                    continue
                da, db, dc = a - r, b - r, c - r
                s_src = tuple(
                    slice(max(0, -d), field.shape[i] + min(0, -d))
                    for i, d in enumerate((da, db, dc))
                )
                s_dst = tuple(
                    slice(max(0, d), field.shape[i] + min(0, d))
                    for i, d in enumerate((da, db, dc))
                )
                out[s_dst] += w * field[s_src]
    return out


# ---------------------------------------------------------------------------
# 幾何マルチグリッド前処理 (solver="mg")
# ---------------------------------------------------------------------------
# **なぜこれが要るか**: 対角(Jacobi)前処理では CG の反復数が格子の細かさに比例して増える
# (条件数 ∝ h^-2、CG反復 ∝ √条件数 ∝ h^-1)。1反復の費用が N に比例するので合計は N^(4/3)
# になる。マルチグリッドは反復数を格子非依存にするので、**指数が 4/3 から 1 へ落ちる。**
#
# 決めた3点(選択肢があるところ。理由は README「マルチグリッド前処理」節にも書いてある):
#
# 1. **粗格子作用素は Galerkin (A_c = P^T A P)。密度を粗格子へ移さない。**
#    `emin=1e-9` と `penal=3` で実効剛性は 10^9 倍開くので、密度を算術平均で落とすと
#    空の領域が硬くなりすぎて粗格子が別の問題になる(調和平均・最小値でも「どれだけ
#    間違えるか」が変わるだけ)。**RAP は近似ではなく、細格子の作用素そのものを粗空間へ
#    射影した唯一の対称作用素**なので、移し方を選ぶ問題自体が消える。加えて、拘束
#    (固定DOF)と浮いた節点の扱いが自動的に引き継がれる —— 粗格子で境界条件を組み直す
#    必要がない。代償は毎 solve の RAP 費用(実測は README の表)。
# 2. **平滑化は damped Jacobi。** Gauss-Seidel より1回あたりの効きは弱いが、
#    行列ベクトル積と要素ごとの積だけで書けるので `20260904-1415`(CPU並列)がそのまま
#    載せられる。減衰係数は下記のとおり**行ごとの絶対和(Gershgorin)から決める**。
# 3. **粗視化は軸ごとに独立**(`_coarse_axis`)。要素数が `min_axis_elements`(既定4)
#    未満の軸は間引かない —— 薄板題材では厚み方向が先に潰れるので、**等方に間引き続けると
#    粗格子が形をなさなくなる**。この規則は自動的に半粗視化になる(薄い軸だけ残る)。
#    要素数が奇数の軸も扱える: 粗節点を 0,2,4,... と採り、端が合わなければ最後の1つだけ
#    幅1の粗セルにする。**格子寸法が2の冪でない**(ボクセル化が決めるので当然そうなる)
#    ので、偶数しか扱えない実装では2段目でほぼ必ず止まる。


@dataclass(frozen=True)
class MGOptions:
    """V-cycle の形。**既定以外の値は主に検出力の検査(`check_multigrid.py`)のためにある。**

    `coarse_correction=False` / `max_levels=1` / `weight_scale` を下げる、は前処理を意図的に
    劣化させる口で、これで反復数が増えないなら**その部品は最初から効いていない**。

    既定値の根拠(片持ち梁 30x12x12〜60x24x24 の実密度で実測。README「マルチグリッド前処理」):

    - `smooth_steps=2` —— 1 にすると反復が 1.25倍、3 にしても 1割しか減らず1回が重くなる。
    - `smooth_growth=2` —— **粗い段ほど平滑化を増やす。** SIMP の 10^9 の剛性差のせいで
      粗格子の補正が段ごとに劣化するのを、粗い段の安い平滑化で買い戻す
      (60x24x24 で 37 → 34反復、秒はむしろ短い。粗い段の非零は細い段の 13% しかない)。
    - `cycle_gamma=1`(V-cycle)—— 2(W-cycle)にすると反復は 34 → 30 に減るが、
      **秒は 2.39 → 3.30 と伸びる**ので既定にしない。
    - `direct_dofs=6000` —— 最粗格子を直接解法(`splu`)で解く上限。ここを上げると2段で
      済んで反復は 19 まで落ちるが、**`splu` の費用が急に効く**(粗15,210自由度で 0.96秒/solve・
      充填 1,680万。粗2,205自由度なら 0.04秒)。6000 は「splu が 0.1秒未満に収まる最大の段」。
    """

    smooth_steps: int = 2
    smooth_growth: int = 2
    cycle_gamma: int = 1
    coarse_correction: bool = True
    max_levels: int = 8
    min_axis_elements: int = 4
    direct_dofs: int = 6000
    weight_scale: float = 1.0


MG_DEFAULT = MGOptions()


def _coarse_axis(n_elem: int, min_axis_elements: int) -> np.ndarray | None:
    """1軸の粗節点位置(細格子の節点番号)。間引かないと決めたら None。

    要素数が `min_axis_elements` 未満の軸は間引かない。既定の 4 は「粗格子でも各軸に
    2要素以上残す」という意味で、**1要素まで潰れた軸は曲げを表現できない**。
    """
    if n_elem < max(2, int(min_axis_elements)):
        return None
    pos = np.arange(0, n_elem + 1, 2)
    if pos[-1] != n_elem:
        pos = np.append(pos, n_elem)
    return pos


def _interp_axis(n_elem: int, pos: np.ndarray) -> sp.csr_matrix:
    """1軸の線形補間 (n_elem+1, len(pos))。`pos` が全節点なら単位行列になる。"""
    fine = np.arange(n_elem + 1)
    idx = np.clip(np.searchsorted(pos, fine, side="right") - 1, 0, pos.size - 2)
    left = pos[idx].astype(float)
    right = pos[idx + 1].astype(float)
    w = (fine - left) / (right - left)
    m = sp.coo_matrix(
        (
            np.concatenate([1.0 - w, w]),
            (np.concatenate([fine, fine]), np.concatenate([idx, idx + 1])),
        ),
        shape=(n_elem + 1, pos.size),
    ).tocsr()
    m.eliminate_zeros()
    return m


def _prolongation(dims: tuple[int, int, int], pos: list[np.ndarray]) -> sp.csr_matrix:
    """粗→細の補間 P(全自由度)。節点番号の規約は `_build_edof` と同じ。"""
    node = sp.kron(
        _interp_axis(dims[0], pos[0]),
        sp.kron(_interp_axis(dims[1], pos[1]), _interp_axis(dims[2], pos[2]), format="csr"),
        format="csr",
    )
    return sp.kron(node, sp.identity(3, format="csr"), format="csr").tocsr()


def build_mg_transfers(
    dims: tuple[int, int, int], free: np.ndarray, opts: MGOptions | None = None
) -> list[sp.csr_matrix]:
    """階層の補間作用素を粗い側へ向かって並べて返す。**密度に依らない**(格子と拘束だけ)。

    各 P は「その段の自由DOF × 次の段の自由DOF」。粗格子の自由DOFは**細格子で自由だった
    節点に一致する粗節点**だけ採る —— こうすると P の該当行は単位行列になるので、
    P は必ず**列フルランク**になる。したがって `A` が正定値である限り `P^T A P` も正定値
    (`x != 0` なら `P x != 0`)。**`A` 自体が特異なとき(拘束不足)は保証の外**で、そちらは
    `Model.solve` の残差検査が落とす担当。
    """
    opts = MG_DEFAULT if opts is None else opts
    transfers: list[sp.csr_matrix] = []
    dims = tuple(int(d) for d in dims)
    free = np.asarray(free, dtype=np.int64)
    for depth in range(max(0, int(opts.max_levels))):
        # **1段目は自由度数に関わらず必ず作る。** ここで `direct_dofs` を見てしまうと、
        # 小さい題材(受入検査の 16x8x8 など)が黙って直接解法になり、`solver="mg"` の経路が
        # 検査されないまま通る。
        if depth > 0 and free.size <= int(opts.direct_dofs):
            break
        pos: list[np.ndarray] = []
        coarsened = False
        for d in dims:
            axis = _coarse_axis(d, opts.min_axis_elements)
            if axis is None:
                pos.append(np.arange(d + 1))
            else:
                pos.append(axis)
                coarsened = True
        if not coarsened:
            break
        p_dof = _prolongation(dims, pos)
        ny1, nz1 = dims[1] + 1, dims[2] + 1
        cx, cy, cz = np.meshgrid(pos[0], pos[1], pos[2], indexing="ij")
        fine_node = (cx * ny1 * nz1 + cy * nz1 + cz).ravel()
        fine_dof = 3 * np.repeat(fine_node, 3) + np.tile(np.arange(3), fine_node.size)
        is_free = np.zeros(3 * (dims[0] + 1) * ny1 * nz1, dtype=bool)
        is_free[free] = True
        coarse_free = np.flatnonzero(is_free[fine_dof])
        if coarse_free.size == 0 or coarse_free.size >= free.size:
            break
        transfers.append(p_dof[free][:, coarse_free].tocsr())
        dims = tuple(p.size - 1 for p in pos)
        free = coarse_free
    return transfers


def _lambda_max(a: sp.csr_matrix, diag: np.ndarray, iters: int = 8) -> float:
    """`D^-1 A` の最大固有値をべき乗法で下から見積もる。**開始ベクトルは固定**

    (乱数だと同じ入力で結果が変わり、`check_absolute_volume.py` [4] の SHA が揺れる)。
    レイリー商は下からしか近づかないので、使う側は必ず安全率を掛けること。
    """
    n = a.shape[0]
    v = np.sin(np.arange(n, dtype=float) * 0.7 + 1.0)
    v /= np.linalg.norm(v)
    lam = 0.0
    for _ in range(max(1, int(iters))):
        av = a @ v
        lam = float(v @ av) / float(v @ (diag * v))
        w = av / diag
        nrm = float(np.linalg.norm(w))
        if not np.isfinite(nrm) or nrm == 0.0:
            break
        v = w / nrm
    return lam


def _smoothing_weight(a: sp.csr_matrix, diag: np.ndarray, scale: float) -> np.ndarray:
    """damped Jacobi の重み `omega / diag` を返す。

    **`omega < 2 / λmax(D^-1 A)` が前処理の対称正定値性の十分条件**(`2D/omega - A` が
    正定値)であり、ここを緩めると CG が壊れる。一方 `omega` を必要以上に小さくすると
    平滑化が効かず反復数が増える(実測: Gershgorin の上界そのままだと λmax の 1.7倍を
    見積もることになり、n=50x20x20 の実密度で 56 → 48 反復の差になった)。

    そこでべき乗法の見積もり `lam`(下から)に安全率 1.2 を掛けた `hi` を使い、
    **`gershgorin <= 2.4 * lam` が成り立つときだけ**それを採る —— 成り立てば
    `λmax <= gershgorin <= 2.4 lam = 2 hi` なので `omega = 1/hi < 2/λmax` が**証明できる**。
    成り立たなければ Gershgorin の上界へ退避する(必ず安全側)。
    """
    if float(scale) > 1.0:
        # **1 より大きい値は禁止。** `omega` が `2/λmax` を超えると damped Jacobi が
        # 非正定値になり、V-cycle 全体も非正定値になって CG が壊れる(委譲レビューの実測:
        # 20x8x8・rho=0.3 で `weight_scale=5.0` にすると `v^T M v` が負になり CG が
        # 500反復で収束しなくなった)。この口は**弱める側にしか使わない**。
        raise ValueError(
            f"MGOptions.weight_scale は 1.0 以下であること(受け取った値: {scale})。"
            "1 を超えると前処理が正定値でなくなり CG が収束しない"
        )
    abs_rows = sp.csr_matrix((np.abs(a.data), a.indices, a.indptr), shape=a.shape) @ np.ones(
        a.shape[0]
    )
    gershgorin = float(np.max(abs_rows / diag))
    lam = _lambda_max(a, diag)
    hi = 1.2 * lam if 2.4 * lam >= gershgorin else gershgorin
    return (float(scale) / hi) / diag


def _mg_setup(kf: sp.csr_matrix, transfers: list[sp.csr_matrix], opts: MGOptions) -> tuple:
    """各段の作用素・平滑化の重み・最粗格子の LU を作る。**密度が変わるたびに作り直す。**"""
    a = kf.tocsr()
    levels = []
    for p in transfers:
        diag = a.diagonal()
        if not np.all(diag > 0.0):
            raise ValueError("剛性行列の対角に非正の値がある(マルチグリッドの前提が崩れている)")
        levels.append((a, _smoothing_weight(a, diag, opts.weight_scale), p))
        a = (p.T @ a @ p).tocsr()
    diag = a.diagonal()
    if not np.all(diag > 0.0):
        raise ValueError("最粗格子の対角に非正の値がある")
    return levels, spla.splu(a.tocsc())


def _vcycle(
    levels: list,
    lu,
    b: np.ndarray,
    level: int,
    opts: MGOptions,
    blocked: list | None = None,
) -> np.ndarray:
    """対称 V-cycle。**前平滑化と後平滑化の回数を揃えること**(揃えないと前処理が非対称に
    なり、CG が収束しない/嘘の解を返す)。`check_multigrid.py` [2] がここを直接見ている。

    `blocked` は `_build_blocked_levels` が作る並列 matvec の並び(`levels` と同じ長さ)。
    **`None`(既定)のときは `a @ x` を直に呼び、以前と1バイトも違わない経路になる**——
    `check_multigrid.py` は `blocked` を渡さずに呼ぶので、この関数の意味を変えていない。
    """
    if level >= len(levels):
        return lu.solve(b)
    a, s, p = levels[level]
    if blocked is None:
        av: Callable[[np.ndarray], np.ndarray] = a.__matmul__
        pv: Callable[[np.ndarray], np.ndarray] = p.__matmul__
        ptv: Callable[[np.ndarray], np.ndarray] = p.T.__matmul__
    else:
        av, pv, ptv = blocked[level]
    nu = int(opts.smooth_steps) * max(1, int(opts.smooth_growth)) ** level
    x = s * b if nu > 0 else np.zeros_like(b)
    for _ in range(nu - 1):
        x += s * (b - av(x))
    if opts.coarse_correction:
        for _ in range(max(1, int(opts.cycle_gamma))):
            x += pv(_vcycle(levels, lu, ptv(b - av(x)), level + 1, opts, blocked))
    for _ in range(nu):
        x += s * (b - av(x))
    return x


# ---------------------------------------------------------------------------
# CPU 並列化 (solve(threads=...))
# ---------------------------------------------------------------------------
# **律速は CG の反復**(V-cycle の平滑化 `A@x`・移送 `P@e`/`P.T@r`、および CG 自身の `A@x`)
# であって、**RAP(疎行列×疎行列)と `splu`(SuperLU の数値分解)ではない**
# (`20260904-1414` の契約: RAP+splu の組み直しが solve の 16〜46% を占めるが、
# scipy の疎行列積も SuperLU もスレッド並列しない)。実測でも確かめた:
# 46x28x31 格子(自由度128,064)で RAP を行ブロックに割って `ThreadPoolExecutor` に
# 投げても **0.5〜0.6倍(遅くなる)**——`vstack` での組み直しコストが並列化の利得を上回る。
# splu 自体はそもそもスレッドプールへ切り出す形をしていない。
#
# 一方 **scipy の CSR 行列ベクトル積(疎行列×密ベクトル)は C 実装で GIL を解放する**
# (実測: 同じ格子で 12スレッド 3.8倍。`_dot` の呼び出しだけをスレッドへ投げれば速くなる)。
# したがってスレッド化するのは「疎行列×密ベクトル」の積だけに絞る。
#
# **行ブロックへの分割は答えを変えない**: CSR の行 i の内積は行 i の非零要素だけで決まり、
# 他の行・他のスレッドの計算と独立。どのスレッドがどの行を計算しても、スレッド数を
# 変えても、**丸め誤差が発生する順序は変わらずビット単位で同じ結果になる**
# (numpy の縮約(内積・ノルム。CG 本体が使う)はここでは並列化していないので、
# そちらの再現性はそもそも揺れない)。README「答えの再現性」の選択(a)——加算順序を
# 固定して再現性を保つ——を**追加コストなしで**満たせるのはこのため。

# 環境変数 `TOPOPT_THREADS` で上書きできる既定のスレッド数。**呼び出し時に読む**
# (`MG_DEFAULT` と同じ流儀)。値を取れなければ 1(直列。壊れた環境変数で黙って
# 並列化が有効になるより、壊れた値は無視して安全側へ倒す)。
def _default_thread_count() -> int:
    override = os.environ.get("TOPOPT_THREADS")
    if override:
        try:
            return max(1, int(override))
        except ValueError:
            return 1
    return max(1, os.cpu_count() or 1)


PARALLEL_THREADS: int = _default_thread_count()

# プロセスの寿命だけ生存させるスレッドプールのキャッシュ(スレッド数ごとに1つ)。
# **明示的に閉じない**——閉じても次の solve でまた同じ数だけ要るので、生成コストを
# 毎回払わせない。デーモンスレッドではないが、プロセス終了時に daemon 属性が無くても
# インタプリタは残ったワーカーを待たずに終了する(標準ライブラリの `atexit` フックが
# `shutdown(wait=True)` を試みるので、通常のプロセス終了では待つ。使い切りのCLI
# (`perf.py` / `check_*.py`)ではプロセスごと終わるので問題にならない)。
_THREAD_POOLS: dict[int, ThreadPoolExecutor] = {}


def _thread_pool(n: int) -> ThreadPoolExecutor:
    pool = _THREAD_POOLS.get(n)
    if pool is None:
        pool = ThreadPoolExecutor(max_workers=n, thread_name_prefix="topopt-spmv")
        _THREAD_POOLS[n] = pool
    return pool


# 1スレッドあたりこれ未満の行数しか無い行列は、スレッド化のオーバーヘッド
# (`submit`/`result` の往復)のほうが計算そのものより高くつく。**しきい値の根拠**:
# 最粗格子の1つ上の段(1,000〜数千行)でもスレッド数を機械的に絞らないと、
# 検査で使う小さい格子(数百行)でむしろ遅くなる実測があった。
_MIN_ROWS_PER_THREAD = 512


def _effective_threads(n_rows: int, threads: int) -> int:
    """要求されたスレッド数を、行列の大きさに見合う数へ切り詰める。"""
    threads = max(1, int(threads))
    if n_rows <= 0:
        return 1
    return max(1, min(threads, n_rows // _MIN_ROWS_PER_THREAD or 1))


class _BlockedMatvec:
    """CSR行列を行ブロックへ割った並列 `A @ x`。**スレッド数に関わらずビット一致する**
    (このセクション冒頭の説明を参照)。`threads<=1` または行数がスレッド数に対して
    少なすぎるときは、プールへ投げずに `a @ x` を直に呼ぶ(オーバーヘッドを払わない)。

    行ブロックへの分割(スライス)は**構築時に1回だけ**行う。密度が変わるたびに
    作り直す必要があるので(`_mg_setup` や `Model.solve` が呼ぶ側で)、`solve` 1回の
    寿命を超えてキャッシュしない。
    """

    __slots__ = ("mat", "bounds", "blocks", "pool")

    def __init__(self, mat: sp.spmatrix, threads: int) -> None:
        mat = mat.tocsr() if not sp.isspmatrix_csr(mat) else mat
        self.mat = mat
        eff = _effective_threads(mat.shape[0], threads)
        if eff <= 1:
            self.bounds = None
            self.blocks = None
            self.pool = None
        else:
            self.bounds = np.linspace(0, mat.shape[0], eff + 1).astype(np.int64)
            self.blocks = [mat[self.bounds[i] : self.bounds[i + 1]] for i in range(eff)]
            self.pool = _thread_pool(eff)

    def __call__(self, x: np.ndarray) -> np.ndarray:
        if self.pool is None:
            return self.mat @ x
        out = np.empty(self.mat.shape[0], dtype=float)
        futures = [self.pool.submit(_block_dot, block, x) for block in self.blocks]
        for lo, hi, fut in zip(self.bounds[:-1], self.bounds[1:], futures, strict=True):
            out[lo:hi] = fut.result()
        return out


def _block_dot(block: sp.csr_matrix, x: np.ndarray) -> np.ndarray:
    return block @ x


def parallel_matvec(a: sp.spmatrix, x: np.ndarray, threads: int = 1) -> np.ndarray:
    """`a @ x` を行ブロックへ割って `threads` 個のスレッドで計算する(公開の使い切り口)。

    毎回ブロック分割を作り直すので、同じ `a` に対して繰り返し呼ぶ用途には向かない
    (`Model.solve` / `_vcycle` の内部は `_BlockedMatvec` を密度が変わるたびに1回だけ
    作って使い回している)。`perf.py` の帯域測定と `check_parallel.py` の検出力用。
    """
    return _BlockedMatvec(a, threads)(x)


def _build_blocked_levels(levels: list, threads: int) -> list | None:
    """V-cycle の各段について `A` / `P` / `P^T` の並列 matvec をあらかじめ作る。

    **`_vcycle` の再帰1回ごとに作り直さないためのキャッシュ**(密度が変わるたびに
    `_mg_setup` と同じ頻度=solve 1回に1回だけ作ればよい)。`threads<=1` なら
    何も作らず `None` を返す——`_vcycle` はこれを「並列化しない」の合図として扱う。
    """
    if threads <= 1:
        return None
    built = []
    for a, _s, p in levels:
        pt = p.transpose().tocsr()
        built.append((_BlockedMatvec(a, threads), _BlockedMatvec(p, threads), _BlockedMatvec(pt, threads)))
    return built


class Model:
    """片持ち梁の格子・DOF・境界条件・荷重。密度に依らない部分をここに持つ。"""

    nelx: int
    nely: int
    nelz: int
    nele: int
    ndof: int
    ke: np.ndarray
    edof: np.ndarray
    fixed: np.ndarray
    free: np.ndarray
    f: np.ndarray

    def __init__(
        self,
        nelx: int,
        nely: int,
        nelz: int,
        E: float = 1.0,
        nu: float = 0.3,
        emin: float = 1e-9,
        fixed_dofs: np.ndarray | None = None,
        load: np.ndarray | None = None,
        extra_fixed_dofs: np.ndarray | None = None,
    ) -> None:
        """`fixed_dofs` / `load` を渡すと片持ち梁の既定の境界条件を置き換える。

        **既定(どちらも None)の挙動は 2026-08-30 以前と1ビットも変わらない** ——
        stage2「面アンカー」が CAD の面から起こした拘束・荷重を入れるための口で、
        既存の `check_simp.py` はこの口を使わない経路をそのまま通る。
        番号の規約は `_build_edof` と同じ(節点 `i*(nely+1)*(nelz+1)+j*(nelz+1)+k`、
        自由度 `3n+{0,1,2}`)。ずれると荷重は黙って別の場所に入る。

        `extra_fixed_dofs` は**置き換えではなく追加**。`optimize` が「空要素にしか接して
        いない浮いた節点」を系から外すのに使う。置き換えの口(`fixed_dofs`)に混ぜると、
        **`void_mask` だけを渡して拘束は既定のままにしたい呼び出しで、片持ち梁の拘束が
        黙って消える**(2026-08-31の自己レビューで見つけた欠陥)。
        """
        self.nelx = nelx
        self.nely = nely
        self.nelz = nelz
        self.nele = nelx * nely * nelz
        self.ndof = 3 * (nelx + 1) * (nely + 1) * (nelz + 1)
        self.E = E
        self.nu = nu
        self.emin = emin
        self.ke = element_stiffness(E=E, nu=nu)
        self.edof = _build_edof(nelx, nely, nelz)
        ny1 = nely + 1
        nz1 = nelz + 1
        if fixed_dofs is None:
            fixed_nodes = np.arange(ny1 * nz1)
            self.fixed = np.concatenate(
                [3 * fixed_nodes, 3 * fixed_nodes + 1, 3 * fixed_nodes + 2]
            )
        else:
            given = np.unique(np.asarray(fixed_dofs, dtype=np.int64))
            if given.size == 0:
                raise ValueError("拘束が空(剛体運動が止まらず K が特異になる)")
            if given.min() < 0 or given.max() >= self.ndof:
                raise ValueError(
                    f"拘束の自由度番号が範囲外: [{given.min()}, {given.max()}] / ndof={self.ndof}"
                )
            self.fixed = given
        if extra_fixed_dofs is not None:
            extra = np.asarray(extra_fixed_dofs, dtype=np.int64)
            if extra.size and (extra.min() < 0 or extra.max() >= self.ndof):
                raise ValueError(
                    f"追加の拘束の自由度番号が範囲外: [{extra.min()}, {extra.max()}] / ndof={self.ndof}"
                )
            self.fixed = np.union1d(self.fixed, extra)
        self.free = np.setdiff1d(np.arange(self.ndof), self.fixed)
        if self.free.size == 0:
            raise ValueError("全自由度が拘束されている(解くものが無い)")
        if load is None:
            self.f = _build_load(nelx, nely, nelz, self.ndof)
        else:
            vec = np.asarray(load, dtype=float).ravel()
            if vec.size != self.ndof:
                raise ValueError(f"荷重ベクトルの長さが {vec.size}、期待は ndof={self.ndof}")
            if not np.all(np.isfinite(vec)):
                raise ValueError("荷重ベクトルに非有限値がある")
            self.f = vec.copy()
        # **拘束DOFに載った荷重は解に一切効かない。** 固定面と荷重面が節点を共有していると
        # ここに全部落ち、「変位ゼロ・コンプライアンスゼロ」で最適化がもっともらしく
        # 収束してしまう(荷重を掛けたつもりで何も掛かっていない)。先に落とす。
        if not np.any(self.f[self.free] != 0.0):
            raise ValueError(
                "荷重が自由な自由度に1つも載っていない(全部が拘束DOFに落ちている)。"
                "固定面と荷重面が同じ節点を共有していないか確認すること"
            )
        self._iK = np.kron(self.edof, np.ones((24, 1), dtype=np.int32)).ravel()
        self._jK = np.kron(self.edof, np.ones((1, 24), dtype=np.int32)).ravel()
        # マルチグリッドの補間作用素は格子と拘束だけで決まる(密度に依らない)ので、
        # 設計反復のあいだ使い回す。作り直すのは各段の作用素と LU のほうだけ。
        self._mg_transfers: dict[tuple, list] = {}

    def assemble(self, rho: np.ndarray, penal: float) -> sp.spmatrix:
        """全体剛性行列 K (ndof,ndof) を返す。SIMP補間 E_e = emin + rho_e**penal * (E - emin)。"""
        ee = self.emin + rho.ravel() ** penal * (self.E - self.emin)
        sk = (self.ke.ravel()[None, :] * ee[:, None]).ravel()
        return sp.coo_matrix((sk, (self._iK, self._jK)), shape=(self.ndof, self.ndof)).tocsc()

    def mg_transfers(self, opts: MGOptions | None = None) -> list[sp.csr_matrix]:
        """この格子・この拘束に対する補間作用素の階層(密度に依らないのでキャッシュする)。"""
        opts = MG_DEFAULT if opts is None else opts
        key = (opts.max_levels, opts.min_axis_elements, opts.direct_dofs)
        cached = self._mg_transfers.get(key)
        if cached is None:
            cached = build_mg_transfers(
                (self.nelx, self.nely, self.nelz), self.free, opts
            )
            self._mg_transfers[key] = cached
        return cached

    def mg_preconditioner(
        self, kf: sp.spmatrix, opts: MGOptions | None = None, *, threads: int = 1
    ) -> spla.LinearOperator:
        """`kf`(自由DOFに縮めた K)に対する V-cycle 前処理。

        **対称正定値**である(`_smoothing_weight` の重み・前後で同じ回数の平滑化・
        R = P^T・最粗格子は直接解法)。CG に差せるのはそのため。

        `threads>1` なら V-cycle の行列ベクトル積(平滑化・移送)を行ブロックで並列化する
        (`_build_blocked_levels`)。**答えは変わらない**——`_vcycle` のセクション冒頭を参照。
        """
        opts = MG_DEFAULT if opts is None else opts
        levels, lu = _mg_setup(kf.tocsr(), self.mg_transfers(opts), opts)
        blocked = _build_blocked_levels(levels, threads)
        return spla.LinearOperator(
            kf.shape, matvec=lambda b: _vcycle(levels, lu, b, 0, opts, blocked), dtype=float
        )

    def solve(
        self,
        rho: np.ndarray,
        penal: float,
        *,
        solver: str = "mg",
        cg_rtol: float = 1e-9,
        u0: np.ndarray | None = None,
        mg: MGOptions | None = None,
        threads: int | None = None,
    ) -> np.ndarray:
        """K U = F を解いて U (ndof,) を返す。拘束DOFの成分はきっかり 0.0。

        **解いた後に残差で検算し、通らなければ落とす。** 拘束が足りない系(剛体運動が
        止まっていない)を渡されると K は特異になるが、`Model.__init__` の検査は
        「拘束が非空で範囲内」しか見ないので通ってしまう。そのまま解くと CG は収束せず、
        直接解法へ落ちた先で **有限だが桁違いの解**(実測 `fixed_dofs=[0]` で max|u| = 1.2e16)
        が返り、最適化はその値を信じて進んだ末に別の場所で落ちる(実測 OC 更新で
        `ZeroDivisionError`)。**原因から遠い場所で落ちるのが最悪**なので、ここで名指しする。
        剛体運動の完全な検出はしない —— 残差が通らなければ落とす、それだけでよい。

        `threads` は CG 反復・V-cycle が使うスレッド数(**行列ベクトル積だけを並列化**、
        RAP と `splu` は並列化しても速くならないので触らない。README「CPU並列化」参照)。
        **既定は `None` → `simp.PARALLEL_THREADS`**(呼び出し時に読む。`MG_DEFAULT` と同じ
        流儀で `simp.PARALLEL_THREADS = 1` を代入すれば全経路が直列に戻る)。
        `threads=1` は並列化の前と**同じコード経路**(`blocked=None` のまま `_vcycle` を呼ぶ・
        CG にも生の `kf` を渡す)を通るので、速度も答えも変わらない(退避経路)。
        """
        if solver not in ("mg", "cg", "direct"):
            raise ValueError(f"未知の解法: {solver!r}")
        # **既定を束縛時ではなく呼び出し時に読む。** こうしておくと `simp.MG_DEFAULT` を
        # 差し替えるだけで V-cycle の形を全経路(anchor → design_space → perf)で
        # 一斉に変えられる。既定値の当たりを取るのに要る。
        mg = MG_DEFAULT if mg is None else mg
        threads = PARALLEL_THREADS if threads is None else max(1, int(threads))
        stats = _SOLVE_STATS
        t0 = time.perf_counter()
        kg = self.assemble(rho, penal)
        t1 = time.perf_counter()
        kf = kg[self.free, :][:, self.free].tocsc()
        ff = self.f[self.free]
        t2 = time.perf_counter()
        x0 = np.zeros(self.free.size) if u0 is None else u0[self.free].copy()
        iterations = 0
        fell_back = False
        setup_seconds = 0.0
        if solver == "direct":
            u_free = spla.spsolve(kf, ff)
        else:
            if solver == "mg":
                t_setup = time.perf_counter()
                precond = self.mg_preconditioner(kf, mg, threads=threads)
                setup_seconds = time.perf_counter() - t_setup
            else:
                diag = kf.diagonal()
                precond = spla.LinearOperator(kf.shape, matvec=lambda v, d=diag: v / d)
            counter = None
            if stats is not None:

                def counter(_xk: np.ndarray) -> None:
                    nonlocal iterations
                    iterations += 1

            # **CG 自身の `A@x` も行ブロックで並列化する**(threads<=1 なら `kf` を生のまま
            # 渡すので `spla.cg` は元のコード経路と同じ:`_BlockedMatvec` を経由しない)。
            a_op: sp.spmatrix | spla.LinearOperator = kf
            if threads > 1:
                a_mv = _BlockedMatvec(kf, threads)
                a_op = spla.LinearOperator(kf.shape, matvec=a_mv, dtype=float)
            u_free, info = spla.cg(a_op, ff, x0=x0, M=precond, rtol=cg_rtol, callback=counter)
            fell_back = info != 0
            if info != 0:
                warnings.warn(
                    f"CG が収束しませんでした (info={info})。直接解法へフォールバックします。",
                    RuntimeWarning,
                    stacklevel=2,
                )
                with warnings.catch_warnings():
                    # 特異行列の警告はここでは要らない(下の残差検査が本番の判定)。
                    warnings.simplefilter("ignore")
                    u_free = spla.spsolve(kf, ff)
        if stats is not None:
            stats["solves"] += 1
            stats["assemble_seconds"] += t1 - t0
            stats["slice_seconds"] += t2 - t1
            stats["solve_seconds"] += time.perf_counter() - t2
            stats["cg_iterations"] += iterations
            stats["cg_iterations_each"].append(iterations)
            stats["mg_setup_seconds"] += setup_seconds
            stats["fallbacks"] += int(fell_back)
            stats["direct_solves"] += int(solver == "direct")
        self._verify_solution(kf, ff, u_free)
        u = np.zeros(self.ndof)
        u[self.free] = u_free
        return u

    def _verify_solution(self, kf, ff: np.ndarray, u_free: np.ndarray) -> None:
        """`K_ff u = f_f` を残差で検算する。通らなければ拘束不足を名指しして落とす。"""
        finite = bool(np.all(np.isfinite(u_free)))
        rel = float("inf")
        if finite:
            denominator = float(np.linalg.norm(ff))
            rel = float(np.linalg.norm(kf @ u_free - ff)) / denominator
        if finite and rel <= SOLVE_RESIDUAL_TOL:
            return
        raise ValueError(
            "K U = F が解けていない"
            f"(残差 ||K u - f|| / ||f|| = {rel:.3e} > {SOLVE_RESIDUAL_TOL:.0e}"
            f"{'' if finite else ' / 解に非有限値がある'}、"
            f"max|u| = {np.max(np.abs(u_free)) if u_free.size else 0.0:.3e})。"
            "**拘束不足(剛体運動が止まっていない)の可能性が高い。** "
            f"いま拘束されているのは {self.fixed.size} / {self.ndof} 自由度。"
            "面アンカーから作った拘束なら、その面の節点が少なすぎないか"
            "(`AnchorTable.empty_faces()`)、1点だけを拘束していないかを確認すること"
        )

    def compliance(
        self, rho: np.ndarray, penal: float, u: np.ndarray
    ) -> tuple[float, np.ndarray]:
        """(コンプライアンス c, 要素ひずみエネルギー ce) を返す。"""
        ue = u[self.edof]
        ce = np.einsum("ij,jk,ik->i", ue, self.ke, ue).reshape(self.nelx, self.nely, self.nelz)
        ee = self.emin + rho**penal * (self.E - self.emin)
        c = float((ee * ce).sum())
        return c, ce


def _as_mask(mask: np.ndarray | None, shape: tuple[int, int, int], label: str) -> np.ndarray:
    """パッシブ要素のマスクを検算して bool 配列にする。"""
    if mask is None:
        return np.zeros(shape, dtype=bool)
    arr = np.asarray(mask)
    if arr.shape != shape:
        raise ValueError(f"{label} の形が {arr.shape}、期待は {shape}")
    return arr.astype(bool)


def _floating_dofs(nelx: int, nely: int, nelz: int, void: np.ndarray) -> np.ndarray:
    """空要素にしか接していない節点の自由度番号(系から外すために拘束へ回す)。"""
    live = ~void
    active = np.zeros((nelx + 1, nely + 1, nelz + 1), dtype=bool)
    for a in (0, 1):
        for b in (0, 1):
            for c in (0, 1):
                active[a : a + nelx, b : b + nely, c : c + nelz] |= live
    idle = np.flatnonzero(~active.ravel())
    return np.concatenate([3 * idle, 3 * idle + 1, 3 * idle + 2])


@dataclass(frozen=True)
class Iteration:
    it: int
    compliance: float
    volfrac: float
    change: float
    seconds: float


@dataclass
class Result:
    rho: np.ndarray
    history: list[Iteration]
    nelx: int
    nely: int
    nelz: int
    seconds: float
    stopped_by: str
    # 実際に使った体積目標。**入力をそのまま写したものではない** —— 絶対量で渡された場合は
    # ここに換算後の値が入る。呼び出し側が「何を目標に解いたか」を入力の再計算なしで引ける。
    target_volume: float = float("nan")  # 要素数
    volfrac: float = float("nan")  # 動かせる要素に対する実効分率
    element_volume_mm3: float | None = None
    # `optimize(keep_model=True)` のときだけ入る、実際に解いた `Model`。**既定は None** ——
    # `Model` は `_iK`/`_jK` に要素数×576 の添字配列を2本持つので、要らない呼び出しにまで
    # 数十MBを持たせない。後処理(`stress.py`)が拘束・荷重を手で組み直さずに済むための口で、
    # 組み直しを誤ると**落ちずに違う応力**が出る。
    model: "Model | None" = None

    @property
    def target_material_mm3(self) -> float | None:
        """目標の材料総体積[mm^3]。要素体積を渡していなければ None。"""
        if self.element_volume_mm3 is None:
            return None
        return self.target_volume * self.element_volume_mm3

    @property
    def achieved_material_mm3(self) -> float | None:
        """出てきた材料の総体積[mm^3]。要素体積を渡していなければ None。"""
        if self.element_volume_mm3 is None:
            return None
        return float(self.rho.sum()) * self.element_volume_mm3


class VolumeTargetError(ValueError):
    """絶対量(mm^3)の体積目標が達成できない。

    **どちら側にどれだけ足りないかを名指しするための例外。** これが無いと OC の二分探索が
    体積と無関係な例外(`ZeroDivisionError` / 「K U = F が解けていない」)で落ちる
    (2026-09-04実測。README「体積目標を絶対量(mm^3)で指定する」)。
    属性 `side` は "below_minimum" か "above_capacity"。
    """

    def __init__(
        self,
        message: str,
        *,
        material_mm3: float,
        min_mm3: float,
        max_mm3: float,
        shortfall_mm3: float,
        side: str,
        element_volume_mm3: float,
    ) -> None:
        super().__init__(message)
        self.material_mm3 = material_mm3
        self.min_mm3 = min_mm3
        self.max_mm3 = max_mm3
        self.shortfall_mm3 = shortfall_mm3
        self.side = side
        self.element_volume_mm3 = element_volume_mm3


def check_volume_target(volfrac: float | None, material_mm3: float | None) -> None:
    """体積目標の**引数の形**だけを検算する(格子を作る前に落とすための口)。

    上位層(`anchor.run_pipeline` 等)がボクセル化の前にこれを呼ぶ。要素体積を知っている
    層でしか見られない「達成できるか」の判定は `_resolve_volume_target` が持つ。
    """
    if volfrac is not None and material_mm3 is not None:
        raise ValueError(
            "volfrac と material_mm3 は排他(どちらか一方だけ渡すこと)。"
            f"volfrac={volfrac!r} / material_mm3={material_mm3!r}"
        )
    if material_mm3 is not None and not float(material_mm3) > 0.0:
        raise ValueError(f"material_mm3 は正の値であること(受け取った値: {material_mm3!r})")


def _resolve_volume_target(
    *,
    volfrac: float | None,
    material_mm3: float | None,
    element_volume_mm3: float | None,
    n_movable: int,
    n_solid: int,
) -> tuple[float, float]:
    """体積目標を (実効 volfrac, 目標体積[要素数]) へ畳む。

    **絶対量は「材料の総体積」** —— `solid_mask` で ρ=1 に固定された要素のぶんを含む。
    したがって達成できるのは `[solid, solid + movable]` の区間だけ。区間の外は
    **指定と違う量が出る**ので、ここで不足量つきの例外にする(下流の OC も落ちはするが、
    文面に体積の話が出ない)。
    """
    if material_mm3 is None:
        fraction = DEFAULT_VOLFRAC if volfrac is None else float(volfrac)
        return fraction, fraction * n_movable + float(n_solid)

    ev = float(element_volume_mm3)  # type: ignore[arg-type]
    target = float(material_mm3) / ev
    lo, hi = float(n_solid), float(n_solid + n_movable)
    # 端ちょうどの指定が丸めで区間の外へ出るのを許す(1要素の1e-9倍の幅)。
    tol = 1e-9 * max(1.0, hi)
    if target < lo - tol:
        shortfall = lo * ev - float(material_mm3)
        raise VolumeTargetError(
            f"体積目標 {float(material_mm3):.6g}mm^3 は達成できない: "
            f"solid_mask で材料に固定された {n_solid} 要素だけで {lo * ev:.6g}mm^3 あり、"
            f"目標が {shortfall:.6g}mm^3 下回っている"
            "(solid_mask を減らすか、目標を上げること)",
            material_mm3=float(material_mm3),
            min_mm3=lo * ev,
            max_mm3=hi * ev,
            shortfall_mm3=shortfall,
            side="below_minimum",
            element_volume_mm3=ev,
        )
    if target > hi + tol:
        shortfall = float(material_mm3) - hi * ev
        raise VolumeTargetError(
            f"体積目標 {float(material_mm3):.6g}mm^3 は達成できない: "
            f"動かせる {n_movable} 要素と固定材料 {n_solid} 要素を全部埋めても "
            f"{hi * ev:.6g}mm^3 で、目標が {shortfall:.6g}mm^3 超えている"
            "(塊を広げるか、目標を下げること)",
            material_mm3=float(material_mm3),
            min_mm3=lo * ev,
            max_mm3=hi * ev,
            shortfall_mm3=shortfall,
            side="above_capacity",
            element_volume_mm3=ev,
        )
    target = min(max(target, lo), hi)
    return (target - lo) / n_movable, target


def optimize(
    nelx: int,
    nely: int,
    nelz: int,
    *,
    volfrac: float | None = None,
    material_mm3: float | None = None,
    element_volume_mm3: float | None = None,
    penal: float = 3.0,
    rmin: float = 1.5,
    max_iter: int = 60,
    change_tol: float = 0.01,
    E: float = 1.0,
    nu: float = 0.3,
    emin: float = 1e-9,
    move: float = 0.2,
    solver: str = "mg",
    cg_rtol: float = 1e-9,
    threads: int | None = None,
    callback: Callable[[Iteration, np.ndarray], None] | None = None,
    fixed_dofs: np.ndarray | None = None,
    load: np.ndarray | None = None,
    void_mask: np.ndarray | None = None,
    solid_mask: np.ndarray | None = None,
    keep_model: bool = False,
) -> Result:
    """SIMPでコンプライアンス最小化を行う。

    `threads` は `Model.solve` へそのまま渡る(既定 `None` → `simp.PARALLEL_THREADS`)。

    `fixed_dofs` / `load` は境界条件の差し替え(`Model` へそのまま渡る)。
    `void_mask` / `solid_mask` は**設計変数を動かさない要素**(パッシブ要素)で、
    それぞれ ρ=0 / ρ=1 に固定する。stage2 が「設計空間の外」「保持領域」「荷重直下」を
    入れるための口で、**4つとも None のときの挙動は追加前と完全に同じ**。

    体積制約は動かせる要素だけに掛ける(目標体積 = volfrac × 自由要素数 + 固定ρ=1 要素数)。
    そうしないと、設計空間の外の空要素を「もう十分軽い」と数えて、
    設計空間の中がほとんど埋まったまま止まる。

    体積目標は2通りで渡せる。**`volfrac` と `material_mm3` は排他**(両方渡すと例外):

    - `volfrac` —— 動かせる要素に対する分率。塊の大きさが変われば材料の量も変わる。
    - `material_mm3` + `element_volume_mm3` —— **材料の総体積を mm^3 の絶対量で**指定する
      (`solid_mask` のぶんを含む)。`optimize` は格子の寸法を知らないので、要素1つの体積
      `element_volume_mm3` を一緒に渡す(呼び出し側は `voxelize.Grid.element_volume`)。
      **質量(グラム)は扱わない** —— 密度を掛けるのは表示の仕事で、内部表現は体積である。
    - どちらも渡さなければ `DEFAULT_VOLFRAC`。

    `element_volume_mm3` は `volfrac` の側でも渡してよい(体積目標は変わらず、`Result` が
    mm^3 でも答えられるようになるだけ)。

    `keep_model=True` にすると `Result.model` に**実際に解いた `Model`** が入る
    (既定は入らない)。応力の後処理(`stress.py`)が拘束・荷重・浮いた節点の扱いを
    手で組み直さずに済ませるための口で、**数値は1ビットも変わらない**。
    """
    check_volume_target(volfrac, material_mm3)
    if material_mm3 is not None and element_volume_mm3 is None:
        raise ValueError(
            "material_mm3 を渡すときは element_volume_mm3(要素1つの体積[mm^3])も渡すこと"
            "(optimize は格子の寸法を知らないので、要素数へ換算できない)"
        )
    if element_volume_mm3 is not None and not float(element_volume_mm3) > 0.0:
        raise ValueError(
            f"element_volume_mm3 は正の値であること(受け取った値: {element_volume_mm3!r})"
        )
    t_total = time.perf_counter()
    shape = (nelx, nely, nelz)
    void = _as_mask(void_mask, shape, "void_mask")
    solid = _as_mask(solid_mask, shape, "solid_mask")
    overlap = int(np.count_nonzero(void & solid))
    if overlap:
        raise ValueError(
            f"void_mask と solid_mask が {overlap} 要素で重なっている"
            "(同じ要素を空と材料の両方に固定できない)"
        )
    # 空要素にしか接していない節点は、剛性 emin でしか繋がっていない浮いた自由度。
    # 残すと K がほぼ特異になって CG が収束しないので、拘束側へ**足して**系から外す。
    # `fixed_dofs` へ代入してはいけない —— それだと `void_mask` だけを渡した呼び出しで
    # 既定の片持ち梁の拘束が消える。
    floating = _floating_dofs(nelx, nely, nelz, void) if void.any() else None
    model = Model(
        nelx,
        nely,
        nelz,
        E=E,
        nu=nu,
        emin=emin,
        fixed_dofs=fixed_dofs,
        load=load,
        extra_fixed_dofs=floating,
    )
    kernel, r = _filter_kernel(rmin)
    hs = _filter_conv(np.ones((nelx, nely, nelz)), kernel, r)
    movable = ~void & ~solid
    n_movable = int(movable.sum())
    if n_movable == 0:
        raise ValueError("動かせる要素が1つも無い(void と solid で埋まっている)")
    volfrac, target_volume = _resolve_volume_target(
        volfrac=volfrac,
        material_mm3=material_mm3,
        element_volume_mm3=element_volume_mm3,
        n_movable=n_movable,
        n_solid=int(np.count_nonzero(solid)),
    )
    live = movable.astype(float)

    def _physical(design: np.ndarray) -> np.ndarray:
        """設計変数 → 物理密度。**パッシブ要素は密度フィルタの後で上書きする。**

        設計変数だけを固定して物理密度を上書きしないと、フィルタが設計空間の外へ
        密度をにじませる(実測: 半径 rmin=1.5 で設計空間の外の要素が ρ=0.29 になった)。
        にじんだぶんは marching cubes がそのまま形にするので、**CADの設計空間から
        はみ出した形**が出てくる。上書きに合わせて感度も `live` で刈る(下の `dcn`/`dvn`)
        ので、連鎖律は上書き後の定義と厳密に一致する。
        """
        field = _filter_conv(design, kernel, r) / hs
        field[void] = 0.0
        field[solid] = 1.0
        return field

    x = np.full((nelx, nely, nelz), volfrac)
    x[void] = 0.0
    x[solid] = 1.0
    x_phys = _physical(x)
    u = np.zeros(model.ndof)
    history: list[Iteration] = []
    stopped_by = "max_iter"

    for it in range(1, max_iter + 1):
        t0 = time.perf_counter()
        u = model.solve(x_phys, penal, solver=solver, cg_rtol=cg_rtol, u0=u, threads=threads)
        c, ce = model.compliance(x_phys, penal, u)
        dc = -penal * x_phys ** (penal - 1.0) * (E - emin) * ce
        dv = np.ones_like(x)
        # `live` は「その要素の物理密度が設計変数で動くか」。パッシブ要素の物理密度は
        # 上書きで定数になっているので、そこへの感度は連鎖律上ちょうど 0 になる。
        dcn = _filter_conv(dc * live / hs, kernel, r)
        dvn = _filter_conv(dv * live / hs, kernel, r)
        # 近傍が全部パッシブな要素は dvn=0 になりうる。そのままだと 0 除算で NaN が
        # 全反復へ伝播する(その要素はどのみち下の上書きで固定されるので値は使わない)。
        dvn = np.maximum(dvn, 1e-300)
        l1, l2 = 0.0, 1e9
        while (l2 - l1) / (l1 + l2) > 1e-9:
            lm = 0.5 * (l1 + l2)
            # `-dcn` は理屈上つねに正だが、`ce` がきっかり0の要素(荷重の届かない孤島)や
            # 丸めで符号が反転した要素があると sqrt が NaN を返し、**以後の反復が全部
            # NaN で埋まる**。0で潰しておく(その要素は下限へ寄るだけで済む)。
            xnew = np.clip(
                x * np.sqrt(np.maximum(0.0, -dcn) / (dvn * lm)),
                x - move,
                x + move,
            )
            xnew = np.clip(xnew, 0.0, 1.0)
            xnew[void] = 0.0
            xnew[solid] = 1.0
            xp = _physical(xnew)
            if xp.sum() > target_volume:
                l1 = lm
            else:
                l2 = lm
        change = float(np.abs(xnew - x).max())
        x = xnew
        x_phys = _physical(x)
        seconds = time.perf_counter() - t0
        iteration = Iteration(
            it=it,
            compliance=c,
            volfrac=float(x_phys.mean()),
            change=change,
            seconds=seconds,
        )
        history.append(iteration)
        if callback is not None:
            callback(iteration, x_phys.copy())
        if change < change_tol:
            stopped_by = "change_tol"
            break

    return Result(
        rho=x_phys,
        history=history,
        nelx=nelx,
        nely=nely,
        nelz=nelz,
        seconds=time.perf_counter() - t_total,
        stopped_by=stopped_by,
        target_volume=target_volume,
        volfrac=volfrac,
        element_volume_mm3=(
            None if element_volume_mm3 is None else float(element_volume_mm3)
        ),
        model=model if keep_model else None,
    )


def run_preset(
    callback: Callable[[Iteration, np.ndarray], None] | None = None,
    **overrides: object,
) -> Result:
    """PRESET の値で optimize を呼ぶ。overrides で個別に上書きできる
    (**格子 `nelx`/`nely`/`nelz` も上書きできる** —— 分けて扱うと `optimize` へ
    同じ引数を2度渡して TypeError になる)。"""
    opts: dict = dict(PRESET)
    opts.update(overrides)
    return optimize(
        int(opts.pop("nelx")),
        int(opts.pop("nely")),
        int(opts.pop("nelz")),
        callback=callback,
        **opts,  # type: ignore[arg-type]
    )


if __name__ == "__main__":
    result = run_preset()
    for item in result.history:
        print(
            f"反復 {item.it:3d}  C={item.compliance:10.4f}  "
            f"体積={item.volfrac:.4f}  change={item.change:.4f}  {item.seconds:.2f}秒"
        )
    print(f"完了: {result.stopped_by}  合計 {result.seconds:.1f}秒")
