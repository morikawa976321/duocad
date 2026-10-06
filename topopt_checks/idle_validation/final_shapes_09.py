"""段階9: 保存済み完成形候補の幾何検査(平滑化・集合演算前後)と、最終形を同じ格子へ戻した固定形状の再解析。

結果と解釈は `docs/topopt_idle_validation/09_FINAL_SHAPES.md`。製品コード(`duocad.topopt.*`)は変更しない。新規の最適化はしない。
1形状 = 1子プロセス(`run_09_final_shapes.py` がwall timeoutを掛ける)。

    convert:  マスクの前検査 → 平滑化(`build_surface_from_mask`) → 製品の `finalize`(保持・除外のメッシュ集合演算)
              → 最終メッシュを同じ格子と半分の格子へ占有化 → 最小幅を半格子で測る。FEMなし。
    resolve:  最終メッシュ(ブーリアン不成立なら平滑化メッシュ。smoothed_keep はそれに保持セルを足した、最終ブーリアンの
              保持のfuseの近似)を同じ格子へ占有化したマスクを1回FEMで解く。条件(荷重・拘束の節点被覆)が通らなければFEMへ進まない。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "topopt_checks"))
import _bootstrap  # noqa: E402,F401

import numpy as np  # noqa: E402

from duocad import optimize_run  # noqa: E402
from duocad.topopt import acceptance, binary_search, min_width, voxelize  # noqa: E402
from duocad.topopt import mesh as topo_mesh  # noqa: E402

import _common  # noqa: E402
import refine_shape  # noqa: E402
import search_methods as sm  # noqa: E402
import synthetic_matrix_support as sup  # noqa: E402

OUT = sup.OUT
WORK = OUT / "09_work"
LOCK = sup.LOCK
HANGER_MODEL = refine_shape.HANGER_MODEL

#: 保存済みの完成形候補。`ref` は選定の根拠になった保存台帳の値(再計算ではない)。
SHAPES = {
    "S1_dil25_realloc": dict(
        model=sup.DILUTION_MODEL, load_n=300.0, size_mm=2.5, width_mm=6.0,
        mask="05_masks/dilution_res2.5_w6_reallocation_best_ever.npz",
        ledger="05_main_matrix.json", ledger_case="dilution_res2.5_w6", ledger_method="reallocation"),
    "S1c_dil25_step20": dict(
        model=sup.DILUTION_MODEL, load_n=300.0, size_mm=2.5, width_mm=6.0,
        mask="05_masks/dilution_res2.5_w6_step20_best_ever.npz",
        ledger="05_main_matrix.json", ledger_case="dilution_res2.5_w6", ledger_method="step20"),
    "S2_hanger200_step20": dict(
        model=HANGER_MODEL, load_n=200.0, size_mm=2.0, width_mm=10.0,
        mask="06_masks/hanger_h1_baseline_step20_best_ever.npz",
        ledger="06_main_matrix.json", ledger_case="hanger_h1_baseline", ledger_method="step20"),
    "S3_hanger2000_step20": dict(
        model=HANGER_MODEL, load_n=2000.0, size_mm=2.2, width_mm=10.0,
        mask="07_masks/F2000_thick10mm_h2.2_step20_best_ever.npz",
        ledger="07_grid.json", ledger_case="F2000_thick10mm_h2.2_step20", ledger_method=None),
}


def load_shape(shape_id: str) -> dict:
    spec = SHAPES[shape_id]
    doc = refine_shape.open_session(spec["model"]).document
    refine_shape._set_load_doc(doc, spec["load_n"])
    bundle = optimize_run.build_conditions(doc)
    space = sup.ds.derive(bundle.conditions, spacing=spec["size_mm"])
    table, mapping = optimize_run._map_boundary_conditions(space, bundle)
    cond = optimize_run._build_search_conditions(
        doc, space, bundle, optimize_run.OptimizeOptions(element_size_mm=spec["size_mm"]), table=table, mapping=mapping)
    resolved = optimize_run._resolve_min_width(space.grid.spacing, spec["width_mm"])
    ctx = sm.SearchContext(model_path=spec["model"], load_n=spec["load_n"], element_size_mm=spec["size_mm"],
                           min_width_mm=spec["width_mm"], cond=cond, space=space, table=table,
                           min_width_cells=int(resolved["cells"]))
    with np.load(OUT / spec["mask"]) as z:
        mask = np.asarray(z["mask"], dtype=bool)
    return dict(spec=spec, doc=doc, bundle=bundle, ctx=ctx, mask=mask)


def saved_reference(spec: dict) -> dict:
    """選定の根拠にした保存台帳の値(再計算しない)。"""
    data = json.loads((OUT / spec["ledger"]).read_text(encoding="utf-8"))
    cases = data["cases"]
    cases = cases if isinstance(cases, list) else list(cases.values())
    row = next(c for c in cases if c.get("case_id") == spec["ledger_case"])
    if spec["ledger_method"]:
        rec = row.get(spec["ledger_method"]) or row["results"][spec["ledger_method"]]
    else:
        rec = row["search"]
    keys = ("material_mm3", "safety_factor_product", "m5b_raw_exceedance_mpa_mm3", "m5b_raw_passed",
            "m5b_target_exceedance_mpa_mm3", "m5b_target_passed", "mask_hash", "stop_reason", "evaluations")
    return {k: rec.get(k) for k in keys}


def _mask_stats(mask: np.ndarray, ctx: sm.SearchContext) -> dict:
    cond, grid = ctx.cond, ctx.grid
    keep = binary_search._keep_cells(cond, mask.shape)
    exempt = binary_search._min_width_exempt(cond, mask.shape, ctx.min_width_cells)
    labels, components = _components(mask)
    return dict(
        cells=int(mask.sum()), volume_mm3=float(mask.sum() * grid.element_volume),
        components=components, mask_hash=acceptance.material_sha256(mask),
        min_width_cells=int(ctx.min_width_cells), min_width_realized_mm=float(ctx.min_width_cells * grid.spacing),
        min_width_ok=bool(binary_search._mask_satisfies_min_width(mask, ctx.min_width_cells, exempt)),
        boundary_status=acceptance.boundary_check(mask)["status"],
        closed_voids=len(acceptance.closed_voids(mask)),
        keep_cells=None if keep is None else int(keep.sum()),
        keep_cells_missing=None if keep is None else int((keep & ~mask).sum()),
        outside_design_cells=int((mask & ~ctx.space.mask).sum()))


def _components(mask: np.ndarray):
    from scipy import ndimage
    labels, n = ndimage.label(mask, structure=ndimage.generate_binary_structure(3, 1))
    return labels, int(n)


def _conditions(mask: np.ndarray, ctx: sm.SearchContext) -> dict:
    cond = ctx.cond
    report, _, _ = acceptance.condition_check(mask, ctx.table, cond.fixed_faces, cond.load_faces, cond.forces)
    return dict(status=report["status"], faces=report["faces"])


def _soup(vertices: np.ndarray, faces: np.ndarray) -> voxelize.TriangleSoup:
    return voxelize.TriangleSoup(positions=np.asarray(vertices, dtype=float), triangles=np.asarray(faces, dtype=np.int64),
                                 face_index=np.zeros(len(faces), dtype=np.int64))


def _occupancy(vertices: np.ndarray, faces: np.ndarray, grid) -> tuple[np.ndarray, dict]:
    return voxelize.occupancy(_soup(vertices, faces), grid, allow_empty=True)


def _half_grid(grid) -> "voxelize.Grid":
    return voxelize.Grid(origin=tuple(grid.origin), spacing=float(grid.spacing) / 2.0,
                         shape=tuple(int(n) * 2 for n in grid.shape))


def _opening_rows(fine: np.ndarray, exempt, m: int, half_spacing: float) -> list[dict]:
    total = int(fine.sum())
    rows = []
    for side in range(2, 2 * m + 1):
        lost = fine & ~min_width.cube_opening(fine, side)
        if exempt is not None:
            lost &= ~exempt
        rows.append(dict(side_half_cells=side, side_mm=float(side * half_spacing), lost_cells=int(lost.sum()),
                         lost_fraction=float(lost.sum()) / total if total else None))
    return rows


def _width_at(rows: list[dict], fraction: float):
    """開き(opening)で失う体積がこの割合以下になる最大の立方の一辺[mm]。しきい値は解析者が置いた値(未校正)。"""
    ok = [r["side_mm"] for r in rows if r["lost_fraction"] is not None and r["lost_fraction"] <= fraction]
    return max(ok) if ok else None


def _fine_width_report(vertices, faces, ctx: sm.SearchContext, voxel_mask: np.ndarray) -> dict:
    """最終メッシュを半格子へ占有化し、一辺 s 半セルの立方のopeningで失う材料(保持セルは対象外)を s ごとに数える。
    `2m` が実現最小幅(粗い格子のm セル)に当たる。**対照**として、元のボクセルマスクを半格子へ単純に細分した形でも
    同じ数え方をする(m セル幅の部材は s=2m で損失0になるはずの検算)。"""
    grid = ctx.grid
    half = _half_grid(grid)
    fine, stats = _occupancy(vertices, faces, half)
    keep = binary_search._keep_cells(ctx.cond, tuple(grid.shape))
    up = lambda a: np.repeat(np.repeat(np.repeat(a, 2, 0), 2, 1), 2, 2)
    exempt = None if keep is None else up(keep)
    m = int(ctx.min_width_cells)
    rows = _opening_rows(fine, exempt, m, half.spacing)
    control = _opening_rows(up(voxel_mask), exempt, m, half.spacing)
    return dict(half_spacing_mm=float(half.spacing), fine_cells=int(fine.sum()),
                fine_volume_mm3=float(fine.sum() * half.element_volume),
                occupancy_stats={k: v for k, v in stats.items() if isinstance(v, (int, float, bool, str))},
                rows=rows, control_rows_voxel_subdivided=control,
                width_at_loss_le_0p1pct_mm=_width_at(rows, 0.001), width_at_loss_le_1pct_mm=_width_at(rows, 0.01),
                realized_min_width_mm=float(m * grid.spacing))


def _diff(a: np.ndarray, b: np.ndarray) -> dict:
    inter = int((a & b).sum())
    union = int((a | b).sum())
    return dict(cells_a=int(a.sum()), cells_b=int(b.sum()), only_a=int((a & ~b).sum()), only_b=int((b & ~a).sum()),
                iou=(inter / union) if union else None)


def convert(shape_id: str) -> dict:
    from duocad.mesh_geometry import TOPOLOGY_DEFECTS, topology
    from duocad.topopt.finalize import final_mesh_checks, finalize

    t0 = time.perf_counter()
    ld = load_shape(shape_id)
    ctx, mask, bundle, doc = ld["ctx"], ld["mask"], ld["bundle"], ld["doc"]
    grid = ctx.grid
    rec = dict(shape_id=shape_id, status="running", saved_reference=saved_reference(ld["spec"]),
               grid=dict(spacing_mm=float(grid.spacing), shape=list(grid.shape), origin_mm=[float(x) for x in grid.origin]),
               load_n=ld["spec"]["load_n"], min_width_requested_mm=ld["spec"]["width_mm"])
    if tuple(mask.shape) != tuple(grid.shape):
        raise ValueError(f"保存マスクの形 {mask.shape} が格子 {tuple(grid.shape)} と違う")
    rec["voxel"] = _mask_stats(mask, ctx)
    rec["voxel"]["conditions"] = _conditions(mask, ctx)
    rec["voxel"]["hash_matches_saved"] = rec["voxel"]["mask_hash"] == rec["saved_reference"]["mask_hash"]

    surface = topo_mesh.build_surface_from_mask(mask)
    world = np.asarray(optimize_run._mesh_to_world(surface.verts_smooth, grid)).reshape(-1, 3)
    stats, _ = topology(world, surface.faces)
    rec["smoothed"] = dict(
        triangles=int(len(surface.faces)), volume_raw_mm3=float(surface.volume_raw * grid.element_volume),
        volume_smooth_mm3=float(surface.volume_smooth * grid.element_volume),
        topology_defects={k: int(stats[k]) for k in TOPOLOGY_DEFECTS})
    pre = final_mesh_checks(world, surface.faces)
    rec["smoothed"]["checks_failed"] = pre["failed"]
    rec["smoothed"]["self_intersection_counts"] = pre["self_intersections"]["counts"]
    smoothed_mask, _ = _occupancy(world, surface.faces, grid)
    rec["smoothed"]["vs_voxel"] = _diff(mask, smoothed_mask)
    WORK.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(WORK / f"{shape_id}_smoothed_mask.npz", mask=smoothed_mask)
    rec["smoothed"]["fine_width"] = _fine_width_report(world, surface.faces, ctx, mask)
    rec["elapsed_to_smoothed_seconds"] = time.perf_counter() - t0

    t1 = time.perf_counter()
    try:
        fv, ff = finalize(world, surface.faces,
                         [doc.get_body(b).shape for b in bundle.keep_body_ids],
                         [doc.get_body(b).shape for b in bundle.exclude_body_ids])
        rec["finalize_seconds"] = time.perf_counter() - t1
        checks = final_mesh_checks(fv, ff)
        final_mask, _ = _occupancy(fv, ff, grid)
        np.savez_compressed(WORK / f"{shape_id}_final_mask.npz", mask=final_mask)
        np.savez_compressed(WORK / f"{shape_id}_final_mesh.npz", vertices=fv.astype(np.float32), faces=ff.astype(np.int32))
        rec["final"] = dict(
            status="ok", triangles=int(len(ff)), volume_mm3=float(topo_mesh.mesh_volume(fv, ff)),
            mesh_checks=checks, mesh_checks_failed=checks["failed"],
            stats=_mask_stats(final_mask, ctx), vs_voxel=_diff(mask, final_mask),
            conditions=_conditions(final_mask, ctx), fine_width=_fine_width_report(fv, ff, ctx, mask))
    except Exception as error:
        rec["finalize_seconds"] = time.perf_counter() - t1
        rec["final"] = dict(status="error", error=f"{type(error).__name__}: {error}"[:3000],
                            traceback=traceback.format_exc()[-3000:])
    rec["status"] = "ok"
    rec["wall_seconds"] = time.perf_counter() - t0
    rec.update(_common.peak_memory_mb())
    return rec


def resolve(shape_id: str, source: str) -> dict:
    import hanger_loads_support as hl

    t0 = time.perf_counter()
    ld = load_shape(shape_id)
    ctx = ld["ctx"]
    base = "smoothed" if source == "smoothed_keep" else source
    with np.load(WORK / f"{shape_id}_{base}_mask.npz") as z:
        mask = np.asarray(z["mask"], dtype=bool)
    if source == "smoothed_keep":
        keep = binary_search._keep_cells(ctx.cond, mask.shape)
        mask = mask | keep
    conditions = _conditions(mask, ctx)
    with hl.acquire_fem_lock(f"run_09_resolve:{shape_id}:{source}"):
        diagnosis = hl.diagnose_mask(mask, ctx, label=f"{shape_id}:{source}")
    return dict(shape_id=shape_id, source=source, status=diagnosis.get("status"), diagnosis=diagnosis,
                conditions=conditions, mask_stats=_mask_stats(mask, ctx), wall_seconds=time.perf_counter() - t0,
                **_common.peak_memory_mb())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=("convert", "resolve"))
    ap.add_argument("--shape", required=True, choices=sorted(SHAPES))
    ap.add_argument("--source", choices=("final", "smoothed", "smoothed_keep"), default="final")
    args = ap.parse_args()
    WORK.mkdir(parents=True, exist_ok=True)
    name = f"{args.shape}_{args.action}" + (f"_{args.source}" if args.action == "resolve" else "")
    try:
        rec = convert(args.shape) if args.action == "convert" else resolve(args.shape, args.source)
    except Exception as error:
        rec = dict(shape_id=args.shape, status="error", error=f"{type(error).__name__}: {error}"[:3000],
                   traceback=traceback.format_exc()[-3000:])
    rec["code_version"] = _common.git_state(paths=("duocad", "topopt_checks"))
    rec["finished_at"] = _common.now_iso()
    _common.write_atomic(WORK / f"{name}.json", json.dumps(rec, ensure_ascii=False, indent=1, default=float) + "\n")
    print(f"[{name}] {rec['status']}", flush=True)
    return 0 if rec["status"] == "ok" else 3


if __name__ == "__main__":
    raise SystemExit(main())
