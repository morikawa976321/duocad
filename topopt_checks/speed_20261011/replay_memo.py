r"""終盤の再現で、段階4の `PeelWalk.take` の呼び出し回数と秒を数え、探索の軌道(各評価の候補セル数・安全率)を JSON に残す。

段階4は `PeelWalk` を直接使い `binary_search.peel()` を呼ばないので、`peel` の差し替えは効かない。PeelMemo(`peel_resume.py`)との
新旧の突き合わせはここではしない(PeelMemo は `scar` を持たず、現行の探索とは突き合わせられない)。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import tail_profile as tp


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--eval", type=int, default=96)
    p.add_argument("--budget", type=int, default=20)
    p.add_argument("--out", required=True)
    a = p.parse_args()
    import _bootstrap  # noqa
    from duocad import optimize_run
    from duocad.topopt import binary_search, fem
    from _documents import open_session
    from sweep_smoothing_corpus import MODEL, _set_load

    fem.FEM_DEVICE = "cpu"
    seed = tp.load_eval_mask(a.eval)
    real_take = binary_search.PeelWalk.take
    state = dict(calls=0, peel_seconds=0.0)

    def timed_take(self, order, count):
        t = time.perf_counter()
        out = real_take(self, order, count)
        state["calls"] += 1
        state["peel_seconds"] += time.perf_counter() - t
        return out

    binary_search.PeelWalk.take = timed_take
    events = []
    real_find = binary_search.find_best_feasible

    def patched(dsm, conditions, **kw):
        kw["max_evaluations"] = a.budget
        user_cb = kw.get("on_evaluation")

        def cb(ev):
            events.append(dict(ev))
            if user_cb:
                user_cb(ev)
        kw["on_evaluation"] = cb
        t = time.perf_counter()
        res = real_find(seed & dsm, conditions, **kw)
        state["search_seconds"] = time.perf_counter() - t
        state["final_hash"] = None if res.best_feasible is None else res.best_feasible.mask_hash
        raise tp.Stop()

    binary_search.find_best_feasible = patched
    session = open_session(MODEL); doc = session.document; _set_load(doc, 180.0)
    opts = optimize_run.OptimizeOptions(element_size_mm=2.4, min_width_mm=10.0, target_safety_factor=2.0)
    try:
        optimize_run.run(doc, opts)
    except tp.Stop:
        pass
    out = dict(**state, events=events)
    Path(a.out).write_text(json.dumps(out, default=float, indent=0), encoding="utf-8")
    print(json.dumps({k: v for k, v in out.items() if k != "events"}, default=float))


if __name__ == "__main__":
    main()
