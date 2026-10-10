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

# 解いた後に残差で検算するときの上限(相対)。CG は `cg_rtol=1e-9` まで詰め、直接解法は
# 1e-14 程度まで出るので、1e-6 は「収束した解」に対して6桁以上の余裕がある。
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
# 瞬間に数え口が外れ、反復数が黙って 0 になるので、数え口は解法と同じ場所に置く。
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
        "fallbacks": 0,
        "direct_solves": 0,
        "gpu_solves": 0,
        "gpu_fallbacks": 0,
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


#: 局所節点 l = 4a+2b+c ↔ 単位立方体の頂点 (a,b,c) の符号 (2a-1, 2b-1, 2c-1)。**`_build_edof` の
#: l のまわし方に一致していること** —— ずれると応力(`stress`)は「もっともらしい別の値」になって落ちない。
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
# CPU 並列化 (solve(threads=...))
# ---------------------------------------------------------------------------
# **律速は CG の反復(行列ベクトル積)。** **scipy の CSR 行列ベクトル積(疎×密ベクトル)は
# C実装で GIL を解放する**ので、スレッド化するのはこの積だけに絞る(数字は docs/TOPOPT.md「CPU並列化」)。
#
# **行ブロックへの分割は答えを変えない**: CSR の行 i の内積は行 i の非零要素だけで決まり、
# 他の行・他のスレッドの計算と独立。どのスレッドが計算しても、スレッド数を変えても、
# **丸め誤差が発生する順序は変わらずビット単位で同じ結果になる**(numpyの縮約はここでは
# 並列化していないので、そちらの再現性はそもそも揺れない)。

# `PARALLEL_THREADS` の初期値。環境変数 `TOPOPT_THREADS` は**import 時に1回だけ読む**
# (後から変えるなら `PARALLEL_THREADS` へ代入する)。値を取れなければ 1(直列。壊れた環境変数で黙って
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

# CG を解く装置(`cpu` / `gpu`)。`prepare` が呼ばれたときに読む。**既定は cpu** —— GPU は機械ごとに人が
# 画面の「設定」で有効にし、サーバがその値(`duocad.local_settings`)をここへ入れる(VRAM を他の作業と
# 分け合うので、使えるからといって黙って取らない。gpu_feasibility/CONCLUSION.md「方針」)。
# `gpu` でも、GPU で解けない・合わないときは CPU で解き直し、理由を `linear-solve` の記録に残す(`gpu_cg`)。
FEM_DEVICE: str = "cpu"

# プロセスの寿命だけ生存させるスレッドプールのキャッシュ(スレッド数ごとに1つ)。
# **明示的に閉じない**——閉じても次の solve でまた同じ数だけ要るので、生成コストを
# 毎回払わせない。ワーカーはデーモンスレッドではなく、プロセス終了時は `concurrent.futures`
# の終了フックがワーカーを止めて待つ(solve の外ではワーカーは待機しているだけ)。
_THREAD_POOLS: dict[int, ThreadPoolExecutor] = {}


def _thread_pool(n: int) -> ThreadPoolExecutor:
    pool = _THREAD_POOLS.get(n)
    if pool is None:
        pool = ThreadPoolExecutor(max_workers=n, thread_name_prefix="topopt-spmv")
        _THREAD_POOLS[n] = pool
    return pool


