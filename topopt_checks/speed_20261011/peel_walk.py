r"""tail_profile と同じ再現で、peel の呼び出しごとに「順位の何番目まで歩いたか・何手抜いたか・秒」を記録する(cProfile なし)。"""
from __future__ import annotations
import argparse, json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import tail_profile as tp

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--eval", type=int, default=96)
    p.add_argument("--budget", type=int, default=30)
    p.add_argument("--out", required=True)
    a = p.parse_args()
    import _bootstrap  # noqa
    import numpy as np
    from duocad import optimize_run
    from duocad.topopt import binary_search, fem
    from _documents import open_session
    from sweep_smoothing_corpus import MODEL, _set_load
    fem.FEM_DEVICE = "cpu"
    seed = tp.load_eval_mask(a.eval)
    calls = []
    real_peel = binary_search.peel
    def peel(mask, order, count, anchor_ids, **kw):
        t = time.perf_counter()
        cand, st = real_peel(mask, order, count, anchor_ids, **kw)
        dt = time.perf_counter() - t
        pos = {int(c): i for i, c in enumerate(order[: min(len(order), 2_000_000)].tolist())}
        last = max((pos.get(int(c), -1) for c in st.picked_cells), default=-1)
        examined = st.picked + st.already_removed + st.skipped_support + st.skipped_surface + st.skipped_manifold \
            + st.skipped_connectivity + st.skipped_neck + st.skipped_core
        calls.append(dict(order_len=int(len(order)), count=int(count), picked=st.picked, casualties=st.casualties,
                          islands=st.islands, last_pick_pos=last, examined=examined, seconds=dt,
                          mask_hash=hash(mask.tobytes()) , stats=st.as_dict()))
        return cand, st
    binary_search.peel = peel
    real = binary_search.find_best_feasible
    def patched(dsm, conditions, **kw):
        kw["max_evaluations"] = a.budget
        res = real(seed & dsm, conditions, **kw)
        raise tp.Stop()
    binary_search.find_best_feasible = patched
    session = open_session(MODEL); doc = session.document; _set_load(doc, 180.0)
    opts = optimize_run.OptimizeOptions(element_size_mm=2.4, min_width_mm=10.0, target_safety_factor=2.0)
    try:
        optimize_run.run(doc, opts)
    except tp.Stop:
        pass
    Path(a.out).write_text(json.dumps(calls, default=float, indent=0), encoding="utf-8")
    tot = sum(c["seconds"] for c in calls)
    print("calls", len(calls), "peel seconds", round(tot, 1))
    for c in calls:
        print(c["count"], c["picked"], c["casualties"], c["examined"], c["last_pick_pos"], round(c["seconds"], 2))

if __name__ == "__main__":
    main()
