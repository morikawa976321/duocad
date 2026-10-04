"""段階6: 探索(`binary_search.find_best_feasible`)を CPU の CG と GPU の CG(ビット一致版)で走らせ、
試行の履歴と採用形状が一致するかを確かめる。

実行(検証用 venv。PYTHONPATH にリポジトリ直下を入れる):

    gpu_feasibility/.venv/Scripts/python.exe gpu_feasibility/scripts/s6_search_identity.py gpu --load 1000 --size 4.8 --width 10
    gpu_feasibility/.venv/Scripts/python.exe gpu_feasibility/scripts/s6_search_identity.py cpu --load 1000 --size 4.8 --width 10 --threads 4
    gpu_feasibility/.venv/Scripts/python.exe gpu_feasibility/scripts/s6_search_identity.py compare --load 1000 --size 4.8 --width 10

GPU の CG は**このプロセスの中でだけ** `fem.spla.cg` を差し替えて入れる。製品のファイルは変更しない。
FEM ロックは取らない。CPU の秒は 07 と競合しスレッド数も既定と違うので、ベースラインではない。
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
REPO = ROOT.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "topopt_checks"))
sys.path.insert(0, str(REPO / "topopt_checks" / "idle_validation"))

import numpy as np  # noqa: E402

import _winmem  # noqa: E402

HANGER_MODEL = REPO / "models" / "hanger_bar_attachment.json"
RESULTS = ROOT / "results"


class GpuCg:
    """`scipy.sparse.linalg.cg` と同じ引数で呼べる、GPU のビット一致版 CG。

    `fem.PARALLEL_THREADS = 1` のとき製品は K_ff(疎行列)をそのまま渡してくるので、それを GPU へ送る。
    内積が `np.dot` と一致しない環境では CPU の cg に戻す(戻した回数を数える)。
    """

    def __init__(self, real_cg) -> None:
        import _blasdot

        self.real_cg = real_cg
        self.threads = _blasdot.openblas_threads()
        self.calls: list[dict] = []
        self.fallbacks = 0
        self._dot_ok: dict[int, bool] = {}

    def __call__(self, a_op, b, x0=None, *, rtol=1e-5, atol=0.0, maxiter=None, M=None, callback=None):
        import cupy as cp
        import scipy.sparse as sp

        import _blasdot
        import _gpucg

        if x0 is not None or atol != 0.0 or not sp.issparse(a_op):
            self.fallbacks += 1
            return self.real_cg(a_op, b, x0, rtol=rtol, atol=atol, maxiter=maxiter, M=M, callback=callback)
        t0 = time.perf_counter()
        n = a_op.shape[0]
        if n not in self._dot_ok:
            self._dot_ok[n] = _blasdot.self_test(n, self.threads, trials=2)
        if not self._dot_ok[n]:
            self.fallbacks += 1
            return self.real_cg(a_op, b, x0, rtol=rtol, atol=atol, maxiter=maxiter, M=M, callback=callback)
        kf = a_op.tocsr()
        kf.sort_indices()
        system = _gpucg.GpuSystem(kf.data, kf.indices, kf.indptr, kf.shape)
        dot = _blasdot.BlasDot(n, self.threads)
        apply = system.matvec("ell_nofma")
        del apply
        t1 = time.perf_counter()
        u_gpu, iterations, info = _gpucg.cg_bitexact(system, cp.asarray(b), dot, rtol=rtol,
                                                     matvec_kind="ell_nofma", maxiter=maxiter)
        u = cp.asnumpy(u_gpu)
        t2 = time.perf_counter()
        if callback is not None:
            for _ in range(iterations):
                callback(u)
        self.calls.append({"free_dofs": int(n), "nnz": int(kf.nnz), "iterations": int(iterations),
                           "setup_seconds": t1 - t0, "cg_seconds": t2 - t1})
        del system, u_gpu
        cp.get_default_memory_pool().free_all_blocks()
        return u, info


def tag_of(args) -> str:
    return f"F{int(args.load):04d}_w{args.width:g}_h{args.size:g}_step{args.step:g}"


def run(args) -> None:
    import search_methods as sm
    from duocad.topopt import binary_search, fem

    start = dt.datetime.now().astimezone()
    ctx = sm.build_context(HANGER_MODEL, load_n=args.load, element_size_mm=args.size, min_width_mm=args.width)
    injector = None
    if args.mode == "gpu":
        fem.PARALLEL_THREADS = 1
        injector = GpuCg(fem.spla.cg)
        fem.spla.cg = injector
    else:
        fem.PARALLEL_THREADS = args.threads
    evaluation_walls: list[float] = []
    last = [time.perf_counter()]

    def on_evaluation(*_a, **_k) -> None:
        now = time.perf_counter()
        evaluation_walls.append(now - last[0])
        last[0] = now

    try:
        with fem.collect_solve_stats() as stats:
            t0 = time.perf_counter()
            result = binary_search.find_best_feasible(
                ctx.space.mask, ctx.cond, max_evaluations=args.max_evaluations, max_step_ratio=args.step,
                min_width_cells=ctx.min_width_cells, on_evaluation=on_evaluation)
            wall = time.perf_counter() - t0
    finally:
        if injector is not None:
            fem.spla.cg = injector.real_cg
    best = result.best_feasible
    record = {
        "mode": args.mode, "tag": tag_of(args), "measured_at": start.isoformat(timespec="seconds"),
        "threads": int(fem.PARALLEL_THREADS), "grid_shape": list(ctx.space.mask.shape),
        "spacing_mm": float(ctx.grid.spacing), "design_cells": int(np.asarray(ctx.space.mask).sum()),
        "min_width_cells": int(ctx.min_width_cells),
        "status": result.status, "termination": result.termination, "evaluations": int(result.evaluations),
        "wall_seconds": wall,
        "best_mask_sha": None if best is None else hashlib.sha256(np.packbits(best.mask).tobytes()).hexdigest(),
        "best_cells": None if best is None else int(best.mask.sum()),
        "history": [
            {"index": t.index, "operation": t.operation, "mask_hash": t.mask_hash, "outcome": t.outcome,
             "material_mm3": t.material_mm3,
             "safety_factor_hex": None if t.safety_factor is None else float(t.safety_factor).hex()}
            for t in result.history
        ],
        "solve_stats": {k: stats[k] for k in ("solves", "cg_iterations", "cg_iterations_each", "assemble_seconds",
                                                "slice_seconds", "solve_seconds", "fallbacks", "direct_solves")},
        "evaluation_walls": evaluation_walls,
        "peak_working_set_gb": _winmem.peak_working_set_gb(),
    }
    if injector is not None:
        record["gpu_calls"] = injector.calls
        record["gpu_fallbacks_to_cpu"] = injector.fallbacks
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / f"s6_search_{tag_of(args)}_{args.mode}.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    _winmem.log_load_window(RESULTS, start, f"s6_search_identity {args.mode} {tag_of(args)}",
                            cpu_threads=int(fem.PARALLEL_THREADS))
    print(f"{args.mode}: status={result.status} termination={result.termination} evaluations={result.evaluations} "
          f"wall={wall:.1f}s best_cells={record['best_cells']} solves={stats['solves']} "
          f"direct={stats['direct_solves']} cg_iterations={stats['cg_iterations']} "
          f"solve_seconds={stats['solve_seconds']:.1f}")


def compare(args) -> None:
    cpu = json.loads((RESULTS / f"s6_search_{tag_of(args)}_cpu.json").read_text(encoding="utf-8"))
    gpu = json.loads((RESULTS / f"s6_search_{tag_of(args)}_gpu.json").read_text(encoding="utf-8"))
    same_history = cpu["history"] == gpu["history"]
    out = {
        "tag": tag_of(args),
        "history_identical": same_history,
        "history_length": [len(cpu["history"]), len(gpu["history"])],
        "best_mask_identical": cpu["best_mask_sha"] == gpu["best_mask_sha"],
        "cg_iterations_each_identical": cpu["solve_stats"]["cg_iterations_each"] == gpu["solve_stats"]["cg_iterations_each"],
        "evaluations": [cpu["evaluations"], gpu["evaluations"]],
        "termination": [cpu["termination"], gpu["termination"]],
        "wall_seconds": {"cpu_contended": cpu["wall_seconds"], "cpu_threads": cpu["threads"], "gpu": gpu["wall_seconds"]},
        "solve_seconds": {"cpu_contended": cpu["solve_stats"]["solve_seconds"], "gpu": gpu["solve_stats"]["solve_seconds"]},
        "gpu_fallbacks_to_cpu": gpu.get("gpu_fallbacks_to_cpu"),
    }
    if not same_history:
        for a, b in zip(cpu["history"], gpu["history"]):
            if a != b:
                out["first_difference"] = {"cpu": a, "gpu": b}
                break
    (RESULTS / f"s6_search_{tag_of(args)}_compare.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("cpu", "gpu", "compare"))
    parser.add_argument("--load", type=float, default=1000.0)
    parser.add_argument("--size", type=float, default=4.8)
    parser.add_argument("--width", type=float, default=10.0)
    parser.add_argument("--step", type=float, default=0.2)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--max-evaluations", type=int, default=2000)
    args = parser.parse_args()
    if args.mode == "compare":
        compare(args)
    else:
        run(args)


if __name__ == "__main__":
    main()
