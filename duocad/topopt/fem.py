"""二値FEMの格子・剛性・線形解法(要素剛性・節点番号・行列組立・CG/direct・残差検算)。"""

from __future__ import annotations

import contextlib
import os
import time
import warnings
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from . import trace
from .material import POISSON_RATIO
import scipy.sparse as sp
import scipy.sparse.linalg as spla

# 解いた後に残差で検算するときの上限(相対)。CG は `cg_rtol=1e-9`、直接解法は 1e-14 程度まで出るので 1e-6 は収束した解に6桁以上の余裕がある。
# **拘束不足の系を黙って通さないための関門**であって、精度の要求ではない。
SOLVE_RESIDUAL_TOL = 1e-6

# `solver="auto"` が直接解法(`spsolve`)を選ぶ上限。実測(docs/TOPOPT_SPEED.md「表E′」、hanger と合成の塊の 315 対):
# 自由な自由度 5 万以下かつ nnz(K_ff)/自由度 60 以下では、探索が出す細い形(nnz/自由度 53〜63)で direct が cg の
# 0.07〜0.20 倍の秒で、この範囲で direct のほうが遅かった評価は 0。2つ目の条件は太い形を外すためにある ——
# 同じ自由度でも太い形(nnz/自由度 63〜74)の直接解法は桁違いに重い(自由度 8.7 万の塊で 156 秒・常駐 +5.6GB)。
AUTO_DIRECT_MAX_DOFS = 50_000
AUTO_DIRECT_MAX_NNZ_PER_DOF = 60.0


def choose_solver(solver: str, free_dofs: int, nnz: int) -> str:
    """`solver` が `"auto"` なら、縮めた K_ff の大きさ(自由な自由度)と密さ(nnz)から `"direct"` / `"cg"` を選ぶ。
    それ以外の名前はそのまま返す。"""
    if solver != "auto":
        return solver
    fits = 0 < free_dofs <= AUTO_DIRECT_MAX_DOFS and nnz <= AUTO_DIRECT_MAX_NNZ_PER_DOF * free_dofs
    return "direct" if fits else "cg"


class SolveResidualError(ValueError):
    """`K U = F` が残差検算(`SOLVE_RESIDUAL_TOL`)を通らなかった。`ValueError` の派生。

    別の型にしてあるのは、低密度で悪条件になっただけなら材料を増やせば直るが、拘束不足は増やしても直らない(逆向きの手当てが要る)ため。
    """


# `Model.solve` が反復数と各段の秒数を積む先。**None のときは何も積まず callback も渡さない**ので本番経路の数値は変わらない。
# **外から `scipy.sparse.linalg.cg` を差し替えて数える方式は採らない**(差し替えた瞬間に数え口が外れ、反復数が黙って 0 になる)。
_SOLVE_STATS: dict | None = None


def new_solve_stats() -> dict:
    """`collect_solve_stats` が積む器。"""
    return {
        "solves": 0,
        "cg_iterations": 0,
        "cg_iterations_each": [],
        "assemble_seconds": 0.0,
        "slice_seconds": 0.0,
        "solve_seconds": 0.0,
        "fallbacks": 0,
        "direct_solves": 0,
        "gpu_solves": 0,
        "gpu_fallbacks": 0,
    }


@contextlib.contextmanager
def collect_solve_stats() -> Iterator[dict]:
    """この文脈の中で `Model.solve` が積んだ実測を返す。**入れ子にしない**(後勝ちになる)。`cg_iterations_each` は solve 1回ごとの反復数。"""
    global _SOLVE_STATS
    previous = _SOLVE_STATS
    stats = new_solve_stats()
    _SOLVE_STATS = stats
    try:
        yield stats
    finally:
        _SOLVE_STATS = previous


#: 局所節点 l = 4a+2b+c ↔ 単位立方体の頂点 (a,b,c) の符号 (2a-1, 2b-1, 2c-1)。**`_build_edof` の l のまわし方に一致していること**
#: (ずれると応力(`stress`)は落ちずに別の値になる)。
NODE_SIGNS = np.array(
    [[2 * a - 1, 2 * b - 1, 2 * c - 1] for a in (0, 1) for b in (0, 1) for c in (0, 1)],
    dtype=float,
)


