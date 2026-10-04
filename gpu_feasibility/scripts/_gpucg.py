"""GPU 上のヤコビ前処理つき CG と、行列ベクトル積の実装(段階3・4 が共有する)。

漸化式と収束判定は `scipy.sparse.linalg.cg`(製品の `fem.PreparedSystem.solve` が呼ぶもの)と同じ順序:
反復の頭で `||r|| < rtol * ||b||` を判定し、z = r / diag、rho = r·z、p = z + beta p、q = A p、
alpha = rho / p·q、x += alpha p、r -= alpha q。初期値は 0。
"""
from __future__ import annotations

import math

import cupy as cp
import cupyx.scipy.sparse as csp
import numpy as np

# 1 行を 1 スレッドが列番号の昇順に足す。足す順序が固定なので実行ごとに同じ値になる。
_CSR_SPMV_SRC = r"""
extern "C" __global__
void csr_spmv(const int n, const int* indptr, const int* indices, const double* data,
              const double* x, double* y) {
    int i = blockDim.x * blockIdx.x + threadIdx.x;
    if (i >= n) return;
    double s = 0.0;
    for (int j = indptr[i]; j < indptr[i + 1]; ++j) s += data[j] * x[indices[j]];
    y[i] = s;
}
"""
# 同じ足し方を、行の k 番目の項を k ごとにまとめた並び(列優先)で行う。隣のスレッドが隣のメモリを読む。
# 足す順序は CSR 版と同じ(列番号の昇順)なので値も同じ。
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
_BLOCK = 256
_KERNELS: dict[bool, cp.RawKernel] = {}
_NOFMA = ("--fmad=false",)

_update_p = cp.ElementwiseKernel("T z, T beta", "T p", "p = z + beta * p", "gpucg_update_p")
_update_xr = cp.ElementwiseKernel("T alpha, T p, T q", "T x, T r", "x += alpha * p; r -= alpha * q",
                                  "gpucg_update_xr")
# numpy の `p *= beta; p += z`、`x += alpha*p`、`r -= alpha*q`、`r / d` と同じ丸め(積と和を融合しない)。
_exact_update_p = cp.ElementwiseKernel("T z, T beta", "T p", "p = p * beta + z", "gpucg_exact_update_p",
                                       options=_NOFMA)
_exact_update_xr = cp.ElementwiseKernel("T alpha, T p, T q", "T x, T r", "x = x + alpha * p; r = r - alpha * q",
                                        "gpucg_exact_update_xr", options=_NOFMA)
_exact_divide = cp.ElementwiseKernel("T r, T d", "T z", "z = r / d", "gpucg_exact_divide", options=_NOFMA)


def _raw_kernel(fmad: bool) -> cp.RawKernel:
    if fmad not in _KERNELS:
        options = () if fmad else ("--fmad=false",)
        _KERNELS[fmad] = cp.RawKernel(_CSR_SPMV_SRC, "csr_spmv", options=options)
    return _KERNELS[fmad]


