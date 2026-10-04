"""保存済みの場で応力集約と探索軌道を比較する。FEM・製品コードの変更は行わない。

    .venv/Scripts/python.exe topopt_checks/analyze_idle_alternatives.py
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from scipy import ndimage


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output/idle_alternatives_20260928"
RUNS = {
    "issue93": ROOT / "output/optimization_runs/20260925T035538-4eba5fbaf3f8",
    "base_200_h2_w5": ROOT / "output/idle_study/_trace/20260927T064844-ad08174c1ce0",
    "R2_200_h2_w5": ROOT / "output/idle_study/_trace/20260927T061944-af912c1275b6",
    "step05_200_h2_w5": ROOT / "output/idle_study/_trace/20260927T091728-3b27ff6e92da",
    "R2_2000_h24_w2": ROOT / "output/idle_study/_trace/20260927T052611-acd9f5077145",
}


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def array(run, ref):
    with np.load(run / ref["$array"]) as data:
        return data[data.files[0]]


def pairs(run):
    inputs = sorted(run.glob("*-stress-input.json"))
    outputs = sorted(run.glob("*-stress-output.json"))
    assert len(inputs) == len(outputs) and inputs
    for ip, op in zip(inputs, outputs):
        assert int(op.name[:6]) == int(ip.name[:6]) + 2
    return list(zip(inputs, outputs))


def metrics(values, reference_count, tail_count, spacing):
    values = np.sort(values)
    assert len(values) >= tail_count
    p8_sum = float(np.sum(values**8))
    return {
        "count": len(values),
        "q995_mpa": float(np.percentile(values, 99.5)),
        "sf_q995": float(40 / np.percentile(values, 99.5)),
        "fixed_rank_mpa": float(values[-tail_count]),
        "fixed_top_mean_mpa": float(values[-tail_count:].mean()),
        "p8_current_mean_mpa": (p8_sum / len(values)) ** (1 / 8),
        "p8_fixed_reference_mpa": (p8_sum / reference_count) ** (1 / 8),
        "exceed_volume_mm3": float(np.count_nonzero(values > 40 / 4.8) * spacing**3),
        "max_mpa": float(values[-1]),
    }


def frozen_field(name, run):
    ip, op = pairs(run)[-1]
    inp, rep = read(ip), read(op)
    mask = array(run, inp["rho"]) >= 0.5
    vm = array(run, rep["field"]["von_mises"])
    derived = read(next(run.glob("*-derive-output.json")))
    keep = array(run, derived["model"]["keep"])
    near = ndimage.binary_dilation(keep, iterations=2)
    spacing = inp["scale"]["element_mm"]
    rows = []
    for region, selector in (("full", mask), ("outside_keep_R2", mask & ~near)):
        values = np.sort(vm[selector])
        n = len(values)
        k = max(1, int(np.ceil(0.005 * n)))
        for fraction in (0, 0.02, 0.05, 0.10):
            selected = values[int(np.floor(fraction * n)):]
            rows.append(dict(region=region, removed_fraction=fraction,
                             fixed_tail_count=k, fixed_tail_volume_mm3=k * spacing**3,
                             **metrics(selected, n, k, spacing)))
    return dict(name=name, input=str(ip.relative_to(ROOT)), output=str(op.relative_to(ROOT)),
                recorded_sf=rep["safety_factor"], spacing=spacing, rows=rows)


def compliance_history(run):
    result = []
    previous_mask = None
    previous_load = None
    for index, (ip, op) in enumerate(pairs(run)):
        inp, rep = read(ip), read(op)
        mask = array(run, inp["rho"]) >= 0.5
        u = array(run, inp["u"])
        f = array(run, inp["model"]["f"])
        if previous_load is not None:
            assert np.array_equal(f, previous_load)
            assert not np.any(mask & ~previous_mask)
        scale = inp["scale"]
        factor = (scale["load_n"] / scale["solved_load"]
                  * scale["solved_youngs_modulus"] / scale["youngs_modulus_mpa"]
                  / scale["element_mm"])
        c = float(f @ u) * factor
        row = dict(evaluation=index, n=int(mask.sum()), safety_factor=rep["safety_factor"],
                   compliance_n_mm=c, max_displacement_mm=rep["max_displacement_mm"])
        if result:
            row["compliance_ratio"] = c / result[-1]["compliance_n_mm"]
            row["removed_fraction"] = 1 - row["n"] / result[-1]["n"]
        result.append(row)
        previous_mask, previous_load = mask, f
    return result


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    frozen = [frozen_field(name, run) for name, run in RUNS.items()]
    history = compliance_history(RUNS["base_200_h2_w5"])
    result = dict(method="Stored fields only; no new FEM or geometry feasibility checks.",
                  frozen_fields=frozen, compliance_history=history)
    (OUT / "analysis.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    for item in frozen:
        for region in ("full", "outside_keep_R2"):
            rows = [r for r in item["rows"] if r["region"] == region]
            assert all(r["fixed_rank_mpa"] == rows[0]["fixed_rank_mpa"] for r in rows)
            assert all(r["fixed_top_mean_mpa"] == rows[0]["fixed_top_mean_mpa"] for r in rows)
            assert all(r["exceed_volume_mm3"] == rows[0]["exceed_volume_mm3"] for r in rows)
            print(item["name"], region, "SF at 0/2/5/10% removal:",
                  [round(r["sf_q995"], 4) for r in rows], flush=True)
    for row in history:
        if row["evaluation"] in (0, 5, 6, 9, 12, 13, 20):
            print(json.dumps(row), flush=True)


if __name__ == "__main__":
    main()
