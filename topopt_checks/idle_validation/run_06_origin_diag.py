r"""タスク6・診断: 全軸+0.5セル原点シフトのstep20が`evaluation_error`で全材料の99.94%のまま打ち切られた原因を特定する。

`run_06_origin_shift.py`の`capture_unknown_safety`は安全率が`unknown`だった呼び出しを0件としか拾えなかった。
ここでは`acceptance.inspect`の全呼び出し(材料セル数・各検査のstatus・安全率のstatus/reason/error)と、
`binary_search.find_best_feasible`が返す`SearchResult`の`diagnostics`(段階4の`stop_reason`・
`final_region_recheck`・`evaluation_errors`)をそのまま残す。条件はrun_06_origin_shiftの全軸ケースと同一。

再開: `output/idle_validation_20260928/06_origin_diag.json`。
実行: `.venv\Scripts\python.exe topopt_checks\idle_validation\run_06_origin_diag.py`
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

from duocad.topopt import acceptance, binary_search  # noqa: E402

import _common  # noqa: E402
import hanger_low_support as hls  # noqa: E402
import synthetic_matrix_support as sup  # noqa: E402

LEDGER_PATH = hls.OUT / "06_origin_diag.json"
CASE_ID = "hanger_h1_origin_shift_all_axes_diag"


def run() -> dict:
    t0 = time.time()
    spacing = hls.BASELINE["element_size_mm"]
    ctx = sup.build_context_with_origin_shift(
        hls.HANGER_MODEL, load_n=hls.BASELINE["load_n"], element_size_mm=spacing,
        min_width_mm=hls.BASELINE["min_width_mm"], shift_mm=(0.5 * spacing,) * 3)
    calls: list[dict] = []
    original = acceptance.inspect

    def wrapped(material, before, grid, **kwargs):
        report = original(material, before, grid, **kwargs)
        checks = report.get("checks", {})
        safety = checks.get("safety", {})
        calls.append(dict(
            call=len(calls), cells=int(np.asarray(material).sum()), report_status=report.get("status"),
            check_status={k: v.get("status") for k, v in checks.items()},
            safety_factor=safety.get("safety_factor"), safety_reason=safety.get("reason"),
            safety_error=None if safety.get("error") is None else str(safety.get("error"))[:600]))
        return report

    region_calls: list[dict] = []
    original_region = acceptance.region_check

    def wrapped_region(material, grid, bodies, *, preserve, cache=None, overlaps=None):
        row = original_region(material, grid, bodies, preserve=preserve, cache=cache, overlaps=overlaps)
        region_calls.append(dict(
            call=len(region_calls), cells=int(np.asarray(material).sum()), preserve=preserve,
            precompute=overlaps is not None, status=row["status"],
            non_pass=[dict(body=b["body"], status=b["status"], error=b.get("error"),
                           body_volume_mm3=b.get("body_volume_mm3"), intersection_mm3=b.get("intersection_mm3"))
                      for b in row["bodies"] if b["status"] != "pass"]))
        return row

    acceptance.inspect = wrapped
    acceptance.region_check = wrapped_region
    try:
        result = binary_search.find_best_feasible(
            ctx.space.mask, ctx.cond, max_evaluations=2000, max_step_ratio=0.2, min_width_cells=ctx.min_width_cells)
    finally:
        acceptance.inspect = original
        acceptance.region_check = original_region
    reduction = (result.diagnostics or {}).get("reduction", {})
    return dict(
        case_id=CASE_ID, status="ok", result_status=result.status, termination=result.termination,
        evaluations=result.evaluations,
        best_cells=None if result.best_feasible is None else int(result.best_feasible.mask.sum()),
        stage4_stop_reason=reduction.get("stop_reason"), final_region_recheck=reduction.get("final_region_recheck"),
        evaluation_errors=reduction.get("evaluation_errors"), fem_solves=reduction.get("fem_solves"),
        geometry_rejections=len(reduction.get("geometry_rejections", [])),
        initial_diagnostics_keys=sorted((result.diagnostics or {}).get("initial", {}).keys()),
        inspect_calls=calls, region_check_calls=region_calls, wall_seconds_total=time.time() - t0)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    ledger = _common.load_ledger(LEDGER_PATH)
    if _common.is_case_done(ledger, CASE_ID) and not args.force:
        print(f"[{CASE_ID}] skip(既に完了)")
        return 0
    with _common.ExecutionLock(hls.LOCK, tag="run_06_origin_diag"):
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
