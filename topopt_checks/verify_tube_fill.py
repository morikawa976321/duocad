"""平滑化の検証材料(64条件)の確定マスクを、製品の探索後の経路(1×1 の空隙を埋める → 6条件の取り直し → 平滑化 →
メッシュ集合演算)に実際の荷重条件で通し、埋める前後の安全率と最終処理の合否を記録する。SIMP・探索は再実行しない。

    .venv/Scripts/python.exe topopt_checks/verify_tube_fill.py --one F0200_h2.4_w2
    .venv/Scripts/python.exe topopt_checks/verify_tube_fill.py            # 全条件を1つずつ(済んだ条件は飛ばす)
    .venv/Scripts/python.exe topopt_checks/verify_tube_fill.py --min-free-gb 7   # 子を起動する前に要る空きメモリ

探索は `binary_search.find_best_feasible` を差し替えて、保存済みマスク(`output/smoothing_corpus/voxel/<条件名>.npz`)を
そのまま「探索の確定マスク」として返す(そのとき実際の条件で `acceptance.inspect` を1回解き、埋める前の安全率にする)。
以降は製品の `optimize_run.run` がそのまま走る。出力は `output/smoothing_corpus/tube_fill_records/<条件名>.json` と
`tube_fill_manifest.json`。FEM は要素サイズ 2.4 mm でも数 GB を使うので並列にしない(`sweep_smoothing_corpus.py` の実測
`PEAK_GB`)。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

import _bootstrap  # noqa: F401

REPO_ROOT = Path(__file__).resolve().parents[1]
MODEL = REPO_ROOT / "models" / "hanger_bar_attachment.json"
OUT = REPO_ROOT / "output" / "smoothing_corpus"
RECORDS = OUT / "tube_fill_records"
MANIFEST = OUT / "tube_fill_manifest.json"


def _free_gb() -> float:
    import psutil
    return psutil.virtual_memory().available / 2**30


def _spec(name: str) -> dict:
    load, size, width = name.split("_")
    return dict(name=name, load_n=int(load[1:]), element_size_mm=float(size[1:]), cells=int(width[1:]))


def run_one(name: str) -> int:
    import numpy as np
    from unittest.mock import patch

    import psutil
    from _documents import open_session
    from sweep_smoothing_corpus import _set_load
    from duocad import optimize_run
    from duocad.topopt import acceptance, binary_search, trace

    spec = _spec(name)
    record: dict = dict(name=name, **{k: v for k, v in spec.items() if k != "name"})
    started = time.perf_counter()
    data = np.load(OUT / "voxel" / f"{name}.npz")
    saved_mask = np.asarray(data["mask"], dtype=bool)
    captured: dict = {}

    def fake_find(design_space_mask, conditions, **kwargs):
        if saved_mask.shape != design_space_mask.shape:
            raise RuntimeError(f"保存済みマスクと設計空間の形が違う: {saved_mask.shape} / {design_space_mask.shape}")
        if (saved_mask & ~np.asarray(design_space_mask, dtype=bool)).any():
            raise RuntimeError("保存済みマスクが設計空間の外に材料を持つ")
        t0 = time.perf_counter()
        report = acceptance.inspect(saved_mask, saved_mask, conditions.grid, **conditions.inspect_kwargs())
        captured["search_inspect_seconds"] = time.perf_counter() - t0
        captured["search_report"] = report
        best = binary_search.FeasibleModel(mask=saved_mask, before_mask=saved_mask, report=report, trial_index=0)
        return binary_search.SearchResult(status="feasible", termination="replayed", best_feasible=best,
                                          history=[], evaluations=1, conditions_identifier="replayed")

    try:
        with tempfile.TemporaryDirectory(prefix="tube-fill-trace-") as trace_root, \
                patch.object(trace, "DEFAULT_ROOT", Path(trace_root)), \
                patch.object(binary_search, "find_best_feasible", fake_find):
            session = open_session(MODEL)
            doc = session.document
            _set_load(doc, spec["load_n"])
            limits = optimize_run.resolution_limits(doc)
            spacing = optimize_run._snap_to_grid(limits["bbox_min"], limits["bbox_max"],
                                                 optimize_run._resolve_element_size(limits, spec["element_size_mm"]))
            options = optimize_run.OptimizeOptions(element_size_mm=spec["element_size_mm"], min_width_mm=spec["cells"] * spacing)
            result = optimize_run.run(doc, options)
            archive = trace.RunArchive(result["diagnostic_path"])
            fill = archive.last("tube-fill")
            search = captured.get("search_report", {})
            record.update(
                status="ok" if result["state"] == "done" else "not_done", state=result["state"], warning=result.get("warning"),
                search_status=search.get("status"), search_safety_factor=result.get("search_safety_factor"),
                safety_factor=result.get("safety_factor"), search_target=result.get("search_target_safety_factor"),
                filled_cells=result.get("filled_cells"), fill_rounds=None if fill is None else fill.get("rounds"),
                fill_report_status=None if fill is None or "report" not in fill else fill["report"]["status"],
                material_mm3=result.get("material_mm3"), triangle_count=result.get("triangle_count"),
                search_inspect_seconds=captured.get("search_inspect_seconds"), run_seconds=result.get("seconds"))
    except Exception as error:  # 失敗は条件ごとに記録して次へ進む
        record.update(status="error", error=f"{type(error).__name__}: {error}"[:4000], traceback=traceback.format_exc()[-4000:])
    record["seconds"] = time.perf_counter() - started
    record["peak_working_set_mb"] = round(psutil.Process().memory_info().peak_wset / 2**20, 1)
    RECORDS.mkdir(exist_ok=True)
    (RECORDS / f"{name}.json").write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: record.get(k) for k in ("name", "status", "state", "search_safety_factor", "safety_factor",
                                                 "filled_cells", "seconds", "peak_working_set_mb", "error")},
                     ensure_ascii=False), flush=True)
    return 0 if record["status"] == "ok" else 1


def drive(min_free_gb: float, only_sizes: tuple[float, ...] | None) -> int:
    RECORDS.mkdir(exist_ok=True)
    names = sorted(p.stem for p in (OUT / "voxel").glob("*.npz"))
    if only_sizes:
        names = [n for n in names if _spec(n)["element_size_mm"] in only_sizes]
    todo = [n for n in names if not (RECORDS / f"{n}.json").exists()]
    for name in todo:
        while _free_gb() < min_free_gb:
            print(f"{time.strftime('%H:%M:%S')} 空き {_free_gb():.1f} GB < {min_free_gb} GB、待機", flush=True)
            time.sleep(30)
        code = subprocess.call([sys.executable, __file__, "--one", name])
        print(f"{time.strftime('%H:%M:%S')} {name} exit={code}", flush=True)
    records = [json.loads((RECORDS / f"{n}.json").read_text(encoding="utf-8")) for n in names if (RECORDS / f"{n}.json").exists()]
    MANIFEST.write_text(json.dumps(
        dict(ok=sum(r["status"] == "ok" for r in records), not_ok=sum(r["status"] != "ok" for r in records),
             rows=[{k: v for k, v in r.items() if k != "traceback"} for r in records]),
        ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--one")
    parser.add_argument("--min-free-gb", type=float, default=7.0,
                        help="子を起動する前に要る空きメモリ[GB](既定は sweep の要素 2.4 mm のピーク 7.5 GB × 0.85 の切り下げ)")
    parser.add_argument("--sizes", type=float, nargs="*", help="要素サイズ[mm]で絞る(例: 2.4 2.2)")
    args = parser.parse_args()
    return run_one(args.one) if args.one else drive(args.min_free_gb, tuple(args.sizes) if args.sizes else None)


if __name__ == "__main__":
    sys.exit(main())
