"""タスク6・評価ごとの軌跡: 200N・2.0mm・最小幅5セル(w5)の製品step20(21評価)で、`acceptance.inspect`が
呼ばれるたびに(評価順)材料セル数・安全率・最大変位・2.2mmトラス専有セルの残存率を記録する。

TOPOPT_IDLE_MATERIAL.md 2-1節の評価ごとの表(旧`measure_idle_counterfactual.py`)を、06と同じ現行コードで
再取得し、「評価6〜13で斜めの部材がいつ・どこまで失われたか」を体積・変位と対にして残す。
`run_06_w5_reproduction.py`と同一条件なので、最終形が保存形hanger_baseと一致すること(31,337セル)も再確認する。

再開: `output/idle_validation_20260928/06_w5_trajectory.json`。
実行: `.venv\Scripts\python.exe topopt_checks\idle_validation\run_06_w5_trajectory.py`
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

from duocad.topopt import acceptance  # noqa: E402

import _common  # noqa: E402
import hanger_low_support as hls  # noqa: E402
import refine_shape as rs  # noqa: E402
import search_methods as sm  # noqa: E402

LEDGER_PATH = hls.OUT / "06_w5_trajectory.json"
CASE_ID = "hanger_h1_w5_trajectory"
MIN_WIDTH_MM_FOR_5_CELLS = 9.98


def run() -> dict:
    t0 = time.time()
    ctx = sm.build_context(hls.HANGER_MODEL, load_n=hls.BASELINE["load_n"],
                           element_size_mm=hls.BASELINE["element_size_mm"], min_width_mm=MIN_WIDTH_MM_FOR_5_CELLS)
    if ctx.min_width_cells != 5:
        raise RuntimeError(f"最小幅が5セルに解決されなかった: {ctx.min_width_cells}")
    truss_only = hls.truss_only_cells(ctx)
    volume = float(ctx.grid.element_volume)
    calls: list[dict] = []
    original = acceptance.inspect

    def wrapped(material, before, grid, **kwargs):
        report = original(material, before, grid, **kwargs)
        safety = report.get("checks", {}).get("safety", {})
        mask = np.asarray(material, dtype=bool)
        calls.append(dict(
            call=len(calls), cells=int(mask.sum()), material_mm3=float(mask.sum()) * volume,
            truss_residual_fraction=hls.truss_residual_fraction(mask, truss_only),
            safety_status=safety.get("status"), safety_factor=safety.get("safety_factor"),
            max_displacement_mm=safety.get("max_displacement_mm"), report_status=report.get("status")))
        return report

    acceptance.inspect = wrapped
    try:
        step20 = sm.run_fixed_step(ctx, max_step_ratio=0.2)
    finally:
        acceptance.inspect = original

    stored = np.asarray(rs._resolve_shape("hanger_base", "F0200_h2.0_w5")["mask"], dtype=bool)
    final = step20.best_ever_mask
    return dict(
        case_id=CASE_ID, min_width_cells_resolved=ctx.min_width_cells, truss_only_cells_total=int(truss_only.sum()),
        stop_reason=step20.stop_reason, evaluations=step20.evaluations,
        final_cells=None if final is None else int(final.sum()),
        final_equals_stored_hanger_base=None if final is None else bool(np.array_equal(final, stored)),
        inspect_calls=calls, status="ok", wall_seconds_total=time.time() - t0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    ledger = _common.load_ledger(LEDGER_PATH)
    if _common.is_case_done(ledger, CASE_ID) and not args.force:
        print(f"[{CASE_ID}] skip(既に完了)")
        return 0
    with _common.ExecutionLock(hls.LOCK, tag="run_06_w5_trajectory"):
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
