r"""hanger 180N(2.4mm・最小幅10mm・目標2.0)の探索を、指定したソースの版で製品の入口から回し、探索が終わったら止めて記録する。
usage: python -I compare_search.py <repo_root> <out.json> [--device gpu] [--budget N] [--seed-eval N]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

MAIN = Path(r"C:\Users\tm976\mf\duocad")
RUN = MAIN / "output" / "optimization_runs" / "20261011T093810-13bab2bbaf4d"


class Stop(Exception):
    pass


def main():
    p = argparse.ArgumentParser()
    p.add_argument("root")
    p.add_argument("out")
    p.add_argument("--device", default="cpu")
    p.add_argument("--budget", type=int, default=None)
    p.add_argument("--seed-eval", type=int, default=None)
    a = p.parse_args()
    root = Path(a.root)
    sys.path.insert(0, str(root / "topopt_checks"))
    sys.path.insert(0, str(root))
    import _bootstrap  # noqa: F401
    import numpy as np
    from duocad import optimize_run
    from duocad.topopt import binary_search, fem
    from _documents import open_session
    from sweep_smoothing_corpus import _set_load

    assert Path(binary_search.__file__).resolve().is_relative_to(root.resolve()), binary_search.__file__
    fem.FEM_DEVICE = a.device
    seed = None
    if a.seed_eval:
        files = sorted(q for q in RUN.iterdir() if q.name.endswith("-stress-input.json"))
        meta = json.loads(files[a.seed_eval - 1].read_text(encoding="utf-8"))
        arr = np.load(RUN / meta["rho"]["$array"])
        seed = arr[arr.files[0]] > 0.5
    real = binary_search.find_best_feasible
    out: dict = {}

    def patched(dsm, conditions, **kw):
        if a.budget is not None:
            kw["max_evaluations"] = a.budget
        events = []
        user = kw.get("on_evaluation")

        def cb(ev):
            events.append(dict(ev))
            if user:
                user(ev)
        kw["on_evaluation"] = cb
        start = dsm if seed is None else seed & dsm
        t = time.perf_counter()
        with fem.collect_solve_stats() as stats:
            res = real(start, conditions, **kw)
        steps = res.diagnostics.get("reduction", {}).get("steps", [])
        out.update(
            search_seconds=time.perf_counter() - t, evaluations=res.evaluations, termination=res.termination,
            final_hash=None if res.best_feasible is None else res.best_feasible.mask_hash,
            peel_seconds=sum(s["peel"]["seconds"] for s in steps),
            peel_calls=res.diagnostics.get("reduction", {}).get("peel_calls"),
            repeated_candidates=res.diagnostics.get("reduction", {}).get("repeated_candidates"),
            solve_seconds=stats["solve_seconds"], gpu_solves=stats["gpu_solves"], gpu_fallbacks=stats["gpu_fallbacks"],
            events=events,
            steps=[{**{k: v for k, v in s.items() if k != "peel"},
                    "peel": {k: v for k, v in s["peel"].items() if k != "seconds"}} for s in steps])
        raise Stop()

    binary_search.find_best_feasible = patched
    session = open_session(MAIN / "models" / "hanger_bar_attachment.json")
    doc = session.document
    _set_load(doc, 180)
    opts = optimize_run.OptimizeOptions(element_size_mm=2.4, min_width_mm=10.0, target_safety_factor=2.0)
    try:
        optimize_run.run(doc, opts)
    except Stop:
        pass
    Path(a.out).write_text(json.dumps(out, default=float, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: v for k, v in out.items() if k not in ("events", "steps")}, default=float))


if __name__ == "__main__":
    main()
