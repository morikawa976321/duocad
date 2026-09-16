"""3D SIMP トポロジー最適化 (片持ち梁)。"""

from __future__ import annotations

import contextlib
import os
import time
import warnings
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import numpy as np
from . import trace
import scipy.optimize as sopt
import scipy.sparse as sp
import scipy.sparse.linalg as spla

# 解いた後に残差で検算するときの上限(相対)。CG は `cg_rtol=1e-9` まで詰め、直接解法は
# 1e-14 程度まで出るので、1e-6 は「収束した解」に対して6桁以上の余裕がある。
# **拘束不足の系を黙って通さないための関門**であって、精度の要求ではない。
SOLVE_RESIDUAL_TOL = 1e-6


class SolveResidualError(ValueError):
    """`K U = F` が残差検算(`SOLVE_RESIDUAL_TOL`)を通らなかった。

    **`ValueError` の派生なので、既存の `except ValueError` はそのまま効く。**
    別の型にしてあるのは、呼び出し側が**この失敗だけ**を他の `ValueError` と区別して
    扱えるようにするため —— 低密度で系が悪条件になっただけなら材料を増やせば直るが、
    `VolumeTargetError(side="above_capacity")` は増やすと悪化する(逆向きの手当てが要る)。
    `design_space.build_reference` の取り直しがこの区別に依存している。
    """


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
        # `dynamic_active=True` のときに active set を作り直した秒数(COO 添字の作り直しが
        # 主。`prepare` の中で `assemble` より前に払う)。既定経路では常に 0.0。
        "active_refresh_seconds": 0.0,
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

# **数値側の床**[要素]。`rmin_mm` で指定した半径がこれを下回ると `FilterRadiusError`。
# 1.5要素という値の根拠(下回るとチェッカーボードが出る)は `check_filter_radius.py` [6] が
# 実測で示す(docs/TOPOPT.md「1.5要素という下限の根拠」)。
MIN_RMIN = 1.5

# `rmin` も `rmin_mm` も渡さなかったときのフィルタ半径[要素]。**PRESET の rmin とは別物**
# (PRESET は片持ち梁の題材=回帰基準、こちらは `optimize` の既定値)。
# **`MIN_RMIN` とも別物**。床は「これより下はフィルタが壊れる」、
# 既定は「普通に使ったときに出したい形」で、決める根拠が違う —— 1つの定数に兼ねさせると、
# 既定を動かした瞬間に `rmin_mm` の受理範囲と `optimize_run.resolution_limits` の
# 解像度要求まで黙って動く。
#
# 床(1.5要素)の上でも薄板・傾けた題材は1要素幅のフィラメント(チェッカーボード)を出す。
# **複数の合成条件すべてで消える最小が2.5要素**(docs/TOPOPT.md「1要素幅のフィラメントを
# 消す既定値を選び直した」の段1)。代償はコンプライアンスが 1.08〜1.40倍。
DEFAULT_RMIN = 2.5

# `update` を渡さなかったときの更新則。選べる名前は `UPDATES`(下の方で更新則の実装と一緒に
# 定義する)。**ここで先に置いてあるのは `Result.update` の既定値に使うため。**
DEFAULT_UPDATE = "oc"

# 片持ち梁の基準題材。**受入検査3本がこれを回すので、大きさがそのまま検証の1周の長さになる。**
# 40x16x16(1周およそ11分)から 20x8x8 へ落としてある。**反復数は 60 のまま** ——
# 30反復まで落とすと終盤10反復の変動が 2.47% になり、`check_simp.py` (b) の1%を割る。
# 消してはならない(`Model` の既定の境界条件そのものが片持ち梁で、`check_anchor.py` は「拘束を
# 明示した結果と既定の結果が一致する」を面アンカー経路の保証にしている。解析的に検算できる
# 唯一の系でもある)が、検査の基準として使い続ける限り寸法と反復数は変えてよい。
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
# 対角(Jacobi)前処理は CG の反復数が格子の細かさに比例して増える。マルチグリッドは
# 反復数を格子非依存にする。決めた3点(理由・実測は docs/TOPOPT.md「マルチグリッド前処理」):
#
# 1. **粗格子作用素は Galerkin (A_c = P^T A P)。密度は粗格子へ移さない。**
#    `emin=1e-9`・`penal=3` の剛性差がある密度を平均で落とすと粗格子が別の問題になる。
#    RAP は細格子の作用素そのものを粗空間へ射影するので、拘束の扱いも自動的に引き継がれる。
# 2. **平滑化は damped Jacobi。** 行列ベクトル積と要素ごとの積だけで書けるので、
#    CPU並列化(下記のセクション)がそのまま載せられる。減衰係数は**行ごとの絶対和
#    (Gershgorin)から決める**(`_smoothing_weight`)。
# 3. **粗視化は軸ごとに独立**(`_coarse_axis`)。要素数が `min_axis_elements`(既定4)
#    未満の軸は間引かない —— 等方に間引くと薄板題材で粗格子が形をなさなくなる。
#    要素数が奇数の軸・2の冪でない格子寸法も扱う(粗節点を0,2,4,...と採り、端が
#    合わなければ最後の1つだけ幅1の粗セルにする)。


@dataclass(frozen=True)
class MGOptions:
    """V-cycle の形。**既定以外の値は主に検出力の検査(`check_multigrid.py`)のためにある**——
    `coarse_correction=False` / `max_levels=1` / `weight_scale` を下げるは前処理を意図的に
    劣化させる口で、これで反復数が増えないなら**その部品は最初から効いていない**。

    既定値(`smooth_steps` / `smooth_growth` / `cycle_gamma` / `direct_dofs`)は実測で決めた
    (根拠は docs/TOPOPT.md「マルチグリッド前処理」)。**なんとなくの既定ではない。**
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

    **`omega < 2 / λmax(D^-1 A)` が前処理の対称正定値性の十分条件**であり、ここを緩めると
    CG が壊れる。一方 `omega` を必要以上に小さくすると平滑化が効かず反復数が増える
    (Gershgorin の上界をそのまま使うと過大評価になる。実測は docs/TOPOPT.md「マルチグリッド前処理」)。

    そこでべき乗法の見積もり `lam`(下から)に安全率 1.2 を掛けた `hi` を使い、
    **`gershgorin <= 2.4 * lam` が成り立つときだけ**それを採る —— 成り立てば
    `λmax <= gershgorin <= 2.4 lam = 2 hi` なので `omega = 1/hi < 2/λmax` が**証明できる**。
    成り立たなければ Gershgorin の上界へ退避する(必ず安全側)。
    """
    if float(scale) > 1.0:
        # **1 より大きい値は禁止。** `omega` が `2/λmax` を超えると damped Jacobi が
        # 非正定値になり、V-cycle 全体も非正定値になって CG が壊れる(実測:
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
# **律速は CG の反復**(V-cycle の平滑化・移送・CG自身の行列ベクトル積)であって、
# **RAP(疎行列×疎行列)と `splu`(SuperLU の数値分解)ではない**——scipy の疎行列積も
# SuperLU もスレッド並列しないうえ、実測では行ブロックに割っても遅くなる(数字は
# docs/TOPOPT.md「CPU並列化」)。一方 **scipy の CSR 行列ベクトル積(疎×密ベクトル)は
# C実装で GIL を解放する**ので、スレッド化するのはこの積だけに絞る。
#
# **行ブロックへの分割は答えを変えない**: CSR の行 i の内積は行 i の非零要素だけで決まり、
# 他の行・他のスレッドの計算と独立。どのスレッドが計算しても、スレッド数を変えても、
# **丸め誤差が発生する順序は変わらずビット単位で同じ結果になる**(numpyの縮約はここでは
# 並列化していないので、そちらの再現性はそもそも揺れない)。

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
    # 総論理コアの半分(下限1)。duocad本体はこのマシン上でユーザーの操作と同時に
    # 走るので、既定が全論理コアを占有するとフロントエンドまで応答しなくなる
    # (probe単体なら困らないが、既定が本体まで持っていかれると困る)。
    return max(1, (os.cpu_count() or 1) // 2)


PARALLEL_THREADS: int = _default_thread_count()

# プロセスの寿命だけ生存させるスレッドプールのキャッシュ(スレッド数ごとに1つ)。
# **明示的に閉じない**——閉じても次の solve でまた同じ数だけ要るので、生成コストを
# 毎回払わせない。デーモンスレッドではないが、プロセス終了時に daemon 属性が無くても
# インタプリタは残ったワーカーを待たずに終了する(標準ライブラリの `atexit` フックが
# `shutdown(wait=True)` を試みるので、通常のプロセス終了では待つ。使い切りのCLI
# 使い切りのCLI(`check_*.py` 等)ではプロセスごと終わるので問題にならない)。
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
    作って使い回している)。`check_parallel.py` の帯域測定と検出力用。
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


