"""段階0: GPU の倍精度疎行列 CG が動くかを確認する。

実行(開発用の .venv。`uv sync --extra gpu` で CuPy を入れておく): .venv/Scripts/python.exe gpu_feasibility/scripts/s0_env_check.py
出力: gpu_feasibility/results/s0_env.json
"""
from __future__ import annotations

import json
import platform
import sys
import time
from pathlib import Path

import numpy as np
import scipy
import scipy.sparse as sp
import scipy.sparse.linalg as spla

RESULTS = Path(__file__).resolve().parents[1] / "results"


def laplacian_3d(n: int) -> sp.csr_matrix:
    """n^3 点の 7 点差分ラプラシアン(対称正定値)。動作確認用で、製品の行列ではない。"""
    one = sp.identity(n, format="csr")
    d = sp.diags([-1.0, 2.0, -1.0], [-1, 0, 1], shape=(n, n), format="csr")
    return (sp.kron(sp.kron(d, one), one) + sp.kron(sp.kron(one, d), one) + sp.kron(sp.kron(one, one), d)).tocsr()


def main() -> None:
    out: dict = {
        "python": sys.version,
        "platform": platform.platform(),
        "numpy": np.__version__,
        "scipy": scipy.__version__,
    }
    import cupy as cp
    import cupyx
    import cupyx.scipy.sparse as csp
    import cupyx.scipy.sparse.linalg as cspla

    out["cupy"] = cp.__version__
    dev = cp.cuda.Device(0)
    props = cp.cuda.runtime.getDeviceProperties(0)
    free_b, total_b = dev.mem_info
    out["gpu"] = {
        "name": props["name"].decode() if isinstance(props["name"], bytes) else str(props["name"]),
        "compute_capability": dev.compute_capability,
        "vram_total_mib": total_b / 2**20,
        "vram_free_mib": free_b / 2**20,
        "cuda_runtime_version": cp.cuda.runtime.runtimeGetVersion(),
        "cuda_driver_version": cp.cuda.runtime.driverGetVersion(),
    }

    # (b) 倍精度 CSR の行列ベクトル積が CPU と一致するか
    a = laplacian_3d(60)
    rng = np.random.default_rng(0)
    x = rng.standard_normal(a.shape[0])
    y_cpu = a @ x
    a_gpu = csp.csr_matrix(a)
    y_gpu = cp.asnumpy(a_gpu @ cp.asarray(x))
    out["spmv"] = {
        "n": int(a.shape[0]),
        "nnz": int(a.nnz),
        "dtype": str(a_gpu.dtype),
        "max_rel_diff": float(np.max(np.abs(y_gpu - y_cpu)) / np.max(np.abs(y_cpu))),
    }

    # (c) ヤコビ前処理つき CG が rtol=1e-9 で収束するか(CPU の scipy と反復数・解を比べる)
    b = rng.standard_normal(a.shape[0])
    diag = a.diagonal()
    it_cpu = 0

    def count_cpu(_xk):
        nonlocal it_cpu
        it_cpu += 1

    m_cpu = spla.LinearOperator(a.shape, matvec=lambda v: v / diag)
    t0 = time.perf_counter()
    u_cpu, info_cpu = spla.cg(a, b, M=m_cpu, rtol=1e-9, callback=count_cpu)
    t_cpu = time.perf_counter() - t0

    b_gpu = cp.asarray(b)
    d_gpu = cp.asarray(diag)
    it_gpu = 0

    def count_gpu(_xk):
        nonlocal it_gpu
        it_gpu += 1

    m_gpu = cspla.LinearOperator(a.shape, matvec=lambda v: v / d_gpu, dtype=cp.float64)
    cspla.cg(a_gpu, b_gpu, M=m_gpu, rtol=1e-9, maxiter=5)  # カーネルのコンパイルを先に済ませる
    cp.cuda.Stream.null.synchronize()
    t0 = time.perf_counter()
    u_gpu, info_gpu = cspla.cg(a_gpu, b_gpu, M=m_gpu, rtol=1e-9, callback=count_gpu)
    cp.cuda.Stream.null.synchronize()
    t_gpu = time.perf_counter() - t0
    u_gpu_h = cp.asnumpy(u_gpu)
    bn = np.linalg.norm(b)
    out["cg"] = {
        "cpu": {"info": int(info_cpu), "iterations": it_cpu, "seconds": t_cpu,
                "rel_residual": float(np.linalg.norm(a @ u_cpu - b) / bn)},
        "gpu": {"info": int(info_gpu), "iterations": it_gpu, "seconds": t_gpu,
                "rel_residual": float(np.linalg.norm(a @ u_gpu_h - b) / bn)},
        "max_rel_diff_solution": float(np.max(np.abs(u_gpu_h - u_cpu)) / np.max(np.abs(u_cpu))),
    }
    out["cupyx_show_config"] = str(cupyx.get_runtime_info())

    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "s0_env.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in out.items() if k != "cupyx_show_config"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
