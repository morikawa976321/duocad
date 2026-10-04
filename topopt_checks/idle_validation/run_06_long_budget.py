"""タスク6・十分な探索後の到達点: 適応・再配置を、縮小予算(50評価/30評価x2)ではなく大きい予算で全材料開始から走らせる。

`run_06_main_matrix.py`の適応(50評価)・再配置(30x2)は評価上限で止まり未収束だった。同じ設定(凍結版)を
300評価・再配置は05と同じ60x3まで広げ、停止理由が評価上限のままか、停滞で止まるかを見る。
`run_06_adaptive_variants.py`のうち、目標SFを守るゲート(`m5b_target`)の版も300評価まで広げる。
新規実行(再開しない)。短い予算の結果(06_main_matrix.json・06_adaptive_variants.json)とは別の台帳に残す。

再開: `output/idle_validation_20260928/06_long_budget.json`(ケース単位で`is_case_done`)。
実行: `.venv\Scripts\python.exe topopt_checks\idle_validation\run_06_long_budget.py [--only <case_id>...] [--force]`
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "topopt_checks"))
import _bootstrap  # noqa: E402,F401

import _common  # noqa: E402
import hanger_low_support as hls  # noqa: E402
import search_methods as sm  # noqa: E402
import synthetic_matrix_support as sup  # noqa: E402

LEDGER_PATH = hls.OUT / "06_long_budget.json"
ADAPTIVE_EVALS = 300
REALLOC_EVALS = 60
REALLOC_GROW_CYCLES = 3
PREDICTION_ERROR_DISABLED = 1e6
CASES = (
    "hanger_h1_long_adaptive_nopredcheck_m5b_target",
    "hanger_h1_long_adaptive_frozen",
    "hanger_h1_long_reallocation_frozen",
)


def run_case(case_id: str) -> dict:
    t0 = time.time()
    ctx = sm.build_context(hls.HANGER_MODEL, load_n=hls.BASELINE["load_n"],
                           element_size_mm=hls.BASELINE["element_size_mm"], min_width_mm=hls.BASELINE["min_width_mm"])
    checkpoint = hls.OUT / f"06_{case_id}.checkpoint.json"
    out = dict(case_id=case_id, design_elements=int(ctx.space.mask.sum()))
    if case_id == "hanger_h1_long_adaptive_nopredcheck_m5b_target":
        cfg = dataclasses.replace(sup.revert_config(seed=42, max_evaluations=ADAPTIVE_EVALS, gate="m5b_target"),
                                  prediction_error_multiplier=PREDICTION_ERROR_DISABLED)
        rec = sm.run_adaptive(ctx, cfg, checkpoint_path=checkpoint)
        out.update(gate="m5b_target", prediction_error_multiplier=PREDICTION_ERROR_DISABLED,
                   max_evaluations=ADAPTIVE_EVALS, method_key="adaptive")
        curve_gate = "m5b_target"
    elif case_id == "hanger_h1_long_adaptive_frozen":
        rec = sm.run_adaptive(ctx, sup.revert_config(seed=42, max_evaluations=ADAPTIVE_EVALS), checkpoint_path=checkpoint)
        out.update(gate="m5b_raw", prediction_error_multiplier=2.5, max_evaluations=ADAPTIVE_EVALS, method_key="adaptive")
        curve_gate = "m5b_raw"
    else:
        cfg = sup.reallocation_config(seed=43, max_evaluations=REALLOC_EVALS, grow_cycles=REALLOC_GROW_CYCLES)
        rec = sm.run_reallocation(ctx, cfg, checkpoint_path=checkpoint)
        out.update(gate="m5b_raw", prediction_error_multiplier=2.5, max_evaluations=REALLOC_EVALS,
                   grow_cycles=REALLOC_GROW_CYCLES, method_key="reallocation")
        curve_gate = "m5b_raw"
    key = out["method_key"]
    out[key] = sup.record_summary(rec)
    out[f"{key}_full"] = rec.to_json(mask_dir=hls.MASK_DIR, tag=case_id)
    out[f"{key}_best_so_far_curve"] = sup.best_so_far_curve(rec, gate=curve_gate)
    truss_only = hls.truss_only_cells(ctx)
    out["truss_only_cells_total"] = int(truss_only.sum())
    out[f"{key}_truss_residual_fraction"] = (
        hls.truss_residual_fraction(rec.best_ever_mask, truss_only) if rec.best_ever_mask is not None else None)
    out["status"] = "ok"
    out["wall_seconds_total"] = time.time() - t0
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", choices=list(CASES), default=None)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    ledger = _common.load_ledger(LEDGER_PATH)
    with _common.ExecutionLock(hls.LOCK, tag="run_06_long_budget"):
        for case_id in args.only or list(CASES):
            if _common.is_case_done(ledger, case_id) and not args.force:
                print(f"[{case_id}] skip(既に完了)")
                continue
            print(f"[{case_id}] start", flush=True)
            try:
                record = run_case(case_id)
            except Exception as exc:  # noqa: BLE001
                record = dict(case_id=case_id, status="error", error=repr(exc))
            _common.upsert_case(ledger, case_id, record)
            ledger["generated_at"] = _common.now_iso()
            ledger["code_version"] = _common.git_state()
            _common.write_atomic(LEDGER_PATH, json.dumps(ledger, ensure_ascii=False, indent=1, default=float) + "\n")
            print(f"[{case_id}] wrote {LEDGER_PATH}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
