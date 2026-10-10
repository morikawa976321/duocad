"""段階3: 保存した K_ff を GPU へ送り、ヤコビ前処理つき CG の秒・反復数・残差を測る。

実行(開発用の .venv。`uv sync --extra gpu` で CuPy を入れておく): .venv/Scripts/python.exe gpu_feasibility/scripts/s3_gpu_cg.py [行列名 ...]
出力: gpu_feasibility/results/s3_gpu_cg.json(行列名ごとに上書き)

CPU の秒は測らない(本人指示)。比較値は製品の実行記録から出した「1反復・非ゼロ1個あたり 0.41〜0.48 ns」
(ROADMAP 2章)に、ここで数えた反復数と nnz を掛けた換算値。
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import time
from pathlib import Path

import numpy as np
import scipy.sparse as sp

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import cupy as cp  # noqa: E402
import cupyx.scipy.sparse.linalg as cspla  # noqa: E402

import _gpucg  # noqa: E402
import _winmem  # noqa: E402

DATA = ROOT / "data"
RESULTS = ROOT / "results"
RTOL = 1e-9
CPU_NS_PER_NNZ_ITER = (0.41, 0.48)
SPMV_REPEATS = 200


def sync() -> None:
    cp.cuda.Stream.null.synchronize()


def bench_spmv(apply, x) -> float:
    """行列ベクトル積 1 回の秒(同期込みの平均)。"""
    apply(x)
    sync()
    t0 = time.perf_counter()
    for _ in range(SPMV_REPEATS):
        y = apply(x)
    sync()
    del y
    return (time.perf_counter() - t0) / SPMV_REPEATS


def run(name: str) -> dict:
    z = np.load(DATA / f"{name}.npz")
    data, indices, indptr, shape, rhs = z["data"], z["indices"], z["indptr"], z["shape"], z["rhs"]
    k_host = sp.csr_matrix((data, indices, indptr), shape=tuple(shape))
    n, nnz = int(shape[0]), int(data.size)
    rhs_norm = float(np.linalg.norm(rhs))
    rec: dict = {"name": name, "free_dofs": n, "nnz": nnz, "rtol": RTOL,
                 "measured_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                 "host_ram_available_gb": _winmem.available_gb()}
    free_b, total_b = cp.cuda.Device(0).mem_info
    rec["vram_free_mib_before"] = free_b / 2**20

    t0 = time.perf_counter()
    system = _gpucg.GpuSystem(data, indices, indptr, shape)
    b = cp.asarray(rhs)
    sync()
    rec["transfer_to_gpu_seconds"] = time.perf_counter() - t0

    x_probe = cp.asarray(np.random.default_rng(0).standard_normal(n))
    y_host = k_host @ cp.asnumpy(x_probe)
    rec["spmv"] = {}
    for kind in ("cusparse", "raw", "raw_nofma"):
        apply = system.matvec(kind)
        seconds = bench_spmv(apply, x_probe)
        y = cp.asnumpy(apply(x_probe))
        rec["spmv"][kind] = {
            "ms": seconds * 1e3,
            "ns_per_nnz": seconds / nnz * 1e9,
            "max_rel_diff_vs_cpu": float(np.max(np.abs(y - y_host)) / np.max(np.abs(y_host))),
            "bit_identical_to_cpu": bool(np.array_equal(y, y_host)),
        }

    def finish(u_gpu, iterations: int, info: int, seconds: float) -> dict:
        t1 = time.perf_counter()
        u = cp.asnumpy(u_gpu)
        back = time.perf_counter() - t1
        residual = float(np.linalg.norm(k_host @ u - rhs) / rhs_norm)
        lo, hi = (c * 1e-9 * nnz * iterations for c in CPU_NS_PER_NNZ_ITER)
        return {
            "info": int(info), "iterations": int(iterations), "seconds": seconds,
            "ms_per_iteration": seconds / max(iterations, 1) * 1e3,
            "transfer_back_seconds": back,
            "relative_residual_host": residual,
            "cpu_seconds_converted": [lo, hi],
            "speedup_converted_excl_transfer": [lo / seconds, hi / seconds],
            "speedup_converted_incl_transfer": [
                lo / (seconds + back + rec["transfer_to_gpu_seconds"]),
                hi / (seconds + back + rec["transfer_to_gpu_seconds"]),
            ],
        }

    rec["cg"] = {}
    # CuPy 付属の cg(行列ベクトル積は cuSPARSE)
    count = 0

    def counter(_xk) -> None:
        nonlocal count
        count += 1

    precond = cspla.LinearOperator((n, n), matvec=lambda v: v * system.inv_diag, dtype=cp.float64)
    cspla.cg(system.csr, b, M=precond, rtol=RTOL, maxiter=3)
    sync()
    t0 = time.perf_counter()
    u, info = cspla.cg(system.csr, b, M=precond, rtol=RTOL, maxiter=n * 10, callback=counter)
    sync()
    rec["cg"]["cupyx"] = finish(u, count, info, time.perf_counter() - t0)

    solutions = {}
    for kind in ("cusparse", "raw", "raw_nofma"):
        _gpucg.cg_jacobi(system, b, rtol=RTOL, matvec_kind=kind, maxiter=3)
        sync()
        t0 = time.perf_counter()
        u, iterations, info = _gpucg.cg_jacobi(system, b, rtol=RTOL, matvec_kind=kind)
        sync()
        rec["cg"][f"own_{kind}"] = finish(u, iterations, info, time.perf_counter() - t0)
        solutions[kind] = cp.asnumpy(u)
    ref = solutions["raw_nofma"]
    scale = float(np.max(np.abs(ref)))
    rec["solution_max_rel_diff_vs_own_raw_nofma"] = {
        kind: float(np.max(np.abs(sol - ref)) / scale) for kind, sol in solutions.items() if kind != "raw_nofma"
    }

    pool = cp.get_default_memory_pool()
    rec["vram_pool_total_mib"] = pool.total_bytes() / 2**20
    del system, b, u, x_probe
    pool.free_all_blocks()
    return rec


def main() -> None:
    names = sys.argv[1:] or sorted(p.stem for p in DATA.glob("*.npz"))
    RESULTS.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS / "s3_gpu_cg.json"
    out = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else {}
    for name in names:
        rec = run(name)
        out[name] = rec
        out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        best = min(rec["cg"].items(), key=lambda kv: kv[1]["seconds"])
        print(f"{name}: dofs={rec['free_dofs']} nnz={rec['nnz']} "
              + " ".join(f"{k}={v['seconds']:.2f}s/{v['iterations']}it" for k, v in rec["cg"].items())
              + f" | best={best[0]} x{best[1]['speedup_converted_incl_transfer'][0]:.1f}-"
              f"{best[1]['speedup_converted_incl_transfer'][1]:.1f} (換算)")


if __name__ == "__main__":
    main()
