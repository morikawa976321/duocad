"""hanger の全材料の応力場(段階Aで保存、200N・h=2.4)を荷重倍率で拡大し、領域ごとの目標超過を出す(FEMなし)。

統合案のゲート(部材域の超過体積 ≤ w³、境界域の球平均(半径 w)≤ 目標応力)で、全材料から探索を始められる
荷重の範囲を見る。線形弾性なので応力は荷重に比例する。

    .venv\\Scripts\\python.exe topopt_checks\\stress_gate\\hanger_entry_split.py [--w 4.8 10] [--d 2.4 5]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import phase_a_fields as pa  # noqa: E402

import numpy as np  # noqa: E402

import search_methods as smeth  # noqa: E402

import gate_metrics as gm  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--field", default=str(pa.OUT / "fields" / "hanger_full_k1.npz"))
    ap.add_argument("--w", nargs="*", type=float, default=[4.8, 10.0])
    ap.add_argument("--d", nargs="*", type=float, default=[2.4, 5.0])
    ap.add_argument("--loads", nargs="*", type=float, default=[200, 500, 1000, 2000])
    a = ap.parse_args()
    spec = pa.MODELS["hanger"]
    with np.load(a.field) as z:
        vm200, mask, keep = z["vm"], z["mask"].astype(bool), z["keep"].astype(bool)
        h = float(z["spacing"])
    ctx = smeth.build_context(spec["model"], load_n=spec["load_n"], element_size_mm=h, min_width_mm=10.0)
    assert tuple(ctx.grid.shape) == mask.shape
    target = ctx.cond.allowable_mpa / ctx.cond.target_safety_factor
    dfix = gm.distance_to_points(ctx.grid, ctx.table.positions(list(ctx.cond.fixed_faces)))
    dload = gm.distance_to_points(ctx.grid, ctx.table.positions(list(ctx.cond.load_faces)))
    cell = h ** 3
    out = dict(h=h, target_mpa=target, rows=[])
    for w in a.w:
        ballf = gm.ball_mean_field(vm200, mask, w, h)
        for d in a.d:
            mem = mask & ~keep & (dfix > d) & (dload > d)
            bnd = mask & (keep | (dfix <= d) | (dload <= d))
            for load in a.loads:
                f = load / spec["load_n"]
                vm = vm200 * f
                hot = mask & (vm > target)
                row = dict(w=w, d=d, load_n=load,
                           hot_member_mm3=float((hot & mem).sum() * cell),
                           hot_keep_near_mm3=float((hot & keep & (dfix <= d)).sum() * cell),
                           hot_keep_far_mm3=float((hot & keep & (dfix > d)).sum() * cell),
                           hot_design_near_fixed_mm3=float((hot & ~keep & (dfix <= d)).sum() * cell),
                           hot_near_load_mm3=float((hot & ~keep & (dfix > d) & (dload <= d)).sum() * cell),
                           member_QV_w3=gm.top_volume_value(vm, mem, cell, w ** 3),
                           boundary_ball_w_max=float(ballf[bnd].max() * f),
                           keep_ball_w_max=float(ballf[mask & keep].max() * f),
                           p995_all=float(np.percentile(vm[mask], 99.5)))
                row["member_gate_ok"] = row["member_QV_w3"] <= target
                row["boundary_gate_ok"] = row["boundary_ball_w_max"] <= target
                out["rows"].append(row)
                print(json.dumps({k: (round(v, 2) if isinstance(v, float) else v) for k, v in row.items()}), flush=True)
    (pa.OUT / "hanger_entry_split.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