# 1スレッドあたりこれ未満の行数しか無い行列は、スレッド化のオーバーヘッド
# (`submit`/`result` の往復)のほうが計算そのものより高くつく。**しきい値の根拠**:
# スレッド数を機械的に絞らないと、検査で使う小さい格子(数百行)でむしろ遅くなる実測があった。
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
    作り直す必要があるので(`Model.prepare` が呼ぶ側で)、`solve` 1回の
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
    (`Model.prepare` は `_BlockedMatvec` を密度が変わるたびに1回だけ
    作って使い回している)。`check_parallel.py` の帯域測定と検出力用。
    """
    return _BlockedMatvec(a, threads)(x)


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
        nu: float = POISSON_RATIO,
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
        接していない浮いた節点」(`anchor.floating_dofs`)を系から外すのに使う。**置き換えの口(`fixed_dofs`)に
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
            marked = np.zeros(self.ndof, dtype=bool)
            marked[self.fixed] = True
            marked[extra] = True
            self.fixed = np.flatnonzero(marked)
        # **集合演算ではなく ndof 長のブール配列で取る。** `Model` は候補ごとに作り直すので
        # ここは評価1回ごとに通る。実測(hanger 2.4mm、ndof 2,757,300): `union1d` 2.11秒 +
        # `setdiff1d(arange(ndof), fixed)` 4.18秒 に対し、ブール配列は合わせて 0.02秒
        # (整列済み・重複なしの同じ配列が出る。番号は全て [0, ndof) にあることを上で検査済み)。
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
            if arr.shape != (nelx, nely, nelz):
                raise ValueError(
                    f"active_mask の形が {arr.shape}、期待は {(nelx, nely, nelz)}"
                )
            self.active = np.flatnonzero(arr.ravel())
            if self.active.size == 0:
                raise ValueError("組み立てる要素が1つも無い(active_mask が全部 False)")
            edof_assembled = self.edof[self.active]
        self._iK, self._jK = _coo_index_arrays(edof_assembled)

    def assemble(self, rho: np.ndarray, penal: float) -> sp.spmatrix:
        """全体剛性行列 K (ndof,ndof) を返す。SIMP補間 E_e = emin + rho_e**penal * (E - emin)。"""
        flat = rho.ravel()
        if self.active is not None:
            flat = flat[self.active]
        ee = self.emin + flat**penal * (self.E - self.emin)
        sk = (self.ke.ravel()[None, :] * ee[:, None]).ravel()
        return sp.coo_matrix((sk, (self._iK, self._jK)), shape=(self.ndof, self.ndof)).tocsc()

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

        **解いた後に残差で検算し、通らなければ落とす。** 拘束が足りない系(剛体運動が
        止まっていない)を渡されると K は特異になるが、`Model.__init__` の検査は
        「拘束が非空で範囲内」しか見ないので通ってしまう。そのまま解くと CG は収束せず、
        直接解法へ落ちた先で**有限だが桁違いの解**が返り、探索はその値を信じて進んだ末に
        別の場所(応力の評価・安全率の比較)で落ちる。**原因から遠い場所で落ちるのが最悪**なのでここで名指しする
        (実測は docs/TOPOPT.md「CADの面 → 格子の節点」の「SIMP側に足した口」節)。
        剛体運動の完全な検出はしない —— 残差が通らなければ落とす、それだけでよい。

        `solver="auto"` は K_ff を縮めてから `choose_solver` で `direct` / `cg` を選ぶ(選んだ解法は
        `PreparedSystem.solver` と `linear-solve` の記録に出る)。
        `threads` は CG 反復が使うスレッド数(**行列ベクトル積だけを並列化**。docs/TOPOPT.md「CPU並列化」参照)。
        **既定は `None` → `PARALLEL_THREADS`**(呼び出し時に読む。`PARALLEL_THREADS = 1` を
        代入すれば全経路が直列に戻る)。
        `threads=1` は並列化の前と**同じコード経路**(CG に生の `kf` を渡す)を通るので、
        速度も答えも変わらない(退避経路)。
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

        `solve` はこれを右辺1本で呼ぶ薄い包みで、**1本のときの数値も経路も
        `solve_many` 導入前と同一**である(組み立て → 縮約 → 前処理 → CG の順も同じ)。

        **多重右辺の要点は K の組み立てと縮約を作り直さないこと。** 右辺ごとに `solve` を呼ぶと
        それが本数ぶん重なる。K が同じなら1回で回せば**追加費用は CG の反復ぶんだけ**になる。

        **右辺が零ベクトルの本は CG を回さず零解を返す** —— 残差の相対判定が 0/0 になるため。

        **右辺が最初から揃っていないとき**(反力の随伴は、まず順問題を解かないと右辺が
        決まらない)は `prepare` を直に呼んで `PreparedSystem` を使い回すこと。
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
        """K を組み立て、自由DOFへ縮め、前処理を作るところまでをやって返す。

        **返ってきた `PreparedSystem` に右辺を何本でも後から渡せる。** 反力の随伴は
        順問題の解 `u` から右辺が決まるので、`solve_many` のように右辺を最初に
        揃えられない —— それでも setup を1回で済ませるための口である。

        **持っているものは大きい**(K_ff)。使い終わったら名前を手放すこと。
        """
        if solver not in ("cg", "direct", "auto"):
            raise ValueError(f"未知の解法: {solver!r}")
        device = FEM_DEVICE if device is None else device
        if device not in ("cpu", "gpu"):
            raise ValueError(f"未知の装置: {device!r}")
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
        return PreparedSystem(self, kf, a_op, precond, solver, policy, device)

    def stiffness_apply(self, rho: np.ndarray, penal: float, vec: np.ndarray) -> np.ndarray:
        """K @ vec を、K を組み立てずに要素ループで計算する (ndof,)。

        **`assemble` と同じ要素の範囲**(`active`)で足す —— 範囲が違えば別の行列を掛けた
        ことになる(層をまたぐ契約)。反力 `R = (K u - f)|_fixed`(`reaction_forces`)と残差の検算
        (`acceptance.safety_check`)のための口で、どちらも K を持っていない場所から呼ばれる。
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
        `ce` のほうは**全要素ぶん返す**(要素エネルギーの場。`acceptance.safety_check` が
        空セルを 0 にして `fields['element_energy']` に載せる)。
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
    K の組み立てと縮約が本数ぶん重なる。
    """

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

    @property
    def solver(self) -> str:
        """実際に使う解法(`"cg"` / `"direct"`。`"auto"` は `Model.prepare` が解決済み)。"""
        return self._solver

    @property
    def policy(self) -> str:
        """`Model.prepare` に渡された解法の名前(`"auto"` を含む)。"""
        return self._policy

    def solve(self, rhs: np.ndarray, *, cg_rtol: float = 1e-9) -> np.ndarray:
        """`rhs` (nrhs, ndof) を解いて (nrhs, ndof) を返す。拘束DOFの成分はきっかり 0.0。

        **右辺が零ベクトルの本は CG を回さず零解を返す** —— 残差の相対判定が 0/0 になるため。
        """
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
                if self._device == "gpu":
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
                stats["gpu_solves"] += int(device == "gpu")
                stats["gpu_fallbacks"] += int(gpu_fallback is not None)
            trace.snapshot('linear-solve', solver=self._solver, solver_policy=self._policy,
                           free_dofs=int(model.free.size), nnz=int(self.kf.nnz), cg_rtol=cg_rtol,
                           cg_iterations=iterations, fallback=fell_back,
                           device_requested=self._device, device=device, gpu_fallback=gpu_fallback,
                           rhs_index=index, seconds=time.perf_counter()-t_start)
            if not zero_rhs:
                model._verify_solution(self.kf, ff, u_free)
            out[index, model.free] = u_free
        if stats is not None:
            stats["solve_seconds"] += time.perf_counter() - t_start
        return out

    def _solve_on_gpu(self, ff: np.ndarray, cg_rtol: float) -> tuple[np.ndarray | None, int, int, str | None]:
        """GPU の CG で解く。(解, 反復数, info, CPU へ戻した理由)。戻すときは解が None。

        **戻すときは CPU で初期値から解き直す**(呼び出し側)。途中の解を `x0` に渡すと Krylov 系列が
        変わり、CPU だけで解いたときとビット一致しなくなる。収束したのに残差検算に通らない解も戻す
        (ビット一致なら CPU も同じ解になるはずなので、GPU 側の異常とみなす)。
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

    **層をまたぐ契約**: ここは荷重釣り合いの確認(合計が荷重合力の符号反転になること)
    にだけ使う。拘束面ごとの内訳・下限制約・感度(随伴)は持たない —— それは削除した
    旧SIMP経路(`ReactionGroup`・反力下限制約・MMA配線)の役目で、二値探索(`acceptance.py`/
    `binary_search.py`)はいずれも使わない。
    """
    residual = model.stiffness_apply(rho, penal, u) - model.f
    dofs = model.fixed
    return np.bincount(dofs % 3, weights=residual[dofs], minlength=3)
