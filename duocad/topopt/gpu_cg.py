"""K_ff のヤコビ前処理つき CG を GPU(CuPy)で解く。**解・反復数とも CPU の `scipy.sparse.linalg.cg` とビット一致する。**

`fem.PreparedSystem.solve` が、設定が `gpu` のときだけ呼ぶ(既定は CPU。`fem.FEM_DEVICE`)。
ここで使えない・合わないと分かったら `Unavailable` を投げ、呼び出し側が CPU で初期値から解き直す。

一致させている 3 つの演算(根拠の実測は docs/TOPOPT_GPU.md「実測」):

1. 内積 —— numpy は `np.dot` を同梱 OpenBLAS の ddot に渡す。その足し順(スレッド数ぶんの区間 → 区間ごとに
   16 本の累算器へ積和を融合して足す → 累算器のたたみ方 → 区間の和を先頭から)を GPU でなぞる。
2. 行列ベクトル積 —— scipy の CSR の積と同じく、1 行を列番号の昇順に融合なしで足す。
3. ベクトル更新と前処理 —— numpy の `p *= beta; p += z` などと同じく、積と和を融合しない要素ごとの演算。

**1 は numpy・OpenBLAS の版、CPU の種類、OpenBLAS のスレッド数で、2 は scipy のビルドで変わりうる。**
だから使う前に、内積は実サイズの長さごとに `np.dot` と、2 と 3 は起動時に小さい系の CG で CPU と比べる。
"""
from __future__ import annotations

import ctypes
import glob
import math
import os
import threading
from collections.abc import Callable

import numpy as np
import scipy.sparse as sp
import scipy.sparse.linalg as spla

# OpenBLAS の ddot は長さがこれ以下だとスレッドを分けない(interface/dot.c の `n <= 10000`)。
SINGLE_THREAD_MAX = 10000
_BLOCK = 256


class Unavailable(RuntimeError):
    """GPU で解けない・解いてはいけない。`reason` は記録に残す短い理由。

    `sticky` なら、このプロセスでは以後 GPU を試さない(CuPy が無い・起動時の自己検査に落ちた等、
    次の solve でも同じ結果になるもの)。VRAM 不足のように次は通りうるものは `sticky=False`。
    """

    def __init__(self, reason: str, *, sticky: bool) -> None:
        super().__init__(reason)
        self.reason = reason
        self.sticky = sticky


_disabled: str | None = None
_startup_checked = False
_dot_ok: dict[tuple[int, int], bool] = {}
_kernels: dict | None = None
# 状態(上の3つ)と GPU を守る。GPU は1枚なので、別スレッドの solve を同時に載せても速くならない。
_lock = threading.Lock()

