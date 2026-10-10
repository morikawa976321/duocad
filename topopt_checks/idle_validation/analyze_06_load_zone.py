"""タスク6・48.72 MPaピークの荷重ゾーン診断: 10_SYNTHESIS 付録A(旧 03A_REFERENCE_REPAIR 7-1節)が特定した原因
(hanger一般部のピークは荷重パッド接触端の応力集中だが、`_fem_probe.zone_masks`に荷重面近傍ゾーンが
無いため一般部へ希釈される)を、`_fem_probe.zone_masks_with_load`(本タスクで追加。既存`zone_masks`は
無変更)で複数条件にわたって確認する。「残る作業(06/07へ割当)」(03A 7-1節)への対応。

対象: 保存形4つ(hanger_base〈03Aが48.72MPaを出した現行形〉・r2・step05・transplant)・
H1現行形(`run_06_main_matrix.json`のstep20)・格子解像度1.8/2.2/2.4mm(`run_06_grid_resolution.json`)・
H2現行形(新規に`run_fixed_step`を1回実行——04では最終マスクを保存していなかった)。各マスクについて
4ゾーンのM3診断(`stress_metrics.metric_zone_percentile`と同じpercentile定義)とピーク位置・
荷重面/拘束面への最短距離を記録する。

前提: `run_06_main_matrix.py`・`run_06_grid_resolution.py`を先に完走させること。

出力: `output/idle_validation_20260928/06_load_zone.json`
実行: `.venv\\Scripts\\python.exe topopt_checks\\idle_validation\\analyze_06_load_zone.py`
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "topopt_checks"))
import _bootstrap  # noqa: E402,F401

import _common  # noqa: E402
import _fem_probe  # noqa: E402
import hanger_low_support as hls  # noqa: E402
import run_06_grid_resolution as grid_res  # noqa: E402
import run_06_main_matrix as main_matrix  # noqa: E402
import search_methods as sm  # noqa: E402
import stress_metrics as stm  # noqa: E402

OUT = hls.OUT
OUT_PATH = OUT / "06_load_zone.json"
PERCENTILE = 99.5


def _zone_percentile_all(vm: np.ndarray, zones: dict, *, allowable_mpa: float) -> dict:
    out = {}
    for name in ("holding", "load", "joint", "general"):
        zmask = zones[name]
        count = int(zmask.sum())
        if count == 0:
            out[name] = dict(count=0, value=None, passed=None)
            continue
        vals = vm[zmask]
        value = float(np.percentile(vals, PERCENTILE))
        out[name] = dict(count=count, value=value, passed=bool(value <= allowable_mpa),
                         max_mpa=float(vals.max()))
    return out


def _peak_locality(vm: np.ndarray, mask: np.ndarray, ctx: sm.SearchContext) -> dict:
    """材料内(mask=True)でvon Mises最大のセルの世界座標・拘束面/荷重面までの最短距離
    (10_SYNTHESIS 付録A(旧 03A_REFERENCE_REPAIR 7-1節)と同じ手法)。"""
    idx_flat = int(np.argmax(np.where(mask, vm, -np.inf)))
    idx = np.unravel_index(idx_flat, vm.shape)
    world = np.asarray(ctx.grid.origin) + (np.array(idx, dtype=float) + 0.5) * ctx.grid.spacing
    fixed_boxes = _fem_probe.face_world_boxes(ctx.table, ctx.cond.fixed_faces)
    load_boxes = _fem_probe.face_world_boxes(ctx.table, ctx.cond.load_faces)
    fixed_dist = min(_fem_probe._point_aabb_distance(world[None, :], b)[0] for b in fixed_boxes) if fixed_boxes else None
    load_dist = min(_fem_probe._point_aabb_distance(world[None, :], b)[0] for b in load_boxes) if load_boxes else None
    return dict(index=[int(x) for x in idx], world_mm=[float(x) for x in world], von_mises_mpa=float(vm[idx]),
               distance_to_fixed_mm=float(fixed_dist) if fixed_dist is not None else None,
               distance_to_load_mm=float(load_dist) if load_dist is not None else None)


def _diagnose(mask: np.ndarray, ctx: sm.SearchContext, *, label: str, zone_distance_mm=None) -> dict:
    score = sm.score_mask(mask, ctx)
    if score is None:
        return dict(label=label, status="fem_failed")
    dist = zone_distance_mm if zone_distance_mm is not None else 3.0 * ctx.grid.spacing
    fixed_boxes = _fem_probe.face_world_boxes(ctx.table, ctx.cond.fixed_faces)
    load_boxes = _fem_probe.face_world_boxes(ctx.table, ctx.cond.load_faces)
    zones4 = _fem_probe.zone_masks_with_load(mask, ctx.grid, fixed_world_boxes=fixed_boxes,
                                             load_world_boxes=load_boxes, distance_mm=dist)
    zone_stats = _zone_percentile_all(score.von_mises_mpa, zones4, allowable_mpa=ctx.cond.allowable_mpa)
    peak = _peak_locality(score.von_mises_mpa, mask, ctx)
    old_zone3 = score.zones or {}
    old_general_peak = None
    if old_zone3:
        gen_mask = old_zone3["general"]
        if gen_mask[tuple(peak["index"])]:
            old_general_peak = True
        elif old_zone3["holding"][tuple(peak["index"])]:
            old_general_peak = False
        else:
            old_general_peak = False
    return dict(
        label=label, status="ok", material_mm3=score.material_mm3,
        m7b_ks_mpa=score.m7b_ks_mpa, distance_mm_used=dist,
        zone_counts=zones4["counts"], zone_percentile=zone_stats,
        peak_von_mises_mpa=peak, peak_was_in_general_zone_under_3zone_definition=old_general_peak,
        peak_now_in_load_zone=bool(zones4["load"][tuple(peak["index"])]),
    )


def _mask_from_step20(record: dict) -> tuple[np.ndarray, dict]:
    path = REPO / record["step20_full"]["best_ever_mask_path"]
    with np.load(path) as z:
        return np.asarray(z["mask"], dtype=bool), record


def main() -> int:
    t0 = time.time()
    results = []

    # H1現行形 (run_06_main_matrixのstep20)
    main_ledger = _common.load_ledger(main_matrix.LEDGER_PATH)
    h1_record = next((r for r in main_ledger.get("cases", []) if r.get("case_id") == hls.BASELINE["tag"]), None)
    if h1_record is None or h1_record.get("status") != "ok":
        raise RuntimeError("run_06_main_matrix.py を先に完走させること")
    ctx_h1 = sm.build_context(hls.HANGER_MODEL, load_n=hls.BASELINE["load_n"],
                              element_size_mm=hls.BASELINE["element_size_mm"],
                              min_width_mm=hls.BASELINE["min_width_mm"])
    mask_h1, _ = _mask_from_step20(h1_record)
    results.append(_diagnose(mask_h1, ctx_h1, label="H1_step20_2.0mm"))

    # 保存形(03b・03Aが48.72MPaを出した現行形を含む4形状)。同じ格子(origin・spacing一致)なので素の投影で足りる。
    import refine_shape as rs
    for name in ("hanger_base", "hanger_r2", "hanger_step05", "hanger_transplant"):
        if name == "hanger_transplant":
            src_mask, src_origin, src_spacing = hls.load_transplant_native()
        else:
            cfg = rs._resolve_shape(name, "F0200_h2.0_w5")
            src_mask, src_origin, src_spacing = np.asarray(cfg["mask"], dtype=bool), np.asarray(cfg["origin"], dtype=float), float(cfg["spacing"])
        stored = hls.project_mask_raw(src_mask, src_origin, src_spacing, ctx_h1)
        results.append(_diagnose(stored, ctx_h1, label=f"H1_stored_{name}"))

    # 格子解像度スイープ
    grid_ledger = _common.load_ledger(grid_res.LEDGER_PATH)
    for size in hls.EXTRA_RESOLUTIONS_MM:
        tag = grid_res._case_id(size)
        record = next((r for r in grid_ledger.get("cases", []) if r.get("case_id") == tag), None)
        if record is None or record.get("status") != "ok":
            print(f"skip {tag}: run_06_grid_resolution.py 未完了")
            continue
        ctx = sm.build_context(hls.HANGER_MODEL, load_n=hls.BASELINE["load_n"], element_size_mm=size,
                               min_width_mm=hls.BASELINE["min_width_mm"])
        mask, _ = _mask_from_step20(record)
        results.append(_diagnose(mask, ctx, label=f"resolution_{size:g}mm"))

    # H2現行形(原因1・高荷重代表)。04では最終マスクを保存していないため新規に1回実行する。
    # 実行に十数分かかるので、既に完了した結果が出力にあれば再利用する(マスクは保存していない)。
    h2_label = "H2_step20_2.4mm_5000N"
    previous = json.loads(OUT_PATH.read_text(encoding="utf-8")).get("results", []) if OUT_PATH.exists() else []
    h2_prev = next((r for r in previous if r.get("label") == h2_label and r.get("status") == "ok"), None)
    if h2_prev is not None:
        results.append(h2_prev)
    else:
        print("H2 step20(新規実行、マスク保存のため) ...", flush=True)
        with _common.ExecutionLock(hls.LOCK, tag="analyze_06_load_zone_h2"):
            ctx_h2 = sm.build_context(hls.HANGER_MODEL, load_n=5000.0, element_size_mm=2.4, min_width_mm=None)
            h2_step20 = sm.run_fixed_step(ctx_h2, max_step_ratio=0.2)
        if h2_step20.best_ever_mask is not None:
            results.append(_diagnose(h2_step20.best_ever_mask, ctx_h2, label=h2_label))
        else:
            results.append(dict(label=h2_label, status=h2_step20.status))

    payload = dict(generated_at=_common.now_iso(), code_version=_common.git_state(), percentile=PERCENTILE,
                  results=results, wall_seconds_total=time.time() - t0,
                  note="zone_masks_with_load(_fem_probe.py追加)による4ゾーン診断。既存3ゾーン定義("
                       "zone_masks、02〜05が使用)は無変更。")
    _common.write_atomic(OUT_PATH, json.dumps(payload, ensure_ascii=False, indent=1, default=float) + "\n")
    print(f"wrote {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
