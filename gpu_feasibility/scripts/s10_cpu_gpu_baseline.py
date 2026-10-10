"""段階10(#104 の判断前の実測 a・b): Python 3.14 の環境で、CPU の CG の秒を実測し、同じ行列のビット一致版 GPU の CG と比べる。

実行(開発用の .venv。`uv sync --extra gpu` で CuPy を入れておく。測ったときは Python 3.14・numpy 2.4.6・scipy 1.17.1・cupy-cuda13x 14.2.0):
    .venv/Scripts/python.exe gpu_feasibility/scripts/s10_cpu_gpu_baseline.py [行列名 ...]
出力: gpu_feasibility/results/s10_cpu_gpu_baseline.json

CPU の CG は製品の `Model.prepare` / `PreparedSystem.solve` と同じ形で解く(`_cpucg.py`)。
"""
from __future__ import annotations

import datetime as dt
import json
import platform
import sys
import time
from pathlib import Path

import numpy as np
import scipy
import scipy.sparse as sp

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import cupy as cp  # noqa: E402

import _blasdot  # noqa: E402
import _gpucg  # noqa: E402
import _winmem  # noqa: E402
from _cpucg import DATA, RTOL, THREADS, cpu_cg, digest, load  # noqa: E402

RESULTS = ROOT / "results"


def gpu_cg(k: sp.csr_matrix, rhs: np.ndarray, blas_threads: int) -> dict:
    n = k.shape[0]
    t_test = time.perf_counter()
    self_test = _blasdot.self_test(n, blas_threads)
    t_test = time.perf_counter() - t_test
    cp.cuda.Stream.null.synchronize()
    t0 = time.perf_counter()
    dot = _blasdot.BlasDot(n, blas_threads)
    system = _gpucg.GpuSystem(k.data, k.indices, k.indptr, np.array(k.shape))
    b = cp.asarray(rhs)
    system.matvec("ell_nofma")
    cp.cuda.Stream.null.synchronize()
    setup = time.perf_counter() - t0
    t1 = time.perf_counter()
    u_gpu, iterations, info = _gpucg.cg_bitexact(system, b, dot, rtol=RTOL, matvec_kind="ell_nofma")
    u = cp.asnumpy(u_gpu)
    seconds = time.perf_counter() - t1
    rec = {"dot_self_test": bool(self_test), "dot_self_test_seconds": t_test, "setup_seconds": setup,
           "seconds": seconds, "iterations": int(iterations), "info": int(info), "sha": digest(u),
           "vram_pool_mib": cp.get_default_memory_pool().total_bytes() / 2**20}
    del system, b, u_gpu, dot
    cp.get_default_memory_pool().free_all_blocks()
    return rec


def main() -> None:
    names = sys.argv[1:] or sorted(p.stem for p in DATA.glob("*.npz"))
    blas_threads = _blasdot.openblas_threads()
    out_path = RESULTS / "s10_cpu_gpu_baseline.json"
    out = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else {}
    env = {"python": platform.python_version(), "numpy": np.__version__, "scipy": scipy.__version__,
           "cupy": cp.__version__, "openblas_threads": blas_threads, "spmv_threads": THREADS,
           "openblas": np.show_config(mode="dicts")["Build Dependencies"]["blas"].get("openblas configuration")}
    out["_env"] = env
    # CuPy の初回コンパイルを測定から外す(製品ではプロセスの寿命に1回)。
    k0, r0 = load(names[0])
    gpu_cg(k0, r0, blas_threads)
    for name in names:
        k, rhs = load(name)
        cpu, _u = cpu_cg(k, rhs, THREADS)
        gpu = gpu_cg(k, rhs, blas_threads)
        rec = {"free_dofs": int(k.shape[0]), "nnz": int(k.nnz), "cpu": cpu, "gpu": gpu,
               "bit_identical": cpu["sha"] == gpu["sha"], "iterations_match": cpu["iterations"] == gpu["iterations"],
               "speedup": (cpu["setup_seconds"] + cpu["seconds"]) / (gpu["setup_seconds"] + gpu["seconds"]),
               "cpu_ns_per_nnz_iter": cpu["seconds"] / max(cpu["iterations"], 1) / k.nnz * 1e9,
               "peak_working_set_gb": _winmem.peak_working_set_gb(),
               "measured_at": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
        out[name] = rec
        out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{name}: n={rec['free_dofs']} cpu {cpu['seconds']:.2f}s/{cpu['iterations']}it "
              f"gpu {gpu['setup_seconds']+gpu['seconds']:.2f}s/{gpu['iterations']}it selftest={gpu['dot_self_test']} "
              f"bit={rec['bit_identical']} x{rec['speedup']:.1f} ns/nnz/it={rec['cpu_ns_per_nnz_iter']:.3f}", flush=True)


if __name__ == "__main__":
    main()
