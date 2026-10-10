"""K_ff のヤコビ前処理つき CG を GPU(CuPy)で解く。**解・反復数とも CPU の `scipy.sparse.linalg.cg` とビット一致する。**

`fem.PreparedSystem.solve` が、設定が `gpu` のときだけ呼ぶ(既定は CPU。`fem.FEM_DEVICE`)。
ここで使えない・合わないと分かったら `Unavailable` を投げ、呼び出し側が CPU で初期値から解き直す。

一致させている 3 つの演算(根拠の実測は gpu_feasibility/CONCLUSION.md・results/s5_bitexact_*.json・s10・s12):

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

_DDOT_SRC = r"""
extern "C" __global__
void ddot_chunks(const double* x, const double* y, const long long* starts, const long long* widths,
                 double* chunk_out) {
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
"""
# 行の k 番目の項を k ごとにまとめた並び(列優先)。足す順序は CSR と同じ列番号の昇順。
_ELL_SPMV_SRC = r"""
extern "C" __global__
void ell_spmv(const int n, const int* row_len, const int* cols, const double* vals,
              const double* x, double* y) {
    int i = blockDim.x * blockIdx.x + threadIdx.x;
    if (i >= n) return;
    double s = 0.0;
    const int len = row_len[i];
    for (int k = 0; k < len; ++k) {
        const long long at = (long long)k * n + i;
        s += vals[at] * x[cols[at]];
    }
    y[i] = s;
}
"""


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
        kernels = {
            "cp": cp,
            "ddot": cp.RawKernel(_DDOT_SRC, "ddot_chunks", options=nofma),
            "ell": cp.RawKernel(_ELL_SPMV_SRC, "ell_spmv", options=nofma),
            "update_p": cp.ElementwiseKernel("T z, T beta", "T p", "p = p * beta + z",
                                             "duocad_cg_update_p", options=nofma),
            "update_xr": cp.ElementwiseKernel("T alpha, T p, T q", "T x, T r",
                                              "x = x + alpha * p; r = r - alpha * q",
                                              "duocad_cg_update_xr", options=nofma),
            "divide": cp.ElementwiseKernel("T r, T d", "T z", "z = r / d", "duocad_cg_divide", options=nofma),
        }
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
        self.kernel = k["ddot"]
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


def _cg(kf: sp.csr_matrix, ff: np.ndarray, threads: int, rtol: float) -> tuple[np.ndarray, int, int]:
    """scipy の `cg(A, b, M=ヤコビ, rtol=rtol)`(初期値 0・maxiter 既定)と同じ演算を同じ順序で行う。"""
    cp, k = _cupy()
    n = kf.shape[0]
    dot = _Dot(n, threads)
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
    grid = ((n + _BLOCK - 1) // _BLOCK,)
    head = (cp.int32(n), row_len, cols, vals)

    b = cp.asarray(ff, dtype=cp.float64)
    bnrm2 = math.sqrt(dot(b, b))
    if bnrm2 == 0.0:
        return cp.asnumpy(b), 0, 0
    atol = max(0.0, rtol * bnrm2)
    maxiter = n * 10
    x = cp.zeros(n, dtype=cp.float64)
    r = b.copy()
    z = cp.empty(n, dtype=cp.float64)
    p = cp.empty(n, dtype=cp.float64)
    q = cp.empty(n, dtype=cp.float64)
    rho_prev = 0.0
    for iteration in range(maxiter):
        if math.sqrt(dot(r, r)) < atol:
            return cp.asnumpy(x), iteration, 0
        k["divide"](r, diag, z)
        rho_cur = dot(r, z)
        if iteration > 0:
            k["update_p"](z, rho_cur / rho_prev, p)
        else:
            cp.copyto(p, z)
        k["ell"](grid, (_BLOCK,), (*head, p, q))
        alpha = rho_cur / dot(p, q)
        k["update_xr"](alpha, p, q, x, r)
        rho_prev = rho_cur
    return cp.asnumpy(x), maxiter, maxiter


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


def reset() -> None:
    """覚えている状態(使わない理由・自己検査の結果)を消す。テストと、設定を変えて試し直すとき用。"""
    global _disabled, _startup_checked
    _disabled = None
    _startup_checked = False
    _dot_ok.clear()
