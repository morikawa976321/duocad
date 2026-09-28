"""保存済みの小型模型(`models/topopt_idle_*_small.json`)を製品の入口(`optimize_run.run`)で
端から端まで1回走らせ、時間・メモリ・評価回数を記録する。後続タスクが「最適化1回」を
再実行するときの標準の呼び方(このスクリプト)。

    .\\.venv\\Scripts\\python.exe topopt_checks\\idle_validation\\run_compact_search.py --model topopt_idle_dilution_small.json --size 2.0 --cells 2 --out output\\idle_validation_20260928\\dilution_2.0mm.json
    .\\.venv\\Scripts\\python.exe topopt_checks\\idle_validation\\run_compact_search.py --model topopt_idle_path_small.json --size 2.5 --cells 2 --out output\\idle_validation_20260928\\path_2.5mm.json

`--load-n` で `sweep_smoothing_corpus._set_load` と同じ入口(既存荷重面の大きさだけ差し替え、
モデルJSONは書き換えない)で荷重を差し替えられる。`optimize_run.run` を直接呼ぶ
(`Session.run_optimization` は begin/end のUI用フックを足すだけの薄いラッパーなので、
計測では素通りする方を使う —— `measure_limit_run.py` と同じ方針)。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import tracemalloc
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
#: `idle_validation/` は `topopt_checks/` の子なので、`_bootstrap`(リポジトリ直下を
#: sys.path へ足す)を import するにはまず親を足す必要がある(単体の`topopt_checks/*.py`は
#: 自分の置き場が既にそこなので不要だった)。
sys.path.insert(0, str(REPO / "topopt_checks"))
import _bootstrap  # noqa: E402,F401
import psutil  # noqa: E402
from _documents import open_session  # noqa: E402
import _record  # noqa: E402

from duocad import optimize_run  # noqa: E402

MODELS_DIR = REPO / "models"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _set_load(doc, newtons: float) -> None:
    """既存の荷重面の大きさだけ差し替える(`sweep_smoothing_corpus._set_load` と同じ手口。
    モデルJSONは書き換えない —— `doc.optimize.loads` はverb経由でしか永続化されないので、
    ここでの直接代入はプロセス内だけの一時上書き)。"""
    for load in doc.optimize.loads:
        load["magnitude"] = float(newtons)
        load["vector"] = [c * float(newtons) for c in load["direction"]]


def run(model: Path, *, size: float, cells: int, load_n: float | None, max_evaluations: int,
        min_width_mm: float | None = None) -> dict:
    """`min_width_mm` を渡すと物理mmで固定する(`cells` は無視)。
    **解像度をまたいで比較するときは `min_width_mm` を揃えること** —— `cells * size` は
    格子幅ごとに実際のmmが変わる(製品UIの既定と同じ挙動だが、比較の条件は揃わない)。"""
    process = psutil.Process()
    session = open_session(model)
    doc = session.document
    if load_n is not None:
        _set_load(doc, load_n)
    resolved_min_width_mm = cells * size if min_width_mm is None else min_width_mm
    # `_run()`が実際に使う値(製品と同じ手順でスナップした要素サイズから解決する。QAで指摘された
    # 「recordのcellsが要求値のままで、製品が実際に解決したセル数と食い違う」を避けるため)。
    limits = optimize_run.resolution_limits(doc)
    snapped_spacing = optimize_run._snap_to_grid(limits["bbox_min"], limits["bbox_max"],
                                                  optimize_run._resolve_element_size(limits, size))
    resolved = optimize_run._resolve_min_width(snapped_spacing, resolved_min_width_mm)
    record: dict = dict(
        model=str(model), model_sha256=_sha256(model),
        requested_element_size_mm=size, requested_min_width_mm=resolved_min_width_mm,
        snapped_spacing_mm=snapped_spacing, min_width_cells_resolved=resolved["cells"],
        min_width_guaranteed_mm=resolved["guaranteed_mm"],
        load_n_override=load_n, max_evaluations=max_evaluations,
    )
    options = optimize_run.OptimizeOptions(element_size_mm=size, min_width_mm=resolved_min_width_mm,
                                            max_evaluations=max_evaluations)
    tracemalloc.start()
    rss_before_mb = process.memory_info().rss / 1e6
    started = time.perf_counter()
    result = optimize_run.run(doc, options)
    seconds = time.perf_counter() - started
    _, traced_peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rss_after_mb = process.memory_info().rss / 1e6
    record.update(
        seconds=seconds,
        traced_peak_mb=traced_peak_bytes / 1e6,
        rss_before_mb=rss_before_mb,
        rss_after_mb=rss_after_mb,
        rss_delta_mb=rss_after_mb - rss_before_mb,
        state=result.get("state"),
        termination=result.get("termination"),
        evaluations=result.get("evaluations"),
        design_elements=result.get("design_elements"),
        material_mm3=result.get("material_mm3"),
        safety_factor=result.get("safety_factor"),
        target_safety_factor=result.get("target_safety_factor"),
        search_target_safety_factor=result.get("search_target_safety_factor"),
        triangle_count=result.get("triangle_count"),
        vertex_count=result.get("vertex_count"),
        warning=result.get("warning"),
    )
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, help="models/ 配下のファイル名(topopt_idle_dilution_small.json 等)")
    parser.add_argument("--size", type=float, required=True)
    parser.add_argument("--cells", type=int, default=2, help="--min-width-mm 省略時、最小幅= cells * size")
    parser.add_argument("--min-width-mm", type=float, default=None,
                        help="物理mmで固定する(解像度をまたぐ比較はこちらを揃える)。省略時は --cells から計算")
    parser.add_argument("--load-n", type=float, default=None, help="省略時はモデル保存済みの荷重をそのまま使う")
    parser.add_argument("--max-evaluations", type=int, default=1000)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    model_path = MODELS_DIR / args.model
    record = run(model_path, size=args.size, cells=args.cells, load_n=args.load_n,
                 max_evaluations=args.max_evaluations, min_width_mm=args.min_width_mm)
    _record.write(args.out, record, default=str)
    print(json.dumps(record, ensure_ascii=False, indent=1))
    print(f"書き出し: {args.out}")
    return 0 if record.get("state") == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
