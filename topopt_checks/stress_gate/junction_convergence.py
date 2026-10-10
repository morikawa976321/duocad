r"""完成形の表面の最大応力が、保持ボディとの接合帯と、それより離れた部材で、細分に対して収束するかを測る(docs/TOPOPT_PEAK_GATE.md)。

製品の入口 `optimize_run.run` で完成メッシュまで作り、完成形検査(`final_check.inspect`)を分割数 `--refine` ごとに解き直す。
部材域を「保持から探索格子の1要素以内(接合帯)」と「それより外」に分け、それぞれの露出面の面中心 von Mises の最大と位置を出す。

    .venv\Scripts\python.exe topopt_checks\stress_gate\junction_convergence.py --model dilution --refine 2 3 4 5
    .venv\Scripts\python.exe topopt_checks\stress_gate\junction_convergence.py --model dilution --eps 0.10
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
from scipy import ndimage  # noqa: E402

import refine_shape  # noqa: E402
from duocad import optimize_run  # noqa: E402
from duocad.topopt import acceptance, binary_search, final_check, stress  # noqa: E402


def banded_strength(material, region, keep, layer, fine, fixed, load, conditions, target, *, factor):
    model, _density, displacement, scale, relative = acceptance.solve_material(
        material, fine, fixed, load, youngs_modulus_mpa=conditions.youngs_modulus_mpa, solver=conditions.solver)
    near_keep = ndimage.binary_dilation(keep, structure=np.ones((3, 3, 3), bool), iterations=factor) if keep.any() \
        else np.zeros_like(keep)

    def peak(cells):
        faces = stress.exposed_face_von_mises(model, displacement, material, scale, cells=cells)
        if not np.isfinite(faces).any():
            return None
        flat = int(np.nanargmax(faces))
        cell = np.unravel_index(flat // 6, material.shape)
        return dict(mpa=float(faces.reshape(-1)[flat]),
                    position_mm=[round(float(fine.centers_1d(a)[cell[a]]), 3) for a in range(3)])

    return dict(status='measured', safety_factor=None, spacing_mm=float(fine.spacing), relative_residual=relative,
                member_cells=int(region.sum()), junction=peak(region & near_keep), away=peak(region & ~near_keep),
                keep=peak(keep & material), boundary_layer=peak(layer & material))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", required=True, choices=list(pa.MODELS))
    ap.add_argument("--load", type=float, default=None)
    ap.add_argument("--h", type=float, default=None)
    ap.add_argument("--w", type=float, default=None)
    ap.add_argument("--eps", type=float, default=None)
    ap.add_argument("--refine", nargs="*", type=int, default=[2, 3, 4, 5])
    ap.add_argument("--step", type=float, default=None, help="段階4の max_step_ratio。省くと製品の既定")
    a = ap.parse_args()
    spec = dict(pa.MODELS[a.model])
    for key, val in (("load_n", a.load), ("h", a.h), ("w", a.w)):
        if val is not None:
            spec[key] = val
    doc = refine_shape.open_session(spec["model"]).document
    refine_shape._set_load_doc(doc, spec["load_n"])
    real_conditions, real_inspect = optimize_run._build_search_conditions, final_check.inspect
    real_find = binary_search.find_best_feasible

    def find(*args, **kwargs):
        if a.step is not None:
            kwargs["max_step_ratio"] = a.step
        return real_find(*args, **kwargs)
    levels = {}

    def conditions(*args, **kwargs):
        return dataclasses.replace(real_conditions(*args, **kwargs), peak_epsilon=a.eps)

    def inspect(*args, **kwargs):
        for k in a.refine:
            started = time.perf_counter()
            strength = lambda *s, _k=k: banded_strength(*s, factor=_k)  # noqa: E731
            with patch.object(final_check, "REFINE", k), patch.object(final_check, "MAX_FINE_ELEMENTS", 10 ** 9), \
                    patch.object(final_check, "_strength", strength):
                checked = real_inspect(*args, **kwargs)
            row = dict(checked["safety"] or {}, seconds=round(time.perf_counter() - started, 1))
            levels[f"k{k}"] = row
            print(f"k{k}", json.dumps(row, ensure_ascii=False), flush=True)
        return real_inspect(*args, **kwargs)

    with patch.object(optimize_run, "_build_search_conditions", conditions), patch.object(final_check, "inspect", inspect), \
            patch.object(binary_search, "find_best_feasible", find):
        result = optimize_run.run(doc, optimize_run.OptimizeOptions(element_size_mm=spec["h"], min_width_mm=spec["w"]))
    out = dict(model=a.model, spec={k: v for k, v in spec.items() if k in ("load_n", "h", "w")}, eps=a.eps,
               state=result.get("state"), material_mm3=result.get("material_mm3"), levels=levels)
    name = (f"junction_{a.model}_L{spec['load_n']:g}" + ("" if a.eps is None else f"_eps{a.eps:g}")
            + ("" if a.step is None else f"_step{a.step:g}") + ".json")
    (pa.OUT / "peak_gate" / name).write_text(json.dumps(out, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
