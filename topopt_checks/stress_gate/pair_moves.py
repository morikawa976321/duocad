r"""ピーク判定つき探索が止まった形で、「1セルではピークで落ちる手が、別のセルと組めば通るか」を数える(docs/TOPOPT_PEAK_GATE.md)。

製品の入口で探索を回し(`--eps` でピーク判定)、止まった形 M でエネルギー順位の上位 `--top` セルを1つずつ削った候補と、
ピークだけで落ちたセル A に上位の別セル B を足した2セルの候補を、同じ6条件(+ピーク)で検査する。
2セルが通る A があれば、1セルずつ削る探索がその手を逃している(経路を閉じている)証拠になる。

    .venv\Scripts\python.exe topopt_checks\stress_gate\pair_moves.py --model dilution --eps 0.10 --top 30
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import phase_a_fields as pa  # noqa: E402

import numpy as np  # noqa: E402

import refine_shape  # noqa: E402
from duocad import optimize_run  # noqa: E402
from duocad.topopt import acceptance, binary_search  # noqa: E402


class Done(Exception):
    pass


def verdict(report) -> str:
    checks = report["checks"]
    failed = sorted(name for name, check in checks.items() if check.get("status") != "pass")
    return "pass" if not failed else "+".join(failed)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", required=True, choices=list(pa.MODELS))
    ap.add_argument("--load", type=float, default=None)
    ap.add_argument("--h", type=float, default=None)
    ap.add_argument("--w", type=float, default=None)
    ap.add_argument("--eps", type=float, default=0.10)
    ap.add_argument("--top", type=int, default=30)
    a = ap.parse_args()
    spec = dict(pa.MODELS[a.model])
    for key, val in (("load_n", a.load), ("h", a.h), ("w", a.w)):
        if val is not None:
            spec[key] = val
    doc = refine_shape.open_session(spec["model"]).document
    refine_shape._set_load_doc(doc, spec["load_n"])
    real_conditions, real_find = optimize_run._build_search_conditions, binary_search.find_best_feasible
    out: dict = dict(model=a.model, eps=a.eps, top=a.top)

    def conditions(*args, **kwargs):
        return dataclasses.replace(real_conditions(*args, **kwargs), peak_epsilon=a.eps)

    def find(design_space_mask, cond, **kwargs):
        result = real_find(design_space_mask, cond, **kwargs)
        initial = binary_search.find_initial_feasible(design_space_mask, cond)
        gated, _ = binary_search._attach_peak_limit(initial.best_feasible, cond, dict(fem_solves=0))
        m = int(kwargs.get("min_width_cells", 1))
        mask = result.best_feasible.mask
        anchor_ids = binary_search._anchor_node_ids(gated)
        exempt = binary_search._min_width_exempt(gated, mask.shape, m)
        protected = binary_search._keep_cells(gated, mask.shape)
        fields: dict = {}
        acceptance.inspect(mask, mask, gated.grid, fields=fields, **gated.inspect_kwargs(bbox_cache=None))
        order = binary_search.ranked_cells(mask, fields["element_energy"])
        out.update(stop_reason=result.diagnostics["reduction"]["stop_reason"], material_cells=int(mask.sum()),
                   min_width_cells=m)

        def evaluate(cells):
            candidate, peeled = binary_search.peel(mask, np.asarray(cells), len(cells), anchor_ids, min_width_cells=m,
                                                   exempt=exempt, protected=protected)
            if peeled.picked < len(cells):
                return None, int(peeled.picked)
            survivors, _ = binary_search.resolve_islands(candidate, gated)
            if len(survivors) != 1:
                return "islands", int(peeled.picked)
            report = acceptance.inspect(survivors[0], candidate, gated.grid, **gated.inspect_kwargs(bbox_cache=None))
            return verdict(report), int(peeled.picked)

        started = time.perf_counter()
        singles, legal = {}, []
        for cell in order:
            if len(legal) >= a.top:
                break
            v, _ = evaluate([cell])
            if v is None:
                continue
            legal.append(int(cell))
            singles[int(cell)] = v
        peak_only = [c for c, v in singles.items() if v == "peak"]
        pairs = []
        for first in peak_only:
            for second in legal:
                if second == first:
                    continue
                v, picked = evaluate([first, second])
                if v is not None:
                    pairs.append(dict(first=first, second=second, second_alone=singles[second], verdict=v))
        out.update(singles=dict(sorted({v: sum(1 for x in singles.values() if x == v) for v in set(singles.values())}.items())),
                   peak_only_cells=len(peak_only), pairs_tried=len(pairs),
                   pairs_pass=[p for p in pairs if p["verdict"] == "pass"],
                   pairs_summary={v: sum(1 for p in pairs if p["verdict"] == v) for v in set(p["verdict"] for p in pairs)},
                   seconds=round(time.perf_counter() - started, 1))
        raise Done

    with patch.object(optimize_run, "_build_search_conditions", conditions), patch.object(binary_search, "find_best_feasible", find):
        try:
            optimize_run.run(doc, optimize_run.OptimizeOptions(element_size_mm=spec["h"], min_width_mm=spec["w"]))
        except Done:
            pass
    print(json.dumps({k: v for k, v in out.items() if k != "pairs_pass"} | dict(pairs_pass=len(out.get("pairs_pass", []))),
                     ensure_ascii=False), flush=True)
    dest = pa.OUT / "peak_gate" / f"pairs_{a.model}_eps{a.eps:g}.json"
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
