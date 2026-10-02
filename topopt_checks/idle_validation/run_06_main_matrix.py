"""タスク6・主要比較: H1条件(200N・2.0mm・min_width実寸10mm固定)で4方式
(20%削除・5%削除・適応歩幅+復帰・再配置)をsearch_methods.pyの同一インフラ・同一ctxで新規に実行する。

評価予算は適応50評価・再配置30評価x2サイクル(`run_06_long_budget.py`が大きい予算で再実行する)。
保存形hanger_baseの再現(最小幅5セル)は`run_06_w5_reproduction.py`。

再開: `output/idle_validation_20260928/06_main_matrix.json`を条件台帳として使う(`is_case_done`でスキップ)。
実行: `.venv\Scripts\python.exe topopt_checks\idle_validation\run_06_main_matrix.py`
"""
from __future__ import annotations

import argparse
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

LEDGER_PATH = hls.OUT / "06_main_matrix.json"
ADAPTIVE_EVALS = 50
REALLOC_EVALS = 30
REALLOC_GROW_CYCLES = 2


def run() -> dict:
    spec = hls.BASELINE
    t0 = time.time()
    ctx = sm.build_context(spec["model"], load_n=spec["load_n"], element_size_mm=spec["element_size_mm"],
                           min_width_mm=spec["min_width_mm"])
    out = dict(case_id=spec["tag"], cause=spec["cause"], model=str(spec["model"].relative_to(REPO)),
              model_sha256=_common.sha256_file(spec["model"]), load_n=spec["load_n"],
              element_size_mm=spec["element_size_mm"], element_size_resolved_mm=ctx.grid.spacing,
              min_width_mm_requested=spec["min_width_mm"], min_width_cells_resolved=ctx.min_width_cells,
              min_width_guaranteed_mm=ctx.min_width_cells * ctx.grid.spacing,
              grid_origin_mm=list(ctx.grid.origin), grid_spacing_mm=float(ctx.grid.spacing),
              design_elements=int(ctx.space.mask.sum()))
    tag = spec["tag"]
    hls.MASK_DIR.mkdir(parents=True, exist_ok=True)

    print(f"[{tag}] step20 ...", flush=True)
    step20 = sm.run_fixed_step(ctx, max_step_ratio=0.2)
    out["step20"] = sup.record_summary(step20)
    out["step20_full"] = step20.to_json(mask_dir=hls.MASK_DIR, tag=f"{tag}_step20")

    print(f"[{tag}] step05 ...", flush=True)
    step05 = sm.run_fixed_step(ctx, max_step_ratio=0.05)
    out["step05"] = sup.record_summary(step05)
    out["step05_full"] = step05.to_json(mask_dir=hls.MASK_DIR, tag=f"{tag}_step05")

    print(f"[{tag}] adaptive(revert, m5b_raw, full-material start) ...", flush=True)
    adaptive_cfg = sup.revert_config(seed=42, max_evaluations=ADAPTIVE_EVALS)
    adaptive = sm.run_adaptive(ctx, adaptive_cfg,
                               checkpoint_path=hls.OUT / f"06_{tag}_adaptive.checkpoint.json")
    out["adaptive"] = sup.record_summary(adaptive)
    out["adaptive_full"] = adaptive.to_json(mask_dir=hls.MASK_DIR, tag=f"{tag}_adaptive")
    out["adaptive_best_so_far_curve"] = sup.best_so_far_curve(adaptive)

    print(f"[{tag}] reallocation(full-material start, 縮小予算) ...", flush=True)
    realloc_cfg = sup.reallocation_config(seed=43, max_evaluations=REALLOC_EVALS, grow_cycles=REALLOC_GROW_CYCLES)
    realloc = sm.run_reallocation(ctx, realloc_cfg,
                                  checkpoint_path=hls.OUT / f"06_{tag}_reallocation.checkpoint.json")
    out["reallocation"] = sup.record_summary(realloc)
    out["reallocation_full"] = realloc.to_json(mask_dir=hls.MASK_DIR, tag=f"{tag}_reallocation")
    out["reallocation_best_so_far_curve"] = sup.best_so_far_curve(realloc)

    # 部材復元の定量化(§完了条件「部材復元の証拠」): 2.2mmトラス専有セル(斜めの部材)の残存率
    truss_only = hls.truss_only_cells(ctx)
    out["truss_only_cells_total"] = int(truss_only.sum())
    for method_key, record in (("step20", step20), ("step05", step05), ("adaptive", adaptive), ("reallocation", realloc)):
        mask = record.best_ever_mask
        out[f"{method_key}_truss_residual_fraction"] = (
            hls.truss_residual_fraction(mask, truss_only) if mask is not None else None
        )

    out["adaptive_config"] = dict(max_evaluations=ADAPTIVE_EVALS, note="hanger規模のためtask05(100)より縮小")
    out["reallocation_config"] = dict(max_evaluations=REALLOC_EVALS, grow_cycles=REALLOC_GROW_CYCLES,
                                      note="hanger規模のためtask05(60・3)より縮小")
    out["status"] = "ok"
    out["wall_seconds_total"] = time.time() - t0
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    ledger = _common.load_ledger(LEDGER_PATH)
    already_done = _common.is_case_done(ledger, hls.BASELINE["tag"])
    if already_done and not args.force:
        print(f"[{hls.BASELINE['tag']}] skip(既に完了)")
        return 0

    with _common.ExecutionLock(hls.LOCK, tag="run_06_main_matrix"):
        try:
            record = run()
        except Exception as exc:  # noqa: BLE001
            record = dict(case_id=hls.BASELINE["tag"], status="error", error=repr(exc))
        _common.upsert_case(ledger, hls.BASELINE["tag"], record)
        ledger["generated_at"] = _common.now_iso()
        ledger["code_version"] = _common.git_state()
        _common.write_atomic(LEDGER_PATH, json.dumps(ledger, ensure_ascii=False, indent=1, default=float) + "\n")
        print(f"wrote {LEDGER_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