class GpuSystem:
    """GPU に載せた K_ff(CSR)・対角。`matvec(kind)` で行列ベクトル積の実装を選ぶ。"""

    def __init__(self, data, indices, indptr, shape) -> None:
        self.n = int(shape[0])
        self.data = cp.asarray(data, dtype=cp.float64)
        self.indices = cp.asarray(indices, dtype=cp.int32)
        self.indptr = cp.asarray(indptr, dtype=cp.int32)
        self.csr = csp.csr_matrix((self.data, self.indices, self.indptr), shape=(self.n, self.n))
        self.diag = self.csr.diagonal()
        self.inv_diag = 1.0 / self.diag
        self._ell = None

    def _build_ell(self):
        n, nnz = self.n, int(self.data.size)
        indptr = self.indptr.astype(cp.int64)
        row_len = (indptr[1:] - indptr[:-1]).astype(cp.int32)
        width = int(row_len.max())
        position = cp.arange(nnz, dtype=cp.int64)
        rows = cp.searchsorted(indptr, position, side="right") - 1
        at = (position - indptr[rows]) * n + rows
        del position, rows
        vals = cp.zeros(width * n, dtype=cp.float64)
        cols = cp.zeros(width * n, dtype=cp.int32)
        vals[at] = self.data
        cols[at] = self.indices
        del at
        self._ell = (row_len, cols, vals)
        self.ell_width = width

    def matvec(self, kind: str):
        """`kind`: "cusparse"(CuPy の `A @ x`)/ "raw"(自作カーネル)/ "raw_nofma"(自作・積和の融合なし)/
        "ell_nofma"(自作・融合なし・列優先の並び)。"""
        if kind == "cusparse":
            return lambda x: self.csr @ x
        if kind == "ell_nofma":
            if self._ell is None:
                self._build_ell()
            ell_kernel = cp.RawKernel(_ELL_SPMV_SRC, "ell_spmv", options=_NOFMA)
            ell_grid = ((self.n + _BLOCK - 1) // _BLOCK,)
            ell_head = (cp.int32(self.n), *self._ell)

            def apply_ell(x, out=None):
                y = cp.empty(self.n, dtype=cp.float64) if out is None else out
                ell_kernel(ell_grid, (_BLOCK,), (*ell_head, x, y))
                return y

            return apply_ell
        kernel = _raw_kernel(fmad=(kind == "raw"))
        grid = ((self.n + _BLOCK - 1) // _BLOCK,)
        args_head = (cp.int32(self.n), self.indptr, self.indices, self.data)

        def apply(x, out=None):
            y = cp.empty(self.n, dtype=cp.float64) if out is None else out
            kernel(grid, (_BLOCK,), (*args_head, x, y))
            return y

        return apply


def _pinned_empty(n: int) -> np.ndarray:
    mem = cp.cuda.alloc_pinned_memory(n * 8)
    return np.frombuffer(mem, dtype=np.float64, count=n)


class HybridMatvec:
    """ホストのベクトルを受け取り、K·x だけを GPU で計算してホストへ返す。

    `raw_nofma` の行列ベクトル積は scipy の CSR の積とビット一致する(実測: results/s3_gpu_cg.json の
    `bit_identical_to_cpu`)。これを `scipy.sparse.linalg.cg` の `A` に渡せば、内積やベクトル更新は
    CPU のままなので、解も CPU 版とビット一致する。

    **返す配列は呼ぶたびに上書きされる同じバッファ**(scipy の cg は q を次の積の前に使い切る)。
    """

    def __init__(self, system: GpuSystem, kind: str = "raw_nofma") -> None:
        self.system = system
        self.kernel = _raw_kernel(fmad=(kind == "raw"))
        self.grid = ((system.n + _BLOCK - 1) // _BLOCK,)
        self.x_host = _pinned_empty(system.n)
        self.y_host = _pinned_empty(system.n)
        self.x_gpu = cp.empty(system.n, dtype=cp.float64)
        self.y_gpu = cp.empty(system.n, dtype=cp.float64)

    def __call__(self, x: np.ndarray) -> np.ndarray:
        s = self.system
        np.copyto(self.x_host, x.reshape(-1))
        self.x_gpu.set(self.x_host)
        self.kernel(self.grid, (_BLOCK,), (cp.int32(s.n), s.indptr, s.indices, s.data, self.x_gpu, self.y_gpu))
        self.y_gpu.get(out=self.y_host)
        return self.y_host


def cg_jacobi(system: GpuSystem, b, *, rtol: float = 1e-9, matvec_kind: str = "cusparse",
              maxiter: int | None = None):
    """(解, 反復数, info)。info は scipy と同じ(0 = 収束、正 = maxiter に達した)。"""
    matvec = system.matvec(matvec_kind)
    inv_diag = system.inv_diag
    atol = rtol * float(cp.linalg.norm(b))
    n = b.size
    maxiter = n * 10 if maxiter is None else maxiter
    x = cp.zeros(n, dtype=cp.float64)
    r = b.copy()
    p = cp.zeros(n, dtype=cp.float64)
    rho_prev = 1.0
    for iteration in range(maxiter):
        if float(cp.linalg.norm(r)) < atol:
            return x, iteration, 0
        z = r * inv_diag
        rho = float(cp.dot(r, z))
        _update_p(z, rho / rho_prev if iteration else 0.0, p)
        q = matvec(p)
        alpha = rho / float(cp.dot(p, q))
        _update_xr(alpha, p, q, x, r)
        rho_prev = rho
    return x, maxiter, maxiter


def cg_bitexact(system: GpuSystem, b, dot, *, rtol: float = 1e-9, matvec_kind: str = "ell_nofma",
                maxiter: int | None = None):
    """scipy の cg と同じ演算を、同じ順序・同じ丸めで GPU 上で行う。(解, 反復数, info)。

    `dot` は `_blasdot.BlasDot`(`np.dot` と同じ順序で足す内積)。行列ベクトル積は `raw_nofma` か `ell_nofma`
    (scipy の CSR の積とビット一致)、ベクトル更新と前処理は積と和を融合しない要素ごとの演算。
    3 つがそろうと、解は CPU の `scipy.sparse.linalg.cg` とビット一致する(実測: results/s5_bitexact_cg.json)。
    """
    matvec = system.matvec(matvec_kind)
    diag = system.diag
    n = b.size
    bnrm2 = math.sqrt(dot(b, b))
    if bnrm2 == 0.0:
        return b.copy(), 0, 0
    atol = max(0.0, rtol * bnrm2)
    maxiter = n * 10 if maxiter is None else maxiter
    x = cp.zeros(n, dtype=cp.float64)
    r = b.copy()
    z = cp.empty(n, dtype=cp.float64)
    p = cp.empty(n, dtype=cp.float64)
    q = cp.empty(n, dtype=cp.float64)
    rho_prev = 0.0
    for iteration in range(maxiter):
        if math.sqrt(dot(r, r)) < atol:
            return x, iteration, 0
        _exact_divide(r, diag, z)
        rho_cur = dot(r, z)
        if iteration > 0:
            _exact_update_p(z, rho_cur / rho_prev, p)
        else:
            cp.copyto(p, z)
        matvec(p, out=q)
        alpha = rho_cur / dot(p, q)
        _exact_update_xr(alpha, p, q, x, r)
        rho_prev = rho_cur
    return x, maxiter, maxiter
