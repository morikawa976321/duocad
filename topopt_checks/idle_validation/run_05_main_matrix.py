"""タスク5・主要行列: 4方式(20%削除・5%削除・適応歩幅+復帰・再配置)を、dilution_small・
path_small(04a認定 2.0mm/3000N/6.0mm)それぞれについて 荷重/最小幅/解像度/初期形状 を振って比較する。

**baseline再利用(本スクリプトの`_cases()`は対象外、05_SYNTHETIC.md 1節の文脈説明としての引用)**:
dilution 300N/2.0mm/**4.0mm**幅(task4が既に実測)は`04_searches_compact.json`にあり、本スクリプトは
これを再実行しない(4.0mm/300Nの新規FEMは`run_05_ablation.py`側が行う。=05_SYNTHETIC.md 5節
アブレーションが使うbaselineと同じ4.0mm幅)。**本スクリプトが実際に新規実行するdilution baseline
(`dilution_base_w6`ケース、`_cases()`が定義する)は幅6.0mmで、task4の4.0mmとは別条件**(幅スイープの
起点をtask4と揃えず6.0mmにしたため——05_SYNTHETIC.md 1節参照)。path側は**04aが2.0mm解像度を
認定条件としている**(§04A後続への契約①)ため、
task4の2.5mm実測とは別条件として新規に2.0mm/6.0mm/3000Nを実行する(=本スクリプトの `path_baseline`
ケース)。

条件は `synthetic_matrix_support.py` の `DILUTION_BASELINE`/`PATH_BASELINE` を基点に、
load/width/resolutionを1軸ずつ振る(one-factor-at-a-time。01_PROTOCOL.md 3節「挟み込み比較」と
同じ考え方)。初期形状(warm-start)は別スクリプト `run_05_initshape.py` で扱う(adaptive/reallocationのみ、
本スクリプトが書く`res3.0_w6`ケースの最良形状マスクに依存する)。

再開: `output/idle_validation_20260928/05_main_matrix.json` を `_common` の条件台帳として使う
(`is_case_done`でスキップ)。単一FEM実行ロックは他task4/hanger_external_checkと共有する。

実行(小予算smoke test): `... run_05_main_matrix.py --smoke`
実行(本番): `... run_05_main_matrix.py`
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "topopt_checks"))
import _bootstrap  # noqa: E402,F401

import _common  # noqa: E402
import search_methods as sm  # noqa: E402
import synthetic_matrix_support as sup  # noqa: E402

OUT = sup.OUT
LEDGER_PATH = OUT / "05_main_matrix.json"


def _cases(smoke: bool) -> list[dict]:
    adaptive_evals = 20 if smoke else 100
    realloc_evals = 15 if smoke else 60
    grow_cycles = 1 if smoke else 3

    def case(cid, *, model, load_n, element_size_mm, min_width_mm, axis, cause):
        return dict(case_id=cid, model=model, load_n=load_n, element_size_mm=element_size_mm,
                   min_width_mm=min_width_mm, axis=axis, cause=cause, smoke=smoke,
                   adaptive_evals=adaptive_evals, realloc_evals=realloc_evals, grow_cycles=grow_cycles)

    d = sup.DILUTION_BASELINE
    p = sup.PATH_BASELINE
    cases = [
        # --- dilution: baseline(2.0mm/6.0mm, widthスイープと共有), load, width, resolution ---
        case("dilution_base_w6", model=d["model"], load_n=d["load_n"], element_size_mm=2.0, min_width_mm=6.0,
            axis="width_and_resolution_anchor", cause="dilution"),
        case("dilution_load200", model=d["model"], load_n=200.0, element_size_mm=2.0, min_width_mm=4.0,
            axis="load", cause="dilution"),
        case("dilution_load250", model=d["model"], load_n=250.0, element_size_mm=2.0, min_width_mm=4.0,
            axis="load", cause="dilution"),
        case("dilution_width8", model=d["model"], load_n=300.0, element_size_mm=2.0, min_width_mm=8.0,
            axis="width", cause="dilution"),
        case("dilution_res1.5_w6", model=d["model"], load_n=300.0, element_size_mm=1.5, min_width_mm=6.0,
            axis="resolution", cause="dilution"),
        case("dilution_res2.5_w6", model=d["model"], load_n=300.0, element_size_mm=2.5, min_width_mm=6.0,
            axis="resolution", cause="dilution"),
        case("dilution_res3.0_w6", model=d["model"], load_n=300.0, element_size_mm=3.0, min_width_mm=6.0,
            axis="resolution", cause="dilution"),
        # --- path: baseline(04a条件そのもの、task4の2.5mmとは別), load, width, resolution ---
        case("path_baseline_2.0mm", model=p["model"], load_n=p["load_n"], element_size_mm=2.0, min_width_mm=6.0,
            axis="baseline_04a_condition", cause="path"),
        case("path_load2700", model=p["model"], load_n=2700.0, element_size_mm=2.0, min_width_mm=6.0,
            axis="load", cause="path"),
        case("path_load3300", model=p["model"], load_n=3300.0, element_size_mm=2.0, min_width_mm=6.0,
            axis="load", cause="path"),
        case("path_width4", model=p["model"], load_n=3000.0, element_size_mm=2.0, min_width_mm=4.0,
            axis="width", cause="path"),
        case("path_width8", model=p["model"], load_n=3000.0, element_size_mm=2.0, min_width_mm=8.0,
            axis="width", cause="path"),
        case("path_res1.5_w6", model=p["model"], load_n=3000.0, element_size_mm=1.5, min_width_mm=6.0,
            axis="resolution", cause="path"),
        case("path_res2.5_w6", model=p["model"], load_n=3000.0, element_size_mm=2.5, min_width_mm=6.0,
            axis="resolution", cause="path"),
        case("path_res3.0_w6", model=p["model"], load_n=3000.0, element_size_mm=3.0, min_width_mm=6.0,
            axis="resolution", cause="path"),
    ]
    return cases


def run_case(spec: dict) -> dict:
    t0 = time.time()
    ctx = sm.build_context(spec["model"], load_n=spec["load_n"], element_size_mm=spec["element_size_mm"],
                           min_width_mm=spec["min_width_mm"])
    out = dict(case_id=spec["case_id"], axis=spec["axis"], cause=spec["cause"],
              model=str(spec["model"].relative_to(REPO)), model_sha256=_common.sha256_file(spec["model"]),
              load_n=spec["load_n"], element_size_mm=spec["element_size_mm"],
              element_size_resolved_mm=ctx.grid.spacing, min_width_mm_requested=spec["min_width_mm"],
              min_width_cells_resolved=ctx.min_width_cells,
              min_width_guaranteed_mm=ctx.min_width_cells * ctx.grid.spacing,
              design_elements=int(ctx.space.mask.sum()))
    tag = spec["case_id"]

    print(f"[{tag}] step20 ...", flush=True)
    step20 = sm.run_fixed_step(ctx, max_step_ratio=0.2)
    out["step20"] = sup.record_summary(step20)
    out["step20_full"] = step20.to_json(mask_dir=sup.MASK_DIR, tag=f"{tag}_step20")

    print(f"[{tag}] step05 ...", flush=True)
    step05 = sm.run_fixed_step(ctx, max_step_ratio=0.05)
    out["step05"] = sup.record_summary(step05)
    out["step05_full"] = step05.to_json(mask_dir=sup.MASK_DIR, tag=f"{tag}_step05")

    print(f"[{tag}] adaptive(revert, m5b_raw) ...", flush=True)
    adaptive_cfg = sup.revert_config(seed=42, max_evaluations=spec["adaptive_evals"])
    adaptive = sm.run_adaptive(ctx, adaptive_cfg,
                               checkpoint_path=OUT / f"05_{tag}_adaptive.checkpoint.json")
    out["adaptive"] = sup.record_summary(adaptive)
    out["adaptive_full"] = adaptive.to_json(mask_dir=sup.MASK_DIR, tag=f"{tag}_adaptive")
    out["adaptive_best_so_far_curve"] = sup.best_so_far_curve(adaptive)

    print(f"[{tag}] reallocation ...", flush=True)
    realloc_cfg = sup.reallocation_config(seed=43, max_evaluations=spec["realloc_evals"],
                                         grow_cycles=spec["grow_cycles"])
    realloc = sm.run_reallocation(ctx, realloc_cfg,
                                  checkpoint_path=OUT / f"05_{tag}_reallocation.checkpoint.json")
    out["reallocation"] = sup.record_summary(realloc)
    out["reallocation_full"] = realloc.to_json(mask_dir=sup.MASK_DIR, tag=f"{tag}_reallocation")
    out["reallocation_best_so_far_curve"] = sup.best_so_far_curve(realloc)

    out["status"] = "ok"
    out["smoke"] = bool(spec.get("smoke", False))
    out["wall_seconds_total"] = time.time() - t0
    out["files"] = {}
    return out


def _case_record(ledger: dict, case_id: str) -> Optional[dict]:
    for record in ledger.get("cases", []):
        if record.get("case_id") == case_id:
            return record
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--smoke", action="store_true", help="小さい評価予算で疎通確認だけ行う(結果は本番用に使わない)")
    ap.add_argument("--only", nargs="*", default=None, help="case_id を指定して絞り込む")
    ap.add_argument("--force", action="store_true",
                    help="既に完了済みでも再実行する(疎通確認〈--smoke〉の予算で書かれた記録を"
                        "本予算で上書きし直すときに使う。実測で踏んだ罠: `is_case_done` はstatus"
                        "だけを見るため、--smokeで書いた記録を--smoke無しで再実行しても`smoke=True`の"
                        "記録のままスキップされてしまう〈本タスクで実際に発生した〉)")
    args = ap.parse_args()

    sup.MASK_DIR.mkdir(parents=True, exist_ok=True)
    ledger = _common.load_ledger(LEDGER_PATH)
    cases = _cases(args.smoke)
    if args.only:
        cases = [c for c in cases if c["case_id"] in args.only]

    with _common.ExecutionLock(sup.LOCK, tag="run_05_main_matrix"):
        for spec in cases:
            existing = _case_record(ledger, spec["case_id"])
            already_done = (not args.smoke and _common.is_case_done(ledger, spec["case_id"])
                            and not (existing or {}).get("smoke", False))
            if already_done and not args.force:
                print(f"[{spec['case_id']}] skip(既に完了)")
                continue
            try:
                record = run_case(spec)
            except Exception as exc:  # noqa: BLE001
                record = dict(case_id=spec["case_id"], axis=spec["axis"], cause=spec["cause"],
                              status="error", error=repr(exc))
            _common.upsert_case(ledger, spec["case_id"], record)
            ledger["generated_at"] = _common.now_iso()
            ledger["code_version"] = _common.git_state()
            ledger["smoke"] = args.smoke
            _common.write_atomic(LEDGER_PATH, __import__("json").dumps(ledger, ensure_ascii=False, indent=1, default=float) + "\n")
            print(f"[{spec['case_id']}] done, wrote {LEDGER_PATH}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