def elasticity_matrix(E: float = 1.0, nu: float = POISSON_RATIO) -> np.ndarray:
    """弾性行列 D (6,6)。順は [xx, yy, zz, xy, yz, zx] で、せん断は工学ひずみ。"""
    d = np.zeros((6, 6))
    c1 = E / ((1.0 + nu) * (1.0 - 2.0 * nu))
    d[:3, :3] = c1 * (nu * np.ones((3, 3)) + (1.0 - 2.0 * nu) * np.eye(3))
    d[3:, 3:] = c1 * (1.0 - 2.0 * nu) / 2.0 * np.eye(3)
    return d


def strain_displacement(grad: np.ndarray) -> np.ndarray:
    """節点ごとの形状関数の勾配 (8,3) → B 行列 (6,24)(ひずみの順は `elasticity_matrix` と同じ)。"""
    b = np.zeros((6, 24))
    for l in range(8):
        bx, by, bz = grad[l]
        b[0, 3 * l + 0] = bx
        b[1, 3 * l + 1] = by
        b[2, 3 * l + 2] = bz
        b[3, 3 * l + 0] = by
        b[3, 3 * l + 1] = bx
        b[4, 3 * l + 1] = bz
        b[4, 3 * l + 2] = by
        b[5, 3 * l + 0] = bz
        b[5, 3 * l + 2] = bx
    return b


def element_stiffness(E: float = 1.0, nu: float = POISSON_RATIO) -> np.ndarray:
    """一辺1の8節点六面体(trilinear hex)の要素剛性行列 (24,24) を返す。"""
    d = elasticity_matrix(E, nu)
    g = 1.0 / np.sqrt(3.0)
    ke = np.zeros((24, 24))
    for xi in (-g, g):
        for eta in (-g, g):
            for zeta in (-g, g):
                dn = np.empty((8, 3))
                for l in range(8):
                    s = NODE_SIGNS[l]
                    dn[l, 0] = 0.125 * s[0] * (1.0 + s[1] * eta) * (1.0 + s[2] * zeta)
                    dn[l, 1] = 0.125 * (1.0 + s[0] * xi) * s[1] * (1.0 + s[2] * zeta)
                    dn[l, 2] = 0.125 * (1.0 + s[0] * xi) * (1.0 + s[1] * eta) * s[2]
                det_j = 0.125
                b = strain_displacement(dn * 2.0)
                ke += b.T @ d @ b * det_j
    return ke


def _build_edof(nelx: int, nely: int, nelz: int) -> np.ndarray:
    """要素DOF写像 (nele, 24) を返す。要素 (x,y,z) の 8 頂点を (a,b,c) ∈ {0,1}³ の辞書順に並べ、各頂点の自由度 3n, 3n+1, 3n+2。"""
    nelx, nely, nelz = int(nelx), int(nely), int(nelz)
    nz1 = nelz + 1
    ny1 = nely + 1
    origin = (np.arange(nelx, dtype=np.int32)[:, None, None] * (ny1 * nz1)
              + np.arange(nely, dtype=np.int32)[None, :, None] * nz1
              + np.arange(nelz, dtype=np.int32)[None, None, :]).ravel()
    corners = np.array([a * ny1 * nz1 + b * nz1 + c for a in (0, 1) for b in (0, 1) for c in (0, 1)], dtype=np.int32)
    xyz = np.arange(3, dtype=np.int32)
    return (3 * (origin[:, None, None] + corners[None, :, None]) + xyz[None, None, :]).reshape(-1, 24)


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


