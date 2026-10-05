"""タスク7・全材料(設計空間のすべて)の1回FEM診断。500N/1000N(2.0mm)。

2000Nの全材料は `07_methods.json` の適応3本の初期形(=最良形)が既に持つ。M5b(raw/目標換算)ゲートが
荷重ごとに「探索の出発点(全材料)で既に通るか」を確かめるための固定形状診断で、新規最適化ではない。
最小幅は全材料の形に関係しない(幅キーは build_ctx の引数合わせ)。

実行: `.venv/Scripts/python.exe topopt_checks/idle_validation/run_07_full_material_probe.py --case F0500`
台帳: `output/idle_validation_20260928/07_full_material_probe.json`
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
import hanger_loads_support as hl  # noqa: E402

LEDGER_PATH = hl.OUT / "07_full_material_probe.json"
LOADS = {"F0500": 500.0, "F1000": 1000.0}
SIZE_MM = 2.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", choices=sorted(LOADS), required=True)
    args = ap.parse_args()
    case_id = f"{args.case}_full_material_h{SIZE_MM:g}"
    t0 = time.time()
    with hl.acquire_fem_lock("run_07_full_material_probe"):
        ctx = hl.build_ctx(LOADS[args.case], "thick", SIZE_MM)
        mask = ctx.space.mask.copy()
        diagnosis = hl.diagnose_mask(mask, ctx, label=case_id)
        record = dict(case_id=case_id, status=diagnosis.get("status"), load_n=LOADS[args.case], element_size_mm=SIZE_MM,
                      element_size_resolved_mm=float(ctx.grid.spacing), material_cells=int(mask.sum()),
                      material_mm3=float(mask.sum() * ctx.grid.element_volume), diagnosis=diagnosis,
                      wall_seconds_total=time.time() - t0, **_common.peak_memory_mb())
        ledger = _common.load_ledger(LEDGER_PATH)
        _common.upsert_case(ledger, case_id, record)
        ledger["generated_at"] = _common.now_iso()
        ledger["code_version"] = _common.git_state()
        _common.write_atomic(LEDGER_PATH, json.dumps(ledger, ensure_ascii=False, indent=1, default=float) + "\n")
    print(f"[{case_id}] {record['status']} ({record['wall_seconds_total']:.0f} s)", flush=True)
    return 0 if record["status"] == "ok" else 3


if __name__ == "__main__":
    raise SystemExit(main())
