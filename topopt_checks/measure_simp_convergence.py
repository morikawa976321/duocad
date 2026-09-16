"""hanger_bar_attachment.json で SIMP 収束反復数を材料量スイープする実測。

    .\\.venv\\Scripts\\python.exe topopt_checks\\measure_simp_convergence.py
    .\\.venv\\Scripts\\python.exe topopt_checks\\measure_simp_convergence.py --smoke
    .\\.venv\\Scripts\\python.exe topopt_checks\\measure_simp_convergence.py --workers 4

塊は製品と同じ固定凸包の導出(`design_space.derive`)を1回回したもので、全ケースが同じ塊を
使う(pickle `SPACE_PICKLE` に保存し、`plot_simp_safety_convergence.py` も読む)。
各 SIMP は `threads=1`(1コア)。材料量ケースは `ProcessPoolExecutor` で並列。
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import os
import pickle
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

import _bootstrap  # noqa: F401
import numpy as np

from duocad.optimize_run import build_conditions
from duocad.session import Session
from duocad.topopt import OUTPUT_DIR
from duocad.topopt import bench
from duocad.topopt import design_space as ds
from duocad.topopt import simp

MODEL_NAME = "hanger_bar_attachment.json"
SPACING_MM = 2.4
CHANGE_TOL = 0.01
TAIL_WINDOW = bench.TAIL_WINDOW

# ケース id は過去の出力(docs/TOPOPT.md の表)と揃えてある。"R" は 2026-09-16 以前の参照方式で
# 参照 run の材料量だった 8% で、いまはただの材料量の1点。
CASE_RATIOS: dict[str, float] = {
    "R": 0.08,
    "1": 0.066,
    "2": 0.105,
    "3": 0.19,
    "4": 0.33,
    "5": 0.55,
}

OUTPUT_JSON = OUTPUT_DIR / "simp_convergence_hanger_2.4mm.json"
SPACE_PICKLE = OUTPUT_DIR / "simp_convergence_hanger_2.4mm_space.pkl"


def _peak_rss_mb() -> float | None:
    try:
        import psutil

        return psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)
    except Exception:
        return None


def _load_hanger_bundle():
    session = Session(None, 0.2)
    session.open_document(MODEL_NAME)
    return build_conditions(session.document)


def _iteration_rows(history: list[simp.Iteration]) -> list[dict[str, Any]]:
    return [
        {
            "it": int(item.it),
            "compliance": float(item.compliance),
            "change": float(item.change),
            "volfrac": float(item.volfrac),
            "seconds": float(item.seconds),
        }
        for item in history
    ]


def _compliance_tail_swing(c_hist: np.ndarray) -> float:
    tail = c_hist[-TAIL_WINDOW:]
    if tail.size < TAIL_WINDOW:
        return float("nan")
    mean = float(tail.mean())
    if mean <= 0.0:
        return float("nan")
    return float((tail.max() - tail.min()) / mean)


def _first_iter_where(history: list[simp.Iteration], pred) -> int | None:
    for item in history:
        if pred(item):
            return int(item.it)
    return None


def _metrics_from_history(history: list[simp.Iteration], rho_final: np.ndarray) -> dict[str, Any]:
    c_hist = np.array([item.compliance for item in history], dtype=float)
    change_hist = np.array([item.change for item in history], dtype=float)
    conv_a = _first_iter_where(history, lambda item: item.change < CHANGE_TOL)
    conv_b: int | None = None
    conv_c: int | None = None
    for end in range(TAIL_WINDOW, len(history) + 1):
        swing = _compliance_tail_swing(c_hist[:end])
        if math.isfinite(swing) and swing < 0.01:
            conv_b = end
        tail = c_hist[end - TAIL_WINDOW : end]
        if tail.size == TAIL_WINDOW and tail[0] > 0.0:
            rel = float(abs(tail[-1] - tail[0]) / tail[0])
            if rel < 0.001:
                conv_c = end
    return {
        "iterations": len(history),
        "convergence_a_iter": conv_a,
        "convergence_b_iter": conv_b,
        "convergence_c_iter": conv_c,
        "tail10_swing_final": _compliance_tail_swing(c_hist),
        "compliance_initial": float(c_hist[0]) if c_hist.size else float("nan"),
        "compliance_final": float(c_hist[-1]) if c_hist.size else float("nan"),
        "change_final": float(change_hist[-1]) if change_hist.size else float("nan"),
        "rho_sha256": _sha256(rho_final),
        "history": _iteration_rows(history),
    }


def _sha256(arr: np.ndarray) -> str:
    import hashlib

    return hashlib.sha256(np.ascontiguousarray(arr, dtype=np.float64).tobytes()).hexdigest()


def _rho_relative_error(a: np.ndarray, b: np.ndarray) -> float:
    denom = max(float(np.abs(b).max()), 1e-30)
    return float(np.abs(a - b).max() / denom)


@dataclasses.dataclass(frozen=True)
class WorkerInput:
    case_id: str
    material_ratio: float
    material_mm3: float
    max_iter: int
    space_path: str
    stl_path: str
    smoke: bool


def _run_case_worker(spec: WorkerInput) -> dict[str, Any]:
    os.environ["TOPOPT_THREADS"] = "1"
    os.environ["OMP_NUM_THREADS"] = "1"
    started = time.perf_counter()
    peak_mb: float | None = None
    try:
        with open(spec.space_path, "rb") as handle:
            space, forces = pickle.load(handle)
        from duocad.topopt import design_space as ds_local

        mem0 = _peak_rss_mb()
        out = ds_local.run_topopt(
            space,
            material_mm3=spec.material_mm3,
            max_iter=int(spec.max_iter),
            threads=1,
            forces=forces,
            stl_path=spec.stl_path,
            progress=_simp_progress(spec.case_id),
        )
        peak_mb = _peak_rss_mb()
        result = out["result"]
        metrics = _metrics_from_history(result.history, result.rho)
        return {
            "case_id": spec.case_id,
            "material_ratio": spec.material_ratio,
            "material_mm3": spec.material_mm3,
            "design_elements": int(space.element_count),
            "stopped_by": result.stopped_by,
            "seconds_total": time.perf_counter() - started,
            "seconds_per_iter": (
                (time.perf_counter() - started) / len(result.history) if result.history else float("nan")
            ),
            "peak_rss_mb": peak_mb,
            "peak_rss_delta_mb": (
                None if mem0 is None or peak_mb is None else peak_mb - mem0
            ),
            "achieved_material_mm3": float(out.get("achieved_material_mm3", float("nan"))),
            "error": None,
            **metrics,
        }
    except Exception as exc:
        return {
            "case_id": spec.case_id,
            "material_ratio": spec.material_ratio,
            "material_mm3": spec.material_mm3,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
            "seconds_total": time.perf_counter() - started,
        }


def _choose_workers(requested: int | None, smoke: bool) -> int:
    if smoke:
        return 1
    cpu = os.cpu_count() or 1
    if requested is not None:
        return max(1, min(requested, len(CASE_RATIOS)))
    try:
        import psutil

        avail_gb = psutil.virtual_memory().available / (1024**3)
        by_ram = max(1, int((avail_gb - 4.0) // 6.0))
    except Exception:
        by_ram = 3
    return max(1, min(len(CASE_RATIOS), by_ram, cpu))


def _derive_space(conditions) -> tuple[float, ds.DesignSpace]:
    space = ds.derive(conditions, spacing=SPACING_MM)
    capacity = float(space.element_count) * float(space.grid.element_volume)
    return capacity, space


def run_measurement(
    *,
    smoke: bool = False,
    max_iter: int = 80,
    workers: int | None = None,
    skip_derive: bool = False,
) -> dict[str, Any]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    print(f"題材={MODEL_NAME} spacing={SPACING_MM}mm max_iter={max_iter} smoke={smoke}", flush=True)

    if skip_derive and SPACE_PICKLE.is_file():
        print(f"塊の導出をスキップ: {SPACE_PICKLE}", flush=True)
        with open(SPACE_PICKLE, "rb") as handle:
            space, forces = pickle.load(handle)
        derive_seconds = 0.0
    else:
        bundle = _load_hanger_bundle()
        derive_started = time.perf_counter()
        _capacity, space = _derive_space(bundle.conditions)
        forces = bundle.forces
        derive_seconds = time.perf_counter() - derive_started
        with open(SPACE_PICKLE, "wb") as handle:
            pickle.dump((space, forces), handle, protocol=pickle.HIGHEST_PROTOCOL)
    capacity = float(space.element_count) * float(space.grid.element_volume)
    print(
        f"固定凸包の塊 {space.element_count} 要素 容量 C={capacity:.3f} mm^3  箱={space.grid.shape}  "
        f"箱要素={space.grid.element_count}  導出 {derive_seconds:.1f}秒",
        flush=True,
    )

    case_ids = ["1"] if smoke else list(CASE_RATIOS)
    worker_count = _choose_workers(workers, smoke)
    print(f"並列 workers={worker_count}", flush=True)

    jobs = [
        WorkerInput(
            case_id=case_id,
            material_ratio=CASE_RATIOS[case_id],
            material_mm3=capacity * CASE_RATIOS[case_id],
            max_iter=max_iter,
            space_path=str(SPACE_PICKLE),
            stl_path=str(OUTPUT_DIR / f"simp_convergence_hanger_2.4mm_{case_id}.stl"),
            smoke=smoke,
        )
        for case_id in case_ids
    ]

    results: list[dict[str, Any]] = []
    if worker_count == 1:
        for job in jobs:
            print(f"--- ケース {job.case_id} ({job.material_ratio:.1%}) ---", flush=True)
            results.append(_run_case_worker(job))
    else:
        with ProcessPoolExecutor(max_workers=worker_count) as pool:
            futures = {pool.submit(_run_case_worker, job): job for job in jobs}
            for fut in as_completed(futures):
                job = futures[fut]
                print(f"--- ケース {job.case_id} 完了待ち ---", flush=True)
                results.append(fut.result())

    results.sort(key=lambda item: list(CASE_RATIOS).index(item["case_id"]))

    for item in results:
        if item.get("error"):
            continue
        hist = item.get("history") or []
        if not hist:
            continue
        # D 指標は 80反復相当の最終 rho 同士 —— 並列 run 内では自己参照のみ
        item["convergence_d_vs_final_iter"] = item["iterations"]

    payload = {
        "model": MODEL_NAME,
        "spacing_mm": SPACING_MM,
        "capacity_mm3": capacity,
        "bbox_grid_shape": list(space.grid.shape),
        "bbox_elements": int(space.grid.element_count),
        "change_tol": CHANGE_TOL,
        "max_iter": max_iter,
        "threads_per_run": 1,
        "workers": worker_count,
        "smoke": smoke,
        "space": {
            "derivation": ds.DERIVATION,
            "seconds": derive_seconds,
            "design_elements": int(space.element_count),
            "mask_hash": space.mask_hash,
            "cells": dict(space.stats["cells"]),
        },
        "cases": results,
        "seconds_total": time.perf_counter() - started,
    }
    OUTPUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"書き出し: {OUTPUT_JSON}", flush=True)
    _print_summary(payload)
    return payload


def _simp_progress(case_id: str):
    def callback(iteration: simp.Iteration, _rho: np.ndarray) -> None:
        print(
            f"  [{case_id}] 反復 {iteration.it}: C={iteration.compliance:.4f} "
            f"change={iteration.change:.4f} {iteration.seconds:.1f}s",
            flush=True,
        )

    return callback


def _print_summary(payload: dict[str, Any]) -> None:
    print("\n=== サマリ ===", flush=True)
    print(f"C={payload['capacity_mm3']:.3f} mm^3  workers={payload['workers']}")
    for item in payload["cases"]:
        if item.get("error"):
            print(f"  {item['case_id']}: ERROR {item['error']}")
            continue
        print(
            f"  {item['case_id']} ratio={item['material_ratio']:.1%} "
            f"M={item['material_mm3']:.0f} mm^3 "
            f"塊={item.get('design_elements')} "
            f"A={item.get('convergence_a_iter')} "
            f"B={item.get('convergence_b_iter')} "
            f"stop={item.get('stopped_by')} "
            f"{item.get('seconds_total', 0):.1f}s"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="hanger SIMP 収束反復数の実測")
    parser.add_argument("--smoke", action="store_true", help="ケース1のみ・workers=1")
    parser.add_argument("--max-iter", type=int, default=80)
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument(
        "--skip-derive",
        action="store_true",
        help="既存の塊の pickle があれば導出を省略",
    )
    args = parser.parse_args()
    payload = run_measurement(
        smoke=args.smoke,
        max_iter=args.max_iter,
        workers=args.workers,
        skip_derive=args.skip_derive,
    )
    failed = [item for item in payload["cases"] if item.get("error")]
    raise SystemExit(1 if failed else 0)
