"""タスク7・旧64条件の共通評価での再採点。

`output/smoothing_corpus/` の検証材料64条件(荷重4 x 要素サイズ4 x 最小幅2〜5セル。製品step20が出した形)を、
同じ荷重・格子・最小幅(セル数指定のまま)で解き直し、03bの評価器(M5b/M3/M7b)と
`hanger_loads_support.diagnose_mask`(4ゾーン・高応力域の塊と支持への帰属・支持別反力・低寄与材料)で採点する。
**旧64条件はセル数指定の比較であり、06/07の新規探索(実寸mm固定)と混ぜない**(10_SYNTHESIS 付録A(旧 01_PROTOCOL 4節))。

保存場(応力場)はコーパスに無い(マスクのみ)ので、各条件を1回FEMで解き直す。マスクは保存時の格子
(origin・spacing・shape)が今回の `build_context` と一致することを確認してから使う。一致しなければ素の投影
(島処理なし)をして `grid_match=false` を記録する。

再開: `output/idle_validation_20260928/07_corpus_rescore.json`(条件単位)。
実行: `.venv\\Scripts\\python.exe topopt_checks\\idle_validation\\analyze_07_corpus_rescore.py [--only <name>...] [--force]`
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "topopt_checks"))
import _bootstrap  # noqa: E402,F401

import _common  # noqa: E402
import hanger_loads_support as hl  # noqa: E402
import hanger_low_support as hls  # noqa: E402
import search_methods as sm  # noqa: E402

CORPUS = REPO / "output" / "smoothing_corpus"
LEDGER_PATH = hl.OUT / "07_corpus_rescore.json"
OLD_FIELDS = ("load_n", "element_size_requested_mm", "element_size_resolved_mm", "cells_requested", "evaluations",
              "termination", "search_status", "material_mm3", "safety_factor", "mask_cells", "total_seconds",
              "peak_working_set_mb", "commit", "dirty_product_code")


def _rows() -> list[dict]:
    manifest = json.loads((CORPUS / "manifest.json").read_text(encoding="utf-8"))
    return sorted(manifest["rows"], key=lambda r: (r["load_n"], r["element_size_requested_mm"], r["cells_requested"]))


def run_case(row: dict) -> dict:
    t0 = time.time()
    name = row["name"]
    # 保存時に解決された格子幅を要素サイズに渡す: 要求値(例2.4mm)を今のコードで解決すると保存時と違う格子幅
    # (2.4mm要求は保存時2.3875mm、今は2.406mm)になる条件があるため。最小幅の下限(2x格子幅)に丸め誤差で触れないよう
    # 最小幅mmは少し余裕を持たせて構築し、セル数は保存時の指定(cells_requested)で上書きする。
    resolved = float(row["element_size_resolved_mm"])
    ctx = sm.build_context(hls.HANGER_MODEL, load_n=float(row["load_n"]), element_size_mm=resolved,
                           min_width_mm=max(float(row["min_width_request_mm"]), 2.0 * resolved * (1.0 + 1e-9)))
    ctx = dataclasses.replace(ctx, min_width_cells=int(row["cells_requested"]))
    out = dict(case_id=name, source="output/smoothing_corpus (セル数指定。実寸mm固定の新規探索とは別表)",
               old={k: row.get(k) for k in OLD_FIELDS},
               min_width_cells_requested=int(row["cells_requested"]), min_width_cells_resolved=int(ctx.min_width_cells),
               min_width_realized_mm=float(ctx.min_width_cells * ctx.grid.spacing),
               design_elements=int(ctx.space.mask.sum()))
    with np.load(CORPUS / row["npz"]) as z:
        src_mask = np.asarray(z["mask"], dtype=bool)
        src_origin = np.asarray(z["origin"], dtype=float)
        src_spacing = float(z["spacing"])
    same = (src_mask.shape == tuple(ctx.grid.shape) and np.allclose(src_origin, ctx.grid.origin, atol=1e-6)
            and abs(src_spacing - float(ctx.grid.spacing)) < 1e-9)
    out["grid_match"] = bool(same)
    if same:
        mask = src_mask & ctx.space.mask
    else:
        mask = hls.project_mask_raw(src_mask, src_origin, src_spacing, ctx)
    out["mask_cells_in_design_space"] = int(mask.sum())
    out["mask_cells_in_saved_file"] = int(src_mask.sum())
    diag = hl.diagnose_mask(mask, ctx, label=name)
    out["diagnosis"] = diag
    out["status"] = "ok" if diag.get("status") == "ok" else "error"
    if out["status"] == "error":
        out["error"] = f"diagnose: {diag.get('fem_status')} {diag.get('error')}"
    out["wall_seconds_total"] = time.time() - t0
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", default=None)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    rows = [r for r in _rows() if not args.only or r["name"] in args.only]
    ledger = _common.load_ledger(LEDGER_PATH)
    with hl.acquire_fem_lock("analyze_07_corpus_rescore"):
        for row in rows:
            name = row["name"]
            if _common.is_case_done(ledger, name) and not args.force:
                print(f"[{name}] skip(既に完了)", flush=True)
                continue
            print(f"[{name}] rescore ...", flush=True)
            try:
                record = run_case(row)
            except Exception as exc:  # noqa: BLE001
                record = dict(case_id=name, status="error", error=repr(exc))
            _common.upsert_case(ledger, name, record)
            ledger["generated_at"] = _common.now_iso()
            ledger["code_version"] = _common.git_state()
            _common.write_atomic(LEDGER_PATH, json.dumps(ledger, ensure_ascii=False, indent=1, default=float) + "\n")
            print(f"[{name}] {record['status']} ({record.get('wall_seconds_total', 0):.0f}s)", flush=True)
    errors = [c["case_id"] for c in ledger.get("cases", []) if c.get("status") == "error"]
    if errors:
        print(f"LEDGER_ERRORS {errors}", flush=True)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
