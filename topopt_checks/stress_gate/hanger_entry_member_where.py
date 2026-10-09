"""hanger 全材料の部材域(保持外・拘束/荷重節点から d 超)の目標超過が、どこにあるか(保持ボディ際・荷重面際・それ以外)。

FEMなし。段階Aの 200N の場を荷重倍率で拡大する。
    .venv\Scripts\python.exe topopt_checks\stress_gate\hanger_entry_member_where.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import phase_a_fields as pa  # noqa: E402

import numpy as np  # noqa: E402
from scipy import ndimage  # noqa: E402

import search_methods as smeth  # noqa: E402

import gate_metrics as gm  # noqa: E402

spec = pa.MODELS["hanger"]
with np.load(pa.OUT / "fields" / "hanger_full_k1.npz") as z:
    vm200, mask, keep = z["vm"], z["mask"].astype(bool), z["keep"].astype(bool)
    h = float(z["spacing"])
ctx = smeth.build_context(spec["model"], load_n=spec["load_n"], element_size_mm=h, min_width_mm=10.0)
target = ctx.cond.allowable_mpa / ctx.cond.target_safety_factor
dfix = gm.distance_to_points(ctx.grid, ctx.table.positions(list(ctx.cond.fixed_faces)))
dload = gm.distance_to_points(ctx.grid, ctx.table.positions(list(ctx.cond.load_faces)))
dkeep = ndimage.distance_transform_edt(~keep) * h
cell = h ** 3
d = h
mem = mask & ~keep & (dfix > d) & (dload > d)
out = {}
for load in (1000, 2000):
    hot = mem & (vm200 * load / 200 > target)
    out[load] = dict(member_hot_mm3=float(hot.sum() * cell),
                     within_2h_of_keep=float((hot & (dkeep <= 2 * h)).sum() * cell),
                     within_2h_of_load=float((hot & (dkeep > 2 * h) & (dload <= 2 * h)).sum() * cell),
                     elsewhere=float((hot & (dkeep > 2 * h) & (dload > 2 * h)).sum() * cell),
                     keep_QV_w3_out_d=gm.top_volume_value(vm200 * load / 200, mask & keep & (dfix > d), cell, 1000.0),
                     keep_hot_mm3=float((mask & keep & (vm200 * load / 200 > target)).sum() * cell))
    print(load, {k: round(v, 1) for k, v in out[load].items()})
(pa.OUT / "hanger_entry_member_where.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
