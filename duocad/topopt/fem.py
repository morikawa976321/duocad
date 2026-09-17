"""二値FEMの格子・剛性・線形解法(要素剛性・節点番号・行列組立・CG/direct/MG・残差検算)。"""

from __future__ import annotations

import contextlib
import os
import time
import warnings
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import numpy as np
from . import trace
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
    拘束不足そのものは増やしても直らない(逆向きの手当てが要る)。
    """


# ---------------------------------------------------------------------------
# 性能計測の口
# ---------------------------------------------------------------------------
# `Model.solve` が反復数と各段の秒数を積む先。**None のときは何も積まず、callback も渡さない**
# ので、本番経路の数値は1ビットも変わらない。
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
    ) -> None:
        """`fixed_dofs` / `load` を渡すと片持ち梁の既定の境界条件を置き換える。

        **既定(どちらも None)の挙動は追加前と1ビットも変わらない** —— CAD の面から
        起こした拘束・荷重を入れるための口で、既存の `check_fem.py` はこの口を使わない
        経路をそのまま通る。番号の規約は `_build_edof` と同じ(節点
        `i*(nely+1)*(nelz+1)+j*(nelz+1)+k`、自由度 `3n+{0,1,2}`)。ずれると荷重は
        黙って別の場所に入る。

        `extra_fixed_dofs` は**置き換えではなく追加**。`acceptance.py` が「空要素にしか
        接していない浮いた節点」を系から外すのに使う。**置き換えの口(`fixed_dofs`)に
        混ぜてはいけない** —— それだと `active_mask` だけを渡して拘束は既定のままにしたい
        呼び出しで、片持ち梁の拘束が黙って消える。

        `active_mask` は**組み立てに載せる要素**で、True の要素だけで K を組む。
        **`extra_fixed_dofs` と同じ `void` から作らなければならない**(層をまたぐ契約)
        —— 組み立てから外した要素にしか接していない自由度が `free` に残ると、K_ff に
        全零行ができて解が定まらない。`None` なら全要素で組む。`edof` そのものは
        `compliance` と `stress.py` が全要素で引くので**間引かない**。
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
        self._iK, self._jK = _coo_index_arrays(edof_assembled)
        # マルチグリッドの補間作用素は格子と拘束だけで決まる(密度に依らない)ので、
        # 設計反復のあいだ使い回す。作り直すのは各段の作用素と LU のほうだけ。
        self._mg_transfers: dict[tuple, list] = {}

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
        **既定は `None` → `PARALLEL_THREADS`**(呼び出し時に読む。`MG_DEFAULT` と同じ
        流儀で `PARALLEL_THREADS = 1` を代入すれば全経路が直列に戻る)。
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
        """
        if solver not in ("mg", "cg", "direct"):
            raise ValueError(f"未知の解法: {solver!r}")
        # **既定を束縛時ではなく呼び出し時に読む。** こうしておくと `MG_DEFAULT` を
        # 差し替えるだけで V-cycle の形を全経路(anchor → design_space)で
        # 一斉に変えられる。既定値の当たりを取るのに要る。
        mg = MG_DEFAULT if mg is None else mg
        threads = PARALLEL_THREADS if threads is None else max(1, int(threads))
        stats = _SOLVE_STATS
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


def load_resultant(f: np.ndarray) -> np.ndarray:
    """荷重ベクトル (ndof,) → 合力 (3,)。自由度番号は `3*節点+軸` なので `dof % 3` が軸。"""
    f = np.asarray(f, dtype=float).ravel()
    return np.bincount(np.arange(f.size) % 3, weights=f, minlength=3)


def reaction_forces(model: "Model", rho: np.ndarray, penal: float, u: np.ndarray) -> np.ndarray:
    """拘束自由度全体が受け持つ反力の合力 (3,)。`R = (K u - f)|_fixed` の軸ごとの合計。

    **層をまたぐ契約**: ここは荷重釣り合いの確認(合計が荷重合力の符号反転になること)
    にだけ使う。拘束面ごとの内訳・下限制約・感度(随伴)は持たない —— それは削除した
    旧SIMP経路(`ReactionGroup`・反力下限制約・MMA配線)の役目で、二値探索(`acceptance.py`/
    `binary_search.py`)はいずれも使わない。
    """
    residual = model.stiffness_apply(rho, penal, u) - model.f
    dofs = model.fixed
    return np.bincount(dofs % 3, weights=residual[dofs], minlength=3)
