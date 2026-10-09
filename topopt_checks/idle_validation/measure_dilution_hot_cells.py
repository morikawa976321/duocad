"""原因1の直接証拠: 全材料と、`measure_dilution_counterfactual.py` が保存した各Rの採用マスクで、
目標応力(許容/探索目標4.8)を超える「熱いセル」の数と位置(保持リング近傍 vs 遠方)を数える。
保持リング近傍の熱いセル数がNに依らずほぼ一定である一方、0.5%×Nは母材が減るほど小さくなり、
どこかでその下を割る(原因1の核心)ことを表で示す。

    .\\.venv\\Scripts\\python.exe topopt_checks\\idle_validation\\measure_dilution_hot_cells.py
"""
from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "topopt_checks"))
import _bootstrap  # noqa: E402,F401
import numpy as np  # noqa: E402
from scipy import ndimage  # noqa: E402

import _record  # noqa: E402
from _documents import open_session  # noqa: E402

from duocad import optimize_run  # noqa: E402
from duocad.topopt import acceptance, design_space as ds, stress  # noqa: E402

MODELS_DIR = REPO / "models"
OUT = REPO / "output" / "idle_validation_20260928"
MODEL = MODELS_DIR / "topopt_idle_dilution_small.json"
SIZE, CELLS = 2.0, 2


def _analyze(mask, space, cond, keep, near):
    real_eval = stress.evaluate
    captured = {}

    def evaluate(*a, **k):
        rep = real_eval(*a, **k)
        captured["report"] = rep
        return rep

    with patch.object(stress, "evaluate", evaluate):
        report = acceptance.inspect(mask, mask, space.grid, **cond.inspect_kwargs(bbox_cache=None))
    safety = report["checks"]["safety"]
    vm = np.asarray(captured["report"].field.von_mises)
    thr = safety["allowable_mpa"] / safety["target_safety_factor"]
    hot = mask & (vm > thr)
    n = int(mask.sum())
    return dict(n=n, status=report["status"], safety_factor=safety.get("safety_factor"),
                hot_total=int(hot.sum()), hot_near_ring=int((hot & near).sum()),
                hot_far=int((hot & ~near).sum()), half_pct_n=round(0.005 * n, 1))


def main() -> int:
    session = open_session(MODEL)
    doc = session.document
    bundle = optimize_run.build_conditions(doc)
    space = ds.derive(bundle.conditions, spacing=SIZE)
    opts = optimize_run.OptimizeOptions(element_size_mm=SIZE, min_width_mm=CELLS * SIZE)
    table, mapping = optimize_run._map_boundary_conditions(space, bundle)
    cond = optimize_run._build_search_conditions(doc, space, bundle, opts, table=table, mapping=mapping)
    cond = dataclasses.replace(cond, member_volume_mm3=None)  # この研究は材料全体の99.5%点(2026-10-09 より前の製品)を対象にする
    keep = np.asarray(space.model.keep, bool)
    near = ndimage.binary_dilation(keep, iterations=2)

    rows = [dict(label="full_material", **_analyze(space.mask, space, cond, keep, near))]
    for radius in (-1, 0, 1, 2, 3):
        npz = OUT / f"dilution_mask_r{radius}.npz"
        if not npz.exists():
            continue
        data = np.load(npz)
        rows.append(dict(label=f"counterfactual_r{radius}_adopted", **_analyze(data["mask"], space, cond, keep, near)))

    OUT.mkdir(parents=True, exist_ok=True)
    out_path = OUT / "dilution_hot_cells.json"
    _record.write(out_path, dict(model=str(MODEL), size=SIZE, cells=CELLS, rows=rows), default=str)
    for row in rows:
        print(json.dumps(row, ensure_ascii=False))
    print(f"書き出し: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