# 内積は区間ごとに 1 ブロック 16 スレッドで、OpenBLAS の 16 本の累算器と同じ順に足す(`ddot_body`)。CG の反復のスカラー
# (区間和の畳み込み・α・β・収束判定)は 1 スレッドのカーネルが GPU の上で計算する(`fold` はホストで区間和を先頭から足していたのと同じ順)。
# `state[0]` は 0 = 反復中・1 = 収束・2 = 反復の上限、`state[1]` は済んだ反復数。`scal` は [前の反復の ρ, ρ, β, α]。
# 止まった後のカーネルは何もしないので、ホストが何反復かに 1 回だけ `state` を読んでも、x は止まった反復の値のまま。
# - `cg_check`: 反復の頭。scipy の cg と同じく、反復数が上限に達したら残差を見ずに止め、そうでなければ ||r|| < atol で止める。
# - `cg_ell`: 行の k 番目の項を k ごとにまとめた並び(列優先)。足す順序は scipy の CSR の積と同じ列番号の昇順。
# - 分母が 0 になっても(内積のアンダーフロー等)止めない。scipy の cg も NaN のまま上限まで回して info を返すので、それに揃える。
# **外部の罠(CuPy)**: カーネルのソースは ASCII だけにする。CuPy はソースを OS の既定の文字コード(日本語の Windows では cp932)で
# 書き出してからコンパイルし、符号化できない文字があると読み込みごと失敗する(GPU が使えない扱いになる)。
_CG_SRC = r"""
__device__ __forceinline__ void ddot_body(const double* x, const double* y, const long long* starts,
                                          const long long* widths, double* chunk_out) {
    const int c = blockIdx.x;
    const int lane = threadIdx.x;
    const long long start = starts[c];
    const long long width = widths[c];
    const long long n1 = width & ~15LL;
    __shared__ double acc[16];
    double a = 0.0;
    for (long long i = lane; i < n1; i += 16) a = fma(x[start + i], y[start + i], a);
    acc[lane] = a;
    __syncthreads();
    if (lane == 0) {
        double lo[4], hi[4];
        for (int r = 0; r < 4; ++r) { lo[r] = acc[4 * r] + acc[4 * r + 2]; hi[r] = acc[4 * r + 1] + acc[4 * r + 3]; }
        double dot = ((lo[0] + lo[1]) + (lo[2] + lo[3])) + ((hi[0] + hi[1]) + (hi[2] + hi[3]));
        for (long long i = n1; i < width; ++i) dot = y[start + i] * x[start + i] + dot;
        chunk_out[c] = dot;
    }
}

__device__ __forceinline__ double fold(const double* part, const int chunks) {
    double s = part[0];
    for (int c = 1; c < chunks; ++c) s = s + part[c];
    return s;
}

extern "C" __global__
void ddot_chunks(const double* x, const double* y, const long long* starts, const long long* widths,
                 double* chunk_out) {
    ddot_body(x, y, starts, widths, chunk_out);
}

extern "C" __global__
void cg_ddot(const double* x, const double* y, const long long* starts, const long long* widths,
             double* chunk_out, const int* state) {
    if (state[0]) return;
    ddot_body(x, y, starts, widths, chunk_out);
}

extern "C" __global__
void cg_check(const double* part_rr, const int chunks, const double atol, const int maxiter, int* state) {
    if (state[0]) return;
    if (state[1] >= maxiter) state[0] = 2;
    else if (sqrt(fold(part_rr, chunks)) < atol) state[0] = 1;
}

extern "C" __global__
void cg_divide(const int n, const double* r, const double* d, double* z, const int* state) {
    if (state[0]) return;
    const int i = blockDim.x * blockIdx.x + threadIdx.x;
    if (i < n) z[i] = r[i] / d[i];
}

extern "C" __global__
void cg_beta(const double* part_rz, const int chunks, double* scal, const int* state) {
    if (state[0]) return;
    scal[1] = fold(part_rz, chunks);
    if (state[1] > 0) scal[2] = scal[1] / scal[0];
}

extern "C" __global__
void cg_update_p(const int n, const double* z, double* p, const double* scal, const int* state) {
    if (state[0]) return;
    const int i = blockDim.x * blockIdx.x + threadIdx.x;
    if (i >= n) return;
    if (state[1] == 0) {
        p[i] = z[i];
        return;
    }
    const double t = p[i] * scal[2];
    p[i] = t + z[i];
}

extern "C" __global__
void cg_ell(const int n, const int* row_len, const int* cols, const double* vals, const double* x, double* y,
            const int* state) {
    if (state[0]) return;
    const int i = blockDim.x * blockIdx.x + threadIdx.x;
    if (i >= n) return;
    double s = 0.0;
    const int len = row_len[i];
    for (int k = 0; k < len; ++k) {
        const long long at = (long long)k * n + i;
        s += vals[at] * x[cols[at]];
    }
    y[i] = s;
}

extern "C" __global__
void cg_alpha(const double* part_pq, const int chunks, double* scal, const int* state) {
    if (state[0]) return;
    scal[3] = scal[1] / fold(part_pq, chunks);
}

extern "C" __global__
void cg_update_xr(const int n, const double* p, const double* q, double* x, double* r, const double* scal,
                  const int* state) {
    if (state[0]) return;
    const int i = blockDim.x * blockIdx.x + threadIdx.x;
    if (i >= n) return;
    const double ap = scal[3] * p[i];
    x[i] = x[i] + ap;
    const double aq = scal[3] * q[i];
    r[i] = r[i] - aq;
}

extern "C" __global__
void cg_next(double* scal, int* state) {
    if (state[0]) return;
    scal[0] = scal[1];
    state[1] = state[1] + 1;
}
"""
# 1 回のホスト同期で流す反復数(CUDA Graph に 1 回撮る)。止まった後の余りは何もしないカーネルの起動だけ。
_ITERATIONS_PER_SYNC = 32


