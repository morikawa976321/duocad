"""段階4の評価ごとに、どこを削ったか(選んだセル・一口の重心)と、候補の安全率を決めた要素の位置を記録する(研究用)。

`run_product.py` と同じく製品の入口 `optimize_run.run` を通す。探索が終わったら仕上げ(完成形検査)を走らせずに止める。
出力は `output/stress_gate_20261008/probe/<tag>.json`。

    .venv\\Scripts\\python.exe topopt_checks\\stress_gate\\search_probe.py --load 150 --h 3.29 --w 9.86 --gpu
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import phase_a_fields as pa  # noqa: E402

import numpy as np  # noqa: E402

import refine_shape  # noqa: E402
from duocad import optimize_run  # noqa: E402
from duocad.topopt import binary_search as bs, stress  # noqa: E402


class _SearchDone(Exception):
    pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--load", type=float, required=True)
    ap.add_argument("--h", type=float, required=True)
    ap.add_argument("--w", type=float, required=True)
    ap.add_argument("--gpu", action="store_true")
    ap.add_argument("--max-evaluations", type=int, default=None)
    a = ap.parse_args()
    tag = f"hanger_L{a.load:g}_h{a.h:g}_w{a.w:g}" + ("" if a.max_evaluations is None else f"_max{a.max_evaluations}")
    doc = refine_shape.open_session(pa.MODELS["hanger"]["model"]).document
    refine_shape._set_load_doc(doc, a.load)
    if a.gpu:
        from duocad.topopt import fem
        fem.FEM_DEVICE = "gpu"

    last = dict(take=None, hot=None)
    real_take = bs.PeelWalk.take

    def take(self, order, count):
        candidate, stats = real_take(self, order, count)
        removed = np.flatnonzero(self.parent.ravel() & ~candidate.ravel())
        last["take"] = dict(picked=[int(c) for c in stats.picked_cells[:64]], removed=removed, shape=self.parent.shape)
        return candidate, stats

    bs.PeelWalk.take = take
    real_q = stress.member_volume_stress

    def member_volume_stress(values, region, *, volume_mm3, cell_mm3):
        out = real_q(values, region, volume_mm3=volume_mm3, cell_mm3=cell_mm3)
        flat = np.flatnonzero(np.asarray(region, bool).ravel())
        picked = np.asarray(values, float).ravel()[flat]
        rank = np.argsort(picked)[::-1]
        allowed = int(np.floor(volume_mm3 / cell_mm3 + 1e-9))
        last["hot"] = [int(flat[rank[min(allowed, len(rank) - 1)]]), int(flat[rank[0]])]
        return out

    stress.member_volume_stress = member_volume_stress
    rows = []
    started = time.perf_counter()
    grid = {}
    real_find = bs.find_best_feasible

    def find(*args, **kwargs):
        outer = kwargs.get("on_evaluation")
        conditions = args[1]
        grid["origin"] = np.asarray(conditions.grid.origin, float)
        grid["spacing"] = float(conditions.grid.spacing)

        def on_evaluation(event):
            row = dict(evaluation=event["evaluation"], outcome=event["outcome"],
                       candidate_sf=event["candidate_safety_factor"], sf=event["safety_factor"], cells=event["cells"],
                       seconds=round(time.perf_counter() - started, 1))
            if event["outcome"] != "initial" and last["take"] is not None:
                t = last["take"]
                row["picked"] = t["picked"]
                row["removed"] = int(len(t["removed"]))
                row["removed_centroid"] = np.array(np.unravel_index(t["removed"], t["shape"])).mean(1).tolist() \
                    if len(t["removed"]) else None
                row["removed_cells"] = t["removed"][:400].tolist()
            row["hot"] = last["hot"]
            rows.append(row)
            if outer is not None:
                outer(event)

        kwargs["on_evaluation"] = on_evaluation
        result = real_find(*args, **kwargs)
        grid["shape"] = list(result.best_feasible.mask.shape)
        grid["stop_reason"] = (result.diagnostics.get("reduction") or {}).get("stop_reason")
        grid["termination"] = result.termination
        raise _SearchDone()

    bs.find_best_feasible = find
    optimize_run.binary_search = bs
    options = optimize_run.OptimizeOptions(element_size_mm=a.h, min_width_mm=a.w, max_evaluations=a.max_evaluations)
    try:
        optimize_run.run(doc, options)
    except _SearchDone:
        pass
    out = dict(tag=tag, origin=grid["origin"].tolist(), spacing=grid["spacing"], shape=grid.get("shape"),
               stop_reason=grid.get("stop_reason"), termination=grid.get("termination"),
               seconds=round(time.perf_counter() - started, 1), rows=rows)
    (pa.OUT / "probe").mkdir(parents=True, exist_ok=True)
    path = pa.OUT / "probe" / f"{tag}.json"
    path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(path, len(rows), out["termination"], out["seconds"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