# **律速は CG の反復(行列ベクトル積)。** scipy の CSR 行列ベクトル積は C実装で GIL を解放するので、スレッド化はこの積だけに絞る
# (数字は docs/TOPOPT.md「CPU並列化」)。行ブロックへの分割は、行 i の内積が行 i の非零要素だけで決まるので、
# スレッド数を変えても**丸め誤差の順序は変わらずビット単位で同じ結果になる**。
# 疎×疎の積(`A@P`)や `splu` は行ブロックに割っても速くならない(`sp.vstack` の再結合費用が利得を食い、1スレッドでも単発呼びの約2倍遅い)。
# 効くかどうかは題材でなく自由度の大きさで決まる(14,256自由度で0.77倍、35,250で1.53倍)。

# `PARALLEL_THREADS` の初期値。環境変数 `TOPOPT_THREADS` は**import 時に1回だけ読む**(後から変えるなら `PARALLEL_THREADS` へ代入)。
# 取れない・壊れた値は無視して 1(直列)へ倒す。
def _default_thread_count() -> int:
    override = os.environ.get("TOPOPT_THREADS")
    if override:
        try:
            return max(1, int(override))
        except ValueError:
            return 1
    # 総論理コアの半分(下限1)。全論理コアを占有するとユーザーの操作と同時に走るフロントエンドが応答しなくなる。
    return max(1, (os.cpu_count() or 1) // 2)


PARALLEL_THREADS: int = _default_thread_count()

# CG を解く装置(`cpu` / `gpu`)。`prepare` が呼ばれたときに読む。**既定は cpu**: GPU は機械ごとに人が画面の「設定」で有効にし、
# サーバがその値(`duocad.local_settings`)をここへ入れる(VRAM を他の作業と分け合うため。docs/TOPOPT_GPU.md「方針」)。
# `gpu` でも解けない・合わないときは CPU で解き直し、理由を `linear-solve` の記録に残す(`gpu_cg`)。
FEM_DEVICE: str = "cpu"

# プロセスの寿命だけ生存させるスレッドプールのキャッシュ(スレッド数ごとに1つ)。**明示的に閉じない**(生成コストを毎回払わせない)。
# ワーカーはデーモンではなく、プロセス終了時は `concurrent.futures` の終了フックが止めて待つ。
_THREAD_POOLS: dict[int, ThreadPoolExecutor] = {}


def _thread_pool(n: int) -> ThreadPoolExecutor:
    pool = _THREAD_POOLS.get(n)
    if pool is None:
        pool = ThreadPoolExecutor(max_workers=n, thread_name_prefix="topopt-spmv")
        _THREAD_POOLS[n] = pool
    return pool


# 1スレッドあたりこれ未満の行数しか無い行列は、スレッド化のオーバーヘッドのほうが計算より高くつく
# (絞らないと、検査で使う小さい格子(数百行)でむしろ遅くなる実測があった)。
_MIN_ROWS_PER_THREAD = 512


def _effective_threads(n_rows: int, threads: int) -> int:
    """要求されたスレッド数を、行列の大きさに見合う数へ切り詰める。"""
    threads = max(1, int(threads))
    if n_rows <= 0:
        return 1
    return max(1, min(threads, n_rows // _MIN_ROWS_PER_THREAD or 1))


class _BlockedMatvec:
    """CSR行列を行ブロックへ割った並列 `A @ x`。**スレッド数に関わらずビット一致する**。`threads<=1` または行数が少なすぎるときは
    プールへ投げず `a @ x` を直に呼ぶ。ブロック分割は構築時に1回だけ行い、`solve` 1回の寿命を超えてキャッシュしない(密度が変わるたびに作り直す)。
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
    """`a @ x` を行ブロックへ割って `threads` 個のスレッドで計算する(公開の使い切り口。毎回分割を作り直すので繰り返し用途には向かない)。"""
    return _BlockedMatvec(a, threads)(x)


def _coo_index_arrays(edof: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """要素DOF写像 (n,24) → 組み立て用の COO 行・列添字(各要素 576 項)。

    **並びは `ke.ravel()` と対応していなければならない**(層をまたぐ契約。ずれると K は落ちずに別の行列になる)。
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
        nu: float = POISSON_RATIO,
        emin: float = 1e-9,
        fixed_dofs: np.ndarray | None = None,
        load: np.ndarray | None = None,
        extra_fixed_dofs: np.ndarray | None = None,
        active_mask: np.ndarray | None = None,
    ) -> None:
        """`fixed_dofs` / `load` を渡すと片持ち梁の既定の境界条件を置き換える(CAD の面から起こした拘束・荷重を入れる口。
        番号の規約は `_build_edof` と同じで、ずれると荷重は黙って別の場所に入る)。

        `extra_fixed_dofs` は**置き換えではなく追加**(`acceptance.py` が `anchor.floating_dofs` を系から外すのに使う)。
        `fixed_dofs` に混ぜると、`active_mask` だけ渡して拘束は既定のままにしたい呼び出しで片持ち梁の拘束が黙って消える。

        `active_mask` は**組み立てに載せる要素**(True の要素だけで K を組む。`None` は全要素)。
        **`extra_fixed_dofs` と同じ `void` から作ること**(層をまたぐ契約。組み立てから外した要素にしか接さない自由度が
        `free` に残ると K_ff に全零行ができて解が定まらない)。`edof` は `compliance` と `stress.py` が全要素で引くので間引かない。
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
            marked = np.zeros(self.ndof, dtype=bool)
            marked[self.fixed] = True
            marked[extra] = True
            self.fixed = np.flatnonzero(marked)
        # **集合演算ではなく ndof 長のブール配列で取る**(評価1回ごとに通る)。実測(hanger 2.4mm、ndof 2,757,300):
        # `union1d` 2.11秒 + `setdiff1d(arange(ndof), fixed)` 4.18秒 に対し、ブール配列は合わせて 0.02秒(出る配列は同じ)。
        free = np.ones(self.ndof, dtype=bool)
        free[self.fixed] = False
        self.free = np.flatnonzero(free)
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
        # **拘束DOFに載った荷重は解に効かない。** 固定面と荷重面が節点を共有するとここに全部落ち、「変位ゼロ・コンプライアンスゼロ」で
        # 最適化がもっともらしく収束するので先に落とす。
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
            if arr.shape != (nelx, nely, nelz):
                raise ValueError(
                    f"active_mask の形が {arr.shape}、期待は {(nelx, nely, nelz)}"
                )
            self.active = np.flatnonzero(arr.ravel())
            if self.active.size == 0:
                raise ValueError("組み立てる要素が1つも無い(active_mask が全部 False)")
            edof_assembled = self.edof[self.active]
        self._edof_assembled = edof_assembled
        self._coo = None

    def _coo_indices(self) -> tuple[np.ndarray, np.ndarray]:
        """組み立て用の COO 添字(要素あたり 576 項 × 2)。最初の組み立てで作って持つ(`solver="matrix_free"` は作らない)。"""
        if self._coo is None:
            self._coo = _coo_index_arrays(self._edof_assembled)
        return self._coo

    def assemble(self, rho: np.ndarray, penal: float) -> sp.spmatrix:
        """全体剛性行列 K (ndof,ndof) を返す。SIMP補間 E_e = emin + rho_e**penal * (E - emin)。"""
        flat = rho.ravel()
        if self.active is not None:
            flat = flat[self.active]
        ee = self.emin + flat**penal * (self.E - self.emin)
        sk = (self.ke.ravel()[None, :] * ee[:, None]).ravel()
        rows, cols = self._coo_indices()
        return sp.coo_matrix((sk, (rows, cols)), shape=(self.ndof, self.ndof)).tocsc()

    def solve(
        self,
        rho: np.ndarray,
        penal: float,
        *,
        solver: str = "cg",
        cg_rtol: float = 1e-9,
        threads: int | None = None,
    ) -> np.ndarray:
        """K U = F を解いて U (ndof,) を返す。拘束DOFの成分はきっかり 0.0。

        **解いた後に残差で検算し、通らなければ落とす。** 拘束不足(剛体運動が止まっていない)の系は K が特異だが `Model.__init__` の
        検査は通り、CG は収束せず直接解法へ落ちた先で**有限だが桁違いの解**が返って、原因から遠い場所(応力・安全率)で落ちる。
        (実測は docs/TOPOPT.md「CADの面 → 格子の節点」の「SIMP側に足した口」節)。剛体運動の完全な検出はしない。

        `solver="auto"` は K_ff を縮めてから `choose_solver` で選ぶ(`PreparedSystem.solver` と `linear-solve` の記録に出る)。
        `threads` は CG 反復が使うスレッド数(行列ベクトル積だけを並列化。docs/TOPOPT.md「CPU並列化」)。
        既定 `None` は `PARALLEL_THREADS`(呼び出し時に読む。1 を代入すれば全経路が直列に戻る)。
        `threads=1` は並列化前と同じコード経路(CG に生の `kf` を渡す)。
        """
        return self.solve_many(
            rho,
            penal,
            self.f[None, :],
            solver=solver,
            cg_rtol=cg_rtol,
            threads=threads,
        )[0]

    def solve_many(
        self,
        rho: np.ndarray,
        penal: float,
        rhs: np.ndarray,
        *,
        solver: str = "cg",
        cg_rtol: float = 1e-9,
        threads: int | None = None,
    ) -> np.ndarray:
        """**同じ K を複数の右辺で解く。** `rhs` (nrhs, ndof) → 解 (nrhs, ndof)。

        `solve` は右辺1本のときの薄い包み。K の組み立てと縮約を右辺ごとに作り直さず、追加費用は CG の反復ぶんだけ。
        **右辺が零ベクトルの本は CG を回さず零解を返す**(相対残差が 0/0 になるため)。
        右辺が最初から揃わないとき(反力の随伴は順問題を解かないと右辺が決まらない)は `prepare` の `PreparedSystem` を使い回すこと。
        """
        return self.prepare(rho, penal, solver=solver, threads=threads).solve(rhs, cg_rtol=cg_rtol)

    def prepare(
        self,
        rho: np.ndarray,
        penal: float,
        *,
        solver: str = "cg",
        threads: int | None = None,
        device: str | None = None,
    ) -> "PreparedSystem":
        """K を組み立て、自由DOFへ縮め、前処理を作るところまでをやって返す。返った `PreparedSystem` に右辺を何本でも後から渡せる。
        **持っているものは大きい**(K_ff)ので、使い終わったら名前を手放すこと。"""
        if solver not in ("cg", "direct", "auto", "matrix_free"):
            raise ValueError(f"未知の解法: {solver!r}")
        device = FEM_DEVICE if device is None else device
        if device not in ("cpu", "gpu"):
            raise ValueError(f"未知の装置: {device!r}")
        if solver == "matrix_free":
            return self._prepare_matrix_free(rho, penal, device)
        threads = PARALLEL_THREADS if threads is None else max(1, int(threads))
        stats = _SOLVE_STATS
        t0 = time.perf_counter()
        kg = self.assemble(rho, penal)
        t1 = time.perf_counter()
        kf = kg[self.free, :][:, self.free].tocsc()
        t2 = time.perf_counter()
        policy, solver = solver, choose_solver(solver, kf.shape[0], kf.nnz)
        precond = None
        a_op: sp.spmatrix | spla.LinearOperator = kf
        if solver != "direct":
            diag = kf.diagonal()
            precond = spla.LinearOperator(kf.shape, matvec=lambda v, d=diag: v / d)
            # CG 自身の `A@x` も行ブロックで並列化する(threads<=1 なら `kf` を生のまま渡し `_BlockedMatvec` を経由しない)。
            if threads > 1:
                a_mv = _BlockedMatvec(kf, threads)
                a_op = spla.LinearOperator(kf.shape, matvec=a_mv, dtype=float)
        if stats is not None:
            stats["assemble_seconds"] += t1 - t0
            stats["slice_seconds"] += t2 - t1
            # setup も「解く時間」に数える(右辺1本のときの `solve_seconds` は setup + CG)。
            stats["solve_seconds"] += time.perf_counter() - t2
        return PreparedSystem(self, kf, a_op, precond, solver, policy, device)

    def _prepare_matrix_free(self, rho: np.ndarray, penal: float, device: str = "cpu") -> "PreparedSystem":
        """K を組み立てず、`K_ff x` を要素ごとの積で返す CG(Jacobi 前処理)を用意する。`device="gpu"` なら GPU で解き
        (`gpu_cg.solve_matrix_free`)、使えなければ CPU で解き直す。

        CPU ではメモリは組み立てる CG の約 1/8、時間は約 2.2 倍(完成形検査の実測。docs/TOPOPT_PEAK_GATE.md 4-13・4-15)。
        CG が収束しなくても直接解法へは戻らない(戻ると組み立てでメモリが溢れる)。残差の検算が落とす。
        """
        flat = np.asarray(rho, dtype=float).ravel()
        if self.active is not None:
            flat = flat[self.active]
        ee = self.emin + flat**penal * (self.E - self.emin)
        edof = self._edof_assembled
        index = edof.ravel().astype(np.intp)  # 毎回の bincount で添字を変換しないよう先に作る
        free = self.free
        diag = np.bincount(index, weights=(np.diag(self.ke)[None, :] * ee[:, None]).ravel(), minlength=self.ndof)[free]
        full = np.zeros(self.ndof)

        def apply(x: np.ndarray) -> np.ndarray:
            full[free] = x
            # `ke` は対称なので `ve @ ke` が要素ごとの `ke @ v_e` になる(`stiffness_apply` と同じ)
            return np.bincount(index, weights=((full[edof] @ self.ke) * ee[:, None]).ravel(), minlength=self.ndof)[free]

        n = int(free.size)
        a_op = spla.LinearOperator((n, n), matvec=apply, dtype=float)
        precond = spla.LinearOperator((n, n), matvec=lambda v, d=diag: v / d, dtype=float)
        system = PreparedSystem(self, None, a_op, precond, "matrix_free", "matrix_free", device)
        system._matrix_free = (edof, ee)
        return system

    def stiffness_apply(self, rho: np.ndarray, penal: float, vec: np.ndarray) -> np.ndarray:
        """K @ vec を、K を組み立てずに要素ループで計算する (ndof,)。

        **`assemble` と同じ要素の範囲**(`active`)で足す(層をまたぐ契約)。反力 `R = (K u - f)|_fixed` と
        残差の検算(`acceptance.safety_check`)用。
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

    def element_energy(self, u: np.ndarray, cells: np.ndarray) -> np.ndarray:
        """要素 `cells`(平坦添字)の u_eᵀ k₀ u_e。`compliance` の `ce` の同じ要素とビット一致する(einsum は行ごとに独立。
        `tests/test_fem_dofs.py`)ので、材料の要素だけが要るときは箱の全要素を計算しない。"""
        ue = u[self.edof[cells]]
        return np.einsum("ij,jk,ik->i", ue, self.ke, ue)

    def compliance(
        self, rho: np.ndarray, penal: float, u: np.ndarray
    ) -> tuple[float, np.ndarray]:
        """(コンプライアンス c, 要素ひずみエネルギー ce) を返す。

        **`c` は組み立てに載せた要素だけで足す**(層をまたぐ契約。全要素で足すと解いた系に無い emin 媒質のエネルギーが混ざる)。
        `ce` は全要素ぶん返す(`acceptance.safety_check` が空セルを 0 にして `fields['element_energy']` に載せる)。
        """
        ue = u[self.edof]
        ce = np.einsum("ij,jk,ik->i", ue, self.ke, ue).reshape(self.nelx, self.nely, self.nelz)
        ee = self.emin + rho**penal * (self.E - self.emin)
        energy = (ee * ce).ravel()
        c = float(energy.sum() if self.active is None else energy[self.active].sum())
        return c, ce


class PreparedSystem:
    """1つの ρ に対する K_ff と、その前処理。**右辺を何本でも後から解ける**(`Model.prepare` が作る)。"""

    def __init__(
        self,
        model: "Model",
        kf: sp.spmatrix,
        a_op: "sp.spmatrix | spla.LinearOperator",
        precond: "spla.LinearOperator | None",
        solver: str,
        policy: str | None = None,
        device: str = "cpu",
    ) -> None:
        self.model = model
        self.kf = kf
        self._a_op = a_op
        self._precond = precond
        self._solver = solver
        self._policy = solver if policy is None else policy
        self._device = device
        self._matrix_free = None  # (組み立てる要素の edof, 要素の剛性の倍率)。`solver="matrix_free"` のときだけ

    @property
    def solver(self) -> str:
        """実際に使う解法(`"cg"` / `"direct"` / `"matrix_free"`。`"auto"` は `Model.prepare` が解決済み)。"""
        return self._solver

    @property
    def policy(self) -> str:
        """`Model.prepare` に渡された解法の名前(`"auto"` を含む)。"""
        return self._policy

    def solve(self, rhs: np.ndarray, *, cg_rtol: float = 1e-9) -> np.ndarray:
        """`rhs` (nrhs, ndof) を解いて (nrhs, ndof) を返す。拘束DOFの成分はきっかり 0.0。**零ベクトルの右辺は CG を回さず零解を返す**(相対残差が 0/0 になるため)。"""
        model = self.model
        rhs = np.atleast_2d(np.asarray(rhs, dtype=float))
        if rhs.shape[1] != model.ndof:
            raise ValueError(f"右辺の長さが {rhs.shape[1]}、期待は ndof={model.ndof}")
        stats = _SOLVE_STATS
        t_start = time.perf_counter()
        out = np.zeros((rhs.shape[0], model.ndof))
        for index in range(rhs.shape[0]):
            ff = rhs[index][model.free]
            iterations = 0
            fell_back = False
            device = "cpu"
            gpu_fallback = None
            zero_rhs = not np.any(ff)
            if zero_rhs:
                u_free = np.zeros(model.free.size)
            elif self._solver == "direct":
                u_free = spla.spsolve(self.kf, ff)
            else:
                u_free = None
                if self._device == "gpu" and self._matrix_free is not None:
                    u_free, iterations, info, gpu_fallback = self._solve_matrix_free_on_gpu(ff, cg_rtol)
                    device = "cpu" if u_free is None else "gpu"
                elif self._device == "gpu":
                    u_free, iterations, info, gpu_fallback = self._solve_on_gpu(ff, cg_rtol)
                    device = "cpu" if u_free is None else "gpu"
                    if info != 0:
                        device = "cpu"
                        gpu_fallback = f"not-converged: GPU の CG が {iterations} 反復で収束しない(直接解法へ)"
                if u_free is None:
                    iterations = 0
                    counter = None
                    if stats is not None or trace.current() is not None:

                        def counter(_xk: np.ndarray) -> None:
                            nonlocal iterations
                            iterations += 1

                    u_free, info = spla.cg(self._a_op, ff, M=self._precond, rtol=cg_rtol, callback=counter)
                fell_back = info != 0 and self.kf is not None
                if fell_back:
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
                stats["gpu_solves"] += int(device == "gpu")
                stats["gpu_fallbacks"] += int(gpu_fallback is not None)
            trace.snapshot('linear-solve', solver=self._solver, solver_policy=self._policy,
                           free_dofs=int(model.free.size), nnz=None if self.kf is None else int(self.kf.nnz),
                           cg_rtol=cg_rtol,
                           cg_iterations=iterations, fallback=fell_back,
                           device_requested=self._device, device=device, gpu_fallback=gpu_fallback,
                           rhs_index=index, seconds=time.perf_counter()-t_start)
            if not zero_rhs:
                model._verify_solution(self._a_op if self.kf is None else self.kf, ff, u_free)
            out[index, model.free] = u_free
        if stats is not None:
            stats["solve_seconds"] += time.perf_counter() - t_start
        return out

    def _solve_matrix_free_on_gpu(self, ff: np.ndarray, cg_rtol: float) -> tuple[np.ndarray | None, int, int, str | None]:
        """組み立てない CG を GPU で解く。(解, 反復数, info, CPU へ戻した理由)。収束しない・残差検算に通らないときも
        解を None にして CPU の組み立てない CG で解き直させる(直接解法へは戻れない)。"""
        from . import gpu_cg

        model = self.model
        edof, ee = self._matrix_free
        try:
            u_free, iterations, info = gpu_cg.solve_matrix_free(edof, model.ke, ee, model.free, model.ndof, ff,
                                                                 rtol=cg_rtol)
        except gpu_cg.Unavailable as exc:
            warnings.warn(f"GPU で解けないので CPU で解き直す: {exc.reason}", RuntimeWarning, stacklevel=3)
            return None, 0, 0, exc.reason
        reason = None
        if info != 0:
            reason = f"not-converged: GPU の CG が {iterations} 反復で収束しない"
        elif not bool(np.all(np.isfinite(u_free))) or (
                np.linalg.norm(self._a_op @ u_free - ff) > SOLVE_RESIDUAL_TOL * np.linalg.norm(ff)):
            reason = "residual: GPU の解が残差検算に通らない"
        if reason is not None:
            warnings.warn(f"GPU で解けないので CPU で解き直す: {reason}", RuntimeWarning, stacklevel=3)
            return None, iterations, 0, reason
        return u_free, iterations, 0, None

    def _solve_on_gpu(self, ff: np.ndarray, cg_rtol: float) -> tuple[np.ndarray | None, int, int, str | None]:
        """GPU の CG で解く。(解, 反復数, info, CPU へ戻した理由)。戻すときは解が None。

        **戻すときは CPU で初期値から解き直す**(途中の解を `x0` に渡すと Krylov 系列が変わりビット一致しなくなる)。
        収束したのに残差検算に通らない解も戻す(ビット一致なら CPU も同じ解のはずなので GPU 側の異常とみなす)。
        """
        from . import gpu_cg

        try:
            u_free, iterations, info = gpu_cg.solve(self.kf, ff, rtol=cg_rtol,
                                                    make_startup_system=_gpu_startup_system)
        except gpu_cg.Unavailable as exc:
            warnings.warn(f"GPU で解けないので CPU で解き直す: {exc.reason}", RuntimeWarning, stacklevel=3)
            return None, 0, 0, exc.reason
        if info == 0:
            finite = bool(np.all(np.isfinite(u_free)))
            if not finite or np.linalg.norm(self.kf @ u_free - ff) > SOLVE_RESIDUAL_TOL * np.linalg.norm(ff):
                reason = "residual: GPU の解が残差検算に通らない"
                warnings.warn(f"GPU で解けないので CPU で解き直す: {reason}", RuntimeWarning, stacklevel=3)
                return None, 0, 0, reason
        return u_free, iterations, info, None


def _gpu_startup_system() -> tuple[sp.spmatrix, np.ndarray]:
    """GPU の起動時の自己検査に使う小さい系(K_ff, f_f)。製品の組立と縮約の経路で作る。"""
    model = Model(10, 4, 4)
    kg = model.assemble(np.ones((10, 4, 4)), 3.0)
    return kg[model.free, :][:, model.free].tocsc(), model.f[model.free]


def load_resultant(f: np.ndarray) -> np.ndarray:
    """荷重ベクトル (ndof,) → 合力 (3,)。自由度番号は `3*節点+軸` なので `dof % 3` が軸。"""
    f = np.asarray(f, dtype=float).ravel()
    return np.bincount(np.arange(f.size) % 3, weights=f, minlength=3)


def reaction_forces(model: "Model", rho: np.ndarray, penal: float, u: np.ndarray) -> np.ndarray:
    """拘束自由度全体が受け持つ反力の合力 (3,)。`R = (K u - f)|_fixed` の軸ごとの合計。

    荷重釣り合いの確認(合計が荷重合力の符号反転になること)にだけ使う。拘束面ごとの内訳・下限制約・感度は持たない。
    """
    residual = model.stiffness_apply(rho, penal, u) - model.f
    dofs = model.fixed
    return np.bincount(dofs % 3, weights=residual[dofs], minlength=3)
