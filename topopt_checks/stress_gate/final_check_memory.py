r"""完成形検査(`final_check.inspect`)が細かい格子の要素数・材料セル数に対してどれだけメモリを使うかを測る
(docs/TOPOPT_PEAK_GATE.md 4-11。`final_check.FINE_ELEMENT_BYTES`・`MATERIAL_CELL_BYTES` の根拠)。

製品の入口 `optimize_run.run` で完成メッシュまで作って検査の引数を保存し、分割数ごとに別プロセスで検査だけを解き直して、
検査の前後の確定メモリ(private)のピークの差と、常駐メモリ(working set)のピークの差を出す。Windows 専用。

    .venv\Scripts\python.exe topopt_checks\stress_gate\final_check_memory.py --model dilution --refine 2 3 4 5
"""
from __future__ import annotations

import argparse
import json
import pickle
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import phase_a_fields as pa  # noqa: E402

import numpy as np  # noqa: E402
import psutil  # noqa: E402

import refine_shape  # noqa: E402
from duocad import optimize_run  # noqa: E402
from duocad.topopt import final_check  # noqa: E402


def capture(spec, path: Path) -> None:
    doc = refine_shape.open_session(spec["model"]).document
    refine_shape._set_load_doc(doc, spec["load_n"])
    real = final_check.inspect

    def inspect(*args, **kwargs):
        path.write_bytes(pickle.dumps((args, kwargs)))
        return real(*args, **kwargs)

    with patch.object(final_check, "inspect", inspect):
        optimize_run.run(doc, optimize_run.OptimizeOptions(element_size_mm=spec["h"], min_width_mm=spec["w"]))


def measure(path: Path, k: int) -> dict:
    args, kwargs = pickle.loads(path.read_bytes())
    lo, hi = final_check._crop(kwargs["grid"], np.asarray(args[0], dtype=float))
    process = psutil.Process()
    before = process.memory_info()
    started = time.perf_counter()
    spacing = float(kwargs["grid"].spacing) / k
    with patch.object(final_check, "MEASURE_SPACING_MM", spacing * (1 + 1e-6)), \
            patch.object(final_check, "fits", lambda *_: True):
        checked = final_check.inspect(*args, **kwargs)
    after = process.memory_info()
    safety = checked.get("safety") or {}
    return dict(k=k, fine_elements=int(np.prod(hi - lo)) * k ** 3, material_cells=checked.get("material_cells"),
                spacing_mm=safety.get("spacing_mm"), solver=safety.get("solver"), status=safety.get("status"),
                top_area_mpa=safety.get("top_area_mpa"), peak_mpa=safety.get("peak_mpa"),
                seconds=round(time.perf_counter() - started, 1), wset_before_mb=before.wset / 2 ** 20,
                peak_wset_before_mb=before.peak_wset / 2 ** 20, peak_wset_after_mb=after.peak_wset / 2 ** 20,
                used_mb=(after.peak_wset - before.wset) / 2 ** 20,
                # 確定メモリ(private)のピーク。空きが減ると working set は削られて実際より小さく出る
                committed_mb=(after.peak_pagefile - before.pagefile) / 2 ** 20)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", required=True, choices=list(pa.MODELS))
    ap.add_argument("--h", type=float, default=None)
    ap.add_argument("--w", type=float, default=None)
    ap.add_argument("--refine", nargs="*", type=int, default=[3, 4, 5])
    ap.add_argument("--measure", type=int, default=None, help="内部用: 保存した引数で分割数 k の検査だけを解く")
    ap.add_argument("--args", type=Path, default=None)
    a = ap.parse_args()
    if a.measure is not None:
        print(json.dumps(measure(a.args, a.measure)), flush=True)
        return 0
    spec = dict(pa.MODELS[a.model])
    for key, val in (("h", a.h), ("w", a.w)):
        if val is not None:
            spec[key] = val
    out_dir = pa.OUT / "peak_gate"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"final_check_memory_{a.model}_h{spec['h']:g}_w{spec['w']:g}"
    args_path = out_dir / f"{stem}.pickle"
    if not args_path.exists():
        capture(spec, args_path)
    rows = []
    args, kwargs = pickle.loads(args_path.read_bytes())
    lo, hi = final_check._crop(kwargs["grid"], np.asarray(args[0], dtype=float))
    for k in a.refine:
        # 1要素 6 KB と見て空きの 6 割を超える分割は解かない(2026-09-26 にメモリ不足で PC が落ちた)
        if int(np.prod(hi - lo)) * k ** 3 * 6000 > 0.6 * psutil.virtual_memory().available:
            rows.append(dict(k=k, skipped="memory"))
            print(f"k{k} skipped (memory)", flush=True)
            continue
        done = subprocess.run([sys.executable, __file__, "--model", a.model, "--measure", str(k), "--args", str(args_path)],
                              capture_output=True, text=True, encoding="utf-8")
        if done.returncode != 0:
            rows.append(dict(k=k, error=done.stderr[-2000:]))
            print(f"k{k} failed", done.stderr[-500:], flush=True)
            continue
        row = json.loads(done.stdout.strip().splitlines()[-1])
        rows.append(row)
        print(json.dumps(row), flush=True)
    (out_dir / f"{stem}.json").write_text(json.dumps(dict(model=a.model, spec={k: spec[k] for k in ("load_n", "h", "w")},
                                                          rows=rows), indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
