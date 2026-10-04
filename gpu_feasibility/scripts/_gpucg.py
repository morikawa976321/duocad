"""GPU 上のヤコビ前処理つき CG と、行列ベクトル積の実装(段階3・4 が共有する)。

漸化式と収束判定は `scipy.sparse.linalg.cg`(製品の `fem.PreparedSystem.solve` が呼ぶもの)と同じ順序:
反復の頭で `||r|| < rtol * ||b||` を判定し、z = r / diag、rho = r·z、p = z + beta p、q = A p、
alpha = rho / p·q、x += alpha p、r -= alpha q。初期値は 0。
"""
from __future__ import annotations

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
_BLOCK = 256
_KERNELS: dict[bool, cp.RawKernel] = {}

_update_p = cp.ElementwiseKernel("T z, T beta", "T p", "p = z + beta * p", "gpucg_update_p")
_update_xr = cp.ElementwiseKernel("T alpha, T p, T q", "T x, T r", "x += alpha * p; r -= alpha * q",
                                  "gpucg_update_xr")


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

    def matvec(self, kind: str):
        """`kind`: "cusparse"(CuPy の `A @ x`)/ "raw"(自作カーネル)/ "raw_nofma"(自作・積和の融合なし)。"""
        if kind == "cusparse":
            return lambda x: self.csr @ x
        kernel = _raw_kernel(fmad=(kind == "raw"))
        grid = ((self.n + _BLOCK - 1) // _BLOCK,)
        args_head = (cp.int32(self.n), self.indptr, self.indices, self.data)

        def apply(x):
            y = cp.empty(self.n, dtype=cp.float64)
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
