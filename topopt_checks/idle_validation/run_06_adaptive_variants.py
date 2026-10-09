r"""タスク6・適応方式の追加調整版: H1条件(2.0mm・200N・min_width実寸10mm)の全材料開始で、
`06_main_matrix.json`の凍結版(`prediction_error_multiplier=2.5`)が材料180万mm³(全材料510万の35%)・
安全率84のまま50評価を使い切った原因を切り分ける。

凍結版のステップ記録では、評価5以降の拒否はすべて`prediction_error`(実際の変化が予測の2.5倍超)で、
実際の変化量は相対0.3%〜1e-6まで小さくなっても拒否され続けた(`06_HANGER_LOW.md`6節)。
ここでは`prediction_error_multiplier`を実質無効(1e6)にした版を、ゲート2種で走らせる:
- `m5b_raw`: 凍結版と**ゲートは同じ**でこの1パラメータだけ変える(停滞の原因の切り分け)
- `m5b_target`: 方式1・2(製品の安全率4.8)と**同じ安全余裕**で体積を比べられる版
  (`AdaptiveConfig`のdocstring。`m5b_raw`は材料の許容応力までしか守らないので体積の比較には使えない)

**凍結版の主成績とは混ぜない**(共通指示書「凍結版と追加調整版を分ける」)。評価予算は凍結版の50より
多い80(凍結版は停滞で打ち切られたため、無効化後に仕上げ局面まで届くかを見る)。

再開: `output/idle_validation_20260928/06_adaptive_variants.json`。
実行: `.venv\Scripts\python.exe topopt_checks\idle_validation\run_06_adaptive_variants.py`
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

LEDGER_PATH = hls.OUT / "06_adaptive_variants.json"
MAX_EVALUATIONS = 80
PREDICTION_ERROR_DISABLED = 1e6
VARIANTS = {
    "hanger_h1_adaptive_nopredcheck_m5b_raw": "m5b_raw",
    "hanger_h1_adaptive_nopredcheck_m5b_target": "m5b_target",
}


def run_case(case_id: str) -> dict:
    t0 = time.time()
    gate = VARIANTS[case_id]
    ctx = sm.build_context(hls.HANGER_MODEL, load_n=hls.BASELINE["load_n"],
                           element_size_mm=hls.BASELINE["element_size_mm"], min_width_mm=hls.BASELINE["min_width_mm"])
    cfg = dataclasses.replace(sup.revert_config(seed=42, max_evaluations=MAX_EVALUATIONS, gate=gate),
                              prediction_error_multiplier=PREDICTION_ERROR_DISABLED)
    print(f"[{case_id}] adaptive(full-material start, prediction_error無効, gate={gate}) ...", flush=True)
    rec = sm.run_adaptive(ctx, cfg, checkpoint_path=hls.OUT / f"06_{case_id}.checkpoint.json")
    out = dict(case_id=case_id, gate=gate, prediction_error_multiplier=PREDICTION_ERROR_DISABLED,
              max_evaluations=MAX_EVALUATIONS, design_elements=int(ctx.space.mask.sum()),
              adaptive=sup.record_summary(rec), adaptive_full=rec.to_json(mask_dir=hls.MASK_DIR, tag=case_id),
              adaptive_best_so_far_curve=sup.best_so_far_curve(rec, gate=gate))
    truss_only = hls.truss_only_cells(ctx)
    out["truss_only_cells_total"] = int(truss_only.sum())
    out["adaptive_truss_residual_fraction"] = (
        hls.truss_residual_fraction(rec.best_ever_mask, truss_only) if rec.best_ever_mask is not None else None)
    out["status"] = "ok"
    out["wall_seconds_total"] = time.time() - t0
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", choices=list(VARIANTS), default=None)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    ledger = _common.load_ledger(LEDGER_PATH)
    with _common.ExecutionLock(hls.LOCK, tag="run_06_adaptive_variants"):
        for case_id in args.only or list(VARIANTS):
            if _common.is_case_done(ledger, case_id) and not args.force:
                print(f"[{case_id}] skip(既に完了)")
                continue
            try:
                record = run_case(case_id)
            except Exception as exc:  # noqa: BLE001
                record = dict(case_id=case_id, status="error", error=repr(exc))
            _common.upsert_case(ledger, case_id, record)
            ledger["generated_at"] = _common.now_iso()
            ledger["code_version"] = _common.git_state()
            _common.write_atomic(LEDGER_PATH, json.dumps(ledger, ensure_ascii=False, indent=1, default=float) + "\n")
            print(f"[{case_id}] wrote {LEDGER_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
