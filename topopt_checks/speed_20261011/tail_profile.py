r"""R1(hanger 180N・2.4mm・最小幅10mm)の終盤を再現して cProfile を取る。
探索の開始マスクを R1 の評価 N の候補(合格形)に差し替え、評価予算を絞って段階4を回し、探索が終わったら止める。
usage: python tail_profile.py --eval 96 --budget 25 --out <dir> [--device cpu|gpu]
"""
from __future__ import annotations

import argparse
import cProfile
import json
import pstats
import sys
import time
from pathlib import Path

REPO = Path(r"C:\Users\tm976\mf\duocad")
RUN = REPO / "output" / "optimization_runs" / "20261011T093810-13bab2bbaf4d"
sys.path.insert(0, str(REPO / "topopt_checks"))
sys.path.insert(0, str(REPO))


class Stop(Exception):
    pass


def load_eval_mask(n: int):
    import numpy as np
    files = sorted(p for p in RUN.iterdir() if p.name.endswith("-stress-input.json"))
    meta = json.loads(files[n - 1].read_text(encoding="utf-8"))
    arr = np.load(RUN / meta["rho"]["$array"])
    rho = arr[arr.files[0]]
    return rho > 0.5


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--eval", type=int, default=96)
    p.add_argument("--budget", type=int, default=25)
    p.add_argument("--device", default="cpu")
    p.add_argument("--load-n", type=float, default=180.0)
    p.add_argument("--out", required=True)
    a = p.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    import _bootstrap  # noqa: F401
    from duocad import optimize_run
    from duocad.topopt import binary_search, fem
    from _documents import open_session
    from sweep_smoothing_corpus import MODEL, _set_load

    fem.FEM_DEVICE = a.device
    seed = load_eval_mask(a.eval)
    real = binary_search.find_best_feasible
    captured = {}

    def patched(design_space_mask, conditions, **kw):
        if design_space_mask.shape != seed.shape:
            raise SystemExit(f"grid mismatch {design_space_mask.shape} vs {seed.shape}")
        kw["max_evaluations"] = a.budget
        prof = cProfile.Profile()
        t0 = time.perf_counter()
        with fem.collect_solve_stats() as stats:
            prof.enable()
            try:
                res = real(seed & design_space_mask, conditions, **kw)
            finally:
                prof.disable()
        captured.update(wall=time.perf_counter() - t0, evaluations=res.evaluations, termination=res.termination,
                        solve=dict((k, v) for k, v in stats.items() if k != "cg_iterations_each"),
                        best_cells=None if res.best_feasible is None else int(res.best_feasible.mask.sum()))
        prof.dump_stats(str(out / "tail.prof"))
        raise Stop()

    binary_search.find_best_feasible = patched
    session = open_session(MODEL)
    doc = session.document
    _set_load(doc, a.load_n)
    options = optimize_run.OptimizeOptions(element_size_mm=2.4, min_width_mm=10.0, target_safety_factor=2.0)
    try:
        optimize_run.run(doc, options)
    except Stop:
        pass
    captured.update(seed_eval=a.eval, seed_cells=int(seed.sum()), device=a.device)
    (out / "summary.json").write_text(json.dumps(captured, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    for key, name in (("cumulative", "cumulative.txt"), ("tottime", "tottime.txt")):
        with (out / name).open("w", encoding="utf-8") as f:
            pstats.Stats(str(out / "tail.prof"), stream=f).sort_stats(key).print_stats(80)
    print(json.dumps(captured, ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
