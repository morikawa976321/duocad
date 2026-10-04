"""段階2: 07 の保存マスクから、製品の組立コードで計測用の K_ff・右辺を作って保存する。

**製品の `.venv` の python で、リポジトリ直下から実行する**(検証用 venv には duocad の依存が無い)。

    .venv/Scripts/python.exe gpu_feasibility/scripts/s2_build_matrices.py F0200_thin5mm_h2.4_step20:0 ...

引数は `<07_masks のファイル名の幹>:<膨らませる層数>`。層数 > 0 は探索序盤の太い形の代わりで、
設計空間の内側へ 6 近傍で膨らませる。CG は回さない(組立と切り出しまで)。
FEM ロックは取らない。07 の成果物は読むだけ。
"""
from __future__ import annotations

import datetime as dt
import gc
import json
import os
import re
import sys
import time
from pathlib import Path

os.environ.setdefault("TOPOPT_THREADS", "1")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
REPO = ROOT.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "topopt_checks"))
sys.path.insert(0, str(REPO / "topopt_checks" / "idle_validation"))

import numpy as np  # noqa: E402
from scipy import ndimage  # noqa: E402

import _winmem  # noqa: E402
import search_methods as sm  # noqa: E402
from duocad.topopt import acceptance, anchor, fem, stress  # noqa: E402

MASK_DIR = REPO / "output" / "idle_validation_20260928" / "07_masks"
HANGER_MODEL = REPO / "models" / "hanger_bar_attachment.json"
DATA = ROOT / "data"
RESULTS = ROOT / "results"

# 足りなければ組まずに飛ばすための見積もり(実測ではない): 1 セル 576 項 ×(int64 の添字 2 本 + 値 = 24B)が
# `fem.Model` に常駐し、CSC 変換の作業域として同じ量を上乗せした。実際のピークは結果の `peak_working_set_gb`。
BYTES_PER_CELL_ESTIMATE = 576 * 48
RAM_MARGIN_GB = 2.0

_TAG = re.compile(r"F(?P<load>\d+)_(?P<width>thin5|thick10)mm_h(?P<size>[\d.]+)_")


def build(spec: str, contexts: dict) -> dict:
    stem, _, layers = spec.partition(":")
    layers = int(layers or 0)
    tag = _TAG.match(stem)
    if tag is None:
        raise ValueError(f"マスク名から荷重・格子を読めない: {stem}")
    load_n, size_mm = float(tag["load"]), float(tag["size"])
    width_mm = 5.0 if tag["width"] == "thin5" else 10.0
    name = f"{stem}_d{layers}"
    record: dict = {"name": name, "mask": stem, "dilate_layers": layers, "load_n": load_n, "size_mm": size_mm}

    key = (load_n, size_mm)
    if key not in contexts:
        t0 = time.perf_counter()
        contexts[key] = sm.build_context(HANGER_MODEL, load_n=load_n, element_size_mm=size_mm, min_width_mm=width_mm)
        record["context_seconds"] = time.perf_counter() - t0
    ctx = contexts[key]
    cond = ctx.cond

    mask = np.asarray(np.load(MASK_DIR / f"{stem}_best_ever.npz")["mask"], dtype=bool)
    if mask.shape != ctx.space.mask.shape:
        raise ValueError(f"マスクの形 {mask.shape} が格子 {ctx.space.mask.shape} と違う")
    if layers:
        mask = ndimage.binary_dilation(mask, structure=acceptance.NEIGHBOURHOOD_STRUCTURE, iterations=layers)
        mask &= np.asarray(ctx.space.mask, dtype=bool)
    cells = int(mask.sum())
    need_gb = cells * BYTES_PER_CELL_ESTIMATE / 2**30
    avail_gb = _winmem.available_gb()
    record.update(cells=cells, design_cells=int(np.asarray(ctx.space.mask).sum()), grid_shape=list(mask.shape),
                  spacing_mm=float(ctx.grid.spacing), ram_available_gb_before=avail_gb, ram_estimate_gb=need_gb)
    if need_gb + RAM_MARGIN_GB > avail_gb:
        record["status"] = "skipped_ram"
        return record

    checks, fixed, load = acceptance.condition_check(mask, ctx.table, cond.fixed_faces, cond.load_faces, cond.forces)
    if checks.get("status") != "pass":
        record["status"] = "condition_check_failed"
        return record
    floating = anchor.floating_dofs(mask)
    model = fem.Model(*mask.shape, fixed_dofs=fixed, load=load, extra_fixed_dofs=floating, active_mask=mask)
    with fem.collect_solve_stats() as stats:
        prepared = model.prepare(mask.astype(float), stress.PENAL, solver="cg", threads=1)
    kf = prepared.kf.tocsr()
    kf.sort_indices()
    ff = model.f[model.free]
    record.update(
        status="ok",
        free_dofs=int(kf.shape[0]),
        nnz=int(kf.nnz),
        nnz_per_dof=float(kf.nnz / kf.shape[0]),
        ndof_full=int(model.ndof),
        auto_solver=fem.choose_solver("auto", kf.shape[0], kf.nnz),
        assemble_seconds=stats["assemble_seconds"],
        slice_seconds=stats["slice_seconds"],
        index_dtype=str(kf.indices.dtype),
    )
    DATA.mkdir(parents=True, exist_ok=True)
    np.savez(DATA / f"{name}.npz", data=kf.data, indices=kf.indices, indptr=kf.indptr,
             shape=np.asarray(kf.shape), rhs=ff, free=model.free, mask=mask)
    record["file_mb"] = (DATA / f"{name}.npz").stat().st_size / 2**20
    del prepared, kf, model
    gc.collect()
    record["peak_working_set_gb"] = _winmem.peak_working_set_gb()
    return record


def main() -> None:
    specs = sys.argv[1:]
    if not specs:
        raise SystemExit(__doc__)
    start = dt.datetime.now().astimezone()
    RESULTS.mkdir(parents=True, exist_ok=True)
    ledger_path = RESULTS / "s2_matrices.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8")) if ledger_path.exists() else {}
    contexts: dict = {}
    for spec in specs:
        record = build(spec, contexts)
        record["built_at"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        ledger[record["name"]] = record
        ledger_path.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(record, ensure_ascii=False))
    _winmem.log_load_window(RESULTS, start, "s2_build_matrices " + " ".join(specs), cpu_threads=1)


if __name__ == "__main__":
    main()
