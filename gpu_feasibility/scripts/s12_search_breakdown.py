"""段階12(#104 の実測1): 実寸の探索 1 本を、プロファイラなしで「線形求解 / 候補生成 / それ以外」に分けて秒を取る。

実行(製品の .venv に CuPy は無いので検証用 venv。PYTHONPATH にリポジトリ直下を入れる):

    gpu_feasibility/.venv/Scripts/python.exe gpu_feasibility/scripts/s12_search_breakdown.py cpu --load 200 --width 5 --size 2.0
    gpu_feasibility/.venv/Scripts/python.exe gpu_feasibility/scripts/s12_search_breakdown.py gpu --load 200 --width 5 --size 2.0

出力: gpu_feasibility/results/s12_search_<tag>_<mode>.json(探索の履歴は s6 と同じ形で、cpu と gpu の一致を比べられる)

s6 と違い、gpu でも `fem.PARALLEL_THREADS` を製品の既定(論理コアの半分)のままにする。製品は threads>1 のとき
K_ff を `_BlockedMatvec` の LinearOperator に包んで `spla.cg` へ渡すので、包みの中の CSR を取り出して GPU へ送る
(組立など CG 以外の部分は cpu と同じスレッド数で動く)。
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
REPO = ROOT.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "topopt_checks"))
sys.path.insert(0, str(REPO / "topopt_checks" / "idle_validation"))

import _winmem  # noqa: E402
import s6_search_identity as s6  # noqa: E402

RESULTS = ROOT / "results"


class GpuCgUnwrap(s6.GpuCg):
    """`_BlockedMatvec` の LinearOperator を受けたら、中の CSR を取り出して s6 の GPU 版へ渡す。"""

    def __call__(self, a_op, b, x0=None, **kw):
        import scipy.sparse as sp

        if not sp.issparse(a_op):
            impl = getattr(a_op, "_CustomLinearOperator__matvec_impl", None)
            if getattr(impl, "mat", None) is not None:
                a_op = impl.mat
        return super().__call__(a_op, b, x0, **kw)


class Timed:
    """モジュール属性の関数を包み、呼び出しの秒の合計と回数を数える。"""

    def __init__(self, module, name: str) -> None:
        self.module, self.name, self.real = module, name, getattr(module, name)
        self.seconds, self.calls = 0.0, 0
        setattr(module, name, self)

    def __call__(self, *a, **k):
        t0 = time.perf_counter()
        try:
            return self.real(*a, **k)
        finally:
            self.seconds += time.perf_counter() - t0
            self.calls += 1

    def restore(self) -> None:
        setattr(self.module, self.name, self.real)


def tag_of(args) -> str:
    return f"F{int(args.load):04d}_w{args.width:g}_h{args.size:g}_step{args.step:g}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("cpu", "gpu"))
    parser.add_argument("--load", type=float, required=True)
    parser.add_argument("--size", type=float, default=2.0)
    parser.add_argument("--width", type=float, required=True)
    parser.add_argument("--step", type=float, default=0.2)
    parser.add_argument("--max-evaluations", type=int, default=2000)
    args = parser.parse_args()

    import search_methods as sm
    from duocad.topopt import acceptance, binary_search, fem

    start = dt.datetime.now().astimezone()
    ctx = sm.build_context(s6.HANGER_MODEL, load_n=args.load, element_size_mm=args.size, min_width_mm=args.width)
    injector = None
    if args.mode == "gpu":
        injector = GpuCgUnwrap(fem.spla.cg)
        fem.spla.cg = injector
    peel = Timed(binary_search, "peel")
    inspect = Timed(acceptance, "inspect")
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
        peel.restore()
        inspect.restore()
        if injector is not None:
            fem.spla.cg = injector.real_cg
    best = result.best_feasible
    solve = stats["solve_seconds"]
    record = {
        "mode": args.mode, "tag": tag_of(args), "measured_at": start.isoformat(timespec="seconds"),
        "threads": int(fem.PARALLEL_THREADS), "grid_shape": list(ctx.space.mask.shape),
        "spacing_mm": float(ctx.grid.spacing), "design_cells": int(np.asarray(ctx.space.mask).sum()),
        "min_width_cells": int(ctx.min_width_cells),
        "status": result.status, "termination": result.termination, "evaluations": int(result.evaluations),
        "wall_seconds": wall,
        "breakdown_seconds": {
            "linear_solve": solve,
            "assemble_and_slice": stats["assemble_seconds"] + stats["slice_seconds"],
            "inspect_other": inspect.seconds - solve - stats["assemble_seconds"] - stats["slice_seconds"],
            "peel": peel.seconds,
            "rest": wall - inspect.seconds - peel.seconds,
        },
        "inspect_calls": inspect.calls, "peel_calls": peel.calls,
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
    (RESULTS / f"s12_search_{tag_of(args)}_{args.mode}.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    b = record["breakdown_seconds"]
    print(f"{args.mode} {tag_of(args)}: status={result.status} evaluations={result.evaluations} wall={wall:.1f}s "
          + " ".join(f"{k}={v:.1f}" for k, v in b.items())
          + f" solves={stats['solves']} direct={stats['direct_solves']} gpu_fallbacks={record.get('gpu_fallbacks_to_cpu')}",
          flush=True)


if __name__ == "__main__":
    main()
