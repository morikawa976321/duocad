"""試作: gpu_cg._cg と同じ演算順で、スカラーを GPU に置いてホスト同期を K 反復に1回へ減らした CG。

内積の区間和(ddot_chunks)は製品のカーネルをそのまま使い、区間の和を先頭から足す処理と alpha/beta の計算を
各カーネルの中で(全スレッドが同じ順序で)行う。収束したら done を立て、以後の更新を全部止める(x は収束時の値で凍る)。
"""
from __future__ import annotations

import math

import numpy as np

from duocad.topopt import gpu_cg

_SRC = r"""
__device__ __forceinline__ double seqsum(const double* part, int chunks) {
    double s = part[0];
    for (int c = 1; c < chunks; ++c) s = s + part[c];
    return s;
}

// z = r / d。全スレッドが同じ rr から収束を判定し、スレッド0が done と反復数を書く。
extern "C" __global__
void cg_divide(const int n, const double* r, const double* d, double* z, const double* part_rr, const int chunks,
               const double atol, int* state) {
    // state[0] = done, state[1] = iteration
    if (state[0]) return;
    double rr = seqsum(part_rr, chunks);
    bool conv = sqrt(rr) < atol;
    int i = blockDim.x * blockIdx.x + threadIdx.x;
    if (conv) {
        if (i == 0) state[0] = 1;
        return;
    }
    if (i < n) z[i] = r[i] / d[i];
}

// p = p * beta + z(初回は p = z)。beta = rho_cur / rho_prev。
extern "C" __global__
void cg_update_p(const int n, const double* z, double* p, const double* part_rz, const int chunks,
                 const double* scal, const int* state) {
    if (state[0]) return;
    int i = blockDim.x * blockIdx.x + threadIdx.x;
    if (i >= n) return;
    if (state[1] == 0) { p[i] = z[i]; return; }
    double rho_cur = seqsum(part_rz, chunks);
    double beta = rho_cur / scal[0];
    double t = p[i] * beta;
    p[i] = t + z[i];
}

extern "C" __global__
void cg_ell(const int n, const int* row_len, const int* cols, const double* vals,
            const double* x, double* y, const int* state) {
    if (state[0]) return;
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

// alpha = rho_cur / (p.q); x += alpha p; r -= alpha q。スレッド0 が rho_prev と反復数を進める
// (このカーネルの中で rho_prev を読むスレッドは無い)。
extern "C" __global__
void cg_update_xr(const int n, const double* p, const double* q, double* x, double* r,
                  const double* part_rz, const double* part_pq, const int chunks, double* scal, int* state) {
    if (state[0]) return;
    double rho_cur = seqsum(part_rz, chunks);
    double alpha = rho_cur / seqsum(part_pq, chunks);
    int i = blockDim.x * blockIdx.x + threadIdx.x;
    if (i < n) {
        double ap = alpha * p[i];
        x[i] = x[i] + ap;
        double aq = alpha * q[i];
        r[i] = r[i] - aq;
    }
    if (i == 0) { scal[0] = rho_cur; state[1] = state[1] + 1; }
}

// 製品の ddot_chunks と同じ(done なら何もしない版)。
extern "C" __global__
void ddot_chunks_g(const double* x, const double* y, const long long* starts, const long long* widths,
                   double* chunk_out, const int* state) {
    if (state[0]) return;
    const int c = blockIdx.x;
    const int lane = threadIdx.x;
    const long long start = starts[c];
    const long long width = widths[c];
    const long long n1 = width & ~15LL;
    __shared__ double acc[16];
    double a = 0.0;
    long long i = lane;
    const long long n8 = n1 - 16LL * 7;
    for (; i < n8; i += 16 * 8) {
        double xv[8], yv[8];
        #pragma unroll
        for (int u = 0; u < 8; ++u) { xv[u] = __ldg(x + start + i + 16 * u); yv[u] = __ldg(y + start + i + 16 * u); }
        #pragma unroll
        for (int u = 0; u < 8; ++u) a = fma(xv[u], yv[u], a);
    }
    for (; i < n1; i += 16) a = fma(x[start + i], y[start + i], a);
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

_K = None


def _kernels():
    global _K
    if _K is None:
        import cupy as cp
        mod = cp.RawModule(code=_SRC, options=("--fmad=false",))
        _K = {name: mod.get_function(name) for name in
              ("cg_divide", "cg_update_p", "cg_ell", "cg_update_xr", "ddot_chunks_g")}
    return _K


def cg(kf, ff, threads: int, rtol: float, *, check_every: int = 32, use_graph: bool = False):
    import cupy as cp
    k = _kernels()
    kf = gpu_cg._as_csr(kf)
    n = kf.shape[0]
    widths = gpu_cg.chunk_widths(n, threads)
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
    # 初期の bnrm2 は製品と同じくホストで足す
    dot0 = gpu_cg._Dot(n, threads)
    bnrm2 = math.sqrt(dot0(b, b))
    if bnrm2 == 0.0:
        return cp.asnumpy(b), 0, 0
    atol = max(0.0, rtol * bnrm2)
    maxiter = n * 10
    x = cp.zeros(n, dtype=cp.float64)
    r = b.copy()
    z = cp.empty(n, dtype=cp.float64)
    p = cp.empty(n, dtype=cp.float64)
    q = cp.empty(n, dtype=cp.float64)
    part_rr = cp.empty(chunks, dtype=cp.float64)
    part_rz = cp.empty(chunks, dtype=cp.float64)
    part_pq = cp.empty(chunks, dtype=cp.float64)
    scal = cp.zeros(1, dtype=cp.float64)
    state = cp.zeros(2, dtype=cp.int32)
    B = 256
    grid = ((n + B - 1) // B,)
    ni = np.int32(n)
    ci = np.int32(chunks)
    at_ = np.float64(atol)

    def one():
        k["ddot_chunks_g"]((chunks,), (16,), (r, r, starts, widths_d, part_rr, state))
        k["cg_divide"](grid, (B,), (ni, r, diag, z, part_rr, ci, at_, state))
        k["ddot_chunks_g"]((chunks,), (16,), (r, z, starts, widths_d, part_rz, state))
        k["cg_update_p"](grid, (B,), (ni, z, p, part_rz, ci, scal, state))
        k["cg_ell"](grid, (B,), (ni, row_len, cols, vals, p, q, state))
        k["ddot_chunks_g"]((chunks,), (16,), (p, q, starts, widths_d, part_pq, state))
        k["cg_update_xr"](grid, (B,), (ni, p, q, x, r, part_rz, part_pq, ci, scal, state))

    graph = None
    stream = None
    if use_graph:
        stream = cp.cuda.Stream(non_blocking=True)
        with stream:
            stream.begin_capture()
            for _ in range(check_every):
                one()
            graph = stream.end_capture()
    # 準備(x・r・state の初期化)は既定ストリームに積まれている。非ブロッキングのストリームはそれを待たないので、ここで揃える
    # (揃えないとグラフが初期化前の配列を読み、収束しない・一致しない結果が時々出た)。
    cp.cuda.Device().synchronize()
    launched = 0
    while True:
        if graph is not None:
            graph.launch(stream)
            stream.synchronize()
        else:
            for _ in range(check_every):
                one()
        launched += check_every
        done, it = (int(v) for v in state.get())
        if done:
            return cp.asnumpy(x), it, 0
        if it >= maxiter:
            # 製品は maxiter 反復で止める。ここでは超過分を走らせてしまうので試作では非収束の扱いだけ返す
            return cp.asnumpy(x), maxiter, maxiter