def _cupy():
    """CuPy とカーネルを読み込んで返す。読めなければ `Unavailable`(sticky)。"""
    global _kernels
    if _kernels is not None:
        return _kernels["cp"], _kernels
    try:
        import cupy as cp

        cp.cuda.runtime.getDeviceCount()
        # `--fmad=false`: 融合しない側の式をコンパイラに融合させない(`fma()` の呼び出しは融合のまま)。
        nofma = ("--fmad=false",)
        module = cp.RawModule(code=_CG_SRC, options=nofma)
        kernels = {"cp": cp, **{name: module.get_function(name) for name in
                                ("ddot_chunks", "cg_ddot", "cg_check", "cg_divide", "cg_beta", "cg_update_p",
                                 "cg_ell", "cg_alpha", "cg_update_xr", "cg_next")}}
    except Exception as exc:  # noqa: BLE001 —— import・ドライバ・デバイスのどれで落ちても理由を残して CPU へ
        raise Unavailable(f"cupy: {type(exc).__name__}: {exc}", sticky=True) from exc
    _kernels = kernels
    return cp, kernels


def openblas_threads() -> int:
    """numpy 同梱の OpenBLAS がいま使うスレッド数。読めなければ `Unavailable`(sticky)。"""
    libs = os.path.join(os.path.dirname(np.__file__), os.pardir, "numpy.libs")
    found = glob.glob(os.path.join(libs, "libscipy_openblas64_*.dll")) + glob.glob(
        os.path.join(libs, "libscipy_openblas64_*.so*"))
    try:
        fn = ctypes.CDLL(found[0]).scipy_openblas_get_num_threads64_
    except (IndexError, OSError, AttributeError) as exc:
        raise Unavailable(f"openblas-threads: {type(exc).__name__}: {exc}", sticky=True) from exc
    fn.restype = ctypes.c_int
    return int(fn())


def chunk_widths(n: int, threads: int) -> list[int]:
    """OpenBLAS の ddot が長さ `n` を `threads` 本へ割る区間の幅(残り÷残りのスレッドの切り上げを順に取る)。"""
    if n <= SINGLE_THREAD_MAX or threads <= 1:
        return [n]
    widths, remaining, used = [], n, 0
    while remaining > 0:
        width = (remaining + threads - used - 1) // (threads - used)
        remaining -= width
        if remaining < 0:
            width += remaining
        widths.append(width)
        used += 1
    return widths


class _Dot:
    """`np.dot(x, y)` とビット一致する GPU の内積(長さ `n` 専用)。"""

    def __init__(self, n: int, threads: int) -> None:
        cp, k = _cupy()
        widths = chunk_widths(n, threads)
        self.kernel = k["ddot_chunks"]
        self.chunks = len(widths)
        self.starts = cp.asarray(np.concatenate([[0], np.cumsum(widths)[:-1]]), dtype=cp.int64)
        self.widths = cp.asarray(widths, dtype=cp.int64)
        self.out = cp.empty(self.chunks, dtype=cp.float64)

    def __call__(self, x, y) -> float:
        self.kernel((self.chunks,), (16,), (x, y, self.starts, self.widths, self.out))
        partial = self.out.get()
        dot = float(partial[0])
        for value in partial[1:]:
            dot = dot + float(value)
        return dot


def _dot_matches(n: int, threads: int, trials: int = 4) -> bool:
    """長さ `n` の乱数ベクトルで、GPU の内積が `np.dot` とビット一致するか。"""
    cp, _ = _cupy()
    dot = _Dot(n, threads)
    rng = np.random.default_rng(n)
    for trial in range(trials):
        x = rng.standard_normal(n) * 10.0 ** rng.uniform(-6, 6, n if trial % 2 else 1)
        y = rng.standard_normal(n) * 10.0 ** rng.uniform(-6, 6, n if trial % 2 else 1)
        gx, gy = cp.asarray(x), cp.asarray(y)
        if dot(gx, gy) != float(np.dot(x, y)) or dot(gx, gx) != float(np.dot(x, x)):
            return False
    return True