def _coo_index_arrays(edof: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """要素DOF写像 (n,24) → 組み立て用の COO 行・列添字(各要素 576 項)。

    **並びは `ke.ravel()` と対応していなければならない**(層をまたぐ契約) —— 行は各要素の
    24自由度をそのまま24回並べ、列は各自由度を24回ずつ繰り返す。ずれると K は落ちずに
    別の行列になる。
    """
    rows = np.tile(edof, (1, 24)).ravel()
    cols = np.repeat(edof, 24, axis=1).ravel()
    return rows, cols


class Model:
    """片持ち梁の格子・DOF・境界条件・荷重。密度に依らない部分をここに持つ。"""

    nelx: int
    nely: int
    nelz: int
    nele: int
    ndof: int
    ke: np.ndarray
    edof: np.ndarray
    active: np.ndarray | None
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
        active_mask: np.ndarray | None = None,
        dynamic_active: bool = False,
    ) -> None:
        """`fixed_dofs` / `load` を渡すと片持ち梁の既定の境界条件を置き換える。

        **既定(どちらも None)の挙動は追加前と1ビットも変わらない** —— CAD の面から
        起こした拘束・荷重を入れるための口で、既存の `check_simp.py` はこの口を使わない
        経路をそのまま通る。番号の規約は `_build_edof` と同じ(節点
        `i*(nely+1)*(nelz+1)+j*(nelz+1)+k`、自由度 `3n+{0,1,2}`)。ずれると荷重は
        黙って別の場所に入る。

        `extra_fixed_dofs` は**置き換えではなく追加**。`optimize` が「空要素にしか接して
        いない浮いた節点」を系から外すのに使う。**置き換えの口(`fixed_dofs`)に混ぜてはいけない**
        —— それだと `void_mask` だけを渡して拘束は既定のままにしたい呼び出しで、
        片持ち梁の拘束が黙って消える。

        `active_mask` は**組み立てに載せる要素**で、True の要素だけで K を組む。
        **`extra_fixed_dofs` と同じ `void` から作らなければならない**(層をまたぐ契約)
        —— 組み立てから外した要素にしか接していない自由度が `free` に残ると、K_ff に
        全零行ができて解が定まらない。`None` なら全要素で組む。`edof` そのものは
        `compliance` と `stress.py` が全要素で引くので**間引かない**。

        `dynamic_active=True` にすると、`prepare` が**渡された ρ から active set を毎回
        作り直す**(`active_mask` はその上限として残る)。**既定は False で、そのときの
        挙動は追加前と1ビットも変わらない。** 詳しくは `_refresh_active`。
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
        if active_mask is None:
            self.active = None
            edof_assembled = self.edof
        else:
            arr = np.asarray(active_mask)
            if arr.shape not in ((self.nele,), (nelx, nely, nelz)):
                raise ValueError(
                    f"active_mask の形が {arr.shape}、期待は {(nelx, nely, nelz)}"
                )
            self.active = np.flatnonzero(arr.ravel())
            if self.active.size == 0:
                raise ValueError("組み立てる要素が1つも無い(active_mask が全部 False)")
            edof_assembled = self.edof[self.active]
        # 静的な状態(`active_mask` と `fixed_dofs`+`extra_fixed_dofs` そのもの)を控える。
        # `dynamic_active` が毎反復これを土台に作り直し、`reset_active` がここへ戻す。
        self._base_active = self.active
        self._base_fixed = self.fixed
        self.dynamic_active = bool(dynamic_active)
        self._iK, self._jK = _coo_index_arrays(edof_assembled)
        # マルチグリッドの補間作用素は格子と拘束だけで決まる(密度に依らない)ので、
        # 設計反復のあいだ使い回す。作り直すのは各段の作用素と LU のほうだけ。
        self._mg_transfers: dict[tuple, list] = {}

    def dead_rho_threshold(self, penal: float) -> float:
        """`dynamic_active` が組み立てから外す ρ の上限 = `emin ** (1/penal)`。

        `E_e = emin + ρ^penal (E - emin)` なので、この ρ でちょうど SIMP 項が `emin` と
        同じ大きさになる。**下回る要素の剛性寄与は `emin` と同じ桁**で、外しても K は
        O(emin) しか動かず、その要素自身の感度も同じ桁 —— **すでに数値的に死んでいる
        ものだけを外す**ことになる。既定(`emin=1e-9` / `penal=3`)では ρ < 1e-3。

        **これより上げてはならない。** ρ=1e-2 の要素は SIMP 項が `emin` の1000倍あって
        感度が生きており、外すと「まだ収束していない状態で位相を確定させる」ことになる。
        上げるなら、外した要素が戻れる/戻れない境目(docs/TOPOPT_ACTIVE_SET.md)を
        一緒に見直すこと。
        """
        return float(self.emin) ** (1.0 / float(penal))

    def reset_active(self) -> None:
        """動的な active set を捨て、`__init__` が作った静的な状態へ戻す。

        **`keep_model=True` で外へ出す `Model` はこの状態で返す**(層をまたぐ契約) ——
        最後の反復の ρ で作った active を持ったまま返すと、後処理(`stress.py`)が
        「どの ρ で作られたか分からない」拘束集合を引くことになる。`prepare` は渡された
        ρ から毎回作り直すので、静的な状態から始めても後処理の数値は変わらない。
        """
        self._apply_active(self._base_active, self._base_fixed)

    def _refresh_active(self, rho: np.ndarray, penal: float) -> None:
        """ρ から active set を作り直す(`dynamic_active=True` のときだけ `prepare` が呼ぶ)。

        `dead_rho_threshold` を下回る要素を、静的な `active_mask` の空要素と**同じ扱い**で
        組み立てから外し、外れた要素にしか接していない自由度を拘束へ回す
        (`_floating_dofs`。`extra_fixed_dofs` と同じ `void` から作るという `__init__` の
        契約を、動的な void で満たし直す)。
        """
        threshold = self.dead_rho_threshold(penal)
        alive = np.asarray(rho).ravel() >= threshold
        if self._base_active is not None:
            base = np.zeros(self.nele, dtype=bool)
            base[self._base_active] = True
            alive &= base
        active = np.flatnonzero(alive)
        if active.size == 0:
            raise ValueError(
                f"ρ が閾値 {threshold:.3e}(emin**(1/penal))を超える要素が1つも無い"
                "(組み立てる要素が残らない)"
            )
        current = self.active
        if active.size == (self.nele if current is None else current.size) and (
            current is None or np.array_equal(active, current)
        ):
            return
        floating = _floating_dofs(
            self.nelx, self.nely, self.nelz, ~alive.reshape(self.nelx, self.nely, self.nelz)
        )
        fixed, free = self._split_dofs(floating)
        # **荷重が全部 fixed 側へ落ちると、解は零で残差検算も通る**(`ff` が零ベクトルの本は
        # CG を回さず零解を返す契約)。コンプライアンス 0 の「解けた」が返るので、
        # 静的な口(`__init__`)と同じ文面でここでも落とす。
        if not np.any(self.f[free] != 0.0):
            raise ValueError(
                "ρ から作り直した active set で、荷重が自由な自由度に1つも載らなくなった"
                f"(閾値 {threshold:.3e} / 組み立てる要素 {active.size} / {self.nele})。"
                "荷重面の要素が保持(solid)に入っているか確認すること"
            )
        self._apply_active(active, fixed, free=free)

    def _split_dofs(self, extra: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """静的な拘束 + `extra` を fixed に、残りを free にした昇順の2本を返す。

        **`np.union1d` / `np.setdiff1d` を使わない。** どちらも 3.6M 自由度を並べ替えるので
        合わせて **8.0秒/反復** かかる(2026-09-12 実測。`hanger_bar_attachment` 2.4mm・
        ndof 3,623,730 で union1d 1.66秒 + setdiff1d 6.32秒)。真偽マスクなら O(ndof) で
        済み、同じ2本(昇順・重複なし)が出る。
        """
        mask = np.zeros(self.ndof, dtype=bool)
        mask[self._base_fixed] = True
        if extra.size:
            mask[extra] = True
        return np.flatnonzero(mask), np.flatnonzero(~mask)

    def _apply_active(
        self, active: np.ndarray | None, fixed: np.ndarray, *, free: np.ndarray | None = None
    ) -> None:
        self.active = active
        self.fixed = fixed
        self.free = self._split_dofs(np.empty(0, dtype=np.int64))[1] if free is None else free
        # **古い添字を先に手放してから作る。** 658,136要素では `_iK`/`_jK` が 1.5GB ずつ
        # あり、新旧を同時に持つと峰が 3GB 増える(2026-09-12 実測の規模)。
        self._iK = self._jK = np.empty(0, dtype=self.edof.dtype)
        self._iK, self._jK = _coo_index_arrays(
            self.edof if active is None else self.edof[active]
        )
        # 移送作用素は `free` から作られている(`build_mg_transfers`)ので、作り直す。
        self._mg_transfers.clear()

    def assemble(self, rho: np.ndarray, penal: float) -> sp.spmatrix:
        """全体剛性行列 K (ndof,ndof) を返す。SIMP補間 E_e = emin + rho_e**penal * (E - emin)。"""
        flat = rho.ravel()
        if self.active is not None:
            flat = flat[self.active]
        ee = self.emin + flat**penal * (self.E - self.emin)
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
        直接解法へ落ちた先で**有限だが桁違いの解**が返り、最適化はその値を信じて進んだ末に
        別の場所(OC更新)で落ちる。**原因から遠い場所で落ちるのが最悪**なのでここで名指しする
        (実測は docs/TOPOPT.md「CADの面 → 格子の節点」の「SIMP側に足した口」節)。
        剛体運動の完全な検出はしない —— 残差が通らなければ落とす、それだけでよい。

        `threads` は CG 反復・V-cycle が使うスレッド数(**行列ベクトル積だけを並列化**、
        RAP と `splu` は並列化しても速くならないので触らない。docs/TOPOPT.md「CPU並列化」参照)。
        **既定は `None` → `simp.PARALLEL_THREADS`**(呼び出し時に読む。`MG_DEFAULT` と同じ
        流儀で `simp.PARALLEL_THREADS = 1` を代入すれば全経路が直列に戻る)。
        `threads=1` は並列化の前と**同じコード経路**(`blocked=None` のまま `_vcycle` を呼ぶ・
        CG にも生の `kf` を渡す)を通るので、速度も答えも変わらない(退避経路)。
        """
        return self.solve_many(
            rho,
            penal,
            self.f[None, :],
            solver=solver,
            cg_rtol=cg_rtol,
            u0=None if u0 is None else u0[None, :],
            mg=mg,
            threads=threads,
        )[0]

    def solve_many(
        self,
        rho: np.ndarray,
        penal: float,
        rhs: np.ndarray,
        *,
        solver: str = "mg",
        cg_rtol: float = 1e-9,
        u0: np.ndarray | None = None,
        mg: MGOptions | None = None,
        threads: int | None = None,
    ) -> np.ndarray:
        """**同じ K を複数の右辺で解く。** `rhs` (nrhs, ndof) → 解 (nrhs, ndof)。

        `solve` はこれを右辺1本で呼ぶ薄い包みで、**1本のときの数値も経路も
        `solve_many` 導入前と同一**である(組み立て → 縮約 → 前処理 → CG の順も同じ)。

        **多重右辺の要点は前処理を作り直さないこと。** `mg_preconditioner` は毎回
        RAP と最粗格子の LU をやり直す(移送作用素だけがキャッシュ済み)ので、右辺ごとに
        `solve` を呼ぶと setup が本数ぶん重なる。同じ設計反復では K が同じなので、
        setup 1回で回せば**追加費用は CG の反復ぶんだけ**になる。反力制約の随伴
        (拘束面グループごとに1本)がこの口を使う。

        `u0` を渡すなら `rhs` と同じ形 (nrhs, ndof)。**右辺が零ベクトルの本は CG を
        回さず零解を返す** —— 残差の相対判定が 0/0 になるため。

        **右辺が最初から揃っていないとき**(反力の随伴は、まず順問題を解かないと右辺が
        決まらない)は `prepare` を直に呼んで `PreparedSystem` を使い回すこと。
        """
        return self.prepare(
            rho, penal, solver=solver, mg=mg, threads=threads
        ).solve(rhs, u0=u0, cg_rtol=cg_rtol)

    def prepare(
        self,
        rho: np.ndarray,
        penal: float,
        *,
        solver: str = "mg",
        mg: MGOptions | None = None,
        threads: int | None = None,
    ) -> "PreparedSystem":
        """K を組み立て、自由DOFへ縮め、前処理を作るところまでをやって返す。

        **返ってきた `PreparedSystem` に右辺を何本でも後から渡せる。** 反力の随伴は
        順問題の解 `u` から右辺が決まるので、`solve_many` のように右辺を最初に
        揃えられない —— それでも setup を1回で済ませるための口である。

        **持っているものは大きい**(K_ff と多重格子の各段)。設計反復の中で使い終わったら
        名前を手放すこと —— 峰メモリを反復の後半まで持ち越すことになる。

        **`dynamic_active=True` なら、ここで `active`/`fixed`/`free` が渡された ρ から
        作り直される**(`_refresh_active`)。返した `PreparedSystem` を使い終わる前に
        別の ρ で `prepare` を呼ぶと `model.free` が変わり、先の `PreparedSystem` の
        `kf` と噛み合わなくなる —— 1つの ρ につき1つを使い切ること。
        """
        if solver not in ("mg", "cg", "direct"):
            raise ValueError(f"未知の解法: {solver!r}")
        # **既定を束縛時ではなく呼び出し時に読む。** こうしておくと `simp.MG_DEFAULT` を
        # 差し替えるだけで V-cycle の形を全経路(anchor → design_space)で
        # 一斉に変えられる。既定値の当たりを取るのに要る。
        mg = MG_DEFAULT if mg is None else mg
        threads = PARALLEL_THREADS if threads is None else max(1, int(threads))
        stats = _SOLVE_STATS
        if self.dynamic_active:
            t_active = time.perf_counter()
            self._refresh_active(rho, penal)
            if stats is not None:
                stats["active_refresh_seconds"] += time.perf_counter() - t_active
        t0 = time.perf_counter()
        kg = self.assemble(rho, penal)
        t1 = time.perf_counter()
        kf = kg[self.free, :][:, self.free].tocsc()
        t2 = time.perf_counter()
        precond = None
        setup_seconds = 0.0
        a_op: sp.spmatrix | spla.LinearOperator = kf
        if solver != "direct":
            if solver == "mg":
                t_setup = time.perf_counter()
                precond = self.mg_preconditioner(kf, mg, threads=threads)
                setup_seconds = time.perf_counter() - t_setup
            else:
                diag = kf.diagonal()
                precond = spla.LinearOperator(kf.shape, matvec=lambda v, d=diag: v / d)
            # **CG 自身の `A@x` も行ブロックで並列化する**(threads<=1 なら `kf` を生のまま
            # 渡すので `spla.cg` は元のコード経路と同じ:`_BlockedMatvec` を経由しない)。
            if threads > 1:
                a_mv = _BlockedMatvec(kf, threads)
                a_op = spla.LinearOperator(kf.shape, matvec=a_mv, dtype=float)
        if stats is not None:
            stats["assemble_seconds"] += t1 - t0
            stats["slice_seconds"] += t2 - t1
            # **setup も「解く時間」に数える。** 右辺ごとの CG は `PreparedSystem.solve` が
            # 足すので、右辺1本のときの `solve_seconds` は割る前と同じ意味(setup + CG)になる。
            stats["solve_seconds"] += time.perf_counter() - t2
            stats["mg_setup_seconds"] += setup_seconds
        return PreparedSystem(self, kf, a_op, precond, solver)

    def stiffness_apply(self, rho: np.ndarray, penal: float, vec: np.ndarray) -> np.ndarray:
        """K @ vec を、K を組み立てずに要素ループで計算する (ndof,)。

        **`assemble` と同じ要素の範囲**(`active`)で足す —— 範囲が違えば別の行列を掛けた
        ことになる(層をまたぐ契約)。反力 `R = (K u - f)|_fixed` と、その随伴の右辺
        `K_fs c` を作るための口で、どちらも K を持っていない場所から呼ばれる。
        """
        flat = np.asarray(rho).ravel()
        edof = self.edof
        if self.active is not None:
            flat = flat[self.active]
            edof = edof[self.active]
        ee = self.emin + flat**penal * (self.E - self.emin)
        # `ke` は対称なので `ve @ ke` が要素ごとの `ke @ v_e` になる。
        contrib = (vec[edof] @ self.ke) * ee[:, None]
        return np.bincount(edof.ravel(), weights=contrib.ravel(), minlength=self.ndof)

    def _verify_solution(self, kf, ff: np.ndarray, u_free: np.ndarray) -> None:
        """`K_ff u = f_f` を残差で検算する。通らなければ拘束不足を名指しして落とす。"""
        finite = bool(np.all(np.isfinite(u_free)))
        rel = float("inf")
        if finite:
            denominator = float(np.linalg.norm(ff))
            rel = float(np.linalg.norm(kf @ u_free - ff)) / denominator
        trace.snapshot('linear-residual', finite=finite, relative_residual=rel,
                       tolerance=SOLVE_RESIDUAL_TOL)
        if finite and rel <= SOLVE_RESIDUAL_TOL:
            return
        trace.snapshot('linear-failure', rhs_free=ff, displacement_free=u_free,
                       free=self.free, fixed=self.fixed, active=self.active)
        raise SolveResidualError(
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
        """(コンプライアンス c, 要素ひずみエネルギー ce) を返す。

        **`c` は組み立てに載せた要素だけで足す**(層をまたぐ契約) —— `assemble` が
        `active` だけで K を作るので、`c = F・U` はこの範囲で足したときにだけ成り立つ。
        全要素で足すと、解いた系に無い emin 媒質のひずみエネルギーが目的値に混ざる。
        `ce` のほうは感度に使うので**全要素ぶん返す**(`optimize` が `x_phys**(penal-1)`
        と `live` で空要素を落とす)。
        """
        ue = u[self.edof]
        ce = np.einsum("ij,jk,ik->i", ue, self.ke, ue).reshape(self.nelx, self.nely, self.nelz)
        ee = self.emin + rho**penal * (self.E - self.emin)
        energy = (ee * ce).ravel()
        c = float(energy.sum() if self.active is None else energy[self.active].sum())
        return c, ce


class PreparedSystem:
    """1つの ρ に対する K_ff と、その前処理。**右辺を何本でも後から解ける。**

    `Model.prepare` が作る。同じ設計反復のあいだ K は変わらないので、右辺を足すたびの
    追加費用は CG の反復ぶんだけになる —— 素朴に `Model.solve` を右辺ごとに呼ぶと、
    多重格子の setup(RAP と最粗格子の LU)が本数ぶん重なる。
    """

    def __init__(
        self,
        model: "Model",
        kf: sp.spmatrix,
        a_op: "sp.spmatrix | spla.LinearOperator",
        precond: "spla.LinearOperator | None",
        solver: str,
    ) -> None:
        self.model = model
        self.kf = kf
        self._a_op = a_op
        self._precond = precond
        self._solver = solver

    def solve(
        self, rhs: np.ndarray, *, u0: np.ndarray | None = None, cg_rtol: float = 1e-9
    ) -> np.ndarray:
        """`rhs` (nrhs, ndof) を解いて (nrhs, ndof) を返す。拘束DOFの成分はきっかり 0.0。

        **右辺が零ベクトルの本は CG を回さず零解を返す** —— 残差の相対判定が 0/0 に
        なるため(随伴の右辺は感度が全部消えた配置で 0 になりうる)。
        """
        model = self.model
        rhs = np.atleast_2d(np.asarray(rhs, dtype=float))
        if rhs.shape[1] != model.ndof:
            raise ValueError(f"右辺の長さが {rhs.shape[1]}、期待は ndof={model.ndof}")
        if u0 is not None:
            u0 = np.atleast_2d(np.asarray(u0, dtype=float))
            if u0.shape != rhs.shape:
                raise ValueError(f"u0 の形が {u0.shape}、期待は右辺と同じ {rhs.shape}")
        stats = _SOLVE_STATS
        t_start = time.perf_counter()
        out = np.zeros((rhs.shape[0], model.ndof))
        for index in range(rhs.shape[0]):
            ff = rhs[index][model.free]
            x0 = np.zeros(model.free.size) if u0 is None else u0[index][model.free].copy()
            iterations = 0
            fell_back = False
            if not np.any(ff):
                u_free = np.zeros(model.free.size)
            elif self._solver == "direct":
                u_free = spla.spsolve(self.kf, ff)
            else:
                counter = None
                if stats is not None or trace.current() is not None:

                    def counter(_xk: np.ndarray) -> None:
                        nonlocal iterations
                        iterations += 1

                u_free, info = spla.cg(
                    self._a_op, ff, x0=x0, M=self._precond, rtol=cg_rtol, callback=counter
                )
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
                        u_free = spla.spsolve(self.kf, ff)
            if stats is not None:
                stats["solves"] += 1
                stats["cg_iterations"] += iterations
                stats["cg_iterations_each"].append(iterations)
                stats["fallbacks"] += int(fell_back)
                stats["direct_solves"] += int(self._solver == "direct")
            trace.snapshot('linear-solve', solver=self._solver, cg_rtol=cg_rtol,
                           cg_iterations=iterations, fallback=fell_back,
                           rhs_index=index, seconds=time.perf_counter()-t_start)
            model._verify_solution(self.kf, ff, u_free)
            out[index, model.free] = u_free
        if stats is not None:
            stats["solve_seconds"] += time.perf_counter() - t_start
        return out


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
class ReactionGroup:
    """反力をまとめて取り出す拘束自由度の集まり(ふつうは CAD の拘束面1枚)。

    **層をまたぐ契約**: `dofs` は `Model.fixed` の部分集合で、グループどうしは
    重ならないこと。重なると合計が全体の釣り合い(Σ R = -F)を超え、`fraction` が
    「その取付点が受け持つ割合」を意味しなくなる。`optimize` が両方とも検算する。
    """

    name: str
    dofs: np.ndarray


@dataclass(frozen=True)
class Reaction:
    """1つの拘束面グループが受け持っている反力。

    **単位は `optimize` に渡した荷重と同じ**(`anchor.run_pipeline` 経由なら無次元で、
    荷重の総量が 1.0)。`fraction` は `magnitude / |全荷重の合力|` なので単位に依らない
    —— 「この取付点が荷重の何割を受け持っているか」を読むのはこちら。
    """

    name: str
    force: tuple[float, float, float]
    magnitude: float
    fraction: float


def load_resultant(f: np.ndarray) -> np.ndarray:
    """荷重ベクトル (ndof,) → 合力 (3,)。自由度番号は `3*節点+軸` なので `dof % 3` が軸。"""
    f = np.asarray(f, dtype=float).ravel()
    return np.bincount(np.arange(f.size) % 3, weights=f, minlength=3)


def reaction_forces(
    model: "Model",
    rho: np.ndarray,
    penal: float,
    u: np.ndarray,
    groups: Sequence[ReactionGroup],
) -> np.ndarray:
    """拘束面グループごとの反力の合力 (len(groups), 3)。

    `R = (K u - f)|_fixed` —— **いま解いている `K u = f` から直接出る**ので、新しい
    順問題は要らない。符号は「支持が構造に及ぼす力」で、全グループを足すと荷重の合力の
    **符号を反転したもの**になる(剛体並進が K の零空間にあることから従う)。
    """
    residual = model.stiffness_apply(rho, penal, u) - model.f
    out = np.zeros((len(groups), 3))
    for index, group in enumerate(groups):
        dofs = np.asarray(group.dofs, dtype=np.int64).ravel()
        out[index] = np.bincount(dofs % 3, weights=residual[dofs], minlength=3)
    return out


def _as_reactions(
    groups: Sequence[ReactionGroup], vectors: np.ndarray, reference: float
) -> tuple[Reaction, ...]:
    """合力ベクトルの並び → `Reaction` の並び。`reference` は荷重の合力の大きさ。"""
    out = []
    for group, vector in zip(groups, vectors):
        magnitude = float(np.linalg.norm(vector))
        out.append(
            Reaction(
                name=group.name,
                force=(float(vector[0]), float(vector[1]), float(vector[2])),
                magnitude=magnitude,
                fraction=magnitude / reference if reference > 0.0 else float("nan"),
            )
        )
    return tuple(out)


def reaction_sensitivity(
    model: "Model",
    system: "PreparedSystem",
    rho: np.ndarray,
    penal: float,
    u: np.ndarray,
    groups: Sequence[ReactionGroup],
    vectors: np.ndarray,
    *,
    adjoint0: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """反力の**大きさ** `|R_i|` の、物理密度に対する感度 (len(groups), nele) と随伴解を返す。

    **反力は自己随伴ではない。** コンプライアンスが追加の線形解なしで感度を作れるのは
    「目的が `f^T u` で、随伴の右辺が荷重そのもの」という特殊事情で、反力にはグループ
    ごとに随伴が1本要る:

        R_i = |v_i|,  v_i = C_i^T (K u - f)      C_i は グループ i の自由度を軸ごとに拾う行列
        c_i = C_i (v_i / |v_i|)                  この反復で凍結する向き(|.| の勾配)
        K_ff λ_i = K_fs c_i
        dR_i/dρ_e = -penal ρ_e^(p-1) (E - emin) · (λ_e^T Ke u_e)
                    ただし λ は自由DOFに λ_i、固定DOFに -c_i を入れた全長ベクトル

    **「全体の釣り合い Σ R = -F があるからグループ数-1本でよい」は、大きさには効かない。**
    一定なのは合力**ベクトル**の和であって大きさの和ではないので、最後の1本の勾配を
    他から作るには各グループの3成分ヤコビアン(3(n-1)本)が要り、n 本より多くなる。

    `adjoint0` は前の反復の随伴解(ウォームスタート)。`system` は**順問題と同じ**
    `PreparedSystem` を渡すこと —— そうすれば多重格子の setup が反復あたり1回で済む。
    """
    ndof = model.ndof
    selectors = np.zeros((len(groups), ndof))
    for index, group in enumerate(groups):
        dofs = np.asarray(group.dofs, dtype=np.int64).ravel()
        magnitude = float(np.linalg.norm(vectors[index]))
        if magnitude > 0.0:
            unit = vectors[index] / magnitude
            selectors[index, dofs] = unit[dofs % 3]
        # 大きさが厳密に 0 の点では |.| が微分できない。その本は右辺が零になり、
        # `PreparedSystem.solve` が零解を返す(= その反復では感度 0)。制約は違反側に
        # あるので、次の反復で他の変数が動けば向きが決まる。
    rhs = np.stack(
        [model.stiffness_apply(rho, penal, selectors[index]) for index in range(len(groups))]
    )
    adjoint = system.solve(rhs, u0=adjoint0)
    # 固定DOF側は -c。`adjoint` は固定DOFがきっかり 0 なので引き算で入る。
    full = adjoint - selectors
    edof = model.edof
    keu = u[edof] @ model.ke
    coefficient = -penal * np.asarray(rho).ravel() ** (penal - 1.0) * (model.E - model.emin)
    out = np.empty((len(groups), edof.shape[0]))
    for index in range(len(groups)):
        out[index] = coefficient * np.einsum("ij,ij->i", full[index][edof], keu)
    return out, adjoint


def _check_reaction_groups(model: "Model", groups: Sequence[ReactionGroup]) -> None:
    """`ReactionGroup` の契約(`fixed` の部分集合・互いに重ならない)を検算する。"""
    if not groups:
        raise ValueError("reactions が空(反力を取り出すグループが1つも無い)")
    collected = []
    for group in groups:
        dofs = np.asarray(group.dofs, dtype=np.int64).ravel()
        if dofs.size == 0:
            raise ValueError(f"反力グループ {group.name!r} に自由度が1つも無い")
        outside = np.setdiff1d(dofs, model.fixed)
        if outside.size:
            raise ValueError(
                f"反力グループ {group.name!r} に拘束されていない自由度が "
                f"{outside.size} 個ある(例 {int(outside[0])})。"
                "反力は固定自由度にしか立たないので、拘束面から作った自由度を渡すこと"
            )
        collected.append(dofs)
    allof = np.concatenate(collected)
    if np.unique(allof).size != allof.size:
        raise ValueError(
            "反力グループどうしが自由度を共有している"
            f"(のべ {allof.size} に対して相異なるのは {np.unique(allof).size})。"
            "同じ節点を2つの取付点に数えると、受け持ちの割合が全体の釣り合いと合わなくなる"
        )


@dataclass(frozen=True)
class Iteration:
    it: int
    compliance: float
    volfrac: float
    change: float
    seconds: float
    # 反力を測っていない(`optimize(reactions=None)`)ときは空。**この反復で解いた
    # `x_phys` に対する値**であって、`step` の後の設計変数に対する値ではない
    # (`compliance` と同じ足並み)。
    reactions: tuple[Reaction, ...] = ()


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
    # 実際に使ったフィルタ半径[要素]。**入力をそのまま写したものではない** —— mm で渡された
    # 場合はここに換算後の値が入る(volfrac/target_volume と同じ役割分担)。
    rmin: float = float("nan")
    # 実際に使った更新則(`UPDATES` の名前)。**体積目標の意味が更新則で変わる** ——
    # `"oc"` は等式(届かなければ `VolumeTargetError`)、`"mma"` は上限(届かなくても
    # 例外にならず軽い解が返る)。受け取った側が `target_volume` との差をどう読むかを
    # 決めるのに要る。
    update: str = DEFAULT_UPDATE
    # 最後に解いた密度場での、拘束面グループごとの反力。`optimize(reactions=...)` を
    # 渡さなければ空。**「効いていない取付点」はここが `fraction ≈ 0` で出る。**
    reactions: tuple[Reaction, ...] = ()
    # 課した反力の下限(荷重の合力に対する割合)。制約を掛けていなければ None。
    reaction_min: float | None = None
    # ペナルティ連続化の入口。**`penal_start` が None なら `penal` 固定**(連続化なし)。
    # `history` のコンプライアンスは、**`penal_iters` 反復目より前は違う目的関数の値**
    # (灰色が許されるぶん小さく出る)。比較するときはそこから後だけを見ること。
    penal_start: float | None = None
    penal_iters: int = 0
    # `optimize(keep_model=True)` のときだけ入る、実際に解いた `Model`。**既定は None** ——
    # `Model` は `_iK`/`_jK` に**組み立てる要素数**×576 の添字配列を2本持つので、要らない
    # 呼び出しにまで数十MBを持たせない。後処理(`stress.py`)が拘束・荷重を手で組み直さずに
    # 済むための口で、組み直しを誤ると**落ちずに違う応力**が出る。
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

    @property
    def rmin_mm(self) -> float | None:
        """実効フィルタ半径[mm]。要素体積を渡していなければ None(格子は立方体要素なので
        `element_volume_mm3 ** (1/3)` が要素サイズ[mm]に厳密に一致する)。"""
        if self.element_volume_mm3 is None:
            return None
        return self.rmin * (self.element_volume_mm3 ** (1.0 / 3.0))


class VolumeTargetError(ValueError):
    """絶対量(mm^3)の体積目標が達成できない。

    **どちら側にどれだけ足りないかを名指しするための例外。** これが無いと OC の二分探索が
    体積と無関係な例外(`ZeroDivisionError` / 「K U = F が解けていない」)で落ちる
    (docs/TOPOPT.md「体積目標を絶対量(mm^3)で指定する」)。
    属性 `side` は "below_minimum" か "above_capacity"。

    **区間の内側でも `above_capacity` で落ちることがある。** 要素数で見れば入る量でも、
    密度フィルタが塊の縁で目減りさせるぶん、ρ を上限まで上げても目標体積に届かない配置が
    あり、そのとき OC の二分探索が退化する。`volfrac` 指定の経路でも起きるので、
    その場合 `material_mm3` / `min_mm3` / `max_mm3` は NaN(要素体積を知らされていない)。
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


class ReactionTargetError(ValueError):
    """反力の下限制約を満たす形が出なかった。

    **満たせない支持配置がある。** 静定な配置では反力の配り方が幾何だけで決まり、
    構造をどう変えても動かせない(hanger は面ごとに多数の節点を固定するので不静定だが、
    一般には成り立たない)。そこへ「各取付点が |F| の ε 倍を受け持て」と要求すると、
    妥協した設計ではなく**失敗**が正しい返事である。

    属性: `required`(要求した割合 ε)/ `reactions`(最後に測った `Reaction` の並び)/
    `result`(そこまでで出た `Result`。**捨てずに渡す** —— 30分回した結果を見ずに
    捨てさせないため)。
    """

    def __init__(
        self,
        message: str,
        *,
        required: float,
        reactions: tuple[Reaction, ...],
        result: "Result",
    ) -> None:
        super().__init__(message)
        self.required = float(required)
        self.reactions = reactions
        self.result = result


class FilterRadiusError(ValueError):
    """密度フィルタ半径を mm で指定したが、要素サイズから決まる下限(1.5要素)を下回る。

    **`rmin` が1要素を大きく下回るとフィルタが実質効かなくなり、チェッカーボード
    (市松模様の密度)が出る**(docs/TOPOPT.md「1.5要素という下限の根拠」が隣接要素の密度差の指標で
    実測している)。1.5要素は安全側に取った下限で、`MIN_RMIN` の値
    (**既定値 `DEFAULT_RMIN` とは別物**)。

    **逃げ道を2つとも数字で持つ**(`VolumeTargetError` と同じ形):
    `max_element_size_mm`(この `rmin_mm` を通すには要素サイズをいくつ以下にすればよいか)・
    `min_elements_long_axis`(それが最長軸の分割数でいくつに当たるか)・
    `min_rmin_mm`(この解像度で指定できる `rmin_mm` の下限)。
    """

    def __init__(
        self,
        message: str,
        *,
        rmin_mm: float,
        element_size_mm: float,
        min_rmin_mm: float,
        max_element_size_mm: float,
        min_elements_long_axis: int,
        shortfall_mm: float,
    ) -> None:
        super().__init__(message)
        self.rmin_mm = rmin_mm
        self.element_size_mm = element_size_mm
        self.min_rmin_mm = min_rmin_mm
        self.max_element_size_mm = max_element_size_mm
        self.min_elements_long_axis = min_elements_long_axis
        self.shortfall_mm = shortfall_mm


#: 反力の下限を「満たした」と読む相対の緩み。**実測では1度も要らなかった** ——
#: 合成題材(遠い壁・ε=0.02〜0.70・40反復)では毎回きっかり境界に乗り、超過は
#: `fraction/ε - 1` で 4.6e-05〜2.1e-03 だった(docs/TOPOPT_REACTION.md)。**この緩みは
#: 下の `floor` のとおり割る側で効くので、絶対差 `fraction - ε` と比べてはいけない**
#: (同じ実測が 1.4e-05〜1.4e-03 になり桁が1つずれる)。0 にしないのは、`max_iter` の
#: 打ち切りで届き切らなかったときに**30分回した結果ごと捨てる**のを避けるための頭であり、
#: 実際の割合は `Result.reactions` に残るので呼び出し側が自分で厳しく見てよい。
REACTION_TOLERANCE = 0.02


def _require_reactions(
    result: "Result",
    reactions: tuple[Reaction, ...],
    required: float,
) -> None:
    """最後に測った反力が下限を満たしているかを見て、満たしていなければ落とす。

    **満たしていない解を黙って返さない。** 反力の下限は「全ての取付点が実際に荷重を
    受け持つ」という取付要求で、満たしていない形は保持ソリッドが浮き島になって
    `mesh.remove_islands` に消える —— そこまで進んでから「なぜか部品が足りない」に
    なるより、ここで名指しして落ちるほうがよい。
    """
    if not reactions:
        # **1度も測っていないものを「満たした」と読まない。** 反復が1回も回らなければ
        # (`max_iter=0`・早期break)ここは空で来る。空のまま return すると
        # `reaction_min` を持ったまま無検証の `Result` が合格として出る。
        raise ReactionTargetError(
            f"反力の下限 {required:.1%} を課したのに、反力を1度も測っていない"
            "(最適化の反復が1回も回っていない)。max_iter を1以上にすること",
            required=required,
            reactions=reactions,
            result=result,
        )
    floor = required * (1.0 - REACTION_TOLERANCE)
    short = [item for item in reactions if not item.fraction >= floor]
    if not short:
        return
    breakdown = "、".join(f"{item.name} {item.fraction:.2%}" for item in reactions)
    named = "、".join(f"{item.name}({item.fraction:.2%})" for item in short)
    raise ReactionTargetError(
        f"反力の下限 {required:.1%} を満たさない取付点が {len(short)}/{len(reactions)} ある: "
        f"{named}。内訳は {breakdown}(荷重の合力に対する割合)。"
        "**支持配置によっては満たせない** —— 静定な配置では反力の配り方が幾何だけで決まり、"
        "構造を変えても動かせない。εを下げる / 材料を増やす / 反復を増やす、"
        "のどれかを試すこと(そのどれでも動かないなら、その取付点は構造上いらない)",
        required=required,
        reactions=reactions,
        result=result,
    )


def check_rmin_target(rmin: float | None, rmin_mm: float | None) -> None:
    """密度フィルタ半径の**引数の形**だけを検算する(格子を作る前に落とすための口)。

    上位層(`anchor.run_pipeline` 等)がボクセル化の前にこれを呼ぶ。要素サイズを知っている
    層でしか見られない「下限を割っていないか」の判定は `_resolve_rmin` が持つ
    (`check_volume_target` / `_resolve_volume_target` と同じ役割分担)。
    """
    if rmin is not None and rmin_mm is not None:
        raise ValueError(
            "rmin と rmin_mm は排他(どちらか一方だけ渡すこと)。"
            f"rmin={rmin!r} / rmin_mm={rmin_mm!r}"
        )
    if rmin is not None and not float(rmin) > 0.0:
        raise ValueError(f"rmin は正の値であること(受け取った値: {rmin!r})")
    if rmin_mm is not None and not float(rmin_mm) > 0.0:
        raise ValueError(f"rmin_mm は正の値であること(受け取った値: {rmin_mm!r})")


def _resolve_rmin(
    *,
    rmin: float | None,
    rmin_mm: float | None,
    element_volume_mm3: float | None,
    nelx: int,
    nely: int,
    nelz: int,
) -> float:
    """フィルタ半径を実効値(要素単位)へ畳む。

    `rmin_mm` は要素サイズが要る。格子は立方体要素(`voxelize.Grid`)なので
    `element_volume_mm3 ** (1/3)` が要素の一辺 = 要素サイズ[mm]に厳密に一致する
    (別の値を測り直す必要はない)。要素サイズから決まる下限(`MIN_RMIN` = 1.5要素。
    **既定値 `DEFAULT_RMIN` とは別物**)を下回ると `FilterRadiusError`。
    """
    if rmin_mm is None:
        return DEFAULT_RMIN if rmin is None else float(rmin)
    if element_volume_mm3 is None:
        raise ValueError(
            "rmin_mm を渡すときは element_volume_mm3(要素1つの体積[mm^3])も渡すこと"
            "(optimize は格子の寸法を知らないので、mm から要素単位へ換算できない)"
        )
    element_size_mm = float(element_volume_mm3) ** (1.0 / 3.0)
    resolved = float(rmin_mm) / element_size_mm
    if resolved < MIN_RMIN:
        min_rmin_mm = MIN_RMIN * element_size_mm
        max_element_size_mm = float(rmin_mm) / MIN_RMIN
        elements_long_axis = max(int(nelx), int(nely), int(nelz))
        length_long_axis_mm = element_size_mm * elements_long_axis
        min_elements_long_axis = int(np.ceil(length_long_axis_mm / max_element_size_mm))
        raise FilterRadiusError(
            f"rmin_mm={float(rmin_mm):.6g} はこの解像度(要素サイズ {element_size_mm:.6g}mm、"
            f"最長軸 {elements_long_axis}分割)では表現できない: 換算すると "
            f"{resolved:.4g}要素で、下限の {MIN_RMIN}要素に "
            f"{min_rmin_mm - float(rmin_mm):.6g}mm 足りない。"
            "**逃げ道は2つ**: "
            f"(1) この rmin_mm を通すには要素サイズを {max_element_size_mm:.6g}mm 以下にする"
            f"(最長軸で {min_elements_long_axis}分割以上に相当)。"
            f"(2) この解像度で指定できる rmin_mm の下限は {min_rmin_mm:.6g}mm。",
            rmin_mm=float(rmin_mm),
            element_size_mm=element_size_mm,
            min_rmin_mm=min_rmin_mm,
            max_element_size_mm=max_element_size_mm,
            min_elements_long_axis=min_elements_long_axis,
            shortfall_mm=min_rmin_mm - float(rmin_mm),
        )
    return resolved


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


# ---------------------------------------------------------------------------
# 更新則(OC / MMA)
# ---------------------------------------------------------------------------
# **差し替えられるのは更新則だけ。** 組み立て・多重格子・CG(`Model`)、密度フィルタ、
# 感度フィルタ(`dcn`/`dvn`)、パッシブ要素の扱い(`_physical`)は共通で、どの更新則も
# 同じ感度を受け取る。


@dataclass(frozen=True)
class Constraint:
    """更新則へ渡す**追加の不等式制約** `g(x) <= 0`。体積制約は含めない(更新則が自分で持つ)。

    **層をまたぐ契約**: `value` は現在点での g、`grad` は**設計変数**に対する感度
    (密度フィルタを通した後。`dcn`/`dvn` と同じ足並み)。どちらも**1のオーダーへ
    正規化して渡すこと** —— MMA の凸化の下駄(`MMA_RAA0`)が絶対値なので、桁が題材ごとに
    変わると近似の効き方が変わる。
    """

    name: str
    value: float
    grad: np.ndarray


class _Update:
    """更新則の口。`optimize` が1反復に1回 `step()` を呼ぶ。

    **層をまたぐ契約**:

    - `step()` が返すのは**設計変数の新しい値だけ**。物理密度への写像(`_physical`)・
      収束判定・履歴・`Result` の組み立ては `optimize` の仕事で、更新則は触らない。
    - **パッシブ要素(`void`/`solid`)の設計変数を動かしてはならない。** `_physical` が
      物理密度を上書きするので、動かしても物理には出ないが、体積制約の勘定が
      「動かしても物理体積が変わらない変数」に対して立ってしまう。
    - 目的(コンプライアンス)と体積の**感度は `optimize` が作って渡す**。更新則が
      フィルタを掛け直すことはない。
    - `constraints` は**体積以外の追加の不等式制約**(`Constraint`)。受け取れない更新則は
      **黙って無視せず例外**にすること —— 制約が効かないまま「もっともらしい形」が
      返るのが最悪である。
    """

    def __init__(
        self,
        *,
        move: float,
        target_volume: float,
        void: np.ndarray,
        solid: np.ndarray,
        physical: Callable[[np.ndarray], np.ndarray],
        n_movable: int,
        n_solid: int,
        element_volume_mm3: float | None,
    ) -> None:
        self.move = move
        self.target_volume = target_volume
        self.void = void
        self.solid = solid
        self.physical = physical
        self.n_movable = n_movable
        self.n_solid = n_solid
        self.element_volume_mm3 = element_volume_mm3

    def step(
        self,
        *,
        it: int,
        x: np.ndarray,
        x_phys: np.ndarray,
        compliance: float,
        dcn: np.ndarray,
        dvn: np.ndarray,
        constraints: Sequence[Constraint] = (),
    ) -> np.ndarray:
        raise NotImplementedError


class _OCUpdate(_Update):
    """OC(Optimality Criteria)。ラグランジュ乗数1本の二分探索。

    **制約1本でしか成り立たない。** 乗数が1つのスカラーで、二分探索の判定が
    「体積が目標を超えたか」の1ビットだから、制約を2本目にすると探索する対象が
    1次元でなくなる。反力制約のように制約が増える段では `mma` を使う。
    """

    def step(
        self,
        *,
        it: int,
        x: np.ndarray,
        x_phys: np.ndarray,
        compliance: float,
        dcn: np.ndarray,
        dvn: np.ndarray,
        constraints: Sequence[Constraint] = (),
    ) -> np.ndarray:
        if constraints:
            raise ValueError(
                f"OC は制約を1本しか扱えない(体積のほかに {len(constraints)} 本渡された: "
                f"{[item.name for item in constraints]})。"
                "乗数がスカラー1つで、二分探索の判定が「体積が目標を超えたか」の1ビット"
                "だから、制約を増やすと探索する対象が1次元でなくなる。"
                'update="mma" を使うこと'
            )
        move, void, solid = self.move, self.void, self.solid
        target_volume = self.target_volume
        l1, l2 = 0.0, 1e9
        # **二分探索が退化することがある。** 目標体積がこの反復で到達できる上限
        # (移動制限 `move` と、密度フィルタが塊の縁で落とすぶん)を超えていると、`l2` が 0 まで
        # 潰れて `(l2 - l1) / (l1 + l2)` が 0除算になる —— 文面に体積も塊も出ないので、
        # 原因から一番遠い場所で落ちる。**健全な側の計算は
        # 1ビットも変えない**: `l1 + l2 > 0.0` は退化していない限りつねに真。
        while l1 + l2 > 0.0 and (l2 - l1) / (l1 + l2) > 1e-9:
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
            xp = self.physical(xnew)
            if xp.sum() > target_volume:
                l1 = lm
            else:
                l2 = lm
        if l1 + l2 <= 0.0:
            ev = (
                float(self.element_volume_mm3)
                if self.element_volume_mm3 is not None
                else float("nan")
            )
            n_movable, n_solid = self.n_movable, self.n_solid
            capacity = float(n_movable + n_solid)
            reached = float(xp.sum())
            raise VolumeTargetError(
                f"体積目標 {target_volume:.6g} 要素に対し、この反復("
                f"it={it})で到達できる最大が {reached:.6g} 要素しかない: "
                f"塊の容量は {capacity:.6g} 要素(動かせる {n_movable} + 固定材料 {n_solid})で、"
                f"目標はその {target_volume / capacity:.1%}。"
                "**塊に対して材料が多すぎる** —— 密度フィルタが塊の縁で目減りさせるぶん、"
                "要素数で入る量でも ρ を上限まで上げて届かないことがある。"
                "塊を広げる(連続値保持なら k を上げる)か、"
                "材料を減らすこと",
                material_mm3=target_volume * ev,
                min_mm3=float("nan"),
                max_mm3=capacity * ev,
                shortfall_mm3=(target_volume - reached) * ev,
                side="above_capacity",
                element_volume_mm3=ev,
            )
        return xnew


# MMA(Method of Moving Asymptotes、Svanberg 1987)の定数。**論文の記述から自前で実装した**
# —— 出回っている Python 実装(`mmapy` / `GCMMA-MMA-Python`)は原典の MATLAB が GPLv3 なので
# 見ていない(2026-09-10 本人決定。配布方針が未定のうちは GPL を取り込まない)。
#
# 値はどれも文献の標準値そのままで、この題材で選び直した数字は1つも無い。
# 設計変数の範囲は [0, 1] 固定なので「範囲に対する比」はそのまま設計変数の絶対値でもある。
MMA_ASYMPTOTE_INIT = 0.5  # 最初の2反復の漸近線の半幅[範囲比]。直前2点が無く縮めようがない
MMA_ASYMPTOTE_SHRINK = 0.7  # 直前2歩で符号が反転した変数(振動)の漸近線を縮める率
MMA_ASYMPTOTE_GROW = 1.2  # 符号が揃った変数(単調)の漸近線を広げる率
MMA_ASYMPTOTE_MIN = 0.01  # 漸近線を x に近づけてよい下限[範囲比]。近すぎると歩幅が消える
MMA_ASYMPTOTE_MAX = 10.0  # 漸近線を x から離してよい上限[範囲比]。遠いと近似がただの線形
MMA_ALBEFA = 0.1  # 移動限界を漸近線から離す割合。漸近線の極に触ると近似が発散する
# 近似を**厳密に**凸にするための下駄。感度が 0 の変数でも p/q が正になるので、部分問題の
# 解の閉じた式(下の `_x_of`)の分母が 0 にならない。目的と制約を1のオーダーへ正規化した
# 上での 1e-5 なので、感度が正常な変数には効かない。
MMA_RAA0 = 1e-5
# 双対変数(体積制約の乗数)を1次元二分探索で決めるときの上界と回数。
# 上界は OC の `l2 = 1e9` と揃えた(乗数が大きいほど解は移動限界の下端へ寄るので、
# ここまで振っても届かなければ「この反復で到達できる最小体積」に達している)。
# 60回で区間は 1e9 * 2^-60 ≈ 1e-9 まで縮む(OC の停止条件 1e-9 と同じ桁)。
MMA_DUAL_MAX = 1e9
MMA_DUAL_ITERS = 60
# 制約が2本以上あるときの双対(L-BFGS-B)。上限は Svanberg の人工変数の罰金 c_j にあたり、
# **部分問題が必ず解けることを保証する**(その反復で満たせない制約があっても双対が発散しない)。
# 文献の標準値 1000 をそのまま使っている —— 目的も制約も1のオーダーへ正規化してあるので、
# 「制約1単位の違反は目的 1000 単位ぶん悪い」という重み付けになる。
MMA_CONSTRAINT_PENALTY = 1000.0
MMA_DUAL_MAX_ITERS = 200


class _MMAUpdate(_Update):
    """MMA(Method of Moving Asymptotes)。

    **OC と違って体積は不等式制約**(`sum(rho) <= target`)で、乗数が 0 に落ちれば
    目標より軽い解をそのまま返す。OC のように「目標にきっかり乗せる」ことはしないし、
    目標へ届かないときに `VolumeTargetError` を上げることもない
    (`optimize` の docstring「更新則を選ぶ」節)。

    **反復をまたいで状態を持つ**(OC は `x` 以外の状態を持たない): 移動漸近線
    `_low`/`_upp` と直前2点 `_xold1`/`_xold2`。1つの `_MMAUpdate` は1回の
    `optimize` 呼び出しに属する。
    """

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._low: np.ndarray | None = None
        self._upp: np.ndarray | None = None
        self._xold1: np.ndarray | None = None
        self._xold2: np.ndarray | None = None
        self._c0: float | None = None
        self._lam: np.ndarray | None = None

    def step(
        self,
        *,
        it: int,
        x: np.ndarray,
        x_phys: np.ndarray,
        compliance: float,
        dcn: np.ndarray,
        dvn: np.ndarray,
        constraints: Sequence[Constraint] = (),
    ) -> np.ndarray:
        span = 1.0  # 設計変数の範囲 [0, 1] の幅
        # **目的と制約を1のオーダーへ揃える。** 乗数の二分探索は正規化に対して不変
        # (目的を定数倍すると乗数が同じだけ動くだけ)だが、`MMA_RAA0` の下駄だけは
        # 絶対値なので、感度の桁が題材ごとに変わると効き方が変わる。基準は初回の
        # コンプライアンス(以後の反復でも同じ基準を使う —— 反復ごとに取り直すと
        # 近似の尺度が毎回変わる)。
        if self._c0 is None:
            self._c0 = abs(float(compliance)) or 1.0
        df0 = dcn / self._c0
        # 体積制約 g(x) = sum(rho_phys) / target - 1 <= 0。感度 `dvn` は OC と同じもの。
        g = float(x_phys.sum()) / self.target_volume - 1.0
        dg = dvn / self.target_volume

        if self._xold2 is None or self._low is None or self._upp is None:
            low = x - MMA_ASYMPTOTE_INIT * span
            upp = x + MMA_ASYMPTOTE_INIT * span
        else:
            # 直前2歩の符号が反転した変数は振動しているので漸近線を寄せて歩幅を殺し、
            # 揃った変数は伸ばして歩幅を出す。
            sign = (x - self._xold1) * (self._xold1 - self._xold2)  # type: ignore[operator]
            gamma = np.where(
                sign < 0.0,
                MMA_ASYMPTOTE_SHRINK,
                np.where(sign > 0.0, MMA_ASYMPTOTE_GROW, 1.0),
            )
            low = x - gamma * (self._xold1 - self._low)  # type: ignore[operator]
            upp = x + gamma * (self._upp - self._xold1)  # type: ignore[operator]
        low = np.clip(low, x - MMA_ASYMPTOTE_MAX * span, x - MMA_ASYMPTOTE_MIN * span)
        upp = np.clip(upp, x + MMA_ASYMPTOTE_MIN * span, x + MMA_ASYMPTOTE_MAX * span)

        alpha = np.maximum(
            np.maximum(0.0, low + MMA_ALBEFA * (x - low)), x - self.move * span
        )
        beta = np.minimum(
            np.minimum(1.0, upp - MMA_ALBEFA * (upp - x)), x + self.move * span
        )
        # **パッシブ要素の設計変数を部分問題の中でも凍結する。** 上書きで後から戻すだけだと、
        # 体積制約の勘定が「動かしても物理体積が変わらない変数」の移動を当てにしてしまう
        # (`_Update` の契約)。感度 `dvn` 自体はパッシブ要素でも 0 ではない —— フィルタ越しに
        # 隣の自由要素の物理密度を動かすため —— ので、感度を刈るのでは足りない。
        passive = self.void | self.solid
        alpha = np.where(passive, x, alpha)
        beta = np.where(passive, x, beta)

        ud = upp - x
        ld = x - low
        raa = MMA_RAA0 / (upp - low)
        # 分離可能な凸近似 f(x) ≈ r + Σ p/(U-x) + q/(x-L)。感度の正の側を p、負の側を q へ
        # 振り、逆側にも 0.001 を残す(片側が厳密に 0 だと近似がその方向へ無限に伸びる)。
        p0 = ud * ud * (1.001 * np.maximum(df0, 0.0) + 0.001 * np.maximum(-df0, 0.0) + raa)
        q0 = ld * ld * (0.001 * np.maximum(df0, 0.0) + 1.001 * np.maximum(-df0, 0.0) + raa)
        p1 = ud * ud * (1.001 * np.maximum(dg, 0.0) + 0.001 * np.maximum(-dg, 0.0) + raa)
        q1 = ld * ld * (0.001 * np.maximum(dg, 0.0) + 1.001 * np.maximum(-dg, 0.0) + raa)
        # 定数項は「現在点で近似が真値に一致する」ことから決まる。
        r1 = g - float((p1 / ud + q1 / ld).sum())

        if constraints:
            # **制約が2本以上あると乗数が1次元でなくなる。** 双対は m 次元の凹関数の
            # 最大化になるので、閉じた形の `x(λ)` を使ったまま L-BFGS-B へ渡す(下)。
            # 体積1本のときは**下の二分探索をそのまま通す** —— `20260910-1313` が
            # 測った MMA の数値を、制約を足したあとも再現できるようにしておくため。
            extra_p = []
            extra_q = []
            extra_r = []
            for item in constraints:
                dgj = np.asarray(item.grad)
                pj = ud * ud * (
                    1.001 * np.maximum(dgj, 0.0) + 0.001 * np.maximum(-dgj, 0.0) + raa
                )
                qj = ld * ld * (
                    0.001 * np.maximum(dgj, 0.0) + 1.001 * np.maximum(-dgj, 0.0) + raa
                )
                extra_p.append(pj)
                extra_q.append(qj)
                extra_r.append(float(item.value) - float((pj / ud + qj / ld).sum()))
            xnew = self._dual_step(
                low=low,
                upp=upp,
                alpha=alpha,
                beta=beta,
                p0=p0,
                q0=q0,
                p_stack=np.stack([p1] + extra_p),
                q_stack=np.stack([q1] + extra_q),
                r_vec=np.array([r1] + extra_r),
            )
            self._xold2 = self._xold1
            self._xold1 = x.copy()
            self._low, self._upp = low, upp
            xnew[self.void] = 0.0
            xnew[self.solid] = 1.0
            return xnew

        def _x_of(lam: float) -> np.ndarray:
            """乗数 `lam` を固定したときの部分問題の解(変数ごとに閉じた式)。

            P/(U-x) + Q/(x-L) を最小にするのは x = (L√P + U√Q)/(√P + √Q)。
            `MMA_RAA0` のおかげで P も Q も正なので分母は 0 にならない。
            """
            sp_ = np.sqrt(p0 + lam * p1)
            sq_ = np.sqrt(q0 + lam * q1)
            return np.clip((low * sp_ + upp * sq_) / (sp_ + sq_), alpha, beta)

        def _violation(xn: np.ndarray) -> float:
            """近似した体積制約の値。正なら目標超過。"""
            return float((p1 / (upp - xn) + q1 / (xn - low)).sum()) + r1

        # 乗数を上げるほど解は軽くなる(単調)ので1次元の二分探索で足りる。
        if _violation(_x_of(0.0)) <= 0.0:
            # **制約が効いていない。** 目標より軽い解をそのまま返す —— これが OC との
            # いちばん大きな違いで、体積は等式ではなく上限として扱われる。
            xnew = _x_of(0.0)
        else:
            lo, hi = 0.0, MMA_DUAL_MAX
            for _ in range(MMA_DUAL_ITERS):
                mid = 0.5 * (lo + hi)
                if _violation(_x_of(mid)) > 0.0:
                    lo = mid
                else:
                    hi = mid
            # 上界の側を採る(制約を満たす側)。`hi` が動かないまま抜けたときは、
            # この反復の移動限界の中に目標を満たす点が無い —— そのときの解は
            # 「この反復で到達できるいちばん軽い配置」で、例外にはしない。
            xnew = _x_of(hi)

        self._xold2 = self._xold1
        self._xold1 = x.copy()
        self._low, self._upp = low, upp
        # パッシブ要素は `alpha == beta == x` で凍結済みだが、設計変数の不変条件
        # (void は 0、solid は 1)はここで名指しで守る。
        xnew[self.void] = 0.0
        xnew[self.solid] = 1.0
        return xnew

    def _dual_step(
        self,
        *,
        low: np.ndarray,
        upp: np.ndarray,
        alpha: np.ndarray,
        beta: np.ndarray,
        p0: np.ndarray,
        q0: np.ndarray,
        p_stack: np.ndarray,
        q_stack: np.ndarray,
        r_vec: np.ndarray,
    ) -> np.ndarray:
        """制約が m 本(m>=2)の部分問題を、双対の最大化で解く。

        双対関数 `W(λ) = min_x L(x, λ)` は凹で、`x(λ)` は変数ごとに閉じた形で出る
        (体積1本のときと同じ式)。Danskin より `dW/dλ_j` はその点での近似した制約値
        そのものなので、勾配つきの箱制約最適化(L-BFGS-B)へそのまま渡せる。

        **λ の上限 `MMA_CONSTRAINT_PENALTY` は人工変数の重み。** Svanberg の定式化は
        制約ごとに緩和変数 `y_j >= 0` と罰金 `c_j y_j` を持ち、その双対がちょうど
        `λ_j <= c_j` という箱になる。**部分問題が常に解けることを保証するための装置**で、
        これが無いと、その反復で満たせない制約があったときに双対が発散する。
        """
        def x_of(lam: np.ndarray) -> np.ndarray:
            sp_ = np.sqrt(p0 + np.tensordot(lam, p_stack, axes=1))
            sq_ = np.sqrt(q0 + np.tensordot(lam, q_stack, axes=1))
            return np.clip((low * sp_ + upp * sq_) / (sp_ + sq_), alpha, beta)

        def negative_dual(lam: np.ndarray) -> tuple[float, np.ndarray]:
            xn = x_of(lam)
            ud_ = upp - xn
            ld_ = xn - low
            value = float(
                ((p0 + np.tensordot(lam, p_stack, axes=1)) / ud_
                 + (q0 + np.tensordot(lam, q_stack, axes=1)) / ld_).sum()
            ) + float(lam @ r_vec)
            grad = (p_stack / ud_).sum(axis=tuple(range(1, p_stack.ndim))) + (
                q_stack / ld_
            ).sum(axis=tuple(range(1, q_stack.ndim))) + r_vec
            return -value, -grad

        size = r_vec.size
        start = (
            self._lam
            if self._lam is not None and self._lam.size == size
            else np.zeros(size)
        )
        found = sopt.minimize(
            negative_dual,
            start,
            jac=True,
            method="L-BFGS-B",
            bounds=[(0.0, MMA_CONSTRAINT_PENALTY)] * size,
            options={"maxiter": MMA_DUAL_MAX_ITERS, "ftol": 1e-15, "gtol": 1e-10},
        )
        self._lam = np.clip(np.asarray(found.x, dtype=float), 0.0, MMA_CONSTRAINT_PENALTY)
        return x_of(self._lam)


#: `optimize(update=...)` で選べる更新則。**未知の名前は既定へ落とさず例外**
UPDATES: dict[str, type[_Update]] = {
    "oc": _OCUpdate,
    "mma": _MMAUpdate,
}

if DEFAULT_UPDATE not in UPDATES:  # 既定名を消したまま気づかない事故を止める
    raise RuntimeError(f"DEFAULT_UPDATE={DEFAULT_UPDATE!r} が UPDATES に無い")


def penal_at(it: int, penal: float, start: float | None, iters: int) -> float:
    """反復 `it`(1始まり)で使うペナルティ。`start` が None なら `penal` 固定。

    **低い充填率では連続化は逆効果である(2026-09-12 実測)。**
    `hanger_bar_attachment` 2.412mm・塊 87,752要素・充填率 5.8%・120反復で、
    `start=1.0` / `iters=20` はコンプライアンスを **+18.5%** 悪化させ(111.44 → 132.06)、
    **保持ソリッドを2つ殺し**(結果メッシュから消える)、秒も 26% 増えた。収束も速くならない。
    機序: 充填率が低いと penal の低い区間の最適解が「材料を薄く広げた灰色場」になり、
    形の情報を持たない —— 一様な初期場より悪い出発点から penal=3 の問題を始めることになる
    (it=20 で C=358.97 対 連続化なし 137.95)。**この口を既定にしない。**

    `start` から `penal` まで `iters` 反復かけて線形に上げ、以降は `penal` のまま。
    **`it == iters` でちょうど `penal` に届く**(`iters=1` なら初回から `penal`)。

    **これは目的関数を反復の途中で変える。** 低い penal の区間のコンプライアンスは
    `penal` のそれと比べられない(灰色が許されるぶん必ず小さく出る)ので、**収束の比較は
    両方が `penal` に達したあとの区間で行うこと。**

    **反復をまたぐ状態を持つ更新則(MMA の漸近線・直前2点)とは相性の検証が要る** ——
    それらは同じ目的関数で測った点を前提にしている。OC は状態を持たないので影響しない。
    """
    if start is None:
        return float(penal)
    if it >= int(iters):
        return float(penal)
    span = max(int(iters) - 1, 1)
    return float(start) + (float(penal) - float(start)) * (int(it) - 1) / span


@trace.stage('simp')
def optimize(
    nelx: int,
    nely: int,
    nelz: int,
    *,
    volfrac: float | None = None,
    material_mm3: float | None = None,
    element_volume_mm3: float | None = None,
    penal: float = 3.0,
    penal_start: float | None = None,
    penal_iters: int = 20,
    rmin: float | None = None,
    rmin_mm: float | None = None,
    max_iter: int = 60,
    change_tol: float = 0.01,
    E: float = 1.0,
    nu: float = 0.3,
    emin: float = 1e-9,
    move: float = 0.2,
    update: str = DEFAULT_UPDATE,
    solver: str = "mg",
    cg_rtol: float = 1e-9,
    threads: int | None = None,
    callback: Callable[[Iteration, np.ndarray], None] | None = None,
    fixed_dofs: np.ndarray | None = None,
    load: np.ndarray | None = None,
    void_mask: np.ndarray | None = None,
    solid_mask: np.ndarray | None = None,
    reactions: Sequence[ReactionGroup] | None = None,
    reaction_min: float | None = None,
    keep_model: bool = False,
    dynamic_active: bool = False,
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
    `dynamic_active` と併せて使うときは `Model.reset_active` で静的な状態へ戻して返す。

    `dynamic_active=True` にすると、`Model.prepare` が**毎反復 ρ から active set を
    作り直す** —— `emin**(1/penal)` を下回った要素を `void_mask` と同じ扱いで組み立てと
    自由度から外す(`Model.dead_rho_threshold`)。**既定は False で、そのときの数値は
    1ビットも変わらない。** 効く場・効かない場・限界は docs/TOPOPT_ACTIVE_SET.md。

    フィルタ半径も2通りで渡せる。**`rmin` と `rmin_mm` は排他**(両方渡すと例外):

    - `rmin` —— 要素単位。**格子(解像度)を変えると物理的な平滑化スケールが変わる。**
    - `rmin_mm` + `element_volume_mm3` —— **物理単位(mm)で**指定する。`optimize` は
      格子の寸法を知らないので、要素1つの体積 `element_volume_mm3` を一緒に渡す
      (格子は立方体要素なので、その立方根が要素サイズ[mm]になる)。要素サイズから決まる
      床(`MIN_RMIN` = 1.5要素。下回るとチェッカーボードが出る)を下回ると
      `FilterRadiusError`(逃げ道2つを数字で持つ)。
    - どちらも渡さなければ `DEFAULT_RMIN`。**床とは別物** —— 床は「これより下は壊れる」、
      既定は「普通に使ったときに出したい形」。

    `reactions` は**反力を取り出す拘束自由度のグループ**(ふつう CAD の拘束面1枚に1つ)。
    渡すと各反復で `R = (K u - f)|_fixed` をグループごとに足し、`Iteration.reactions` と
    `Result.reactions` に載せる。**渡さなければ1つも計算しない**(既定の費用は変わらない)。
    追加費用は反復あたり要素ループ1回ぶんで、線形解は増えない。

    `reaction_min` は**各グループが受け持つ反力の下限**(荷重の合力に対する割合 ε)。
    渡すと `|R_i| >= ε |F|` を制約に足す —— 構造最適化は定義上、荷重を運ばない材料を
    置かないので、**これが無いと拘束面が1枚あれば足り、残りの取付点は形から消える**。

    - **「全部を等しくする」は採らない。** 反力はベクトルで、面の向きがばらばらだと
      釣り合いを満たしたまま大きさを揃えられる保証がなく、実現不可能な制約は
      「妥協した設計」ではなく失敗を返す。下限だけを課す。
    - `update="mma"` が要る(体積と合わせて制約が複数本になる)。`reactions` も要る。
    - **費用はグループ数ぶんの随伴**(`reaction_sensitivity`)。多重格子の setup は
      反復あたり1回のままで、増えるのは CG の反復ぶんだけ。
    - 最後まで回しても満たせなければ `ReactionTargetError`(そこまでの `Result` を
      例外に付けて返す)。

    **更新則を選ぶ** —— `update` は `UPDATES` の名前(既定 `DEFAULT_UPDATE` = `"oc"`)。
    知らない名前は既定へ落とさず `ValueError`。差し替わるのは更新則だけで、`Model`・
    密度フィルタ・感度フィルタ・パッシブ要素の扱いは共通(`_Update` の契約)。

    - `"oc"` —— ラグランジュ乗数1本の二分探索。**体積は等式**として扱い、目標に乗せる。
      その反復で目標へ届かないと `VolumeTargetError(side="above_capacity")`。
      **制約1本でしか成り立たない。**
    - `"mma"` —— 移動漸近線法。**体積は不等式(上限)**として扱う。目標より軽い解を
      返すことがあり、届かなくても例外にならない(`Result.target_volume` と
      `rho.sum()` の差を呼び出し側が自分で見ること)。制約を増やせる。
    """
    check_volume_target(volfrac, material_mm3)
    check_rmin_target(rmin, rmin_mm)
    if material_mm3 is not None and element_volume_mm3 is None:
        raise ValueError(
            "material_mm3 を渡すときは element_volume_mm3(要素1つの体積[mm^3])も渡すこと"
            "(optimize は格子の寸法を知らないので、要素数へ換算できない)"
        )
    if element_volume_mm3 is not None and not float(element_volume_mm3) > 0.0:
        raise ValueError(
            f"element_volume_mm3 は正の値であること(受け取った値: {element_volume_mm3!r})"
        )
    if reaction_min is not None:
        # **空の並びも None と同じに落とす。** 空だと制約が1本も立たず、終端の
        # `_require_reactions` も「満たさない取付点は0件」で素通りするので、
        # **下限を頼んだのに何も掛かっていない解が「合格」として返る。**
        if reactions is None or not tuple(reactions):
            raise ValueError(
                "reaction_min を渡すときは reactions(反力を取り出す拘束面グループ)を"
                "1つ以上渡すこと(何に対する下限かが決まらない)"
            )
        if not 0.0 < float(reaction_min) < 1.0:
            raise ValueError(
                f"reaction_min は 0 < ε < 1 であること(受け取った値: {reaction_min!r})"
            )
        if update != "mma":
            raise ValueError(
                f"反力の下限制約は update={update!r} では扱えない —— 体積と合わせて制約が"
                f" {1 + len(tuple(reactions))} 本になり、OC の乗数1本の二分探索が"
                '成り立たない。update="mma" を使うこと'
            )
    if penal_start is not None:
        if not 0.0 < float(penal_start) <= float(penal):
            raise ValueError(
                f"penal_start は 0 < start <= penal であること"
                f"(受け取った値: {penal_start!r} / penal={penal!r})"
            )
        if int(penal_iters) < 1:
            raise ValueError(f"penal_iters は1以上であること(いま {penal_iters!r})")
        # **途中で終わると灰色のまま答えが出る。** penal が目標に届く前に反復を使い切ると、
        # 中間密度が残った密度場が `rho` として返り、メッシュ側の閾値 0.5 がそれを
        # 切り取る —— 例外も警告も出ないまま「別の問題の解」が製品を通る。
        if int(penal_iters) > int(max_iter):
            raise ValueError(
                f"penal_iters({penal_iters})が max_iter({max_iter})を超えている —— "
                f"penal が {penal} へ届く前に終わるので、灰色の残った密度場が返る"
            )
        # **MMA は反復をまたぐ状態が「同じ目的関数で測った点」であることを前提にしている。**
        # `_c0`(感度の正規化)は初回のコンプライアンスで固定されるので、連続化すると
        # 低い penal の値が基準になる(実測 PRESET: penal=1 の初回 39.6 対 penal=3 の 440.3
        # で11倍)。漸近線と直前2点も別の目的関数上の点になる。**確かめていない組み合わせを
        # 黙って通さない。**
        if update == "mma":
            raise ValueError(
                "ペナルティ連続化は update='mma' では確かめていない —— MMA は感度の正規化"
                "(初回のコンプライアンス)・移動漸近線・直前2点を反復をまたいで持つので、"
                "目的関数が反復ごとに変わる前提になっていない。update='oc' を使うこと"
            )
    if dynamic_active and update == "mma":
        # MMA が要るのは実質 `reaction_min` の経路で、そこは随伴も直前の解を
        # ウォームスタートに使う(`reaction_sensitivity(adjoint0=)`)。順問題の `u0` は
        # `free` が変わっても正しく縮むことを確かめてあるが、**随伴と MMA の反復をまたぐ
        # 状態は確かめていない。黙って通さない。**
        raise ValueError(
            "dynamic_active は update='mma' では確かめていない —— MMA は感度の正規化"
            "(初回のコンプライアンス)・移動漸近線・直前2点を反復をまたいで持つので、"
            "組み立てる要素が反復ごとに変わる前提になっていない。update='oc' を使うこと"
            "(反力の下限制約 reaction_min は MMA を要求するので、併用もできない)"
        )
    try:
        update_cls = UPDATES[update]
    except KeyError:
        raise ValueError(
            f"未知の更新則 {update!r}(選べるのは {sorted(UPDATES)})"
        ) from None
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
    # 空要素は剛性行列の組み立てからも外す。**`extra_fixed_dofs` と同じ `void` から作る**
    # (`Model` の `active_mask` の契約)。未知数は1つも減らない —— 外れる要素にしか接して
    # いない自由度は、上の `floating` が既に拘束へ回した集合と定義が同一である
    # (2026-09-07/08 実測・14構成で自由自由度の集合が完全一致)。
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
        active_mask=~void if void.any() else None,
        dynamic_active=dynamic_active,
    )
    rmin_resolved = _resolve_rmin(
        rmin=rmin,
        rmin_mm=rmin_mm,
        element_volume_mm3=element_volume_mm3,
        nelx=nelx,
        nely=nely,
        nelz=nelz,
    )
    kernel, r = _filter_kernel(rmin_resolved)
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
    groups: tuple[ReactionGroup, ...] = () if reactions is None else tuple(reactions)
    if groups:
        _check_reaction_groups(model, groups)
    load_reference = float(np.linalg.norm(load_resultant(model.f)))
    if reaction_min is not None and not load_reference > 0.0:
        raise ValueError(
            "荷重の合力が 0 なので反力の下限を測る基準が無い"
            "(打ち消し合う荷重だけの系では、反力の総和も 0 になる)"
        )

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

    updater = update_cls(
        move=move,
        target_volume=target_volume,
        void=void,
        solid=solid,
        physical=_physical,
        n_movable=n_movable,
        n_solid=int(np.count_nonzero(solid)),
        element_volume_mm3=element_volume_mm3,
    )

    x = np.full((nelx, nely, nelz), volfrac)
    x[void] = 0.0
    x[solid] = 1.0
    x_phys = _physical(x)
    u = np.zeros(model.ndof)
    history: list[Iteration] = []
    stopped_by = "max_iter"
    ramp_until = 0 if penal_start is None else int(penal_iters)
    reaction_now: tuple[Reaction, ...] = ()
    adjoint: np.ndarray | None = None

    trace.snapshot('simp-initial', model=model, x=x, rho=x_phys, movable=movable,
                   void=void, solid=solid, kernel=kernel, hs=hs,
                   rmin_resolved=rmin_resolved, target_volume=target_volume)

    for it in range(1, max_iter + 1):
        t0 = time.perf_counter()
        trace.snapshot('iteration-input', iteration=it, x=x, rho=x_phys)
        # **順問題と随伴で K を1回だけ組み、前処理も1回だけ作る**(`Model.prepare`)。
        # 右辺ごとに `Model.solve` を呼ぶと多重格子の setup が本数ぶん重なる。
        penal_now = penal_at(it, penal, penal_start, penal_iters)
        system = model.prepare(x_phys, penal_now, solver=solver, threads=threads)
        u = system.solve(model.f[None, :], u0=u[None, :], cg_rtol=cg_rtol)[0]
        c, ce = model.compliance(x_phys, penal_now, u)
        constraints: tuple[Constraint, ...] = ()
        if groups:
            vectors = reaction_forces(model, x_phys, penal_now, u, groups)
            reaction_now = _as_reactions(groups, vectors, load_reference)
            if reaction_min is not None:
                raw, adjoint = reaction_sensitivity(
                    model, system, x_phys, penal_now, u, groups, vectors, adjoint0=adjoint
                )
                floor = reaction_min * load_reference
                # g_i = 1 - |R_i| / (ε|F|) <= 0。**感度は目的と同じくフィルタを通す**
                # (連鎖律は `_physical` の上書き後の定義と一致する。`live` で刈るのも同じ)。
                constraints = tuple(
                    Constraint(
                        name=group.name,
                        value=1.0 - reaction.magnitude / floor,
                        grad=-_filter_conv(
                            raw[index].reshape(shape) * live / hs, kernel, r
                        )
                        / floor,
                    )
                    for index, (group, reaction) in enumerate(zip(groups, reaction_now))
                )
        # **K_ff と多重格子の各段をここで手放す。** 下のフィルタと更新則は大きい一時配列を
        # 作るので、峰メモリを反復の後半まで持ち越さない。
        system = None
        dc = -penal_now * x_phys ** (penal_now - 1.0) * (E - emin) * ce
        dv = np.ones_like(x)
        # `live` は「その要素の物理密度が設計変数で動くか」。パッシブ要素の物理密度は
        # 上書きで定数になっているので、そこへの感度は連鎖律上ちょうど 0 になる。
        dcn = _filter_conv(dc * live / hs, kernel, r)
        dvn = _filter_conv(dv * live / hs, kernel, r)
        # 近傍が全部パッシブな要素は dvn=0 になりうる。そのままだと 0 除算で NaN が
        # 全反復へ伝播する(その要素はどのみち下の上書きで固定されるので値は使わない)。
        dvn = np.maximum(dvn, 1e-300)
        trace.snapshot('iteration-analysis', iteration=it, penal=penal_now, u=u, ce=ce,
                       compliance=c, dc=dc, dcn=dcn, dvn=dvn, constraints=constraints,
                       reactions=reaction_now, adjoint=adjoint, active=model.active,
                       fixed=model.fixed, free=model.free)
        xnew = updater.step(
            it=it,
            x=x,
            x_phys=x_phys,
            compliance=c,
            dcn=dcn,
            dvn=dvn,
            constraints=constraints,
        )
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
            reactions=reaction_now,
        )
        history.append(iteration)
        trace.snapshot('iteration-output', iteration=iteration, x=x, rho=x_phys)
        if callback is not None:
            callback(iteration, x_phys.copy())
        # **連続化の途中では収束と読まない。** penal が目標へ届く前に `change_tol` で
        # 降りると、中間密度の残った密度場が `rho` として返る(`penal_iters > max_iter` を
        # 塞いでもこちらの経路が残る)。そもそも目的関数がまだ動いているので、そこでの
        # 「変化が小さい」は解けたことを意味しない。
        if change < change_tol and it >= ramp_until:
            stopped_by = "change_tol"
            break

    if dynamic_active and keep_model:
        model.reset_active()
    result = Result(
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
        rmin=rmin_resolved,
        update=update,
        reactions=reaction_now,
        reaction_min=reaction_min,
        penal_start=None if penal_start is None else float(penal_start),
        penal_iters=0 if penal_start is None else int(penal_iters),
        model=model if keep_model else None,
    )
    if reaction_min is not None:
        _require_reactions(result, reaction_now, reaction_min)
    return result


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
