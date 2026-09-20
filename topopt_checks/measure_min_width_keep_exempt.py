"""最小幅の規則から保持ソリッド由来のセルを外した版で、hanger の探索を1回回す(`docs/TOPOPT_THICKNESS.md` 節I5)。

    .\\.venv\\Scripts\\python.exe topopt_checks\\measure_min_width_keep_exempt.py --cells 3 --budget 20 --result C:\\tmp\\m3.json

要素サイズは既定 2.406306mm(格子 設計空間 367,390 セル・保持 1,475 セル)。最小幅は解決した実 spacing の
`--cells` 倍(段の値)を渡し、`m == --cells` になったことを確かめてから測る。**判定はしない**(入口の事実・
探索の結果・「余地が無い保持セル」の数を書き出すだけ)。`trace.DEFAULT_ROOT` を一時フォルダへ向け、
`output/optimization_runs` に何も残さない。
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
from scipy import ndimage

REPO_ROOT = Path(__file__).resolve().parents[1]
MODEL = REPO_ROOT / "models" / "hanger_bar_attachment.json"
TARGET_SAFETY_FACTOR = 2.0


def entry_facts(space, m: int) -> dict:
    """探索の前に決まる事実: m 立方の opening が設計空間から落とすセルと、その中の保持セル。"""
    from duocad.topopt import binary_search, min_width as min_width_rules

    design, keep = np.asarray(space.mask, dtype=bool), np.asarray(space.keep, dtype=bool)
    opened = min_width_rules.cube_opening(design, m)
    dropped = design & ~opened
    keep_alone = min_width_rules.cube_opening(keep, m)
    room = binary_search.keep_cells_without_room(design, keep, m, space.obstacle)
    stranded = keep & dropped
    ring6 = ndimage.binary_dilation(np.pad(stranded, 1), structure=ndimage.generate_binary_structure(3, 1)) \
        & ~np.pad(design, 1)
    obstacle = np.pad(np.asarray(space.obstacle, dtype=bool), 1)
    return dict(
        design_cells=int(design.sum()), keep_cells=int(keep.sum()),
        opening_dropped_from_design=int(dropped.sum()), opening_dropped_keep_derived=int((dropped & keep).sum()),
        keep_alone_dropped_by_opening=int((keep & ~keep_alone).sum()),
        keep_without_room=room,
        around_stranded_6_neighbourhood=dict(around_exclude=int((ring6 & obstacle).sum()),
                                             around_outside=int((ring6 & ~obstacle).sum())),
    )


def run(m: int, budget: int, requested_element_size_mm: float) -> dict:
    from duocad import optimize_run
    from duocad.server import Session
    from duocad.topopt import binary_search, design_space as ds, min_width as min_width_rules, trace

    out: dict = {"model": str(MODEL), "element_size_requested_mm": requested_element_size_mm,
                 "target_safety_factor": TARGET_SAFETY_FACTOR, "max_evaluations": int(budget)}
    with tempfile.TemporaryDirectory(prefix="topopt-trace-") as trace_root, \
            patch.object(trace, "DEFAULT_ROOT", Path(trace_root)):
        session = Session(None, 0.2, models_dir=MODEL.resolve().parent)
        session.open_document(MODEL.name)
        document = session.document
        bundle = optimize_run.build_conditions(document)
        limits = optimize_run.resolution_limits(document)
        spacing = optimize_run._snap_to_grid(
            limits["bbox_min"], limits["bbox_max"], optimize_run._resolve_element_size(limits, requested_element_size_mm))
        min_width_mm = float(m) * spacing
        min_width = optimize_run._resolve_min_width(spacing, min_width_mm)
        out.update(actual_spacing_mm=spacing, min_width_request_mm=min_width_mm, min_width=min_width)
        if min_width["cells"] != m:
            out["error"] = f"m が {m} にならなかった(m={min_width['cells']})。測定しない"
            return out

        space = ds.derive(bundle.conditions, spacing=spacing)
        out["grid_shape"] = list(space.grid.shape)
        out["entry"] = entry_facts(space, m)
        table, mapping = optimize_run._map_boundary_conditions(space, bundle)
        out["mapping_status"] = mapping["status"]
        if mapping["status"] != "pass":
            out["error"] = f"確定表が不成立: {mapping['status']}"
            return out
        opts = optimize_run.OptimizeOptions(target_safety_factor=TARGET_SAFETY_FACTOR, max_evaluations=budget,
                                            min_width_mm=min_width_mm)
        conditions = optimize_run._build_search_conditions(document, space, bundle, opts, table=table, mapping=mapping)
        started = time.perf_counter()
        result = binary_search.find_best_feasible(space.mask, conditions, max_evaluations=budget,
                                                  min_width_cells=m)
        out["seconds"] = time.perf_counter() - started

    diagnostics = result.diagnostics or {}
    initial = diagnostics.get("initial") or diagnostics
    out.update(search_status=result.status, termination=result.termination, evaluations=int(result.evaluations),
               entry_passed=result.status not in ("boundary_mapping_failed", "input_contradiction",
                                                  "resolution_insufficient"),
               min_width_diagnostics=initial.get("min_width"),
               uncovered_condition_nodes=initial.get("uncovered_condition_nodes"))
    best = result.best_feasible
    if best is None:
        out["error"] = f"best_feasible が無い(status={result.status})"
        return out
    reduction = diagnostics.get("reduction") or {}
    keep = np.asarray(space.keep, dtype=bool)
    steps = reduction.get("steps", [])
    out.update(
        accepted_elements=int(best.mask.sum()),
        material_fraction_of_design_space=float(best.mask.sum() / space.mask.sum()),
        coarse_safety_factor=float(best.safety_factor),
        reduction_stop_reason=reduction.get("stop_reason"),
        peel_casualties_total=sum(int(s.get("peel", {}).get("casualties", 0)) for s in steps),
        reduction_steps=len(steps),
        keep_cells_in_result=int((best.mask & keep).sum()),
        rule_holds_with_keep_exempt=bool(binary_search._mask_satisfies_min_width(best.mask, m, keep)),
        rule_holds_without_exemption=bool(binary_search._mask_satisfies_min_width(best.mask, m)),
        non_keep_cells_outside_cubes=int((best.mask & ~min_width_rules.cube_opening(best.mask, m) & ~keep).sum()),
    )
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cells", type=int, default=3, help="最小幅のセル数 m(既定3)")
    parser.add_argument("--budget", type=int, default=20, help="評価予算(既定20)")
    parser.add_argument("--element-size", type=float, default=2.406306, help="要素サイズ要求[mm]")
    parser.add_argument("--result", type=Path, default=None, help="結果JSONの書き出し先(省略時は標準出力だけ)")
    args = parser.parse_args()
    started = time.perf_counter()
    result = run(args.cells, args.budget, args.element_size)
    result["total_seconds"] = time.perf_counter() - started
    text = json.dumps(result, ensure_ascii=False, indent=2, default=float)
    if args.result:
        args.result.write_text(text, encoding="utf-8")
    print(text, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
