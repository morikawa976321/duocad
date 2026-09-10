"""SIMP 反復ごとの最小安全率を測って SVG に描く。

既存の `simp_convergence_hanger_2.4mm.json` には compliance しか無いので、
参照 pickle を使って各ケースを再実行し、反復 callback で `stress.evaluate` する。

    .\\.venv\\Scripts\\python.exe topopt_checks\\plot_simp_safety_convergence.py
    .\\.venv\\Scripts\\python.exe topopt_checks\\plot_simp_safety_convergence.py --skip-run

出力:
  duocad/topopt/output/simp_safety_curves_2.4mm.json
  duocad/topopt/output/simp_safety_convergence_2.4mm.svg
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

import _bootstrap  # noqa: F401
import numpy as np

from duocad.topopt import OUTPUT_DIR
from duocad.topopt import anchor as an
from duocad.topopt import design_space as ds
from duocad.topopt import safety_loop as sl
from duocad.topopt import simp
from duocad.topopt import stress
from duocad.topopt import voxelize as vx
from topopt_checks.measure_simp_convergence import (
    CASE_RATIOS,
    REF_PICKLE,
    SPACING_MM,
)

CURVES_JSON = OUTPUT_DIR / "simp_safety_curves_2.4mm.json"
SVG_OUT = OUTPUT_DIR / "simp_safety_convergence_2.4mm.svg"
TARGET_SAFETY = 2.0

_COLORS = {
    "R": "#1f77b4",
    "1": "#ff7f0e",
    "2": "#2ca02c",
    "3": "#d62728",
    "4": "#9467bd",
    "5": "#8c564b",
}


@dataclass(frozen=True)
class Job:
    case_id: str
    material_ratio: float
    material_mm3: float
    max_iter: int
    ref_path: str


def _build_stress_context(space: ds.DesignSpace, conditions: ds.Conditions, forces: tuple):
    model = vx.model_from_design_mask(
        space.grid,
        space.mask,
        anchor_shape=space.soup,
        keep=conditions.keep,
    )
    table = space.table if space.table is not None else an.build_anchors(space.soup, space.grid)
    fixed_dofs = table.fixed_dofs(list(space.fixed_faces))
    load, _ = an._assemble_load(table, list(space.load_faces), (0.0, 0.0, -1.0), forces)
    solid = model.keep.copy()
    near = table.element_mask(list(space.fixed_faces) + list(space.load_faces), layers=1)
    solid |= near & model.design
    void = ~model.design | model.obstacle
    nx, ny, nz = model.grid.shape
    floating = simp._floating_dofs(nx, ny, nz, void) if void.any() else None
    simp_model = simp.Model(
        nx,
        ny,
        nz,
        fixed_dofs=fixed_dofs,
        load=load,
        extra_fixed_dofs=floating,
        active_mask=~void if void.any() else None,
    )
    scale = stress.UnitScale.for_model(
        simp_model,
        element_mm=space.grid.spacing,
        load_n=conditions.load_n,
        youngs_modulus_mpa=sl.DEFAULT_YOUNGS_MODULUS_MPA,
    )
    return simp_model, scale


def _run_job(job: Job) -> dict[str, Any]:
    os.environ["TOPOPT_THREADS"] = "1"
    os.environ["OMP_NUM_THREADS"] = "1"
    started = time.perf_counter()
    curve: list[dict[str, float]] = []
    try:
        with open(job.ref_path, "rb") as handle:
            conditions, reference, forces = pickle.load(handle)
        capacity = float(reference.stats["capacity_mm3"])
        space = ds.derive_from_reference(
            conditions,
            material_mm3=job.material_mm3,
            reference=reference,
            threads=1,
            spacing=SPACING_MM,
        )
        simp_model, scale = _build_stress_context(space, conditions, forces)

        def progress(iteration: simp.Iteration, rho: np.ndarray) -> None:
            report = stress.evaluate(
                simp_model,
                rho,
                scale=scale,
                allowable_mpa=sl.DEFAULT_ALLOWABLE_MPA,
                solver="mg",
            )
            curve.append(
                {
                    "it": int(iteration.it),
                    "safety_factor": float(report.safety_factor),
                    "representative_mpa": float(report.representative_mpa),
                }
            )
            print(
                f"  [{job.case_id}] it={iteration.it} SF={report.safety_factor:.4f}",
                flush=True,
            )

        stl = OUTPUT_DIR / f"simp_safety_{job.case_id}.stl"
        ds.run_topopt(
            space,
            material_mm3=job.material_mm3,
            max_iter=int(job.max_iter),
            threads=1,
            forces=forces,
            stl_path=stl,
            progress=progress,
        )
        return {
            "case_id": job.case_id,
            "material_ratio": job.material_ratio,
            "material_mm3": job.material_mm3,
            "capacity_mm3": capacity,
            "curve": curve,
            "seconds": time.perf_counter() - started,
            "error": None,
        }
    except Exception as exc:
        return {
            "case_id": job.case_id,
            "material_ratio": job.material_ratio,
            "material_mm3": job.material_mm3,
            "curve": curve,
            "seconds": time.perf_counter() - started,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }


def _collect_curves(max_iter: int, workers: int, case_ids: list[str]) -> dict[str, Any]:
    if not REF_PICKLE.is_file():
        raise FileNotFoundError(f"参照 pickle が無い: {REF_PICKLE}")
    with open(REF_PICKLE, "rb") as handle:
        _, reference, _ = pickle.load(handle)
    capacity = float(reference.stats["capacity_mm3"])
    jobs = [
        Job(
            case_id=case_id,
            material_ratio=CASE_RATIOS[case_id],
            material_mm3=capacity * CASE_RATIOS[case_id],
            max_iter=max_iter,
            ref_path=str(REF_PICKLE),
        )
        for case_id in case_ids
    ]
    print(f"安全率曲線を収集: {len(jobs)}ケース workers={workers} max_iter={max_iter}", flush=True)
    results: list[dict[str, Any]] = []
    if workers <= 1:
        for job in jobs:
            print(f"--- ケース {job.case_id} ---", flush=True)
            results.append(_run_job(job))
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_run_job, job): job for job in jobs}
            for fut in as_completed(futures):
                job = futures[fut]
                print(f"--- ケース {job.case_id} 完了 ---", flush=True)
                results.append(fut.result())
    results.sort(key=lambda item: list(CASE_RATIOS).index(item["case_id"]))
    payload = {
        "spacing_mm": SPACING_MM,
        "capacity_mm3": capacity,
        "max_iter": max_iter,
        "allowable_mpa": sl.DEFAULT_ALLOWABLE_MPA,
        "target_safety_factor": TARGET_SAFETY,
        "cases": results,
    }
    CURVES_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"曲線データ: {CURVES_JSON}", flush=True)
    return payload


def _svg_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def write_svg(payload: dict[str, Any], path: Path) -> None:
    width, height = 1000, 560
    ml, mr, mt, mb = 72, 200, 48, 64
    plot_w = width - ml - mr
    plot_h = height - mt - mb

    all_points: list[tuple[int, float]] = []
    series: dict[str, list[tuple[int, float]]] = {}
    for case in payload["cases"]:
        if case.get("error"):
            continue
        pts = [(int(p["it"]), float(p["safety_factor"])) for p in case.get("curve", [])]
        if pts:
            series[case["case_id"]] = pts
            all_points.extend(pts)

    if not all_points:
        raise ValueError("描画できる曲線が無い")

    xs = [p[0] for p in all_points]
    ys = [p[1] for p in all_points]
    x_min, x_max = min(xs), max(xs)
    y_min = min(0.0, min(ys) * 0.95)
    y_max = max(ys) * 1.05
    target = float(payload.get("target_safety_factor", TARGET_SAFETY))
    y_max = max(y_max, target * 1.1)

    def x_map(x: float) -> float:
        if x_max == x_min:
            return ml + plot_w / 2
        return ml + (x - x_min) / (x_max - x_min) * plot_w

    def y_map(y: float) -> float:
        if y_max == y_min:
            return mt + plot_h / 2
        return mt + plot_h - (y - y_min) / (y_max - y_min) * plot_h

    lines: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        f'<text x="{width/2}" y="28" text-anchor="middle" font-family="sans-serif" '
        f'font-size="16">hanger SIMP 収束 — 反復数 vs 最小安全率 (spacing={payload["spacing_mm"]}mm)</text>',
    ]

    for i in range(6):
        yv = y_min + (y_max - y_min) * i / 5
        yp = y_map(yv)
        lines.append(
            f'<line x1="{ml}" y1="{yp:.1f}" x2="{width-mr}" y2="{yp:.1f}" stroke="#ddd" stroke-width="1"/>'
        )
        lines.append(
            f'<text x="{ml-8}" y="{yp+4:.1f}" text-anchor="end" font-family="sans-serif" '
            f'font-size="11" fill="#444">{yv:.2f}</text>'
        )

    for i in range(int(x_max - x_min) + 1):
        xv = x_min + i
        if xv > x_max:
            break
        if (xv - x_min) % max(1, int((x_max - x_min) // 8 or 1)) != 0 and xv not in (x_min, x_max):
            continue
        xp = x_map(xv)
        lines.append(
            f'<line x1="{xp:.1f}" y1="{mt}" x2="{xp:.1f}" y2="{mt+plot_h}" stroke="#eee" stroke-width="1"/>'
        )
        lines.append(
            f'<text x="{xp:.1f}" y="{height-mb+20}" text-anchor="middle" font-family="sans-serif" '
            f'font-size="11" fill="#444">{int(xv)}</text>'
        )

    ty = y_map(target)
    lines.append(
        f'<line x1="{ml}" y1="{ty:.1f}" x2="{width-mr}" y2="{ty:.1f}" '
        f'stroke="#888" stroke-width="1" stroke-dasharray="6,4"/>'
    )
    lines.append(
        f'<text x="{width-mr+6}" y="{ty+4:.1f}" font-family="sans-serif" font-size="11" fill="#666">'
        f'目標 {target:g}</text>'
    )

    legend_y = mt + 12
    for idx, (case_id, pts) in enumerate(series.items()):
        color = _COLORS.get(case_id, "#333")
        ratio = next(c["material_ratio"] for c in payload["cases"] if c["case_id"] == case_id)
        d = " ".join(f"L{x_map(x):.1f},{y_map(y):.1f}" for x, y in pts[1:])
        d = f"M{x_map(pts[0][0]):.1f},{y_map(pts[0][1]):.1f} {d}"
        lines.append(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2"/>')
        for x, y in pts:
            lines.append(
                f'<circle cx="{x_map(x):.1f}" cy="{y_map(y):.1f}" r="2.5" fill="{color}"/>'
            )
        ly = legend_y + idx * 18
        lines.append(f'<line x1="{width-mr+8}" y1="{ly}" x2="{width-mr+32}" y2="{ly}" stroke="{color}" stroke-width="2"/>')
        lines.append(
            f'<text x="{width-mr+38}" y="{ly+4}" font-family="sans-serif" font-size="12" fill="#222">'
            f'{_svg_escape(f"ケース {case_id} ({ratio:.1%})")}</text>'
        )

    lines.append(
        f'<text x="{width/2}" y="{height-18}" text-anchor="middle" font-family="sans-serif" '
        f'font-size="13" fill="#222">反復数</text>'
    )
    lines.append(
        f'<text x="18" y="{mt + plot_h/2}" text-anchor="middle" font-family="sans-serif" '
        f'font-size="13" fill="#222" transform="rotate(-90 18 {mt + plot_h/2})">最小安全率</text>'
    )
    lines.append("</svg>")
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"SVG: {path}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-iter", type=int, default=80)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--skip-run", action="store_true", help="既存 JSON から SVG だけ描く")
    parser.add_argument(
        "--cases",
        type=str,
        default="",
        help="ケース ID のカンマ区切り(例 R,1,2)。空なら全ケース",
    )
    args = parser.parse_args()
    case_ids = [c.strip() for c in args.cases.split(",") if c.strip()] or list(CASE_RATIOS)

    if args.skip_run and CURVES_JSON.is_file():
        payload = json.loads(CURVES_JSON.read_text(encoding="utf-8"))
    else:
        payload = _collect_curves(args.max_iter, args.workers, case_ids)

    write_svg(payload, SVG_OUT)
    failed = [c for c in payload["cases"] if c.get("error")]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
