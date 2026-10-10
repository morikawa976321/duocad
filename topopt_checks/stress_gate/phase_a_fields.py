"""段階A: 候補式の性質を、保存形の応力場で調べる(研究用。製品コードは変えない)。

1. 小型2題材の全材料・製品step20保存形を現行のFEMで解き、場を保存する(output/stress_gate_20261008/fields)。
2. 候補式の値・目標超過の体積の内訳(保持セル/固定面からの距離)を出す。
3. 場だけの操作(FEMなし): 低応力の母数の増減、高応力の小領域への集中、高応力の位置の移動。
4. 同じ形を k 分割して解き直す(実FEM): 物理体積・物理半径の候補が格子で動くか。

    .venv\\Scripts\\python.exe topopt_checks\\stress_gate\\phase_a_fields.py [--refine 2 3] [--models dilution path]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "topopt_checks"))
sys.path.insert(0, str(REPO / "topopt_checks" / "idle_validation"))
import _bootstrap  # noqa: E402,F401

import numpy as np  # noqa: E402

import _fem_probe  # noqa: E402
import refine_shape  # noqa: E402
import search_methods as smeth  # noqa: E402
from duocad.topopt import stress  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gate_metrics as gm  # noqa: E402

OUT = REPO / "output" / "stress_gate_20261008"
IV = REPO / "output" / "idle_validation_20260928"

MODELS = {
    "dilution": dict(model=REPO / "models" / "topopt_idle_dilution_small.json", load_n=300.0, h=2.0, w=4.0,
                     step20=IV / "04_masks" / "dilution_2.0mm_f300_step20_best_ever.npz"),
    "path": dict(model=REPO / "models" / "topopt_idle_path_small.json", load_n=3000.0, h=2.0, w=6.0,
                 step20=IV / "05_masks" / "path_baseline_2.0mm_step20_best_ever.npz"),
    "hanger": dict(model=refine_shape.HANGER_MODEL, load_n=200.0, h=2.4, w=10.0,
                   step20=IV / "06_masks" / "hanger_res2.4mm_w10mm_step20_best_ever.npz"),
}


def solve(mask, ctx):
    c = ctx.cond
    t0 = time.perf_counter()
    probe = _fem_probe.evaluate_full(mask, ctx.grid, ctx.table, c.fixed_faces, c.load_faces, c.forces,
                                     allowable_mpa=c.allowable_mpa, youngs_modulus_mpa=c.youngs_modulus_mpa,
                                     percentile=stress.DEFAULT_PERCENTILE, solver=c.solver)
    if probe["status"] != "ok":
        raise RuntimeError(f"FEM失敗: {probe.get('error')}")
    return probe["von_mises_mpa"], time.perf_counter() - t0


def metric_row(vm, mask, keep, dist_fixed, h, w, target_mpa):
    cell = h ** 3
    row = dict(cells=int(mask.sum()), material_mm3=float(mask.sum() * cell),
               p995=gm.percentile_current(vm, mask), max=float(vm[mask].max()))
    for name, vol in (("w3", w ** 3), ("2w3", 2 * w ** 3), ("8w3", 8 * w ** 3)):
        row[f"QV_{name}"] = gm.top_volume_value(vm, mask, cell, vol)
    for name, r in (("R1.5h", 1.5 * h), ("Rw/2", w / 2), ("Rw", w)):
        row[f"ball_{name}"] = gm.ball_mean_max(vm, mask, r, h)
    hot = mask & (vm > target_mpa)
    row["hot_mm3"] = float(hot.sum() * cell)
    row["hot_keep_mm3"] = float((hot & keep).sum() * cell)
    row["hot_within_1h_fixed_mm3"] = float((hot & (dist_fixed <= h)).sum() * cell)
    row["hot_within_2h_fixed_mm3"] = float((hot & (dist_fixed <= 2 * h)).sum() * cell)
    design = mask & ~keep
    row["QV_w3_design"] = gm.top_volume_value(vm, design, cell, w ** 3)
    row["QV_w3_out1h"] = gm.top_volume_value(vm, mask & (dist_fixed > h), cell, w ** 3)
    row["QV_w3_out2mm"] = gm.top_volume_value(vm, mask & (dist_fixed > 2.0), cell, w ** 3)
    row["hot_design_mm3"] = float((hot & ~keep).sum() * cell)
    row["hot_design_out1h_mm3"] = float((hot & ~keep & (dist_fixed > h)).sum() * cell)
    row["hot_out2mm_mm3"] = float((hot & (dist_fixed > 2.0)).sum() * cell)
    # 荷重倍率ごとの目標超過体積(線形弾性なので応力場を倍率で拡大するだけ)
    row["hot_by_load_factor"] = {str(f): dict(all=float((mask & (vm * f > target_mpa)).sum() * cell),
                                             keep=float((keep & mask & (vm * f > target_mpa)).sum() * cell),
                                             out2mm=float((mask & (dist_fixed > 2.0) & (vm * f > target_mpa)).sum() * cell))
                                 for f in (0.5, 1.0, 2.5, 5.0, 10.0)}
    row["p995_count_cells"] = int(np.ceil(0.005 * mask.sum()))
    row["m5b_target"] = gm.exceed_integral(vm, mask, cell, target_mpa)
    idx = np.unravel_index(np.argmax(np.where(mask, vm, -1)), vm.shape)
    row["peak_dist_fixed_mm"] = float(dist_fixed[idx])
    row["peak_in_keep"] = bool(keep[idx])
    return row


def field_only(vm, mask, keep, dist_fixed, h, w, target_mpa, rng):
    """場だけの操作(FEMなし)。リスト系の式は値の配列だけで測れるので、セルの追加は値の追加で表す。"""
    cell = h ** 3
    vals = vm[mask]
    res = {}
    # (a) 母数: 低応力側 x% を抜く / 中央値以下の値を N 倍足す
    def list_metrics(v):
        s = np.sort(v)[::-1]
        def qv(vol):
            k = vol / cell
            lo = int(np.floor(k)) - 1
            fr = k - np.floor(k)
            return float(s[lo] + (s[lo + 1] - s[lo]) * fr) if fr else float(s[lo])
        return dict(p995=float(np.percentile(v, 99.5)), QV_w3=qv(w ** 3), QV_2w3=qv(2 * w ** 3))
    low = np.sort(vals)
    res["dilution"] = {"base": list_metrics(vals)}
    for frac in (0.05, 0.10, 0.30):
        res["dilution"][f"remove_low_{frac}"] = list_metrics(low[int(frac * low.size):])
    for mult in (1, 5, 20):
        extra = rng.choice(low[: low.size // 2], size=mult * low.size)
        res["dilution"][f"add_low_x{mult}"] = list_metrics(np.concatenate([vals, extra]))

    # (b)(c) 集中・位置: n セルの立方塊へ s を注入。場所は 一般部・保持セル・固定面1h以内
    def cube_at(center, n):
        a = int(round(n ** (1 / 3)))
        sl = tuple(slice(max(0, c - a // 2), max(0, c - a // 2) + a) for c in center)
        m = np.zeros_like(mask)
        m[sl] = True
        return m & mask
    sites = {}
    general = mask & ~keep & (dist_fixed > 3 * w)
    for name, region in (("general", general), ("keep", mask & keep), ("near_fixed", mask & (dist_fixed <= h))):
        if region.any():
            # その区域のうち最も応力の低いセル(1.25倍超過の注入で「目立たない所」を狙う)と、中央値付近のセル
            cand = np.argwhere(region)
            vv = vm[region]
            sites[name] = tuple(cand[int(np.argsort(vv)[len(vv) // 2])])
    inj = {}
    for site, center in sites.items():
        for n in (1, 8, 27, 125):
            for factor in (1.25, 3.0):
                m = cube_at(center, n)
                v2 = vm.copy()
                v2[m] = factor * target_mpa * 4.8  # 許容40×factor(=生の許容の何倍か)
                key = f"{site}_n{n}_x{factor}"
                inj[key] = dict(cells=int(m.sum()), p995=gm.percentile_current(v2, mask),
                                QV_w3=gm.top_volume_value(v2, mask, cell, w ** 3),
                                QV_2w3=gm.top_volume_value(v2, mask, cell, 2 * w ** 3),
                                ball_R1_5h=gm.ball_mean_max(v2, mask, 1.5 * h, h),
                                ball_Rw2=gm.ball_mean_max(v2, mask, w / 2, h),
                                m5b_raw=gm.exceed_integral(v2, mask, cell, target_mpa * 4.8))
    res["injection"] = inj
    res["injection_sites"] = {k: [int(x) for x in v] for k, v in sites.items()}
    return res


def fixed_boxes(ctx):
    return _fem_probe.face_world_boxes(ctx.table, ctx.cond.fixed_faces)


def run_model(name, spec, refine_levels, rng):
    print(f"== {name}", flush=True)
    ctx = smeth.build_context(spec["model"], load_n=spec["load_n"], element_size_mm=spec["h"], min_width_mm=spec["w"])
    target = ctx.cond.allowable_mpa / ctx.cond.target_safety_factor
    keep = np.asarray(ctx.cond.keep_cells if ctx.cond.keep_cells is not None else np.zeros(ctx.grid.shape, bool))
    dist = gm.distance_to_boxes(ctx.grid, fixed_boxes(ctx))
    with np.load(spec["step20"]) as z:
        step20 = np.asarray(z["mask"], bool)
    assert step20.shape == tuple(ctx.grid.shape), (step20.shape, ctx.grid.shape)
    out = dict(model=name, load_n=spec["load_n"], h=spec["h"], w=spec["w"], target_mpa=target,
               search_target_sf=float(ctx.cond.target_safety_factor), shapes={})
    (OUT / "fields").mkdir(parents=True, exist_ok=True)
    for sname, mask in (("full", np.asarray(ctx.space.mask, bool)), ("step20", step20)):
        vm, sec = solve(mask, ctx)
        np.savez_compressed(OUT / "fields" / f"{name}_{sname}_k1.npz", vm=vm, mask=mask, keep=keep, dist_fixed=dist,
                            spacing=spec["h"], origin=np.asarray(ctx.grid.origin))
        row = metric_row(vm, mask, keep, dist, spec["h"], spec["w"], target)
        row["fem_s"] = sec
        out["shapes"][sname] = dict(k1=row)
        print(sname, json.dumps({k: (round(v, 3) if isinstance(v, float) else v) for k, v in row.items()},
                                ensure_ascii=False), flush=True)
        if sname == "step20":
            out["field_only"] = field_only(vm, mask, keep, dist, spec["h"], spec["w"], target, rng)
    # 細分(同じ形を k^3 に割って解き直す)
    for k in refine_levels:
        fctx = smeth.build_context(spec["model"], load_n=spec["load_n"], element_size_mm=spec["h"] / k,
                                   min_width_mm=spec["w"])
        fkeep = np.asarray(fctx.cond.keep_cells if fctx.cond.keep_cells is not None else np.zeros(fctx.grid.shape, bool))
        fdist = gm.distance_to_boxes(fctx.grid, fixed_boxes(fctx))
        for sname, mask in (("step20", step20), ("full", np.asarray(ctx.space.mask, bool))):
            if sname == "full" and k >= 3:  # 全材料の k=3 は50万要素を超えるので測らない
                continue
            fmask, diag = refine_shape._project_mask_exact_subdivide(mask, k, fctx.space, ctx.grid.origin, spec["h"])
            fmask = fmask & np.asarray(fctx.space.mask, bool)
            fmask |= fkeep
            vm, sec = solve(fmask, fctx)
            row = metric_row(vm, fmask, fkeep, fdist, spec["h"] / k, spec["w"], target)
            row["fem_s"] = sec
            row["projection"] = diag
            out["shapes"][sname][f"k{k}"] = row
            print(sname, f"k{k}", json.dumps({kk: (round(v, 3) if isinstance(v, float) else v) for kk, v in row.items()
                                              if kk != "projection"}, ensure_ascii=False), flush=True)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="*", default=list(MODELS))
    ap.add_argument("--refine", nargs="*", type=int, default=[2, 3])
    a = ap.parse_args()
    rng = np.random.default_rng(0)
    OUT.mkdir(parents=True, exist_ok=True)
    for name in a.models:
        res = run_model(name, MODELS[name], a.refine, rng)
        (OUT / f"phase_a_{name}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=float),
                                                    encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
