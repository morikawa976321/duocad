"""製品と同じ形の CPU の CG(ヤコビ前処理・rtol 1e-9・行列ベクトル積を行ブロックで並列化)。s10・s11 が使う。

製品の `duocad.topopt` はパッケージの import が OCP を要求するので、並列化の部分(fem.py `_BlockedMatvec`)だけを
ここに写してある。行ブロック並列の積はスレッド数に関わらず `kf @ x` とビット一致するので、写し方が製品と違っても
解のビットは変わらない。
"""
from __future__ import annotations

import hashlib
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import scipy.sparse as sp
import scipy.sparse.linalg as spla

DATA = Path(__file__).resolve().parent.parent / "data"
RTOL = 1e-9
# 製品の既定(fem.py `_default_thread_count`: 論理コアの半分)。
THREADS = max(1, (os.cpu_count() or 1) // 2)
MIN_ROWS_PER_THREAD = 512


def digest(u: np.ndarray) -> str:
    return hashlib.sha256(np.ascontiguousarray(u).tobytes()).hexdigest()[:16]


class BlockedMatvec:
    def __init__(self, mat: sp.csr_matrix, threads: int) -> None:
        eff = max(1, min(threads, mat.shape[0] // MIN_ROWS_PER_THREAD or 1))
        self.mat = mat
        self.bounds = np.linspace(0, mat.shape[0], eff + 1).astype(np.int64)
        self.blocks = [mat[self.bounds[i]:self.bounds[i + 1]] for i in range(eff)]
        self.pool = ThreadPoolExecutor(max_workers=eff) if eff > 1 else None

    def __call__(self, x: np.ndarray) -> np.ndarray:
        if self.pool is None:
            return self.mat @ x
        out = np.empty(self.mat.shape[0], dtype=float)
        futures = [self.pool.submit(lambda b=b: b @ x) for b in self.blocks]
        for lo, hi, fut in zip(self.bounds[:-1], self.bounds[1:], futures, strict=True):
            out[lo:hi] = fut.result()
        return out


def load(name: str) -> tuple[sp.csr_matrix, np.ndarray]:
    z = np.load(DATA / f"{name}.npz")
    k = sp.csr_matrix((z["data"], z["indices"], z["indptr"]), shape=tuple(z["shape"]))
    return k, z["rhs"]


def cpu_cg(k: sp.csr_matrix, rhs: np.ndarray, threads: int) -> tuple[dict, np.ndarray]:
    t0 = time.perf_counter()
    kf = k.tocsc()  # 製品は K_ff を CSC で持ち、`_BlockedMatvec` が CSR に直す
    diag = kf.diagonal()
    precond = spla.LinearOperator(kf.shape, matvec=lambda v: v / diag)
    a_op: sp.spmatrix | spla.LinearOperator = kf
    if threads > 1:
        a_op = spla.LinearOperator(kf.shape, matvec=BlockedMatvec(kf.tocsr(), threads), dtype=float)
    setup = time.perf_counter() - t0
    iterations = 0

    def counter(_x: np.ndarray) -> None:
        nonlocal iterations
        iterations += 1

    t1 = time.perf_counter()
    u, info = spla.cg(a_op, rhs, M=precond, rtol=RTOL, callback=counter)
    seconds = time.perf_counter() - t1
    return {"threads": threads, "setup_seconds": setup, "seconds": seconds, "iterations": iterations,
            "info": int(info), "sha": digest(u),
            "relative_residual": float(np.linalg.norm(k @ u - rhs) / np.linalg.norm(rhs))}, u
