"""製品の入口 `optimize_run.run` を通して、探索(部材域の上位値)と完成形検査(1/3 格子の表面の最大応力)を確かめる。

    .venv\\Scripts\\python.exe topopt_checks\\stress_gate\\run_product.py --model dilution
    .venv\\Scripts\\python.exe topopt_checks\\stress_gate\\run_product.py --model hanger --load 1000 --h 2.4 --w 10
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import phase_a_fields as pa  # noqa: E402

import psutil  # noqa: E402

import refine_shape  # noqa: E402
from _percentile_rule import percentile_rule  # noqa: E402
from duocad import optimize_run  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, choices=list(pa.MODELS))
    ap.add_argument("--load", type=float, default=None)
    ap.add_argument("--h", type=float, default=None)
    ap.add_argument("--w", type=float, default=None)
    ap.add_argument("--legacy", action="store_true", help="探索の代表応力を 2026-10-09 より前の 99.5%% 点にする(完成形検査は今のまま)")
    ap.add_argument("--v-cells", type=float, default=None,
                    help="探索の部材域の上位値で許す超過体積 V を探索格子のセル数で与える(省くと製品の 最小幅³)")
    ap.add_argument("--gpu", action="store_true", help="FEM を GPU で解く(docs/TOPOPT_GPU.md)")
    ap.add_argument("--target-tolerance", default=None,
                    help="探索の target_tolerance(数か none = 許容の止め方を使わない)。省くと製品の既定")
    a = ap.parse_args()
    spec = dict(pa.MODELS[a.model])
    for key, val in (("load_n", a.load), ("h", a.h), ("w", a.w)):
        if val is not None:
            spec[key] = val
    doc = refine_shape.open_session(spec["model"]).document
    refine_shape._set_load_doc(doc, spec["load_n"])
    if a.gpu:
        from duocad.topopt import fem
        fem.FEM_DEVICE = "gpu"
    real_conditions = optimize_run._build_search_conditions
    used = {}

    def conditions(*args, **kwargs):
        built = real_conditions(*args, **kwargs)
        if a.v_cells is not None:
            built = dataclasses.replace(built, member_volume_mm3=a.v_cells * float(built.grid.element_volume))
        used["member_volume_mm3"] = built.member_volume_mm3
        used["cell_mm3"] = float(built.grid.element_volume)
        return built

    optimize_run._build_search_conditions = conditions
    if a.target_tolerance is not None:
        from duocad.topopt import binary_search
        real_find = binary_search.find_best_feasible
        tolerance = None if a.target_tolerance == "none" else float(a.target_tolerance)

        def find(*args, **kwargs):
            kwargs["target_tolerance"] = tolerance
            return real_find(*args, **kwargs)

        binary_search.find_best_feasible = find
    started = time.perf_counter()
    options = optimize_run.OptimizeOptions(element_size_mm=spec["h"], min_width_mm=spec["w"])
    if a.legacy:
        with percentile_rule():
            result = optimize_run.run(doc, options)
    else:
        result = optimize_run.run(doc, options)
    seconds = time.perf_counter() - started
    checked = result.get("final_check") or {}
    safety = checked.get("safety") or {}
    row = dict(model=a.model, load_n=spec["load_n"], h=spec["h"], w=spec["w"], state=result.get("state"),
               seconds=round(seconds, 1), evaluations=result.get("evaluations"), material_mm3=result.get("material_mm3"),
               termination=result.get("termination"), design_elements=result.get("design_elements"),
               target_safety_factor=result.get("target_safety_factor"),
               search_target_safety_factor=result.get("search_target_safety_factor"),
               full_material_limit_load=result.get("full_material_limit_load"),
               search_safety_factor=result.get("safety_factor"), final_safety_factor=result.get("final_safety_factor"),
               top_area_mpa=safety.get("top_area_mpa"), coarse_penalty=safety.get("coarse_penalty"),
               peak_mpa=safety.get("peak_mpa"), peak_position_mm=safety.get("peak_position_mm"),
               refine=safety.get("refine"), fine_spacing_mm=safety.get("spacing_mm"),
               fine_material_cells=safety.get("material_cells"), keep_peak_mpa=safety.get("keep_peak_mpa"),
               boundary_layer_peak_mpa=safety.get("boundary_layer_peak_mpa"), solver=safety.get("solver"),
               member_volume_mm3=used.get("member_volume_mm3"), cell_mm3=used.get("cell_mm3"),
               backtrack=result.get("backtrack"),
               peak_rss_gb=round(psutil.Process().memory_info().peak_wset / 1e9, 2) if hasattr(psutil.Process().memory_info(), "peak_wset") else None,
               warning=result.get("warning"))
    print(json.dumps(row, ensure_ascii=False, default=float), flush=True)
    (pa.OUT / "product").mkdir(parents=True, exist_ok=True)
    tag = (f"{a.model}_L{spec['load_n']:g}_h{spec['h']:g}_w{spec['w']:g}" + ("_legacy" if a.legacy else "")
           + ("" if a.v_cells is None else f"_V{a.v_cells:g}cells")
           + ("" if a.target_tolerance is None else f"_tol{a.target_tolerance}"))
    (pa.OUT / "product" / f"{tag}.json").write_text(json.dumps(row, ensure_ascii=False, indent=1, default=float),
                                                    encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
