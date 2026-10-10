r"""表面ピークの却下判定(`SearchConditions.peak_epsilon`)を製品の入口 `optimize_run.run` で確かめる(docs/TOPOPT_PEAK_GATE.md)。

    .venv\Scripts\python.exe topopt_checks\stress_gate\run_peak_gate.py --model dilution                 # Q_V だけ(今の製品)
    .venv\Scripts\python.exe topopt_checks\stress_gate\run_peak_gate.py --model dilution --eps 0.10      # Q_V + ピーク判定
    .venv\Scripts\python.exe topopt_checks\stress_gate\run_peak_gate.py --model path --eps 0.10 --step 0.05
    .venv\Scripts\python.exe topopt_checks\stress_gate\run_peak_gate.py --model hanger --load 1000 --h 2.4 --w 10 --eps 0.10

`--step` は段階4の1回に削る割合の上限(`max_step_ratio`、製品は 0.2)。`--no-final-check` は完成形検査を回さない
(hanger は約 20 GB を使う)。結果は `output/stress_gate_20261008/peak_gate/<tag>.json` と最終マスク `<tag>.npz`。
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
import psutil  # noqa: E402

import refine_shape  # noqa: E402
from duocad import optimize_run  # noqa: E402
from duocad.topopt import binary_search, final_check  # noqa: E402


def _skipped_final_check(final_vertices, final_faces, *, min_width, **_):
    return dict(safety_factor=None, safety=dict(status="unknown", reason="skipped", safety_factor=None),
                conditions=dict(status="unknown", reason="skipped"), material_cells=0,
                neck=dict(events=[], required_mm=float(min_width["requested_mm"])))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", required=True, choices=list(pa.MODELS))
    ap.add_argument("--load", type=float, default=None)
    ap.add_argument("--h", type=float, default=None)
    ap.add_argument("--w", type=float, default=None)
    ap.add_argument("--eps", type=float, default=None, help="ピーク判定の ε。省くと判定しない(今の製品)")
    ap.add_argument("--step", type=float, default=None, help="段階4の max_step_ratio。省くと製品の既定")
    ap.add_argument("--no-final-check", action="store_true")
    ap.add_argument("--gpu", action="store_true", help="FEM の CG を GPU で解く(CPU とビット一致。docs/TOPOPT_GPU.md)")
    ap.add_argument("--tag", default=None)
    a = ap.parse_args()
    spec = dict(pa.MODELS[a.model])
    for key, val in (("load_n", a.load), ("h", a.h), ("w", a.w)):
        if val is not None:
            spec[key] = val
    if a.gpu:
        from duocad.topopt import fem
        fem.FEM_DEVICE = "gpu"
    doc = refine_shape.open_session(spec["model"]).document
    refine_shape._set_load_doc(doc, spec["load_n"])
    options = optimize_run.OptimizeOptions(element_size_mm=spec["h"], min_width_mm=spec["w"])

    real_conditions = optimize_run._build_search_conditions
    real_find = binary_search.find_best_feasible
    captured: dict = {}

    def conditions(*args, **kwargs):
        return dataclasses.replace(real_conditions(*args, **kwargs), peak_epsilon=a.eps)

    def find(*args, **kwargs):
        if a.step is not None:
            kwargs["max_step_ratio"] = a.step
        result = real_find(*args, **kwargs)
        captured["result"] = result
        return result

    started = time.perf_counter()
    with patch.object(optimize_run, "_build_search_conditions", conditions), \
            patch.object(binary_search, "find_best_feasible", find), \
            (patch.object(final_check, "inspect", _skipped_final_check) if a.no_final_check else _null()):
        result = optimize_run.run(doc, options)
    seconds = time.perf_counter() - started

    search = captured.get("result")
    reduction = (search.diagnostics or {}).get("reduction", {}) if search is not None else {}
    gate = dict(reduction.get("peak_gate") or {})
    sites = gate.pop("rejection_sites", [])
    checked = result.get("final_check") or {}
    safety = checked.get("safety") or {}
    row = dict(model=a.model, load_n=spec["load_n"], h=spec["h"], w=spec["w"], eps=a.eps, step=a.step,
               state=result.get("state"), seconds=round(seconds, 1), evaluations=result.get("evaluations"),
               material_mm3=result.get("material_mm3"), search_safety_factor=result.get("safety_factor"),
               final_safety_factor=result.get("final_safety_factor"), peak_mpa=safety.get("peak_mpa"),
               peak_position_mm=safety.get("peak_position_mm"), refine=safety.get("refine"),
               keep_peak_mpa=safety.get("keep_peak_mpa"), boundary_layer_peak_mpa=safety.get("boundary_layer_peak_mpa"),
               stop_reason=reduction.get("stop_reason"), fem_solves=reduction.get("fem_solves"),
               safety_excluded_cells=reduction.get("safety_excluded_cells"), peak_gate=gate,
               peak_rss_gb=round(psutil.Process().memory_info().peak_wset / 1e9, 2),
               warning=result.get("warning"))
    print(json.dumps(row, ensure_ascii=False, default=float), flush=True)
    out = pa.OUT / "peak_gate"
    out.mkdir(parents=True, exist_ok=True)
    tag = a.tag or (f"{a.model}_L{spec['load_n']:g}_h{spec['h']:g}_w{spec['w']:g}"
                    + ("" if a.eps is None else f"_eps{a.eps:g}") + ("" if a.step is None else f"_step{a.step:g}"))
    (out / f"{tag}.json").write_text(json.dumps(dict(row, rejection_sites=sites), ensure_ascii=False, indent=1,
                                                default=float), encoding="utf-8")
    if search is not None and search.best_feasible is not None:
        np.savez_compressed(out / f"{tag}.npz", mask=search.best_feasible.mask)
    return 0


class _null:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


if __name__ == "__main__":
    raise SystemExit(main())
