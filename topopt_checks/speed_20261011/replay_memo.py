r"""終盤の再現で peel を PeelMemo に替える。--verify なら毎回本物の peel も呼んで候補・手を突き合わせる。
探索の軌道(各評価の候補セル数・安全率)を JSON に残し、memo なしの軌道と比べられるようにする。
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
    p.add_argument("--mode", choices=("real", "memo", "verify"), default="memo")
    p.add_argument("--out", required=True)
    a = p.parse_args()
    import _bootstrap  # noqa
    import numpy as np
    from duocad import optimize_run
    from duocad.topopt import binary_search, fem
    from _documents import open_session
    from sweep_smoothing_corpus import MODEL, _set_load
    import peel_resume

    fem.FEM_DEVICE = "cpu"
    seed = tp.load_eval_mask(a.eval)
    real_peel = binary_search.peel
    state = dict(memo=None, calls=0, mismatches=0, peel_seconds=0.0)

    def memo_peel(mask, order, count, anchor_ids, *, min_width_cells=1, exempt=None, protected=None):
        t = time.perf_counter()
        memo = state["memo"]
        if memo is None or memo.parent is not mask:
            memo = peel_resume.PeelMemo(mask, anchor_ids, min_width_cells=min_width_cells, exempt=exempt,
                                        protected=protected)
            memo.parent = mask  # 同一性で親を見分ける
            state["memo"] = memo
        cand, picked, cells, total = memo.peel(order, count)
        stats = binary_search.PeelStats(picked=picked, casualties=int(mask.sum() - cand.sum()) - picked)
        stats.picked_cells = cells
        stats.seconds = time.perf_counter() - t
        state["calls"] += 1
        state["peel_seconds"] += stats.seconds
        if a.mode == "verify":
            ref, rs = real_peel(mask, order, count, anchor_ids, min_width_cells=min_width_cells, exempt=exempt,
                                protected=protected)
            if not (np.array_equal(ref, cand) and np.array_equal(rs.picked_cells, cells)):
                state["mismatches"] += 1
                print("MISMATCH", state["calls"], count, rs.picked, picked, flush=True)
        return np.ascontiguousarray(cand), stats

    def timed_real(*args, **kw):
        t = time.perf_counter()
        out = real_peel(*args, **kw)
        state["calls"] += 1
        state["peel_seconds"] += time.perf_counter() - t
        return out

    binary_search.peel = timed_real if a.mode == "real" else memo_peel
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
    state.pop("memo")
    out = dict(mode=a.mode, **state, events=events)
    Path(a.out).write_text(json.dumps(out, default=float, indent=0), encoding="utf-8")
    print(json.dumps({k: v for k, v in out.items() if k != "events"}, default=float))


if __name__ == "__main__":
    main()
