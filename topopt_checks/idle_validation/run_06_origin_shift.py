"""タスク6・原点半セル移動: H1条件(2.0mm・200N・min_width実寸10mm)で格子原点を+0.5セル
(=1.0mm。全軸・x・y・zの4方向)動かし、製品コード(step20)で採点する。基準(シフト0)は`run_06_main_matrix.py`のstep20が兼ねる。

05_SYNTHETIC.md 7-1節(小型モデルで原点半セル移動だけで材料が最大50.8%変動・全条件で位相が変化)を
hanger規模で代表確認する(共通指示書「小型題材を主にする変更」節「小型が省略した現象や結論の逆転が
ある場合は必要なhanger実験を追加する」への対応)。フル8方向シフトは小型で実施済みのため、hangerでは
最も変化が大きかった全軸+0.5(05_SYNTHETIC.md 7-1節)と単軸3方向に絞る。

`synthetic_matrix_support.build_context_with_origin_shift`をそのまま再利用する(model引数を取るので
hangerでも無変更で動く)。

再開: `output/idle_validation_20260928/06_origin_shift.json`。
実行: `.venv\\Scripts\\python.exe topopt_checks\\idle_validation\\run_06_origin_shift.py`
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "topopt_checks"))
import _bootstrap  # noqa: E402,F401

import _common  # noqa: E402
import hanger_low_support as hls  # noqa: E402
import search_methods as sm  # noqa: E402
import synthetic_matrix_support as sup  # noqa: E402

LEDGER_PATH = hls.OUT / "06_origin_shift.json"
#: 全軸+0.5は05_SYNTHETIC.md 7-1節で最も変化が大きかった方向。単軸3点は、製品の`evaluation_error`打ち切りが全軸シフト固有かを見る(06_HANGER_LOW.md 4節)。
SHIFTS = {
    "hanger_h1_origin_shift_all_axes_+0.5cell": (0.5, 0.5, 0.5),
    "hanger_h1_origin_shift_x_+0.5cell": (0.5, 0.0, 0.0),
    "hanger_h1_origin_shift_y_+0.5cell": (0.0, 0.5, 0.0),
    "hanger_h1_origin_shift_z_+0.5cell": (0.0, 0.0, 0.5),
}
CASE_ID = next(iter(SHIFTS))
_LEGACY_MASK_TAG = "hanger_origin_shift_step20"  # 全軸+0.5のマスク名(06_masks/の既存ファイル名と揃える)


def _mask_tag(case_id: str) -> str:
    return _LEGACY_MASK_TAG if case_id == CASE_ID else case_id.replace("hanger_h1_origin_shift_", "hanger_origin_shift_") + "_step20"


def run(case_id: str) -> dict:
    t0 = time.time()
    spacing = hls.BASELINE["element_size_mm"]
    shift_mm = tuple(f * spacing for f in SHIFTS[case_id])
    ctx = sup.build_context_with_origin_shift(
        hls.HANGER_MODEL, load_n=hls.BASELINE["load_n"], element_size_mm=spacing,
        min_width_mm=hls.BASELINE["min_width_mm"], shift_mm=shift_mm,
    )
    achieved = [float(a) for a in ctx.grid.origin]
    out = dict(case_id=case_id, requested_shift_mm=list(shift_mm), achieved_origin_mm=achieved,
              element_size_mm=spacing, min_width_cells_resolved=ctx.min_width_cells,
              min_width_guaranteed_mm=ctx.min_width_cells * ctx.grid.spacing,
              design_elements=int(ctx.space.mask.sum()))

    tag = _mask_tag(case_id)
    prev_path = hls.MASK_DIR / f"{tag}_best_ever.npz"
    prev_mask = None
    if prev_path.is_file():
        with np.load(prev_path) as z:
            prev_mask = np.asarray(z["mask"], dtype=bool)

    print(f"[{case_id}] step20 ...", flush=True)
    with hls.capture_unknown_safety() as unknown:
        step20 = sm.run_fixed_step(ctx, max_step_ratio=0.2)
    out["step20"] = sup.record_summary(step20)
    out["step20_full"] = step20.to_json(mask_dir=hls.MASK_DIR, tag=tag)
    out["unknown_safety"] = hls.unknown_safety_summary(unknown)
    if prev_mask is not None and step20.best_ever_mask is not None:
        out["same_mask_as_first_run"] = bool(np.array_equal(prev_mask, step20.best_ever_mask))

    out["status"] = "ok"
    out["wall_seconds_total"] = time.time() - t0
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--only", nargs="*", choices=list(SHIFTS), default=None)
    args = ap.parse_args()

    ledger = _common.load_ledger(LEDGER_PATH)
    with _common.ExecutionLock(hls.LOCK, tag="run_06_origin_shift"):
        for case_id in args.only or list(SHIFTS):
            if _common.is_case_done(ledger, case_id) and not args.force:
                print(f"[{case_id}] skip(既に完了)")
                continue
            try:
                record = run(case_id)
            except Exception as exc:  # noqa: BLE001
                record = dict(case_id=case_id, status="error", error=repr(exc))
            _common.upsert_case(ledger, case_id, record)
            ledger["generated_at"] = _common.now_iso()
            ledger["code_version"] = _common.git_state()
            _common.write_atomic(LEDGER_PATH, json.dumps(ledger, ensure_ascii=False, indent=1, default=float) + "\n")
            print(f"[{case_id}] wrote {LEDGER_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
