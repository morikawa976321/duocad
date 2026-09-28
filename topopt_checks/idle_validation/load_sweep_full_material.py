"""全材料(探索なし)の dilution 模型で荷重スイープし、線形弾性の整合を記録するCLI。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "topopt_checks"))
import _bootstrap  # noqa: E402,F401

import numpy as np  # noqa: E402

from _documents import open_session  # noqa: E402
from duocad import optimize_run  # noqa: E402
from duocad.topopt import design_space as ds, stress  # noqa: E402

import _common  # noqa: E402
import _fem_probe  # noqa: E402

OUT = REPO / "output" / "idle_validation_20260928"
MODEL_PATH = REPO / "models" / "topopt_idle_dilution_small.json"
ELEMENT_MM = 2.0
LOADS_N = [150.0, 200.0, 250.0, 300.0, 350.0]
OUT_JSON = OUT / "03a_load_sweep_full_material.json"


def _set_load_doc(doc, newtons: float) -> None:
    for load in doc.optimize.loads:
        load["magnitude"] = float(newtons)
        vec = load.get("vector")
        if isinstance(vec, (list, tuple)) and len(vec) == 3 and all(
            isinstance(c, (int, float)) for c in vec
        ):
            norm = float(sum(c * c for c in vec)) ** 0.5 or 1.0
            load["vector"] = [c / norm * float(newtons) for c in vec]
        else:
            load["vector"] = [c * float(newtons) for c in load["direction"]]


def _evaluate_load(load_n: float) -> dict:
    doc = open_session(MODEL_PATH).document
    _set_load_doc(doc, load_n)
    bundle = optimize_run.build_conditions(doc)
    space = ds.derive(bundle.conditions, spacing=ELEMENT_MM)
    mask = np.asarray(space.mask, dtype=bool)
    table, mapping = optimize_run._map_boundary_conditions(space, bundle)
    cond = optimize_run._build_search_conditions(
        doc,
        space,
        bundle,
        optimize_run.OptimizeOptions(element_size_mm=ELEMENT_MM),
        table=table,
        mapping=mapping,
    )
    with _common.SingleFemRun():
        probe = _fem_probe.evaluate_full(
            mask,
            space.grid,
            table,
            cond.fixed_faces,
            cond.load_faces,
            cond.forces,
            allowable_mpa=cond.allowable_mpa,
            youngs_modulus_mpa=cond.youngs_modulus_mpa,
            percentile=cond.percentile or stress.DEFAULT_PERCENTILE,
            solver="auto",
        )
    sf = probe.get("safety_factor")
    rep = probe.get("representative_mpa")
    product = float(sf) * float(load_n) if sf is not None and probe["status"] == "ok" else None
    return dict(
        condition_id=_common.condition_id(load_n=load_n, element_mm=ELEMENT_MM, full_material=True),
        load_n=load_n,
        status=probe["status"],
        safety_factor=sf,
        representative_mpa=rep,
        linearity_check_product=product,
        error=probe.get("error"),
        probe_status=probe.get("status"),
        material_elements=int(mask.sum()),
        design_elements=int(space.mask.sum()),
    )


def _print_table(rows: list[dict]) -> None:
    keys = ["load_n", "safety_factor", "representative_mpa", "linearity_check_product", "status"]
    print(" ".join(f"{k:>24}" for k in keys))
    for row in rows:
        print(" ".join(f"{str(row.get(k, '')):>24}"[:24] for k in keys))


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    records = []
    for load_n in LOADS_N:
        records.append(_evaluate_load(load_n))

    payload = dict(
        run_id=_common.condition_id(sweep="full_material_dilution", element_mm=ELEMENT_MM),
        model_path=str(MODEL_PATH.relative_to(REPO)),
        model_sha256=_common.sha256_file(MODEL_PATH),
        element_mm=ELEMENT_MM,
        loads_n=LOADS_N,
        generated_at=_common.now_iso(),
        code_version=_common.git_state(),
        records=records,
    )
    _common.write_atomic(OUT_JSON, json.dumps(payload, ensure_ascii=False, indent=1, default=float) + "\n")
    _print_table(records)
    print(f"wrote {OUT_JSON.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
