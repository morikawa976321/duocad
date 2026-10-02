"""タスク6・初期形状(warm-start): H1条件(2.0mm・200N・min_width実寸10mm)で、適応方式
(`run_adaptive_from_mask`)を2つの初期形状から始める。「全材料開始」は`run_06_main_matrix.py`の
adaptive(`run_adaptive`=`find_initial_feasible`からの全材料開始)が兼ねるのでここでは扱わない。

- `transplant`: 2.2mm由来の移植トラス(`hanger_transplant`、既にSF8.87の優越形)から開始。
  05_SYNTHETIC.md 8節(小型pathで「安全な初期形状から始めても適応方式が目標未達まで削り込む」)を
  hanger規模で確認する。
- `coarse_skeleton`: `run_06_grid_resolution.py`の2.4mm step20結果を2.0mm格子へ投影して開始
  (「粗い骨組み開始」)。**このスクリプトの前に`run_06_grid_resolution.py`を完走させること**
  (2.4mmケースの出力に依存)。

評価予算はhanger規模のため40評価(小型05のwarm-start確認は60評価)。

再開: `output/idle_validation_20260928/06_initshape.json`。
実行: `.venv\\Scripts\\python.exe topopt_checks\\idle_validation\\run_06_initshape.py [--only transplant coarse_skeleton]`
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "topopt_checks"))
import _bootstrap  # noqa: E402,F401

import _common  # noqa: E402
import hanger_low_support as hls  # noqa: E402
import run_06_grid_resolution as grid_res  # noqa: E402
import search_methods as sm  # noqa: E402
import synthetic_matrix_support as sup  # noqa: E402

LEDGER_PATH = hls.OUT / "06_initshape.json"
MAX_EVALUATIONS = 40


def _build_baseline_ctx() -> sm.SearchContext:
    return sm.build_context(hls.HANGER_MODEL, load_n=hls.BASELINE["load_n"],
                            element_size_mm=hls.BASELINE["element_size_mm"],
                            min_width_mm=hls.BASELINE["min_width_mm"])


def _transplant_start(ctx: sm.SearchContext) -> tuple[np.ndarray, dict]:
    native_mask, native_origin, native_spacing = hls.load_transplant_native()
    projected = hls.project_mask_to_context(native_mask, native_origin, native_spacing, ctx)
    meta = dict(source="hanger_transplant(2.2mm由来、01_stage1_shapes.json)投影後",
               projected_material_mm3=float(projected.sum() * ctx.grid.element_volume))
    return projected, meta


def _coarse_skeleton_start(ctx: sm.SearchContext) -> tuple[np.ndarray, dict]:
    ledger = _common.load_ledger(grid_res.LEDGER_PATH)
    tag = grid_res._case_id(hls.COARSE_SOURCE_RESOLUTION_MM)
    record = next((r for r in ledger.get("cases", []) if r.get("case_id") == tag), None)
    if record is None or record.get("status") != "ok":
        raise RuntimeError(f"{grid_res.LEDGER_PATH} に完了済み {tag} が無い。先にrun_06_grid_resolution.pyを実行すること")
    mask_path = REPO / record["step20_full"]["best_ever_mask_path"]
    with np.load(mask_path) as z:
        src_mask = np.asarray(z["mask"], dtype=bool)
    src_origin = np.asarray(record["grid_origin_mm"], dtype=float)
    src_spacing = float(record["grid_spacing_mm"])
    meta = dict(source=f"{tag}のstep20結果を投影")
    try:
        projected = hls.project_mask_to_context(src_mask, src_origin, src_spacing, ctx)
    except RuntimeError as exc:
        raw = hls.project_mask_raw(src_mask, src_origin, src_spacing, ctx)
        projected, repair = hls.repair_projection(raw, ctx)
        meta["projection_failed"] = str(exc)[:400]
        meta["repair"] = repair
    meta["projected_material_mm3"] = float(projected.sum() * ctx.grid.element_volume)
    return projected, meta


STARTERS = {"transplant": _transplant_start, "coarse_skeleton": _coarse_skeleton_start}


def run_case(name: str) -> dict:
    t0 = time.time()
    ctx = _build_baseline_ctx()
    start_mask, meta = STARTERS[name](ctx)
    out = dict(case_id=f"hanger_h1_initshape_{name}", initial_shape=meta,
              design_elements=int(ctx.space.mask.sum()))

    truss_only = hls.truss_only_cells(ctx)
    out["truss_only_cells_total"] = int(truss_only.sum())
    out["initial_truss_residual_fraction"] = hls.truss_residual_fraction(start_mask, truss_only)

    print(f"[{name}] adaptive_from_mask ...", flush=True)
    cfg = sup.revert_config(seed=44, max_evaluations=MAX_EVALUATIONS)
    rec = sm.run_adaptive_from_mask(ctx, start_mask, cfg,
                                    checkpoint_path=hls.OUT / f"06_initshape_{name}_adaptive.checkpoint.json")
    out["adaptive"] = sup.record_summary(rec)
    out["adaptive_full"] = rec.to_json(mask_dir=hls.MASK_DIR, tag=f"hanger_initshape_{name}")
    out["final_truss_residual_fraction"] = (
        hls.truss_residual_fraction(rec.best_ever_mask, truss_only) if rec.best_ever_mask is not None else None
    )
    out["config"] = dict(max_evaluations=MAX_EVALUATIONS, note="hanger規模のためtask05(60)より縮小")
    out["status"] = "ok"
    out["wall_seconds_total"] = time.time() - t0
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", choices=list(STARTERS), default=None)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    names = args.only or list(STARTERS)

    ledger = _common.load_ledger(LEDGER_PATH)
    with _common.ExecutionLock(hls.LOCK, tag="run_06_initshape"):
        for name in names:
            case_id = f"hanger_h1_initshape_{name}"
            if _common.is_case_done(ledger, case_id) and not args.force:
                print(f"[{case_id}] skip(既に完了)")
                continue
            try:
                record = run_case(name)
            except Exception as exc:  # noqa: BLE001
                record = dict(case_id=case_id, status="error", error=repr(exc))
            _common.upsert_case(ledger, case_id, record)
            ledger["generated_at"] = _common.now_iso()
            ledger["code_version"] = _common.git_state()
            _common.write_atomic(LEDGER_PATH, json.dumps(ledger, ensure_ascii=False, indent=1, default=float) + "\n")
            print(f"[{case_id}] done, wrote {LEDGER_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
