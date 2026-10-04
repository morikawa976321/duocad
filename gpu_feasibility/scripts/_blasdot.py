"""numpy の `np.dot`(倍精度ベクトル同士)と**同じ順序で足す**内積を GPU で計算する。

numpy は内積を OpenBLAS の ddot に渡す。ddot は足す順序が実装で決まっているので、同じ順序を GPU で
なぞれば結果はビット一致する。なぞっているのは次の 3 段(このマシンの numpy 2.4.6 同梱
OpenBLAS 0.3.31・実行時カーネル Haswell・14 スレッドで、`s5_bitexact_dot_check.py` が一致を確かめる):

1. 長さが 10000 を超えるとスレッド数ぶんの区間に割る(残り÷残りスレッドの切り上げを順に取る)。
2. 区間ごとに、先頭から 16 の倍数ぶんを 16 本の累算器(AVX2 の 4 レジスタ × 4 レーン)へ積和の融合で足し、
   レジスタごとに上下半分を足してから 4 レジスタを対で足す。端数は融合なしで 1 個ずつ足す。
   (融合の有無 × たたむ順序の 8 通りのうち、長さ 17〜244万の 14 通りすべてで一致したのはこの 1 通りだけ。
   results/s5_bitexact_dot.json)
3. 区間の結果を先頭から順に足す。

**numpy・OpenBLAS の版、CPU の種類、スレッド数のどれかが変わると順序が変わりうる。**
使う前に `self_test` で `np.dot` との一致を確かめること。
"""
from __future__ import annotations

import cupy as cp
import numpy as np

SINGLE_THREAD_MAX = 10000

_SRC = r"""
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
    for (long long i = lane; i < n1; i += 16) a = MAIN_STEP(x[start + i], y[start + i], a);
    acc[lane] = a;
    __syncthreads();
    if (lane == 0) {
        double dot;
#if REDUCE_BY_REGISTER
        double lo[4], hi[4];
        for (int r = 0; r < 4; ++r) { lo[r] = acc[4 * r] + acc[4 * r + 2]; hi[r] = acc[4 * r + 1] + acc[4 * r + 3]; }
        dot = ((lo[0] + lo[1]) + (lo[2] + lo[3])) + ((hi[0] + hi[1]) + (hi[2] + hi[3]));
#else
        double v[4];
        for (int l = 0; l < 4; ++l) v[l] = ((acc[l] + acc[4 + l]) + acc[8 + l]) + acc[12 + l];
        dot = (v[0] + v[2]) + (v[1] + v[3]);
#endif
        for (long long i = n1; i < width; ++i) dot = TAIL_STEP(y[start + i], x[start + i], dot);
        chunk_out[c] = dot;
    }
}
"""


def chunk_widths(n: int, threads: int) -> list[int]:
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


class BlasDot:
    """`np.dot(x, y)` とビット一致する値を返す GPU の内積(長さ `n` 専用)。"""

    def __init__(self, n: int, threads: int, *, fma_main: bool = True, fma_tail: bool = False,
                 reduce_by_register: bool = True) -> None:
        step = {True: "fma(a, b, c)", False: "((a) * (b) + (c))"}
        source = (
            f"#define MAIN_STEP(a, b, c) {step[fma_main]}\n"
            f"#define TAIL_STEP(a, b, c) {step[fma_tail]}\n"
            f"#define REDUCE_BY_REGISTER {int(reduce_by_register)}\n" + _SRC
        )
        # 融合しない側の式がコンパイラに融合されないよう、既定の融合を切る(fma() の呼び出しは融合のまま)。
        self.kernel = cp.RawKernel(source, "ddot_chunks", options=("--fmad=false",))
        widths = chunk_widths(n, threads)
        starts = np.concatenate([[0], np.cumsum(widths)[:-1]])
        self.chunks = len(widths)
        self.starts = cp.asarray(starts, dtype=cp.int64)
        self.widths = cp.asarray(widths, dtype=cp.int64)
        self.out = cp.empty(self.chunks, dtype=cp.float64)

    def __call__(self, x, y) -> float:
        self.kernel((self.chunks,), (16,), (x, y, self.starts, self.widths, self.out))
        partial = self.out.get()
        if self.chunks == 1:
            return float(partial[0])
        dot = 0.0
        for value in partial:
            dot = dot + float(value)
        return dot


def openblas_threads() -> int:
    """numpy 同梱の OpenBLAS がいま使うスレッド数。"""
    import ctypes
    import glob
    import os

    pattern = os.path.join(os.path.dirname(np.__file__), "..", "numpy.libs", "libscipy_openblas64_*.dll")
    lib = ctypes.CDLL(glob.glob(pattern)[0])
    fn = lib.scipy_openblas_get_num_threads64_
    fn.restype = ctypes.c_int
    return int(fn())


def self_test(n: int, threads: int, trials: int = 8, **variant) -> bool:
    """長さ `n` の乱数ベクトルで `np.dot` とビット一致するか。"""
    dot = BlasDot(n, threads, **variant)
    rng = np.random.default_rng(n)
    for trial in range(trials):
        x = rng.standard_normal(n) * 10.0 ** rng.uniform(-6, 6, n if trial % 2 else 1)
        y = rng.standard_normal(n) * 10.0 ** rng.uniform(-6, 6, n if trial % 2 else 1)
        if dot(cp.asarray(x), cp.asarray(y)) != float(np.dot(x, y)):
            return False
        if dot(cp.asarray(x), cp.asarray(x)) != float(np.dot(x, x)):
            return False
    return True
