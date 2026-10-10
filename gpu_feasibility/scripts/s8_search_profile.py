"""探索 1 本を cProfile にかけ、CG 以外でどこに時間を使っているかを出す。

実行(開発用の .venv。`uv sync --extra gpu` で CuPy を入れておく。PYTHONPATH にリポジトリ直下を入れる):

    .venv/Scripts/python.exe gpu_feasibility/scripts/s8_search_profile.py --load 200 --size 4.8 --width 10

CG は GPU のビット一致版に差し替えて短くする(探索の軌道は CPU 版と同じ)。FEM ロックは取らない。
プロファイラの分だけ関数呼び出しの多い箇所は実際より重く出るので、秒は割合を見るために使う。
"""
from __future__ import annotations

import argparse
import cProfile
import json
import pstats
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

import s6_search_identity as s6  # noqa: E402

RESULTS = ROOT / "results"


def rows(stats: pstats.Stats, key: int, limit: int) -> list[dict]:
    """key: 2 = その関数自身の秒、3 = 呼び出し先を含む秒。"""
    items = sorted(stats.stats.items(), key=lambda kv: kv[1][key], reverse=True)[:limit]
    return [{"function": f"{Path(k[0]).name}:{k[1]}:{k[2]}", "calls": v[1],
             "own_seconds": round(v[2], 3), "cumulative_seconds": round(v[3], 3)} for k, v in items]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--load", type=float, default=200.0)
    parser.add_argument("--size", type=float, default=4.8)
    parser.add_argument("--width", type=float, default=10.0)
    parser.add_argument("--step", type=float, default=0.2)
    parser.add_argument("--max-evaluations", type=int, default=2000)
    args = parser.parse_args()

    import search_methods as sm
    from duocad.topopt import binary_search, fem

    ctx = sm.build_context(s6.HANGER_MODEL, load_n=args.load, element_size_mm=args.size, min_width_mm=args.width)
    fem.PARALLEL_THREADS = 1
    injector = s6.GpuCg(fem.spla.cg)
    fem.spla.cg = injector
    profiler = cProfile.Profile()
    try:
        with fem.collect_solve_stats() as solve_stats:
            t0 = time.perf_counter()
            result = profiler.runcall(
                binary_search.find_best_feasible, ctx.space.mask, ctx.cond,
                max_evaluations=args.max_evaluations, max_step_ratio=args.step, min_width_cells=ctx.min_width_cells)
            wall = time.perf_counter() - t0
    finally:
        fem.spla.cg = injector.real_cg
    stats = pstats.Stats(profiler)
    tag = s6.tag_of(args)
    out = {
        "tag": tag, "grid_shape": list(ctx.space.mask.shape), "design_cells": int(ctx.space.mask.sum()),
        "status": result.status, "termination": result.termination, "evaluations": int(result.evaluations),
        "wall_seconds_profiled": wall,
        "solves": solve_stats["solves"], "direct_solves": solve_stats["direct_solves"],
        "assemble_seconds": solve_stats["assemble_seconds"], "slice_seconds": solve_stats["slice_seconds"],
        "solve_seconds": solve_stats["solve_seconds"],
        "gpu_cg_seconds": sum(c["setup_seconds"] + c["cg_seconds"] for c in injector.calls),
        "top_cumulative": rows(stats, 3, 60),
        "top_own": rows(stats, 2, 40),
    }
    (RESULTS / f"s8_search_profile_{tag}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{tag}: evaluations={result.evaluations} wall={wall:.1f}s solve={solve_stats['solve_seconds']:.1f}s "
          f"(direct {solve_stats['direct_solves']}/{solve_stats['solves']})")
    print("--- 呼び出し先を含む秒 ---")
    for r in out["top_cumulative"][:45]:
        print(f"  {r['cumulative_seconds']:8.2f}  {r['calls']:>9}  {r['function']}")
    print("--- 関数自身の秒 ---")
    for r in out["top_own"][:25]:
        print(f"  {r['own_seconds']:8.2f}  {r['calls']:>9}  {r['function']}")


if __name__ == "__main__":
    main()
