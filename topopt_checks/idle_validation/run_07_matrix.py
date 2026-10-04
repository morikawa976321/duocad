"""タスク7・hanger高荷重(500/1000/2000N)x 細い/太い最小幅 x 格子 x 4方式 + 原点移動 + 初期形状。

条件は実寸mm固定の最小幅(細い=5mm・太い=10mm。実現幅は格子ごとに3〜6セル分で変わる〈`hanger_loads_support.WIDTHS_MM`〉)。
旧64条件(セル数固定)とは別表。06の200N・太い幅(`06_main_matrix.json`・`06_grid_resolution.json`)と同じ手順・同じコード。

ステージ(`--stage`)と台帳 `output/idle_validation_20260928/07_<stage>.json`:
- grid   : 製品step20(20%歩幅)を 荷重{500,1000,2000}x幅{thin,thick}x格子{1.8,2.0,2.2,2.4}mm と、200N細い幅x4格子。
- methods: 格子2.0mm・荷重x幅6条件で step05 / 適応(凍結) / 再配置(凍結) / 適応(prediction_error拒否なし・gate=m5b_target)。
- origin : 荷重{500,2000}x幅{thin,thick}・2.0mmで原点を全軸/x/y/z +0.5セル移動して step20。
- init   : 荷重{500,2000}x幅{thin,thick}・2.0mmで、粗い骨組み(2.4mmのstep20結果を投影)開始と、自身のstep20結果開始の適応。

**各ケースは新しいプロセスで走る**(ケース単位のピークメモリを分離し、1ケースの異常終了が他に波及しないようにする)。
親(`--stage`)は子(`--case`)を順に起動し、子が台帳へ書かずに落ちたら `status=error` を台帳に残す。台帳に `error` が1件でも
あれば終了コード2(`LEDGER_ERRORS` を出力)。**終了コード0でも台帳の `error`/`skip` を必ず確認する**。

再開: ケース単位(`is_case_done`)。適応(凍結/拒否なし)は途中の `07_<case>.checkpoint.json` があれば続きから再開する
(再開した場合は記録に `resumed_from_checkpoint=true`。校正係数などは再開で初期化されるので連続実行と同一ではない)。
実行: `.venv\\Scripts\\python.exe topopt_checks\\idle_validation\\run_07_matrix.py --stage grid [--only <case_id>...] [--force]`
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "topopt_checks"))
import _bootstrap  # noqa: E402,F401

import _common  # noqa: E402
import hanger_loads_support as hl  # noqa: E402
import hanger_low_support as hls  # noqa: E402
import search_methods as sm  # noqa: E402
import synthetic_matrix_support as sup  # noqa: E402

STAGES = ("grid", "methods", "origin", "init")
ADAPTIVE_EVALS = 300
REALLOC_EVALS = 60
REALLOC_GROW_CYCLES = 3
INIT_EVALS = 100
PREDICTION_ERROR_DISABLED = 1e6
SHIFTS = {"all": (0.5, 0.5, 0.5), "x": (0.5, 0.0, 0.0), "y": (0.0, 0.5, 0.0), "z": (0.0, 0.0, 0.5)}
ROBUST_CONDITIONS = ((2000.0, "thick"), (2000.0, "thin"), (500.0, "thick"), (500.0, "thin"))
METHOD_ORDER = ((2000.0, "thick"), (2000.0, "thin"), (500.0, "thick"), (500.0, "thin"), (1000.0, "thick"), (1000.0, "thin"))


def ledger_path(stage: str) -> Path:
    return hl.OUT / f"07_{stage}.json"


def _job(stage, kind, load, width, size, **extra) -> dict:
    tag = hl.case_tag(load, width, size)
    suffix = extra.pop("suffix", "")
    return dict(case_id=f"{tag}_{kind}{suffix}", stage=stage, kind=kind, load_n=load, width_key=width,
                size_mm=size, **extra)


def jobs(stage: str) -> list[dict]:
    out = []
    if stage == "grid":
        for size in (2.0, 2.4, 2.2, 1.8):
            for load, width in METHOD_ORDER:
                out.append(_job(stage, "step20", load, width, size))
        for size in (2.0, 2.4, 2.2, 1.8):
            out.append(_job(stage, "step20", 200.0, "thin", size))
    elif stage == "methods":
        for load, width in METHOD_ORDER:
            for kind in ("step05", "adaptive_frozen", "reallocation_frozen", "adaptive_nopred_m5b_target"):
                out.append(_job(stage, kind, load, width, 2.0))
    elif stage == "origin":
        for load, width in ROBUST_CONDITIONS:
            for name, shift in SHIFTS.items():
                out.append(_job(stage, "step20", load, width, 2.0, suffix=f"_shift_{name}_+0.5cell", shift_cells=list(shift)))
    elif stage == "init":
        for load, width in ROBUST_CONDITIONS:
            out.append(_job(stage, "init_coarse_skeleton", load, width, 2.0))
            out.append(_job(stage, "init_own_step20", load, width, 2.0))
    else:
        raise ValueError(stage)
    return out


def _load_step20_mask(load: float, width: str, size: float) -> tuple[np.ndarray, dict]:
    grid_ledger = _common.load_ledger(ledger_path("grid"))
    case_id = f"{hl.case_tag(load, width, size)}_step20"
    rec = next((r for r in grid_ledger.get("cases", []) if r.get("case_id") == case_id), None)
    if rec is None or rec.get("status") != "ok" or not rec.get("search_full", {}).get("best_ever_mask_path"):
        raise RuntimeError(f"{ledger_path('grid')} に完了済み {case_id} が無い(先に --stage grid を完走させる)")
    with np.load(REPO / rec["search_full"]["best_ever_mask_path"]) as z:
        return np.asarray(z["mask"], dtype=bool), rec


def run_case(job: dict) -> dict:
    t0 = time.time()
    load, width, size, kind = job["load_n"], job["width_key"], job["size_mm"], job["kind"]
    shift = job.get("shift_cells")
    ctx = hl.build_ctx(load, width, size, shift_cells=shift)
    case_id = job["case_id"]
    out = dict(case_id=case_id, stage=job["stage"], kind=kind, load_n=load, width_key=width,
              min_width_mm_requested=hl.WIDTHS_MM[width], element_size_mm=size,
              element_size_resolved_mm=float(ctx.grid.spacing), min_width_cells_resolved=int(ctx.min_width_cells),
              min_width_realized_mm=float(ctx.min_width_cells * ctx.grid.spacing),
              grid_origin_mm=[float(o) for o in ctx.grid.origin], origin_shift_cells=list(shift) if shift else [0.0, 0.0, 0.0],
              design_elements=int(ctx.space.mask.sum()))
    mask_tag = case_id.replace("+", "p")
    checkpoint = hl.OUT / f"07_{case_id}.checkpoint.json"
    key = "step20"
    gate_for_curve = "m5b_raw"

    if kind in ("step20", "step05"):
        with hls.capture_unknown_safety() as unknown:
            rec = sm.run_fixed_step(ctx, max_step_ratio=0.2 if kind == "step20" else 0.05)
        out["unknown_safety"] = hls.unknown_safety_summary(unknown)
        key = kind
    elif kind in ("adaptive_frozen", "adaptive_nopred_m5b_target"):
        if kind == "adaptive_frozen":
            cfg = sup.revert_config(seed=42, max_evaluations=ADAPTIVE_EVALS)
            out.update(gate="m5b_raw", prediction_error_multiplier=2.5)
        else:
            cfg = dataclasses.replace(sup.revert_config(seed=42, max_evaluations=ADAPTIVE_EVALS, gate="m5b_target"),
                                      prediction_error_multiplier=PREDICTION_ERROR_DISABLED)
            out.update(gate="m5b_target", prediction_error_multiplier=PREDICTION_ERROR_DISABLED)
            gate_for_curve = "m5b_target"
        resume = None
        if checkpoint.is_file():
            resume = sm.load_checkpoint(checkpoint)
            out["resumed_from_checkpoint"] = True
        with hls.capture_unknown_safety() as unknown:
            rec = sm.run_adaptive(ctx, cfg, checkpoint_path=checkpoint, resume_from=resume)
        out["unknown_safety"] = hls.unknown_safety_summary(unknown)
        out["max_evaluations"] = ADAPTIVE_EVALS
        key = "adaptive"
    elif kind == "reallocation_frozen":
        cfg = sup.reallocation_config(seed=43, max_evaluations=REALLOC_EVALS, grow_cycles=REALLOC_GROW_CYCLES)
        with hls.capture_unknown_safety() as unknown:
            rec = sm.run_reallocation(ctx, cfg, checkpoint_path=checkpoint)
        out["unknown_safety"] = hls.unknown_safety_summary(unknown)
        out.update(gate="m5b_raw", prediction_error_multiplier=2.5, max_evaluations=REALLOC_EVALS, grow_cycles=REALLOC_GROW_CYCLES)
        key = "reallocation"
    elif kind in ("init_coarse_skeleton", "init_own_step20"):
        if kind == "init_coarse_skeleton":
            src_mask, src_rec = _load_step20_mask(load, width, 2.4)
            src_origin = np.asarray(src_rec["grid_origin_mm"], dtype=float)
            src_spacing = float(src_rec["element_size_resolved_mm"])
            meta = dict(source=f"{hl.case_tag(load, width, 2.4)}_step20 を投影")
            try:
                start = hls.project_mask_to_context(src_mask, src_origin, src_spacing, ctx)
            except RuntimeError as exc:
                raw = hls.project_mask_raw(src_mask, src_origin, src_spacing, ctx)
                start, repair = hls.repair_projection(raw, ctx)
                meta.update(projection_failed=str(exc)[:400], repair=repair)
        else:
            start, src_rec = _load_step20_mask(load, width, size)
            meta = dict(source=f"{hl.case_tag(load, width, size)}_step20 の最良形(同じ格子)")
        meta["projected_material_mm3"] = float(start.sum() * ctx.grid.element_volume)
        out["initial_shape"] = meta
        cfg = sup.revert_config(seed=44, max_evaluations=INIT_EVALS)
        with hls.capture_unknown_safety() as unknown:
            rec = sm.run_adaptive_from_mask(ctx, start, cfg, checkpoint_path=checkpoint)
        out["unknown_safety"] = hls.unknown_safety_summary(unknown)
        out.update(gate="m5b_raw", prediction_error_multiplier=2.5, max_evaluations=INIT_EVALS)
        key = "adaptive"
    else:
        raise ValueError(kind)

    out["result_key"] = key
    out["search"] = sup.record_summary(rec)
    out["search_full"] = rec.to_json(mask_dir=hl.MASK_DIR, tag=mask_tag)
    out["search_status"] = rec.status
    if key in ("adaptive", "reallocation"):
        out["best_so_far_curve"] = sup.best_so_far_curve(rec, gate=gate_for_curve)
    if rec.best_ever_mask is not None:
        out["diagnosis"] = hl.diagnose_mask(rec.best_ever_mask, ctx, label=case_id)
        if out["diagnosis"].get("status") != "ok":
            out["status"] = "error"
            out["error"] = f"diagnose: {out['diagnosis']}"
            return out
        out["status"] = "ok"
    else:
        # 合格形が見つからなかった(幾何/制約の不成立または評価誤差)。機械的な分類として残し、台帳エラーとは区別する。
        out["status"] = "no_solution"
        out["no_solution_reason"] = dict(search_status=rec.status, stop_reason=rec.stop_reason,
                                         unknown_safety=out.get("unknown_safety"))
    out["wall_seconds_total"] = time.time() - t0
    out.update(_common.peak_memory_mb())
    return out


def _write(stage: str, ledger: dict, case_id: str, record: dict) -> None:
    _common.upsert_case(ledger, case_id, record)
    ledger["generated_at"] = _common.now_iso()
    ledger["code_version"] = _common.git_state()
    _common.write_atomic(ledger_path(stage), json.dumps(ledger, ensure_ascii=False, indent=1, default=float) + "\n")


def run_child(stage: str, case_id: str, force: bool) -> int:
    job = next(j for j in jobs(stage) if j["case_id"] == case_id)
    ledger = _common.load_ledger(ledger_path(stage))
    if _common.is_case_done(ledger, case_id) and not force:
        print(f"[{case_id}] skip(既に完了)", flush=True)
        return 0
    if force and (hl.OUT / f"07_{case_id}.checkpoint.json").is_file():
        (hl.OUT / f"07_{case_id}.checkpoint.json").unlink()
    with hl.acquire_fem_lock(f"run_07_{stage}"):
        print(f"[{case_id}] start", flush=True)
        try:
            record = run_case(job)
        except Exception as exc:  # noqa: BLE001
            import traceback

            record = dict(case_id=case_id, stage=stage, status="error", error=repr(exc), traceback=traceback.format_exc()[-3000:])
        # ロック保持中に台帳を読み直してから書く(同一ステージの台帳を書くのはこの子だけだが、念のため最新を使う)
        ledger = _common.load_ledger(ledger_path(stage))
        _write(stage, ledger, case_id, record)
        print(f"[{case_id}] {record['status']} ({record.get('wall_seconds_total', 0) / 3600:.2f} h)", flush=True)
    return 0 if record["status"] in ("ok", "no_solution") else 3


def run_stage(stage: str, only: list[str] | None, force: bool) -> int:
    for job in jobs(stage):
        case_id = job["case_id"]
        if only and case_id not in only:
            continue
        ledger = _common.load_ledger(ledger_path(stage))
        if _common.is_case_done(ledger, case_id) and not force:
            print(f"[{case_id}] skip(既に完了)", flush=True)
            continue
        cmd = [sys.executable, str(Path(__file__).resolve()), "--stage", stage, "--case", case_id]
        if force:
            cmd.append("--force")
        code = subprocess.call(cmd)
        ledger = _common.load_ledger(ledger_path(stage))
        rec = next((r for r in ledger.get("cases", []) if r.get("case_id") == case_id), None)
        if rec is None or (code != 0 and rec.get("status") not in ("ok", "no_solution")):
            if rec is None or rec.get("status") not in ("error",):
                _write(stage, ledger, case_id, dict(case_id=case_id, stage=stage, status="error",
                                                    error=f"子プロセスが台帳に書かずに終了(exit={code})。OOMまたは強制終了の疑い"))
    ledger = _common.load_ledger(ledger_path(stage))
    errors = [c["case_id"] for c in ledger.get("cases", []) if c.get("status") == "error"]
    if errors:
        print(f"LEDGER_ERRORS stage={stage} {errors}", flush=True)
        return 2
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=STAGES, required=True)
    ap.add_argument("--case", default=None, help="内部用: 1ケースを現プロセスで実行")
    ap.add_argument("--only", nargs="*", default=None)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    if args.list:
        for j in jobs(args.stage):
            print(j["case_id"])
        return 0
    if args.case:
        return run_child(args.stage, args.case, args.force)
    return run_stage(args.stage, args.only, args.force)


if __name__ == "__main__":
    raise SystemExit(main())
