"""タスク10: 検証済み条件で、最小幅の規則による設計空間の芯が1つにまとまっているかを数える(FEMなし)。

`binary_search.find_initial_feasible` が `diagnostics['min_width']['design_core_groups']` へ記録するのと
同じ手順(設計空間をm立方でopening→保持セルを対象外にして芯の6連結成分を数える)を、探索を走らせずに
設計空間だけに対して行う。TOPOPT.md「最小幅の保証」の本人判断待ち(芯が割れた入力への対処)に、
検証した条件で割れが起きているかの実測を添えるためのもの。
実行: `.venv/Scripts/python.exe topopt_checks/idle_validation/check_10_core_groups.py --case hanger:2.4:5`
(ケース=`hanger:<要素mm>:<最小幅mm>` / `dilution:<要素mm>:<最小幅mm>` / `path:<要素mm>:<最小幅mm>`)
出力: `output/idle_validation_20260928/10_work/core_groups.json`(ケースごとに追記・上書き)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "topopt_checks"))
import _bootstrap  # noqa: E402,F401

import _common  # noqa: E402
import synthetic_matrix_support as sup  # noqa: E402
import hanger_loads_support as hl  # noqa: E402
import search_methods as sm  # noqa: E402
from duocad.topopt import binary_search, min_width  # noqa: E402

OUT = REPO / "output" / "idle_validation_20260928" / "10_work" / "core_groups.json"
MODELS = {"hanger": (hl.HANGER_MODEL, 200.0), "dilution": (sup.DILUTION_MODEL, 300.0), "path": (sup.PATH_MODEL, 3000.0)}


def measure(kind: str, size_mm: float, width_mm: float) -> dict:
    model, load = MODELS[kind]
    t0 = time.time()
    ctx = sm.build_context(model, load_n=load, element_size_mm=size_mm, min_width_mm=width_mm)
    m = int(ctx.min_width_cells)
    design = ctx.space.mask.copy()
    exempt = binary_search._min_width_exempt(ctx.cond, design.shape, m)
    opened = min_width.cube_opening(design, m)
    if exempt is not None:
        opened |= design & exempt
    groups = int(min_width.CoreConnectivity(m, exempt).groups(opened))
    return dict(kind=kind, element_size_mm=size_mm, requested_width_mm=width_mm,
                realized_spacing_mm=float(ctx.grid.spacing), min_width_cells=m,
                realized_width_mm=m * float(ctx.grid.spacing), design_cells=int(design.sum()),
                trimmed_cells=int(design.sum() - opened.sum()), design_core_groups=groups,
                wall_seconds=round(time.time() - t0, 1))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", required=True, action="append")
    args = ap.parse_args()
    for spec in args.case:
        kind, size, width = spec.split(":")
        rec = measure(kind, float(size), float(width))
        ledger = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {"cases": {}}
        ledger["cases"][spec] = rec
        ledger["generated_at"] = _common.now_iso()
        ledger["code_version"] = _common.git_state()
        _common.write_atomic(OUT, json.dumps(ledger, ensure_ascii=False, indent=1) + "\n")
        print(json.dumps(rec, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
