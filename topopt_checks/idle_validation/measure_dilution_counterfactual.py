"""原因1(母数希釈)の認定: `topopt_idle_dilution_small.json` で、代表応力の母集団から
保持リング+周りRセルを除いた版と製品どおりの版を比べる。手口は
`topopt_checks/measure_idle_counterfactual.py`(hanger専用)と同じ(製品コードは変えず、
このプロセスの中だけで `stress.representative_stress` を差し替える。探索の後で止める)。

    .\\.venv\\Scripts\\python.exe topopt_checks\\idle_validation\\measure_dilution_counterfactual.py --radius 2
    .\\.venv\\Scripts\\python.exe topopt_checks\\idle_validation\\measure_dilution_counterfactual.py --radius -1   # 製品どおり(比較の基準)

`--radius -1` は差し替えなし(基準)。`--radius 0..N` は保持セル(0)またはそこから
Rセル以内を母集団から除く。結果は `output/idle_validation_20260928/dilution_counterfactual_r<R>.json`
+ 採用マスクの `.npz`。仕上げ(平滑化・STEP書き出し)は回さない(探索直後で止める)。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
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
from duocad.topopt import acceptance, binary_search, design_space as ds, stress, trace  # noqa: E402

MODELS_DIR = REPO / "models"
OUT = REPO / "output" / "idle_validation_20260928"


class _Stop(Exception):
    pass


def run_counterfactual(model: Path, *, size: float, cells: int, radius: int, max_step_ratio: float | None) -> dict:
    real_derive, real_rep, real_search = ds.derive, stress.representative_stress, binary_search.find_best_feasible
    captured: dict = {}
    history: list = []

    def derive(*a, **k):
        space = real_derive(*a, **k)
        captured["space"] = space
        if radius >= 0:
            keep = np.asarray(space.model.keep, dtype=bool)
            captured["exclude"] = ndimage.binary_dilation(keep, iterations=radius) if radius > 0 else keep
        return space

    def rep(field, rho, **k):
        full = real_rep(field, rho, **k)
        if "exclude" in captured:
            density = np.array(rho, dtype=float, copy=True)
            density[captured["exclude"]] = 0.0
            patched = real_rep(field, density, **k)
        else:
            patched = full
        history.append(dict(n=int((np.asarray(rho) >= 0.5).sum()), full_mpa=full.value, used_mpa=patched.value))
        return patched

    def search(*a, **k):
        if max_step_ratio is not None:
            k["max_step_ratio"] = max_step_ratio
        t0 = time.perf_counter()
        r = real_search(*a, **k)
        captured["search"], captured["seconds"] = r, time.perf_counter() - t0
        raise _Stop()

    OUT.mkdir(parents=True, exist_ok=True)
    with patch.object(trace, "DEFAULT_ROOT", OUT / "_trace"), patch.object(ds, "derive", derive), \
            patch.object(stress, "representative_stress", rep), patch.object(binary_search, "find_best_feasible", search):
        session = open_session(model)
        doc = session.document
        try:
            optimize_run.run(doc, optimize_run.OptimizeOptions(element_size_mm=size, min_width_mm=cells * size))
        except _Stop:
            pass

    r = captured["search"]
    best = r.best_feasible
    space = captured["space"]
    record = dict(model=str(model), size=size, cells=cells, radius=radius, max_step_ratio=max_step_ratio,
                  status=r.status, termination=r.termination, evaluations=r.evaluations,
                  seconds=captured["seconds"], design_elements=int(space.element_count),
                  material_mm3=None if best is None else float(best.material_mm3),
                  cells_final=None if best is None else int(best.mask.sum()),
                  safety_factor_used=None if best is None else float(best.safety_factor),
                  history=history)
    if best is not None:
        # 実際の(希釈していない)代表応力で再評価する — 探索が「合格」と判定したのは
        # 差し替え済みの母集団に対してであり、製品どおりの安全率とは別物(原因1の核心)。
        table, mapping = optimize_run._map_boundary_conditions(space, optimize_run.build_conditions(doc))
        conditions = optimize_run._build_search_conditions(
            doc, space, optimize_run.build_conditions(doc),
            optimize_run.OptimizeOptions(element_size_mm=size, min_width_mm=cells * size), table=table, mapping=mapping)
        recheck = acceptance.inspect(best.mask, best.mask, space.grid, **conditions.inspect_kwargs(bbox_cache=None))
        record["recheck_with_full_percentile"] = dict(
            status=recheck["status"],
            safety_factor=recheck["checks"]["safety"].get("safety_factor"),
            representative_mpa=recheck["checks"]["safety"].get("representative_mpa"),
            target_safety_factor=recheck["checks"]["safety"].get("target_safety_factor"),
        )
        np.savez_compressed(OUT / f"dilution_mask_r{radius}.npz", mask=best.mask,
                            keep=np.asarray(space.model.keep, bool),
                            origin=np.asarray(space.grid.origin, float), spacing=float(space.grid.spacing))
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="topopt_idle_dilution_small.json")
    parser.add_argument("--size", type=float, default=2.0)
    parser.add_argument("--cells", type=int, default=2)
    parser.add_argument("--radius", type=int, required=True, help="-1=製品どおり(差し替えなし)、0以上=保持からRセル以内を除く")
    parser.add_argument("--max-step-ratio", type=float, default=None)
    args = parser.parse_args()
    record = run_counterfactual(MODELS_DIR / args.model, size=args.size, cells=args.cells,
                                radius=args.radius, max_step_ratio=args.max_step_ratio)
    out_path = OUT / f"dilution_counterfactual_r{args.radius}.json"
    _record.write(out_path, record, default=str)
    summary = {k: v for k, v in record.items() if k != "history"}
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    print(f"書き出し: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
