"""タスク6・格子依存: 実寸min_width=10mm固定のまま要素サイズ1.8/2.2/2.4mmを振り、製品コード(step20)
で採点する。2.0mm(BASELINE)は`run_06_main_matrix.py`が兼ねるのでここでは実行しない。

REVIEW_00_02_2026-09-28.md「2.0 mmと2.2 mmの差は格子だけの差ではない(w5=5セルだと実現mmが変わる)」
への対応——本スクリプトは全解像度で`min_width_mm=10.0`を明示指定し、`ctx.min_width_guaranteed_mm`
(=解決後セル数×spacing)を記録することで、要求mmと実現mmの丸め差を格子依存と分離する
(共通指示書「単位と分解能を固定する」)。

2.4mm結果のマスク・格子origin/spacingは`run_06_initshape.py`が「粗い骨組み開始」の投影元として使う
(このスクリプトを先に完走させておくこと)。

再開: `output/idle_validation_20260928/06_grid_resolution.json`。
実行: `.venv\\Scripts\\python.exe topopt_checks\\idle_validation\\run_06_grid_resolution.py`
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

LEDGER_PATH = hls.OUT / "06_grid_resolution.json"


def _case_id(element_size_mm: float) -> str:
    return f"hanger_res{element_size_mm:g}mm_w10mm"


def run_case(element_size_mm: float) -> dict:
    t0 = time.time()
    ctx = sm.build_context(hls.HANGER_MODEL, load_n=hls.BASELINE["load_n"], element_size_mm=element_size_mm,
                           min_width_mm=hls.BASELINE["min_width_mm"])
    tag = _case_id(element_size_mm)
    out = dict(case_id=tag, element_size_mm=element_size_mm, element_size_resolved_mm=ctx.grid.spacing,
              min_width_mm_requested=hls.BASELINE["min_width_mm"], min_width_cells_resolved=ctx.min_width_cells,
              min_width_guaranteed_mm=ctx.min_width_cells * ctx.grid.spacing,
              grid_origin_mm=list(ctx.grid.origin), grid_spacing_mm=float(ctx.grid.spacing),
              design_elements=int(ctx.space.mask.sum()))

    prev_path = hls.MASK_DIR / f"{tag}_step20_best_ever.npz"
    prev_mask = None
    if prev_path.is_file():
        with np.load(prev_path) as z:
            prev_mask = np.asarray(z["mask"], dtype=bool)

    print(f"[{tag}] step20 ...", flush=True)
    with hls.capture_unknown_safety() as unknown:
        step20 = sm.run_fixed_step(ctx, max_step_ratio=0.2)
    out["step20"] = sup.record_summary(step20)
    out["step20_full"] = step20.to_json(mask_dir=hls.MASK_DIR, tag=f"{tag}_step20")
    out["unknown_safety"] = hls.unknown_safety_summary(unknown)
    if prev_mask is not None and step20.best_ever_mask is not None:
        out["same_mask_as_first_run"] = bool(np.array_equal(prev_mask, step20.best_ever_mask))

    try:
        truss_only = hls.truss_only_cells(ctx, resolve_islands=False)
        out["truss_only_definition"] = "投影のみ(島処理なし)。2.0mm以外の格子では島処理付きが空集合になる"
        out["truss_only_cells_total"] = int(truss_only.sum())
        out["step20_truss_residual_fraction"] = (
            hls.truss_residual_fraction(step20.best_ever_mask, truss_only) if step20.best_ever_mask is not None else None
        )
    except Exception as exc:  # noqa: BLE001  step20は数時間かかるので、後処理の失敗で記録を失わない
        out["truss_error"] = repr(exc)

    out["status"] = "ok"
    out["wall_seconds_total"] = time.time() - t0
    return out


def _record_baseline_truss_reference(ledger: dict) -> None:
    """2.0mm(main_matrixのstep20)を、他解像度と同じ「島処理なしの投影」定義で測った残存率。
    `06_main_matrix.json`の値は島処理付きの定義なので、解像度間の比較にはこちらを使う。"""
    import run_06_main_matrix as main_matrix

    record = next((r for r in _common.load_ledger(main_matrix.LEDGER_PATH).get("cases", [])
                   if r.get("case_id") == hls.BASELINE["tag"] and r.get("status") == "ok"), None)
    if record is None:
        return
    ctx = sm.build_context(hls.HANGER_MODEL, load_n=hls.BASELINE["load_n"],
                           element_size_mm=hls.BASELINE["element_size_mm"], min_width_mm=hls.BASELINE["min_width_mm"])
    with np.load(REPO / record["step20_full"]["best_ever_mask_path"]) as z:
        mask = np.asarray(z["mask"], dtype=bool)
    truss_only = hls.truss_only_cells(ctx, resolve_islands=False)
    ledger["baseline_2.0mm_raw_truss_definition"] = dict(
        truss_only_cells_total=int(truss_only.sum()),
        step20_truss_residual_fraction=hls.truss_residual_fraction(mask, truss_only))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", type=float, default=None, help="要素サイズ(mm)を指定して絞り込む")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    sizes = list(hls.EXTRA_RESOLUTIONS_MM)
    if args.only:
        sizes = [s for s in sizes if s in args.only]

    ledger = _common.load_ledger(LEDGER_PATH)
    _record_baseline_truss_reference(ledger)
    with _common.ExecutionLock(hls.LOCK, tag="run_06_grid_resolution"):
        for size in sizes:
            tag = _case_id(size)
            if _common.is_case_done(ledger, tag) and not args.force:
                print(f"[{tag}] skip(既に完了)")
                continue
            try:
                record = run_case(size)
            except Exception as exc:  # noqa: BLE001
                record = dict(case_id=tag, status="error", error=repr(exc))
            _common.upsert_case(ledger, tag, record)
            ledger["generated_at"] = _common.now_iso()
            ledger["code_version"] = _common.git_state()
            _common.write_atomic(LEDGER_PATH, json.dumps(ledger, ensure_ascii=False, indent=1, default=float) + "\n")
            print(f"[{tag}] done, wrote {LEDGER_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
