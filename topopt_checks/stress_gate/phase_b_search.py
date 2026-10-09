"""段階B: 候補の合否判定で製品の探索(step20)を回し、採用形を比べる(研究用。製品コードは変えない)。

削除の順位・歩幅・幾何条件は製品のまま。`stress.representative_stress` だけをプロセス内で差し替え、
代表応力(安全率 = 許容 ÷ 代表応力)を候補式の値にする。探索目標は製品どおり 4.8(= 2.0 × 2.4)。

ゲート(`--gate`):
- current        製品の現行(材料全体の99.5%点)
- qv             固定物理体積の上位値 Q_V(材料全体)。V = c·w³
- qv_design      Q_V を保持セル以外で取る
- qv_baseline    Q_V(材料全体)。V = E_full + c·w³(E_full は全材料の形で目標応力を超える体積。探索の前に1回解いて固定)
- qv_out         Q_V を固定面から d [mm] より離れたセルで取る(固定面から d 以内は判定しない)
- qv_out_zball   qv_out に加えて、固定面から d 以内のセルを中心とする半径 w の球平均(材料だけで平均)の最大も目標応力以下
- qv_out_zrel    qv_out に加えて、同じ球平均の最大が max(目標応力, (1+ε)×全材料の形での値) 以下(全材料が既に超えている分は悪化だけを見る)

採用形は同じ格子で採点し(`phase_a_fields.metric_row`)、`--refine` の k で割って解き直した「部材域」
(保持セル以外・固定面と荷重面から 2h より離れたセル)の応力を、利用者目標 2.0 の応力(40/2.0=20MPa)と比べる。

    .venv\\Scripts\\python.exe topopt_checks\\stress_gate\\phase_b_search.py --model dilution --gate qv_baseline --c 1
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
import phase_a_fields as pa  # noqa: E402  (sys.path・_bootstrap はこちらで済む)

import numpy as np  # noqa: E402
from scipy import ndimage  # noqa: E402

import _fem_probe  # noqa: E402
import refine_shape  # noqa: E402
import search_methods as smeth  # noqa: E402
import synthetic_matrix_support as sup  # noqa: E402
from duocad.topopt import binary_search, stress  # noqa: E402

import gate_metrics as gm  # noqa: E402

REAL_REP = stress.representative_stress


def zone_ball(vm, mask, dist_fixed, h, w, d, keep=None, dist_load=None):
    zone = mask & (dist_fixed <= d)
    if keep is not None:
        zone |= mask & keep
    if dist_load is not None:
        zone |= mask & (dist_load <= d)
    if not zone.any():
        return 0.0
    return float(gm.ball_mean_field(vm, mask, w, h)[zone].max())


def make_rep(gate, *, h, w, c, d, keep, dist_fixed, dist_load, baseline_mm3, zone_full, eps, target, log,
             fields_by_mask=None, peak_full=0.0, surface_box=None):
    cell = h ** 3

    def rep(field, rho, **kw):
        real = REAL_REP(field, rho, **kw)
        vm = np.asarray(field.von_mises, float)
        mask = np.asarray(rho) >= 0.5
        if gate == "current":
            value = real.value
        else:
            pop = mask
            vol = c * w ** 3
            if gate == "qv_design":
                pop = mask & ~keep
            elif gate.startswith("qv_out"):
                pop = mask & (dist_fixed > d)
            elif gate.startswith("split"):
                pop = mask & ~keep & (dist_fixed > d)
            elif gate.startswith("unified"):
                pop = mask & ~keep & (dist_fixed > d) & (dist_load > d)
            elif gate == "qv_baseline":
                vol = baseline_mm3 + c * w ** 3
            value = gm.top_volume_value(vm, pop, cell, vol)
        entry = dict(cells=int(mask.sum()), p995=float(real.value), member=float(value))
        if gate.endswith(("zball", "zrel")):
            zb = zone_ball(vm, mask, dist_fixed, h, w, d, keep if gate.startswith(("split", "unified")) else None,
                           dist_load if gate.startswith("unified") else None)
            limit = target if gate.endswith("zball") else max(target, (1.0 + eps) * zone_full)
            entry["zone_ball"] = zb
            value = max(value, target * zb / limit)  # 両方が目標以下のときだけ代表応力が目標以下
        elif gate == "unified":  # 境界域は判定せず記録だけ(探索中の推移を後で見る)
            entry["zone_ball"] = zone_ball(vm, mask, dist_fixed, h, w, d, keep, dist_load)
        elif gate == "unified_peak":
            # ピーク(判定域=保持外・拘束/荷重節点から d 超、の要素中心の最大)を、全材料のピークの (1+ε) 倍か
            # 目標応力の大きい方以下に保つ。全材料のピークは利用者の形が決める下限で、探索はそれを作り増さない
            region = mask & ~keep & (dist_fixed > d) & (dist_load > d)
            peak = float(vm[region].max()) if region.any() else 0.0
            limit = max(target, (1.0 + eps) * peak_full)
            entry["peak"] = peak
            value = max(value, target * peak / limit)
        elif gate == "unified_speak":
            # 表面(露出面)の面中心 von Mises の最大で同じことをする。面の場は stress.evaluate の差し替えが直前に置く
            region = mask & ~keep & (dist_fixed > d) & (dist_load > d)
            face = surface_box.get("face")
            peak = float(np.nanmax(face[region])) if face is not None and region.any() else 0.0
            limit = max(target, (1.0 + eps) * peak_full)
            entry["peak"] = peak
            value = max(value, target * peak / limit)
        entry["used"] = float(value)
        log.append(entry)
        if fields_by_mask is not None:
            fields_by_mask[np.packbits(mask).tobytes()] = vm
            while len(fields_by_mask) > 4:  # hanger の場は1つ 8MB 級。直近の候補だけ持つ
                fields_by_mask.pop(next(iter(fields_by_mask)))
        return dataclasses.replace(real, value=float(value))

    return rep


def region_check(vm, mask, keep, dist_fixed, dist_load, h_coarse, h, w):
    member = mask & ~keep & (dist_fixed > 2 * h_coarse) & (dist_load > 2 * h_coarse)
    cell = h ** 3
    if not member.any():
        return None
    return dict(member_cells=int(member.sum()),
                QV_w3=gm.top_volume_value(vm, member, cell, w ** 3),
                p995=float(np.percentile(vm[member], 99.5)), max=float(vm[member].max()),
                over20_mm3=float((member & (vm > 20.0)).sum() * cell))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, choices=list(pa.MODELS))
    ap.add_argument("--gate", required=True, choices=["current", "qv", "qv_design", "qv_baseline", "qv_out", "qv_out_zball", "qv_out_zrel",
                                                           "split_zball", "split_zrel",
                                                           "unified", "unified_zball", "unified_zrel", "unified_peak",
                                                           "unified_speak"])
    ap.add_argument("--eps", type=float, default=0.1)
    ap.add_argument("--shift", type=float, default=0.0, help="格子原点を全軸で h×shift ずらす")
    ap.add_argument("--c", type=float, default=1.0)
    ap.add_argument("--d", type=float, default=2.0)
    ap.add_argument("--dist", choices=["aabb", "nodes"], default="aabb",
                    help="固定面からの距離を面の AABB まで(aabb)か拘束節点まで(nodes)で測る")
    ap.add_argument("--load", type=float, default=None)
    ap.add_argument("--h", type=float, default=None)
    ap.add_argument("--w", type=float, default=None)
    ap.add_argument("--refine", nargs="*", type=int, default=[2, 3])
    ap.add_argument("--max-evaluations", type=int, default=1000)
    ap.add_argument("--tag", default="")
    ap.add_argument("--halo", type=int, default=0,
                    help="削除候補から外す、高応力セル(保持外・拘束/荷重面の1層の外で σ ≥ α·σt)のまわりのセル数(0 で無効)")
    ap.add_argument("--halo-alpha", type=float, default=1.0)
    a = ap.parse_args()
    spec = dict(pa.MODELS[a.model])
    for key, val in (("load_n", a.load), ("h", a.h), ("w", a.w)):
        if val is not None:
            spec[key] = val
    h, w = spec["h"], spec["w"]
    shift_mm = a.shift * h

    def context(size):
        if shift_mm:
            return sup.build_context_with_origin_shift(spec["model"], load_n=spec["load_n"], element_size_mm=size,
                                                       min_width_mm=w, shift_mm=shift_mm)
        return smeth.build_context(spec["model"], load_n=spec["load_n"], element_size_mm=size, min_width_mm=w)

    ctx = context(h)
    # このスクリプトの 'current' は 2026-10-08 時点の製品(材料全体の99.5%点)。製品の部材域の規則は使わない
    ctx.cond = binary_search.replace(ctx.cond, member_volume_mm3=None)
    target = ctx.cond.allowable_mpa / ctx.cond.target_safety_factor
    keep = np.asarray(ctx.cond.keep_cells if ctx.cond.keep_cells is not None else np.zeros(ctx.grid.shape, bool))
    def fixed_distance(c):
        if a.dist == "nodes":
            return gm.distance_to_points(c.grid, c.table.positions(list(c.cond.fixed_faces)))
        return gm.distance_to_boxes(c.grid, pa.fixed_boxes(c))

    dist_fixed = fixed_distance(ctx)
    def load_distance(c):
        if a.dist == "nodes":
            return gm.distance_to_points(c.grid, c.table.positions(list(c.cond.load_faces)))
        return gm.distance_to_boxes(c.grid, _fem_probe.face_world_boxes(c.table, c.cond.load_faces))

    dist_load = load_distance(ctx)
    full = np.asarray(ctx.space.mask, bool)
    vm_full, _ = pa.solve(full, ctx)
    surface_box: dict = {}
    real_evaluate = stress.evaluate

    def evaluate_with_surface(model, rho, *, scale, u, **kw):
        surface_box["face"] = gm.exposed_face_vm(model, u, np.asarray(rho) >= 0.5, scale.stress_factor)
        return real_evaluate(model, rho, scale=scale, u=u, **kw)

    if a.gate == "unified_speak":
        cnd = ctx.cond
        pr = _fem_probe.evaluate_full(full, ctx.grid, ctx.table, cnd.fixed_faces, cnd.load_faces, cnd.forces,
                                      allowable_mpa=cnd.allowable_mpa, youngs_modulus_mpa=cnd.youngs_modulus_mpa,
                                      percentile=stress.DEFAULT_PERCENTILE, solver=cnd.solver)
        fac = float(np.nanmax(vm_full[full]) / np.nanmax(stress.element_stress(pr["_model"], pr["_displacement"]).von_mises[full]))
        face_full = gm.exposed_face_vm(pr["_model"], pr["_displacement"], full, fac)
    baseline = gm.exceed_volume(vm_full, full, h ** 3, target)
    zone_full = zone_ball(vm_full, full, dist_fixed, h, w, a.d, keep if a.gate.startswith(("split", "unified")) else None,
                          dist_load if a.gate.startswith("unified") else None)
    region_full = full & ~keep & (dist_fixed > a.d) & (dist_load > a.d)
    peak_full = float(vm_full[region_full].max()) if region_full.any() else 0.0
    if a.gate == "unified_speak":
        peak_full = float(np.nanmax(face_full[region_full])) if region_full.any() else 0.0
    log: list = []
    rep_fields = {} if a.halo else None
    rep = make_rep(a.gate, h=h, w=w, c=a.c, d=a.d, keep=keep, dist_fixed=dist_fixed, dist_load=dist_load,
                   baseline_mm3=baseline,
                   zone_full=zone_full, eps=a.eps, target=target, log=log, peak_full=peak_full,
                   surface_box=surface_box,
                   fields_by_mask=rep_fields)
    real_ranked = binary_search.ranked_cells
    halo_stats = dict(calls=0, protected_last=0)

    def ranked_with_halo(mask, energy):
        order = real_ranked(mask, energy)
        vm = rep_fields.get(np.packbits(np.asarray(mask, bool)).tobytes()) if rep_fields is not None else None
        if vm is None:
            return order
        hot = np.asarray(mask, bool) & ~keep & (dist_fixed > a.d) & (dist_load > a.d) & (vm >= a.halo_alpha * target)
        halo = ndimage.binary_dilation(hot, structure=ndimage.generate_binary_structure(3, 3), iterations=a.halo) & ~hot
        protected = np.flatnonzero((halo | hot).ravel())
        halo_stats["calls"] += 1
        halo_stats["protected_last"] = int(protected.size)
        return order[~np.isin(order, protected)]

    t0 = time.perf_counter()
    with patch.object(stress, "representative_stress", rep), \
            patch.object(stress, "evaluate", evaluate_with_surface if a.gate == "unified_speak" else real_evaluate), \
            patch.object(binary_search, "ranked_cells", ranked_with_halo if a.halo else real_ranked):
        result = binary_search.find_best_feasible(full, ctx.cond, max_evaluations=a.max_evaluations,
                                                  min_width_cells=ctx.min_width_cells)
    wall = time.perf_counter() - t0
    name = f"{a.model}_{a.gate}_c{a.c:g}" + (f"_d{a.d:g}" if a.gate.startswith(("qv_out", "split", "unified")) else "") + \
        (f"_e{a.eps:g}" if a.gate.endswith(("zrel", "peak")) else "") + \
        (f"_{a.dist}" if a.dist != "aabb" else "") + \
        f"_L{spec['load_n']:g}_h{h:g}_w{w:g}" + (f"_s{a.shift:g}" if a.shift else "") + \
        (f"_halo{a.halo}a{a.halo_alpha:g}" if a.halo else "") + (f"_{a.tag}" if a.tag else "")
    out = dict(model=a.model, gate=a.gate, c=a.c, d=a.d, load_n=spec["load_n"], h=h, w=w, target_mpa=target,
               baseline_exceed_mm3=baseline, zone_ball_full=zone_full, eps=a.eps, shift=a.shift, dist=a.dist, status=result.status, termination=result.termination,
               evaluations=result.evaluations, wall_s=wall, log=log, halo=a.halo, halo_alpha=a.halo_alpha,
               halo_stats=halo_stats, peak_full=peak_full)
    print(name, result.status, result.termination, f"eval={result.evaluations}", f"{wall:.0f}s", flush=True)
    (pa.OUT / "searches").mkdir(parents=True, exist_ok=True)
    dest = pa.OUT / "searches" / f"{name}.json"

    def save():
        dest.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=float), encoding="utf-8")

    save()
    if result.status == "feasible" and result.best_feasible is not None:
        mask = np.asarray(result.best_feasible.mask, bool)
        (pa.OUT / "masks").mkdir(parents=True, exist_ok=True)
        np.savez_compressed(pa.OUT / "masks" / f"{name}.npz", mask=mask, origin=np.asarray(ctx.grid.origin), spacing=h)
        vm, _ = pa.solve(mask, ctx)
        row = pa.metric_row(vm, mask, keep, dist_fixed, h, w, target)
        row["member_k1"] = region_check(vm, mask, keep, dist_fixed, dist_load, h, h, w)
        row["zone_ball_d"] = zone_ball(vm, mask, dist_fixed, h, w, a.d)
        row["zone_ball_keep_d"] = zone_ball(vm, mask, dist_fixed, h, w, a.d, keep)
        row["zone_ball_boundary_d"] = zone_ball(vm, mask, dist_fixed, h, w, a.d, keep, dist_load)
        out["k1"] = row
        print("k1", json.dumps({k: (round(v, 3) if isinstance(v, float) else v) for k, v in row.items()
                                if k != "hot_by_load_factor"}, ensure_ascii=False), flush=True)
        save()
        for k in a.refine:
            try:
                fctx = context(h / k)
            except Exception as exc:  # hanger の 1.2mm 格子は保持と除外が重なり設計空間を作れない(DesignSpaceConflictError)
                out[f"k{k}"] = dict(error=f"{type(exc).__name__}: {exc}")
                print(f"k{k} error", type(exc).__name__, flush=True)
                continue
            fkeep = np.asarray(fctx.cond.keep_cells if fctx.cond.keep_cells is not None else np.zeros(fctx.grid.shape, bool))
            fdf = fixed_distance(fctx)
            fdl = gm.distance_to_boxes(fctx.grid, _fem_probe.face_world_boxes(fctx.table, fctx.cond.load_faces))
            fmask, _ = refine_shape._project_mask_exact_subdivide(mask, k, fctx.space, ctx.grid.origin, h)
            fmask = (fmask & np.asarray(fctx.space.mask, bool)) | fkeep
            try:
                fvm, sec = pa.solve(fmask, fctx)
            except RuntimeError as exc:  # 原点をずらした細格子で条件面の対応が取れないことがある
                out[f"k{k}"] = dict(error=str(exc))
                print(f"k{k} error", exc, flush=True)
                continue
            frow = pa.metric_row(fvm, fmask, fkeep, fdf, h / k, w, target)
            frow["member"] = region_check(fvm, fmask, fkeep, fdf, fdl, h, h / k, w)
            frow["zone_ball_d"] = zone_ball(fvm, fmask, fdf, h / k, w, a.d)
            frow["zone_ball_keep_d"] = zone_ball(fvm, fmask, fdf, h / k, w, a.d, fkeep)
            frow["fem_s"] = sec
            out[f"k{k}"] = frow
            print(f"k{k} member", json.dumps(frow["member"]), f"p995={frow['p995']:.3f}", flush=True)
    save()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
