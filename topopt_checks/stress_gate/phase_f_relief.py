"""段階F: 探索後の局所の肉盛りで最高応力を下げられるか(研究用)。

採用マスクを解き、判定域(保持外・拘束/荷重節点から h 超)の高応力セル(要素中心 σ ≥ β·その域の最大)の
半径 r セル以内で、材料に面で接する設計空間の空セルを1層足す。これを n 回繰り返し、毎回 FEM で
最大(要素中心)・Q_{8mm³}・表面の面中心の最大と、足した体積を記録する。各回のマスクを保存する。

    .venv\\Scripts\\python.exe topopt_checks\\stress_gate\\phase_f_relief.py --model hanger --mask <npz> --load 1000 --h 2.4 --w 10
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
from scipy import ndimage  # noqa: E402

import _fem_probe  # noqa: E402
import search_methods as smeth  # noqa: E402
from duocad.topopt import stress  # noqa: E402

import gate_metrics as gm  # noqa: E402


def solve_probe(m, c):
    cnd = c.cond
    probe = _fem_probe.evaluate_full(m, c.grid, c.table, cnd.fixed_faces, cnd.load_faces, cnd.forces,
                                     allowable_mpa=cnd.allowable_mpa, youngs_modulus_mpa=cnd.youngs_modulus_mpa,
                                     percentile=stress.DEFAULT_PERCENTILE, solver=cnd.solver)
    if probe["status"] != "ok":
        raise RuntimeError(probe.get("error"))
    return probe


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, choices=list(pa.MODELS))
    ap.add_argument("--mask", required=True)
    ap.add_argument("--load", type=float, default=None)
    ap.add_argument("--h", type=float, default=None)
    ap.add_argument("--w", type=float, default=None)
    ap.add_argument("--rounds", type=int, default=4)
    ap.add_argument("--beta", type=float, default=0.8)
    ap.add_argument("--r", type=int, default=2)
    a = ap.parse_args()
    spec = dict(pa.MODELS[a.model])
    for key, val in (("load_n", a.load), ("h", a.h), ("w", a.w)):
        if val is not None:
            spec[key] = val
    h, w = spec["h"], spec["w"]
    with np.load(a.mask) as z:
        mask = np.asarray(z["mask"], bool)
    c = smeth.build_context(spec["model"], load_n=spec["load_n"], element_size_mm=h, min_width_mm=w)
    keep = np.asarray(c.cond.keep_cells if c.cond.keep_cells is not None else np.zeros(c.grid.shape, bool))
    space = np.asarray(c.space.mask, bool)
    dfix = gm.distance_to_points(c.grid, c.table.positions(list(c.cond.fixed_faces)))
    dload = gm.distance_to_points(c.grid, c.table.positions(list(c.cond.load_faces)))
    judge = ~keep & (dfix > h) & (dload > h)
    cell = h ** 3
    base_volume = float(mask.sum() * cell)
    rows = []
    stem = Path(a.mask).stem
    (pa.OUT / "relief").mkdir(parents=True, exist_ok=True)
    for rnd in range(a.rounds + 1):
        t0 = time.perf_counter()
        probe = solve_probe(mask, c)
        vm = probe["von_mises_mpa"]
        region = mask & judge
        factor = float(np.nanmax(vm[mask]) / np.nanmax(stress.element_stress(probe["_model"], probe["_displacement"]).von_mises[mask]))
        face = gm.exposed_face_vm(probe["_model"], probe["_displacement"], mask, factor)
        fv = face[region]
        peak = float(vm[region].max())
        row = dict(round=rnd, material_mm3=float(mask.sum() * cell), added_mm3=float(mask.sum() * cell - base_volume),
                   center_max=peak, Q_8mm3=gm.top_volume_value(vm, region, cell, 8.0),
                   Q_w3=gm.top_volume_value(vm, region, cell, w ** 3), surf_max=float(np.nanmax(fv)),
                   p995_sf=float(probe["safety_factor"]), fem_s=time.perf_counter() - t0)
        rows.append(row)
        print(json.dumps({k: (round(v, 2) if isinstance(v, float) else v) for k, v in row.items()}), flush=True)
        np.savez_compressed(pa.OUT / "relief" / f"{stem}_r{rnd}.npz", mask=mask, origin=np.asarray(c.grid.origin), spacing=h)
        if rnd == a.rounds:
            break
        hot = region & (vm >= a.beta * peak)
        near = ndimage.binary_dilation(hot, structure=ndimage.generate_binary_structure(3, 3), iterations=a.r)
        add = ndimage.binary_dilation(mask) & ~mask & space & near
        if not add.any():
            break
        mask = mask | add
    out = dict(mask=a.mask, model=a.model, load_n=spec["load_n"], h=h, w=w, beta=a.beta, r=a.r, rows=rows)
    (pa.OUT / "relief" / f"{stem}_b{a.beta:g}_r{a.r}.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
