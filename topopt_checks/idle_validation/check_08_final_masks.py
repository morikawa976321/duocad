"""段階8・監査2: 08の小型比較が返した最良形を、最小幅・単一成分・正則境界・閉空洞で検査する(FEMなし)。
最小幅は探索が `peel` に渡した規則(`binary_search._mask_satisfies_min_width`・免除域は `_min_width_exempt`)と同じ判定。
出力: `output/idle_validation_20260928/08_mask_checks.json`
実行: `.venv\Scripts\python.exe topopt_checks\idle_validation\check_08_final_masks.py`
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "topopt_checks"))
import _bootstrap  # noqa: E402,F401

import numpy as np  # noqa: E402

from duocad.topopt import acceptance, binary_search  # noqa: E402

import search_methods as sm  # noqa: E402
import synthetic_matrix_support as sup  # noqa: E402

OUT = sup.OUT
MODELS = {"topopt_idle_dilution_small.json": sup.DILUTION_MODEL, "topopt_idle_path_small.json": sup.PATH_MODEL}


def main() -> int:
    ledger = json.loads((OUT / "08_runs.json").read_text(encoding="utf-8"))
    rows = []
    ctx_cache = {}
    for case in ledger["cases"]:
        if case.get("status") != "ok" or not case.get("best_ever_mask_path"):
            continue
        key = (case["model"], case["load_n"], case["element_size_mm"], case["min_width_mm_requested"])
        if key not in ctx_cache:
            ctx_cache[key] = sm.build_context(MODELS[case["model"]], load_n=case["load_n"],
                                              element_size_mm=case["element_size_mm"],
                                              min_width_mm=case["min_width_mm_requested"])
        ctx = ctx_cache[key]
        with np.load(REPO / case["best_ever_mask_path"]) as z:
            mask = np.asarray(z["mask"], dtype=bool)
        exempt = binary_search._min_width_exempt(ctx.cond, mask.shape, ctx.min_width_cells)
        boundary = acceptance.boundary_check(mask)
        connectivity = acceptance.connectivity_check(mask)
        rows.append(dict(
            case_id=case["case_id"], cells=int(mask.sum()),
            min_width_cells=int(ctx.min_width_cells), min_width_realized_mm=float(ctx.min_width_cells * ctx.grid.spacing),
            min_width_ok=bool(binary_search._mask_satisfies_min_width(mask, ctx.min_width_cells, exempt=exempt)),
            boundary_status=boundary["status"], connectivity_status=connectivity["status"],
            closed_voids=len(acceptance.closed_voids(mask)),
            mask_hash=acceptance.material_sha256(mask)))
        print(rows[-1], flush=True)
    (OUT / "08_mask_checks.json").write_text(json.dumps(dict(schema_version=1, rows=rows), ensure_ascii=False, indent=1), encoding="utf-8")
    return 0 if all(r["min_width_ok"] and r["boundary_status"] == "pass" and r["connectivity_status"] == "pass"
                    and r["closed_voids"] == 0 for r in rows) else 2


if __name__ == "__main__":
    raise SystemExit(main())
