"""要素サイズの下限付近(既定 1.6 mm)で hanger を端から端まで回し、ピークメモリ・設計空間の要素数・所要時間を記録する。

    .\.venv\Scripts\python.exe topopt_checks\measure_limit_run.py --derive-only   # derive まで(軽い。箱の数に比例するコストの実測)
    .\.venv\Scripts\python.exe topopt_checks\measure_limit_run.py                 # 探索・表面生成・finalize まで(重い。1時間以上)

荷重の差し替えは `sweep_smoothing_corpus._set_load`(`models/` の文書は書き換えない)。条件は
`F2000_h1.8_w2` と同じ(2000 N・最小幅 2 セル)で要素サイズだけ変える。
**空きメモリが見込みのピーク(設計空間の要素1個あたり 18.6 kB + 0.7 GB。1.798 mm までの4点から引いた外挿)を
下回ると起動を断る**(`--force` で外す)。走行中は空きが `--abort-free-gb` を割ったら降りる(機械ごと落とさないため)。
結果は `output/limit_run/<名前>.json`。
"""
from __future__ import annotations

import argparse
import json
import threading
import time
from datetime import datetime
from pathlib import Path

import _bootstrap  # noqa: F401
import _record
from sweep_smoothing_corpus import MODEL, _peak_memory_mb, _set_load

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT = REPO_ROOT / "output" / "limit_run"
KB_PER_DESIGN_ELEMENT = 18.6  # 4点の最大値から引いた直線(docs/TOPOPT.md)。外挿であって実測ではない
BASE_GB = 0.7


def _available_gb() -> float:
    import psutil

    return psutil.virtual_memory().available / 2**30


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--size", type=float, default=1.6)
    parser.add_argument("--load-n", type=int, default=2000)
    parser.add_argument("--cells", type=int, default=2)
    parser.add_argument("--derive-only", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--abort-free-gb", type=float, default=1.0)
    args = parser.parse_args()

    import os

    from duocad import optimize_run
    from duocad.topopt import design_space as ds
    from _documents import open_session

    name = f"F{args.load_n}_h{args.size}_w{args.cells}" + ("_derive" if args.derive_only else "")
    session = open_session(MODEL)
    doc = session.document
    _set_load(doc, args.load_n)
    limits = optimize_run.resolution_limits(doc)
    record: dict = dict(name=name, size_requested_mm=args.size, load_n=args.load_n, cells=args.cells,
                        started_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                        limits={k: v for k, v in limits.items() if k not in ("steps", "condition_faces")},
                        available_gb_at_start=round(_available_gb(), 2))
    spacing = optimize_run._snap_to_grid(limits["bbox_min"], limits["bbox_max"],
                                         optimize_run._resolve_element_size(limits, args.size))
    estimate = optimize_run._design_element_estimate(optimize_run._design_volume(doc), spacing)
    expected_gb = estimate * KB_PER_DESIGN_ELEMENT / 1e6 + BASE_GB
    record.update(spacing_mm=spacing, design_elements_estimate=estimate, expected_peak_gb_extrapolated=round(expected_gb, 1))
    if not args.derive_only and not args.force and _available_gb() < expected_gb:
        record.update(status="refused_low_memory")
        print(json.dumps(record, ensure_ascii=False, indent=1))
        return 2

    stop = threading.Event()
    aborted = threading.Event()

    def watchdog() -> None:
        while not stop.wait(2.0):
            if _available_gb() < args.abort_free_gb:
                aborted.set()
                print("空きメモリが下限を割ったので降りる", flush=True)
                os._exit(3)

    threading.Thread(target=watchdog, daemon=True).start()
    started = time.perf_counter()
    try:
        if args.derive_only:
            bundle = optimize_run.build_conditions(doc)
            space = ds.derive(bundle.conditions, spacing=spacing)
            record.update(status="ok", design_elements=int(space.element_count), box_elements=int(space.grid.element_count),
                          grid_shape=[int(v) for v in space.grid.shape], derive_seconds=space.stats["seconds"])
        else:
            options = optimize_run.OptimizeOptions(element_size_mm=args.size, min_width_mm=args.cells * spacing)
            result = optimize_run.run(doc, options)
            record.update(status="ok", state=result.get("state"), termination=result.get("termination"),
                          evaluations=result.get("evaluations"), design_elements=result.get("design_elements"),
                          triangle_count=result.get("triangle_count"), warning=result.get("warning"))
    except Exception as error:  # noqa: BLE001 - 失敗も記録する
        record.update(status="error", error=f"{type(error).__name__}: {error}")
    stop.set()
    record.update(seconds=time.perf_counter() - started, **_peak_memory_mb(),
                  finished_at=datetime.now().astimezone().isoformat(timespec="seconds"))
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{name}.json").write_text(_record.dumps(record, default=float) + "\n", encoding="utf-8")
    print(json.dumps(record, ensure_ascii=False, indent=1, default=float))
    return 0 if record["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
