"""タスク6・現行形の再現: 200N・2.0mm・最小幅5セル(w5=実現9.99mm)で製品コード(step20)を新規に走らせ、
保存済みの現行形`hanger_base`(`01_stage1_shapes.json`。21評価・材料249,716mm³)と突き合わせる。

`run_06_main_matrix.py`(最小幅実寸10mm=6セル)のstep20は139,230mm³・253評価で、保存形と
形も体積も大きく違った。違いが**最小幅5セルと6セルの差**なのか**保存形を作った旧探索と現行コードの差**なのかを
切り分けるため、最小幅だけを保存形の条件(5セル)に戻す。5セルは`min_width_mm=9.98`(9.98/1.9974→ceil=5)。

再開: `output/idle_validation_20260928/06_w5_reproduction.json`。
実行: `.venv\Scripts\python.exe topopt_checks\idle_validation\run_06_w5_reproduction.py`
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
import refine_shape as rs  # noqa: E402
import search_methods as sm  # noqa: E402
import synthetic_matrix_support as sup  # noqa: E402

LEDGER_PATH = hls.OUT / "06_w5_reproduction.json"
CASE_ID = "hanger_h1_w5_reproduction"
MIN_WIDTH_MM_FOR_5_CELLS = 9.98


def run() -> dict:
    t0 = time.time()
    ctx = sm.build_context(hls.HANGER_MODEL, load_n=hls.BASELINE["load_n"],
                           element_size_mm=hls.BASELINE["element_size_mm"], min_width_mm=MIN_WIDTH_MM_FOR_5_CELLS)
    if ctx.min_width_cells != 5:
        raise RuntimeError(f"最小幅が5セルに解決されなかった: {ctx.min_width_cells}")
    out = dict(case_id=CASE_ID, min_width_cells_resolved=ctx.min_width_cells,
              min_width_guaranteed_mm=ctx.min_width_cells * ctx.grid.spacing,
              grid_origin_mm=list(ctx.grid.origin), grid_spacing_mm=float(ctx.grid.spacing),
              design_elements=int(ctx.space.mask.sum()))

    print(f"[{CASE_ID}] step20 ...", flush=True)
    with hls.capture_unknown_safety() as unknown:
        step20 = sm.run_fixed_step(ctx, max_step_ratio=0.2)
    out["step20"] = sup.record_summary(step20)
    out["step20_full"] = step20.to_json(mask_dir=hls.MASK_DIR, tag=CASE_ID)
    out["unknown_safety"] = hls.unknown_safety_summary(unknown)

    stored = rs._resolve_shape("hanger_base", "F0200_h2.0_w5")
    stored_mask = np.asarray(stored["mask"], dtype=bool)
    out["stored_hanger_base"] = dict(cells=int(stored_mask.sum()),
                                     material_mm3=float(stored_mask.sum() * ctx.grid.element_volume))
    mask = step20.best_ever_mask
    if mask is not None and mask.shape == stored_mask.shape:
        inter = int((mask & stored_mask).sum())
        out["comparison_with_stored"] = dict(
            same_mask=bool(np.array_equal(mask, stored_mask)), new_cells=int(mask.sum()), intersection_cells=inter,
            fraction_of_new_inside_stored=inter / max(int(mask.sum()), 1),
            fraction_of_stored_inside_new=inter / max(int(stored_mask.sum()), 1))
        truss_only = hls.truss_only_cells(ctx)
        out["truss_only_cells_total"] = int(truss_only.sum())
        out["step20_truss_residual_fraction"] = hls.truss_residual_fraction(mask, truss_only)
    out["status"] = "ok"
    out["wall_seconds_total"] = time.time() - t0
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    ledger = _common.load_ledger(LEDGER_PATH)
    if _common.is_case_done(ledger, CASE_ID) and not args.force:
        print(f"[{CASE_ID}] skip(既に完了)")
        return 0
    with _common.ExecutionLock(hls.LOCK, tag="run_06_w5_reproduction"):
        try:
            record = run()
        except Exception as exc:  # noqa: BLE001
            record = dict(case_id=CASE_ID, status="error", error=repr(exc))
        _common.upsert_case(ledger, CASE_ID, record)
        ledger["generated_at"] = _common.now_iso()
        ledger["code_version"] = _common.git_state()
        _common.write_atomic(LEDGER_PATH, json.dumps(ledger, ensure_ascii=False, indent=1, default=float) + "\n")
        print(f"wrote {LEDGER_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