def _cg(kf: sp.csr_matrix, ff: np.ndarray, threads: int, rtol: float,
        maxiter: int | None = None) -> tuple[np.ndarray, int, int]:
    """scipy の `cg(A, b, M=ヤコビ, rtol=rtol, maxiter=maxiter)`(初期値 0。`maxiter` の既定は scipy と同じ 10n)と同じ演算を
    同じ順序で行う。ホストと同期するのは `_ITERATIONS_PER_SYNC` 反復に 1 回(反復ごとに内積をホストへ戻すと、終盤の 5 万自由度では
    同期とカーネルの起動が計算より長かった。docs/TOPOPT_SPEED.md「GPU の CG の同期」)。"""
    cp, k = _cupy()
    n = kf.shape[0]
    widths = chunk_widths(n, threads)
    chunks = len(widths)
    starts = cp.asarray(np.concatenate([[0], np.cumsum(widths)[:-1]]), dtype=cp.int64)
    widths_d = cp.asarray(widths, dtype=cp.int64)
    indptr = cp.asarray(kf.indptr, dtype=cp.int64)
    row_len = (indptr[1:] - indptr[:-1]).astype(cp.int32)
    width = int(row_len.max()) if n else 0
    position = cp.arange(kf.nnz, dtype=cp.int64)
    rows = cp.searchsorted(indptr, position, side="right") - 1
    at = (position - indptr[rows]) * n + rows
    del position, rows, indptr
    vals = cp.zeros(width * n, dtype=cp.float64)
    cols = cp.zeros(width * n, dtype=cp.int32)
    vals[at] = cp.asarray(kf.data, dtype=cp.float64)
    cols[at] = cp.asarray(kf.indices, dtype=cp.int32)
    del at
    diag = cp.asarray(kf.diagonal())

    b = cp.asarray(ff, dtype=cp.float64)
    bnrm2 = math.sqrt(_Dot(n, threads)(b, b))
    if bnrm2 == 0.0:
        return cp.asnumpy(b), 0, 0
    atol = np.float64(max(0.0, rtol * bnrm2))
    maxiter = np.int32(n * 10 if maxiter is None else maxiter)
    x = cp.zeros(n, dtype=cp.float64)
    r = b.copy()
    z, p, q = (cp.empty(n, dtype=cp.float64) for _ in range(3))
    part_rr, part_rz, part_pq = (cp.empty(chunks, dtype=cp.float64) for _ in range(3))
    scal = cp.zeros(4, dtype=cp.float64)
    state = cp.zeros(2, dtype=cp.int32)
    grid, block, dots, one = ((n + _BLOCK - 1) // _BLOCK,), (_BLOCK,), ((chunks,), (16,)), ((1,), (1,))
    n_, chunks_ = np.int32(n), np.int32(chunks)

    def iteration():
        k["cg_ddot"](*dots, (r, r, starts, widths_d, part_rr, state))
        k["cg_check"](*one, (part_rr, chunks_, atol, maxiter, state))
        k["cg_divide"](grid, block, (n_, r, diag, z, state))
        k["cg_ddot"](*dots, (r, z, starts, widths_d, part_rz, state))
        k["cg_beta"](*one, (part_rz, chunks_, scal, state))
        k["cg_update_p"](grid, block, (n_, z, p, scal, state))
        k["cg_ell"](grid, block, (n_, row_len, cols, vals, p, q, state))
        k["cg_ddot"](*dots, (p, q, starts, widths_d, part_pq, state))
        k["cg_alpha"](*one, (part_pq, chunks_, scal, state))
        k["cg_update_xr"](grid, block, (n_, p, q, x, r, scal, state))
        k["cg_next"](*one, (scal, state))

    # **外部の罠(CuPy)**: 上の準備は既定のストリームに積まれていて、グラフを流す非ブロッキングのストリームはそれを待たない。
    # 先に揃えないと初期化前の配列を読み、収束しない・一致しない結果が時々出た(5 万自由度で 1 回観測。docs/TOPOPT_SPEED.md)。
    cp.cuda.Device().synchronize()
    stream = cp.cuda.Stream(non_blocking=True)
    with stream:
        stream.begin_capture()
        for _ in range(_ITERATIONS_PER_SYNC):
            iteration()
        graph = stream.end_capture()
    while True:
        graph.launch(stream)
        stopped, done = (int(v) for v in state.get(stream=stream))
        if stopped:
            return cp.asnumpy(x), done, 0 if stopped == 1 else done


def _release() -> str | None:
    """解き終わった VRAM をドライバへ返す(Unity・ローカル LLM と分け合っているので抱えない)。失敗したら理由を返す。

    **ここでは投げない。** 先に起きた非同期の CUDA エラーは解放の呼び出しで初めて表に出ることがあり、
    `finally` の中で投げると先に作った `Unavailable` を上書きして CPU への戻りをすり抜ける。失敗したら以後 GPU を使わない。
    """
    global _disabled
    if _kernels is None:
        return None
    try:
        _kernels["cp"].get_default_memory_pool().free_all_blocks()
    except Exception as exc:  # noqa: BLE001
        reason = f"gpu-error: VRAM の解放で {type(exc).__name__}: {exc}"
        if _disabled is None:
            _disabled = reason
        return reason
    return None


def _check_startup(make_system: Callable[[], tuple[sp.spmatrix, np.ndarray]], rtol: float) -> None:
    """小さい系を CPU と GPU の CG で解き、解のビットと反復数が一致するかを見る(scipy の積と更新の番人)。"""
    global _startup_checked
    kf, ff = make_system()
    diag = kf.diagonal()
    iterations = 0

    def count(_x: np.ndarray) -> None:
        nonlocal iterations
        iterations += 1

    u_cpu, info_cpu = spla.cg(kf, ff, M=spla.LinearOperator(kf.shape, matvec=lambda v: v / diag),
                              rtol=rtol, callback=count)
    u_gpu, it_gpu, info_gpu = _cg(_as_csr(kf), ff, openblas_threads(), rtol)
    if not (np.array_equal(u_cpu, u_gpu) and iterations == it_gpu and info_cpu == info_gpu):
        raise Unavailable("startup-check: 小さい系の CG が CPU とビット一致しない", sticky=True)
    _startup_checked = True


def _as_csr(kf: sp.spmatrix) -> sp.csr_matrix:
    csr = kf.tocsr()
    csr.sort_indices()
    return csr


def solve(kf: sp.spmatrix, ff: np.ndarray, *, rtol: float,
          make_startup_system: Callable[[], tuple[sp.spmatrix, np.ndarray]]) -> tuple[np.ndarray, int, int]:
    """K_ff u = f_f を GPU の CG で解き、(解, 反復数, info) を返す。info は scipy と同じ(0 = 収束)。

    使えない・合わないときは `Unavailable`。sticky な理由はこのプロセスの間覚えておき、以後は即座に投げる。
    """
    with _lock:
        return _solve_locked(kf, ff, rtol, make_startup_system)


def _solve_locked(kf, ff, rtol, make_startup_system):
    global _disabled
    if _disabled is not None:
        raise Unavailable(_disabled, sticky=True)
    try:
        _cupy()
        if not _startup_checked:
            _check_startup(make_startup_system, rtol)
        n = kf.shape[0]
        threads = openblas_threads()
        key = (n, 1 if n <= SINGLE_THREAD_MAX else threads)
        if key not in _dot_ok:
            _dot_ok[key] = _dot_matches(n, threads)
        if not _dot_ok[key]:
            raise Unavailable(f"dot-mismatch: n={n} threads={threads}", sticky=False)
        solved = _cg(_as_csr(kf), ff, threads, rtol)
    except Unavailable as exc:
        if exc.sticky:
            _disabled = exc.reason
        raise
    except Exception as exc:  # noqa: BLE001
        # **元の例外を連鎖させず、except の外で投げる。** 元の例外の traceback が `_cg` のフレームごと GPU の配列を
        # 握っているので、`from exc` で残すと `free_all_blocks` が返せず、CPU で解き直す間も VRAM を抱える。
        if isinstance(exc, _kernels["cp"].cuda.memory.OutOfMemoryError):
            failure = Unavailable(f"vram: {exc}", sticky=False)
        else:
            # CUDA の実行時エラー等。原因が分からないので以後は使わない。
            _disabled = f"gpu-error: {type(exc).__name__}: {exc}"
            failure = Unavailable(_disabled, sticky=True)
        solved = None
    finally:
        released = _release()
    if solved is None:
        raise failure
    if released is not None:
        # 解は写し終えているが、CUDA の例外が出た solve は他と同じく CPU で解き直す。
        raise Unavailable(released, sticky=True)
    return solved


def _cg_matrix_free(edof: np.ndarray, ke: np.ndarray, ee: np.ndarray, free: np.ndarray, ndof: int,
                    ff: np.ndarray, rtol: float) -> tuple[np.ndarray, int, int]:
    """K を組み立てず、要素ごとの積で `K_ff x` を作るヤコビ前処理つき CG(`fem` の `solver="matrix_free"` の GPU 版)。
    止め方・初期値は scipy の `cg` と同じだが、足し順は合わせていない(CPU とビット一致しない。完成形検査は1回測るだけなので
    合わせない)。正しさは呼び出し側が CPU で残差を検算する。"""
    cp, _ = _cupy()
    edof_d = cp.asarray(edof)
    index = cp.asarray(edof.ravel().astype(np.intp))
    ke_d = cp.asarray(ke, dtype=cp.float64)
    ee_d = cp.asarray(ee, dtype=cp.float64)[:, None]
    free_d = cp.asarray(free)
    full = cp.zeros(ndof, dtype=cp.float64)
    diag = cp.bincount(index, weights=(cp.diag(ke_d)[None, :] * ee_d).ravel(), minlength=ndof)[free_d]

    def apply(x):
        full[free_d] = x
        return cp.bincount(index, weights=((full[edof_d] @ ke_d) * ee_d).ravel(), minlength=ndof)[free_d]

    n = int(free.size)
    b = cp.asarray(ff, dtype=cp.float64)
    bnrm2 = float(cp.linalg.norm(b))
    if bnrm2 == 0.0:
        return cp.asnumpy(b), 0, 0
    atol = max(0.0, rtol * bnrm2)
    x = cp.zeros(n, dtype=cp.float64)
    r = b.copy()
    p = None
    rho_prev = 0.0
    for iteration in range(n * 10):
        if float(cp.linalg.norm(r)) < atol:
            return cp.asnumpy(x), iteration, 0
        z = r / diag
        rho_cur = float(r @ z)
        p = z if p is None else z + (rho_cur / rho_prev) * p
        q = apply(p)
        alpha = rho_cur / float(p @ q)
        x += alpha * p
        r -= alpha * q
        rho_prev = rho_cur
    return cp.asnumpy(x), n * 10, n * 10


def solve_matrix_free(edof: np.ndarray, ke: np.ndarray, ee: np.ndarray, free: np.ndarray, ndof: int,
                      ff: np.ndarray, *, rtol: float) -> tuple[np.ndarray, int, int]:
    """`_cg_matrix_free` を `solve` と同じ作法(使えなければ `Unavailable`・VRAM を返す)で呼ぶ。(解, 反復数, info)。"""
    global _disabled
    with _lock:
        if _disabled is not None:
            raise Unavailable(_disabled, sticky=True)
        try:
            solved = _cg_matrix_free(edof, ke, ee, free, ndof, ff, rtol)
        except Unavailable as exc:
            if exc.sticky:
                _disabled = exc.reason
            raise
        except Exception as exc:  # noqa: BLE001 —— 例外を連鎖させない理由は `_solve_locked` と同じ
            if isinstance(exc, _kernels["cp"].cuda.memory.OutOfMemoryError):
                failure = Unavailable(f"vram: {exc}", sticky=False)
            else:
                _disabled = f"gpu-error: {type(exc).__name__}: {exc}"
                failure = Unavailable(_disabled, sticky=True)
            solved = None
        finally:
            released = _release()
        if solved is None:
            raise failure
        if released is not None:
            raise Unavailable(released, sticky=True)
        return solved


def cupy_installed() -> bool:
    """CuPy が入っているか(読み込まずに調べる)。"""
    import importlib.util

    return importlib.util.find_spec("cupy") is not None


def describe() -> dict:
    """画面に出す GPU の状態 `{"name": GPU の名前 | None, "problem": 使えない理由 | None}`。CuPy を読む。"""
    if _disabled is not None:
        return {"name": None, "problem": _disabled}
    try:
        cp, _ = _cupy()
        name = cp.cuda.runtime.getDeviceProperties(0)["name"]
    except Unavailable as exc:
        return {"name": None, "problem": exc.reason}
    except Exception as exc:  # noqa: BLE001
        return {"name": None, "problem": f"gpu-error: {type(exc).__name__}: {exc}"}
    return {"name": name.decode() if isinstance(name, bytes) else str(name), "problem": None}


def reset() -> None:
    """覚えている状態(使わない理由・自己検査の結果)を消す。テストと、設定を変えて試し直すとき用。"""
    global _disabled, _startup_checked
    _disabled = None
    _startup_checked = False
    _dot_ok.clear()
