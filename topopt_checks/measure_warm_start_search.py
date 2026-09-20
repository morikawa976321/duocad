"""段階4に warm start を通した探索を、`warm_start` の有無で1回ずつ回して比べる測定。

    .\.venv\Scripts\python.exe topopt_checks\measure_warm_start_search.py --mode warm --result C:\tmp\warm.json
    .\.venv\Scripts\python.exe topopt_checks\measure_warm_start_search.py --mode cold --result C:\tmp\cold.json
    .\.venv\Scripts\python.exe topopt_checks\measure_warm_start_search.py --compare C:\tmp\warm.json C:\tmp\cold.json

hanger・要素サイズ要求 2.4mm・目標2.0・既定の最小幅(m=1)。**1プロセス1モード**(探索は約7GBを使うので、
同じプロセスで2回回して2回目が1回目の残りに乗らないようにする)。**判定はしない**(数字と、採用形状の
`mask_hash` が一致したかを書き出すだけ)。`trace.DEFAULT_ROOT` を一時フォルダへ向け、`output/` に何も残さない。
空きメモリが `--min-free-gb` 未満なら測らずに終わる(終了コード3)。

`--mask-npz` を付けると採用マスクを保存する(`mask_hash` だけでは形の差が分からない)。保存した2つを
`--diff-masks WARM_NPZ COLD_NPZ [--stl-dir DIR]` で並べると、重なり・片方だけのセル・その距離を出し、
ボクセルのままのSTL(warm / cold / warm_only / cold_only)を書く。
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

import _bootstrap  # noqa: F401  (topopt_checks の import 経路を通す)
import numpy as np
from measure_min_width_floor import voxel_surface
from measure_warm_start import free_gb

REPO_ROOT = Path(__file__).resolve().parents[1]
MODEL = REPO_ROOT / "models" / "hanger_bar_attachment.json"
REQUESTED_ELEMENT_SIZE_MM = 2.4
TARGET_SAFETY_FACTOR = 2.0


def run(mode: str, budget: int, mask_npz: Path | None = None) -> dict:
    from duocad import optimize_run
    from duocad.server import Session
    from duocad.topopt import binary_search, design_space as ds, fem, trace

    out: dict = {"mode": mode, "model": str(MODEL), "element_size_requested_mm": REQUESTED_ELEMENT_SIZE_MM,
                 "target_safety_factor": TARGET_SAFETY_FACTOR, "max_evaluations": int(budget),
                 "free_gb_at_start": free_gb()}
    with tempfile.TemporaryDirectory(prefix="topopt-trace-") as trace_root, \
            patch.object(trace, "DEFAULT_ROOT", Path(trace_root)):
        session = Session(None, 0.2, models_dir=MODEL.resolve().parent)
        session.open_document(MODEL.name)
        document = session.document
        bundle = optimize_run.build_conditions(document)
        limits = optimize_run.resolution_limits(document)
        spacing = optimize_run._snap_to_grid(
            limits["bbox_min"], limits["bbox_max"],
            optimize_run._resolve_element_size(limits, REQUESTED_ELEMENT_SIZE_MM))
        min_width = optimize_run._resolve_min_width(spacing, None)
        out.update(actual_spacing_mm=spacing, min_width_cells=min_width["cells"])
        space = ds.derive(bundle.conditions, spacing=spacing)
        table, mapping = optimize_run._map_boundary_conditions(space, bundle)
        out.update(grid_shape=list(space.grid.shape), design_space_elements=int(space.mask.sum()))
        if mapping["status"] != "pass":
            out["error"] = f"確定表が不成立: {mapping['status']}"
            return out
        opts = optimize_run.OptimizeOptions(target_safety_factor=TARGET_SAFETY_FACTOR, max_evaluations=budget)
        conditions = optimize_run._build_search_conditions(document, space, bundle, opts, table=table, mapping=mapping)
        started = time.perf_counter()
        with fem.collect_solve_stats() as stats:
            result = binary_search.find_best_feasible(
                space.mask, conditions, max_evaluations=budget, min_width_cells=min_width["cells"],
                warm_start=(mode == "warm"))
        out["seconds"] = time.perf_counter() - started
    out["free_gb_at_end"] = free_gb()

    out.update(search_status=result.status, termination=result.termination, evaluations=int(result.evaluations),
               seconds_per_evaluation=out["seconds"] / max(1, int(result.evaluations)),
               solves=int(stats["solves"]), cg_iterations_total=int(stats["cg_iterations"]),
               cg_iterations_each=[int(n) for n in stats["cg_iterations_each"]],
               cg_fallbacks=int(stats["fallbacks"]), solve_seconds=float(stats["solve_seconds"]),
               mg_setup_seconds=float(stats["mg_setup_seconds"]))
    best = result.best_feasible
    reduction = (result.diagnostics or {}).get("reduction") or {}
    out.update(
        stop_reason=reduction.get("stop_reason"),
        warm_start_requested=reduction.get("warm_start"),
        warm_start_used=reduction.get("warm_start_used"),
        warm_start_fallbacks=reduction.get("warm_start_fallbacks"),
        reduction_fem_solves=reduction.get("fem_solves"),
        history=[dict(index=t.index, operation=t.operation, outcome=t.outcome, mask_hash=t.mask_hash,
                      safety_factor=t.safety_factor) for t in result.history],
    )
    if best is None:
        out["error"] = f"best_feasible が無い(status={result.status})"
        return out
    out.update(mask_hash=best.mask_hash, accepted_elements=int(best.mask.sum()),
               safety_factor=float(best.safety_factor))
    if mask_npz is not None:
        np.savez_compressed(mask_npz, mask=best.mask, origin=np.asarray(space.grid.origin, dtype=float),
                            spacing=float(space.grid.spacing))
    return out


def compare_masks(warm_npz: Path, cold_npz: Path, stl_dir: Path | None) -> dict:
    from scipy import ndimage
    from duocad.topopt import mesh

    warm, cold = np.load(warm_npz), np.load(cold_npz)
    origin, spacing = np.asarray(warm["origin"], dtype=float), float(warm["spacing"])
    if not (warm["mask"].shape == cold["mask"].shape and np.allclose(origin, cold["origin"])
            and spacing == float(cold["spacing"])):
        raise SystemExit("格子が違う2つのマスクは比べられない")
    w, c = warm["mask"].astype(bool), cold["mask"].astype(bool)
    only = {"warm_only": w & ~c, "cold_only": c & ~w}
    both = int((w & c).sum())
    out: dict = dict(spacing_mm=spacing, warm_cells=int(w.sum()), cold_cells=int(c.sum()), both=both,
                     warm_only=int(only["warm_only"].sum()), cold_only=int(only["cold_only"].sum()),
                     iou=both / int((w | c).sum()))
    for name, other in (("warm_only", c), ("cold_only", w)):
        cells = only[name]
        if not cells.any():
            continue
        distance = ndimage.distance_transform_edt(~other)[cells] * spacing
        labels, count = ndimage.label(cells)
        index = np.argwhere(cells)
        out[name + "_detail"] = dict(
            distance_to_other_mm=dict(mean=float(distance.mean()), max=float(distance.max())),
            components=int(count),
            largest_components=[int(n) for n in sorted(np.bincount(labels.ravel())[1:], reverse=True)[:5]],
            bbox_min_mm=(origin + spacing * index.min(axis=0)).tolist(),
            bbox_max_mm=(origin + spacing * (index.max(axis=0) + 1)).tolist())
    if stl_dir is not None:
        stl_dir.mkdir(parents=True, exist_ok=True)
        for name, cells in {"warm": w, "cold": c, **only}.items():
            verts, faces = voxel_surface(cells, origin, spacing)
            if len(faces):
                mesh.write_stl(stl_dir / f"{name}.stl", verts, faces)
                out[name + "_stl_triangles"] = int(len(faces))
    return out


def compare(warm_path: Path, cold_path: Path) -> dict:
    warm, cold = (json.loads(p.read_text(encoding="utf-8")) for p in (warm_path, cold_path))
    same_history = [(t["mask_hash"], t["outcome"]) for t in warm["history"]] == \
                   [(t["mask_hash"], t["outcome"]) for t in cold["history"]]
    return dict(
        mask_hash_equal=warm.get("mask_hash") == cold.get("mask_hash"),
        history_equal=same_history,
        evaluations=(warm["evaluations"], cold["evaluations"]),
        termination=(warm["termination"], cold["termination"]),
        seconds=(warm["seconds"], cold["seconds"]),
        seconds_ratio=warm["seconds"] / cold["seconds"],
        seconds_per_evaluation=(warm["seconds_per_evaluation"], cold["seconds_per_evaluation"]),
        cg_iterations_total=(warm["cg_iterations_total"], cold["cg_iterations_total"]),
        cg_iterations_ratio=warm["cg_iterations_total"] / cold["cg_iterations_total"],
        warm_start_used_fallbacks=(warm["warm_start_used"], warm["warm_start_fallbacks"]),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=("warm", "cold"))
    parser.add_argument("--budget", type=int, default=20, help="評価予算(既定20)")
    parser.add_argument("--min-free-gb", type=float, default=12.0)
    parser.add_argument("--result", type=Path, default=None, help="結果JSONの書き出し先")
    parser.add_argument("--compare", nargs=2, type=Path, metavar=("WARM_JSON", "COLD_JSON"))
    parser.add_argument("--mask-npz", type=Path, default=None, help="採用マスクの保存先(省略時は保存しない)")
    parser.add_argument("--diff-masks", nargs=2, type=Path, metavar=("WARM_NPZ", "COLD_NPZ"))
    parser.add_argument("--stl-dir", type=Path, default=None, help="--diff-masks のSTLの書き出し先")
    args = parser.parse_args()
    if args.compare:
        print(json.dumps(compare(*args.compare), ensure_ascii=False, indent=2), flush=True)
        return 0
    if args.diff_masks:
        print(json.dumps(compare_masks(*args.diff_masks, args.stl_dir), ensure_ascii=False, indent=2), flush=True)
        return 0
    if args.mode is None:
        parser.error("--mode か --compare が要る")
    free = free_gb()
    if free < args.min_free_gb:
        print(f"空きメモリ {free:.1f}GB が下限 {args.min_free_gb}GB 未満。測らずに終わる", flush=True)
        return 3
    result = run(args.mode, args.budget, args.mask_npz)
    text = json.dumps(result, ensure_ascii=False, indent=2, default=float)
    if args.result:
        args.result.write_text(text, encoding="utf-8")
    print(text, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
