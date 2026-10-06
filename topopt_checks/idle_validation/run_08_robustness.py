"""段階8・小型比較: 05で悪化した既知の小型条件に、研究用の小修正(`prediction_error_floor`・
`grow_max_fraction_of_removed`)の前後を、同じ格子・実現幅・荷重・ゲート・初期形・停止条件で当てる(最大8実行)。

各実行は新しいOSプロセス(`--case`)で走り、親(`--all`/`--only`)がwall timeout(既定1200秒=20分)を掛け、
超過したら子プロセスの木を `taskkill /F /T` で止めて台帳へ `status=timeout` を残す。**新規hanger最適化はしない**
(題材は dilution_small / path_small のみ)。

台帳: `output/idle_validation_20260928/08_runs.json`。マスク: `08_masks/`。途中経過: `08_<case>.checkpoint.json`。
実行: `.venv\\Scripts\\python.exe topopt_checks\\idle_validation\\run_08_robustness.py --all [--only <id>...] [--force]`
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "topopt_checks"))
import _bootstrap  # noqa: E402,F401

import _common  # noqa: E402
import search_methods as sm  # noqa: E402
import synthetic_matrix_support as sup  # noqa: E402

OUT = sup.OUT
LEDGER = OUT / "08_runs.json"
MASK_DIR = OUT / "08_masks"
WALL_TIMEOUT_SECONDS = 20 * 60
PREDICTION_ERROR_FLOOR = 0.01

D3 = dict(model=sup.DILUTION_MODEL, load_n=300.0, size_mm=3.0, width_mm=6.0)
P15 = dict(model=sup.PATH_MODEL, load_n=3000.0, size_mm=1.5, width_mm=6.0)
DBASE = dict(model=sup.DILUTION_MODEL, load_n=300.0, size_mm=2.0, width_mm=6.0)

JOBS = [
    dict(case_id="d3_adaptive_raw_before", cond=D3, kind="adaptive", gate="m5b_raw", floor=0.0, evals=100, seed=42,
         pair="d3_raw", variant="before"),
    dict(case_id="d3_adaptive_raw_after", cond=D3, kind="adaptive", gate="m5b_raw", floor=PREDICTION_ERROR_FLOOR,
         evals=100, seed=42, pair="d3_raw", variant="after"),
    dict(case_id="p15_adaptive_raw_before", cond=P15, kind="adaptive", gate="m5b_raw", floor=0.0, evals=100, seed=42,
         pair="p15_raw", variant="before"),
    dict(case_id="p15_adaptive_raw_after", cond=P15, kind="adaptive", gate="m5b_raw", floor=PREDICTION_ERROR_FLOOR,
         evals=100, seed=42, pair="p15_raw", variant="after"),
    dict(case_id="dbase_realloc_raw_after", cond=DBASE, kind="reallocation", gate="m5b_raw", floor=0.0, evals=60,
         seed=43, grow_cycles=3, grow_cap=0.1, pair="dbase_realloc", variant="after"),
    dict(case_id="d3_step20_percentile", cond=D3, kind="step20", gate="percentile", pair="d3_percentile", variant="product"),
    dict(case_id="d3_adaptive_percentile_after", cond=D3, kind="adaptive", gate="percentile", floor=PREDICTION_ERROR_FLOOR,
         evals=100, seed=42, pair="d3_percentile", variant="after"),
    dict(case_id="dbase_adaptive_target_failfast", cond=DBASE, kind="adaptive", gate="m5b_target", floor=0.0, evals=100,
         seed=42, fail_fast=True, pair="start_fails_gate", variant="after"),
]


def _steps_digest(rec: sm.MethodRecord) -> dict:
    reasons: dict = {}
    for s in rec.steps:
        for r in (s.reasons or []):
            reasons[r] = reasons.get(r, 0) + 1
    events: dict = {}
    for e in rec.mechanism_events:
        events[e["type"]] = events.get(e["type"], 0) + 1
    grows = [dict(source=e.get("source"), before=e.get("material_mm3_before"), after=e.get("material_mm3_after"),
                  added_cells=e.get("added_cells")) for e in rec.mechanism_events if e.get("type") == "grow_accepted"]
    accepted = [s for s in rec.steps if s.outcome == "accepted" and s.kind == "reduce"]
    return dict(step_count=len(rec.steps), accepted_reduce=len(accepted), reject_reasons=reasons,
                mechanism_event_counts=events, grow_accepted=grows,
                grow_steps=[dict(outcome=s.outcome, requested=s.requested_cells, note=s.note, reasons=s.reasons)
                            for s in rec.steps if s.kind == "grow"])


def run_case(job: dict) -> dict:
    t0 = time.time()
    cond = job["cond"]
    ctx = sm.build_context(cond["model"], load_n=cond["load_n"], element_size_mm=cond["size_mm"],
                           min_width_mm=cond["width_mm"])
    case_id = job["case_id"]
    out = dict(case_id=case_id, pair=job["pair"], variant=job["variant"], kind=job["kind"], gate=job["gate"],
               model=cond["model"].name, load_n=cond["load_n"], element_size_mm=cond["size_mm"],
               element_size_resolved_mm=float(ctx.grid.spacing), min_width_mm_requested=cond["width_mm"],
               min_width_cells_resolved=int(ctx.min_width_cells),
               min_width_realized_mm=float(ctx.min_width_cells * ctx.grid.spacing),
               design_elements=int(ctx.space.mask.sum()), prediction_error_floor=job.get("floor"),
               grow_max_fraction_of_removed=job.get("grow_cap"),
               stop_if_start_fails_gate=bool(job.get("fail_fast", False)), max_evaluations=job.get("evals"), seed=job.get("seed"),
               wall_timeout_seconds=WALL_TIMEOUT_SECONDS, code_version=_common.git_state(paths=("duocad", "topopt_checks")))
    checkpoint = OUT / f"08_{case_id}.checkpoint.json"
    if checkpoint.is_file():
        checkpoint.unlink()
    if job["kind"] == "step20":
        rec = sm.run_fixed_step(ctx, max_step_ratio=0.2)
    else:
        adaptive_cfg = sup.revert_config(seed=job["seed"], max_evaluations=job["evals"], gate=job["gate"])
        adaptive_cfg.prediction_error_floor = job["floor"]
        adaptive_cfg.stop_if_start_fails_gate = bool(job.get("fail_fast", False))
        if job["kind"] == "adaptive":
            rec = sm.run_adaptive(ctx, adaptive_cfg, checkpoint_path=checkpoint)
        else:
            cfg = sm.ReallocationConfig(adaptive=adaptive_cfg, grow_cycles=job["grow_cycles"], seed=job["seed"],
                                        grow_max_fraction_of_removed=job["grow_cap"])
            rec = sm.run_reallocation(ctx, cfg, checkpoint_path=checkpoint)
    out["search"] = sup.record_summary(rec)
    out["digest"] = _steps_digest(rec)
    out["config"] = rec.config
    out["initial_material_mm3"] = rec.initial_material_mm3
    if rec.best_ever_mask is not None:
        MASK_DIR.mkdir(parents=True, exist_ok=True)
        import numpy as np

        mask_path = MASK_DIR / f"{case_id}_best_ever.npz"
        np.savez_compressed(mask_path, mask=rec.best_ever_mask)
        out["best_ever_mask_path"] = str(mask_path.relative_to(REPO))
        out["status"] = "ok"
    else:
        out["status"] = "no_solution"
    out["wall_seconds_total"] = time.time() - t0
    out.update(_common.peak_memory_mb())
    return out


def _write(record: dict) -> None:
    ledger = _common.load_ledger(LEDGER)
    _common.upsert_case(ledger, record["case_id"], record)
    ledger["generated_at"] = _common.now_iso()
    _common.write_atomic(LEDGER, json.dumps(ledger, ensure_ascii=False, indent=1, default=float) + "\n")


def run_child(case_id: str) -> int:
    job = next(j for j in JOBS if j["case_id"] == case_id)
    with _common.ExecutionLock(sup.LOCK, tag=f"run_08_{case_id}"):
        try:
            record = run_case(job)
        except Exception as exc:  # noqa: BLE001
            import traceback

            record = dict(case_id=case_id, status="error", error=repr(exc), traceback=traceback.format_exc()[-3000:])
        _write(record)
    print(f"[{case_id}] {record['status']} ({record.get('wall_seconds_total', 0):.0f} s)", flush=True)
    return 0 if record["status"] in ("ok", "no_solution") else 3


def _kill_tree(pid: int) -> None:
    subprocess.call(["taskkill", "/F", "/T", "/PID", str(pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run_all(only, force: bool) -> int:
    child_failures: list[str] = []
    for job in JOBS:
        case_id = job["case_id"]
        if only and case_id not in only:
            continue
        ledger = _common.load_ledger(LEDGER)
        if not force and _common.is_case_done(ledger, case_id):
            print(f"[{case_id}] skip(既に完了)", flush=True)
            continue
        cmd = [sys.executable, str(Path(__file__).resolve()), "--case", case_id]
        print(f"[{case_id}] 起動(wall timeout {WALL_TIMEOUT_SECONDS} s)", flush=True)
        t0 = time.time()
        proc = subprocess.Popen(cmd)
        try:
            code = proc.wait(timeout=WALL_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            _kill_tree(proc.pid)
            proc.wait()
            checkpoint = OUT / f"08_{case_id}.checkpoint.json"
            partial = {}
            if checkpoint.is_file():
                cp = json.loads(checkpoint.read_text(encoding="utf-8"))
                partial = dict(evaluations=cp.get("evaluations"), best_ever_mm3=cp.get("best_ever_mm3"),
                               steps=len(cp.get("steps", [])), checkpoint=str(checkpoint.relative_to(REPO)))
            _write(dict(case_id=case_id, pair=job["pair"], variant=job["variant"], status="timeout",
                        wall_seconds_total=time.time() - t0, wall_timeout_seconds=WALL_TIMEOUT_SECONDS,
                        stop_reason="timeout", partial=partial))
            print(f"[{case_id}] timeout", flush=True)
            continue
        ledger = _common.load_ledger(LEDGER)
        rec = next((r for r in ledger["cases"] if r["case_id"] == case_id), None)
        if rec is None:
            _write(dict(case_id=case_id, status="error", error=f"子プロセスが台帳に書かずに終了(exit={code})"))
        elif code != 0 and rec.get("status") not in ("ok", "no_solution"):
            print(f"[{case_id}] 子プロセスの終了コード {code}(台帳status={rec.get('status')})", flush=True)
        if code != 0:
            child_failures.append(case_id)
    ledger = _common.load_ledger(LEDGER)
    status = {c["case_id"]: c.get("status") for c in ledger["cases"]}
    scope = [j["case_id"] for j in JOBS if not only or j["case_id"] in only]
    bad = [cid for cid in scope if status.get(cid) not in ("ok", "no_solution")] + child_failures
    if bad:
        print(f"LEDGER_NOT_OK {bad}", flush=True)
        return 2
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", default=None, help="内部用: 1ケースを現プロセスで実行")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--only", nargs="*", default=None)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    if args.case:
        return run_child(args.case)
    return run_all(args.only, args.force)


if __name__ == "__main__":
    raise SystemExit(main())
