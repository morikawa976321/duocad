"""保持・除外の共通体積を「候補ごとにブーリアン」から「格子ごとの前計算」へ置き換えた効果を実測する
(`docs/TOPOPT_SPEED.md`「保持・除外の判定を前計算にした」)。

    .\\.venv\\Scripts\\python.exe topopt_checks\\measure_region_overlaps.py --model models\\hanger_bar_attachment.json --spacing 2.4 --out <出力先>.json

同じ設計空間・同じ候補マスクに対して、旧経路(`acceptance.intersection_volume`)と前計算
(`acceptance.RegionOverlaps`)の合否・体積・時間を並べる。前計算の1格子あたりの秒とメモリ、候補1件あたりの
判定時間(旧 / 新)、旧経路との体積差と `region_check` の数値許容差の比を出す。

候補は3種: `preserve-only`(保持ソリッドの外接箱にかかる設計空間のセルだけ = 保持だけを残した細い形の代用)、
`preserve+random10%`(それに設計空間の10%を足す)、`design`(設計空間の全部)。探索が実際に評価する形は
これらの間にある。`--out` を省くと標準出力だけ。**探索(`find_best_feasible`)は走らせない**ので
`output/` に実行記録は残らない。
"""
from __future__ import annotations

import argparse
import statistics
import sys
import tempfile
import time
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

import _bootstrap  # noqa: F401  リポジトリ直下を sys.path へ足す(理由はそのモジュール)
import _record
import numpy as np

from duocad import kernel, optimize_run
from duocad.session import Session
from duocad.topopt import acceptance, design_space, trace


def _bbox_cells(grid, shape) -> tuple[slice, slice, slice]:
    box = kernel.bounding_box(getattr(shape, "wrapped", shape))
    origin, h = np.asarray(grid.origin), float(grid.spacing)
    lo = np.maximum(0, np.floor((np.array(box["min"]) - origin) / h).astype(int))
    hi = np.minimum(grid.shape, np.ceil((np.array(box["max"]) - origin) / h).astype(int))
    return tuple(slice(int(a), int(b)) for a, b in zip(lo, hi))


def candidates(space, preserves, rng) -> dict[str, np.ndarray]:
    grid, design = space.grid, space.mask
    around = np.zeros(grid.shape, bool)
    for _, shape in preserves:
        around[_bbox_cells(grid, shape)] = True
    preserve_only = design & around
    return {
        "preserve-only": preserve_only,
        "preserve+random10%": preserve_only | (design & (rng.random(grid.shape) < 0.1)),
        "design": design,
    }


def _timed(function):
    started = time.perf_counter()
    value = function()
    return time.perf_counter() - started, value


def measure(model: Path, spacing: float, *, repeats: int, seed: int) -> dict:
    session = Session(None, 0.2, models_dir=model.resolve().parent)
    session.open_document(model.name)
    document = session.document
    bundle = optimize_run.build_conditions(document)
    space = design_space.derive(bundle.conditions, spacing=spacing)
    conditions = optimize_run._build_search_conditions(
        document, space, bundle, optimize_run.OptimizeOptions(target_safety_factor=2.0, max_evaluations=1, search_target_factor=1.0))
    grid = space.grid
    groups = (("preserve", conditions.preserves, True), ("exclude", conditions.excludes, False))
    rng = np.random.default_rng(seed)
    masks = candidates(space, conditions.preserves, rng)
    record: dict = dict(
        model=str(model), spacing=float(grid.spacing), grid_shape=list(grid.shape), design_cells=int(space.mask.sum()),
        candidate_cells={name: int(mask.sum()) for name, mask in masks.items()},
        tile_cells=acceptance.TILE_CELLS, bodies=[], candidates={},
    )

    # 前計算: 設計空間の全部を最初の問い合わせにする(候補は設計空間の部分集合なので、以後は足し算だけ)
    overlaps = acceptance.RegionOverlaps()
    tiles: dict[str, int] = {"empty": 0, "full": 0, "partial": 0}
    original = kernel.grid_cell_common_volumes

    def counted(*args):
        kind, volumes = original(*args)
        tiles[kind] += 1
        return kind, volumes

    kernel.grid_cell_common_volumes = counted
    try:
        precompute_seconds = 0.0
        for label, bodies, _ in groups:
            for name, shape in bodies:
                before = overlaps.nbytes
                seconds, (total, overlap) = _timed(lambda: overlaps.overlap_volume(space.mask, grid, shape))
                precompute_seconds += seconds
                record["bodies"].append(dict(group=label, body=name, body_volume_mm3=total,
                                             precompute_seconds=seconds, values_mb=(overlaps.nbytes - before) / 1e6))
    finally:
        kernel.grid_cell_common_volumes = original
    record["precompute"] = dict(seconds=precompute_seconds, tiles=tiles,
                                megabytes=overlaps.nbytes / 1e6,
                                fallbacks=list(overlaps.fallbacks.values()))

    for name, mask in masks.items():
        old_seconds = new_seconds = 0.0
        rows = []
        status_same = True
        for label, bodies, preserve in groups:
            dt_old, old = _timed(lambda: acceptance.region_check(mask, grid, bodies, preserve=preserve,
                                                                 cache=document.bbox_cache))
            dt_new, new = _timed(lambda: acceptance.region_check(mask, grid, bodies, preserve=preserve,
                                                                 overlaps=overlaps))
            old_seconds += dt_old
            new_seconds += dt_new
            status_same &= old["status"] == new["status"] and old["rule"] == new["rule"]
            for a, b in zip(old["bodies"], new["bodies"]):
                status_same &= a["status"] == b["status"]
                if a["status"] == "unknown" or b["status"] == "unknown":
                    continue
                for key in ("intersection_mm3", "empty_volume_mm3"):
                    rows.append(dict(group=label, body=a["body"], quantity=key, old=a[key], new=b[key],
                                     abs_diff=abs(a[key] - b[key]), tolerance_mm3=a["tolerance_mm3"],
                                     diff_over_tolerance=abs(a[key] - b[key]) / a["tolerance_mm3"]))
        worst = max(rows, key=lambda r: r["diff_over_tolerance"])
        record["candidates"][name] = dict(
            cells=int(mask.sum()), old_seconds=old_seconds, new_seconds=new_seconds, status_and_rule_same=status_same,
            max_abs_diff_mm3=max(r["abs_diff"] for r in rows), worst_diff_over_tolerance=worst["diff_over_tolerance"],
            worst_row=worst, rows_beyond_tolerance=sum(r["diff_over_tolerance"] > 1.0 for r in rows), rows=len(rows))

    # 旧経路自身の再現性: 材料を無作為に2つへ分け、全体の共通体積が2つの合計に一致するか。体積は加法的なので
    # 残差は本来0だが、曲面ソリッドではブーリアン結果の体積積分の丸めで0にならない。旧経路と前計算の差が
    # この残差と同じ桁なら、差の原因は前計算ではなくブーリアンの体積積分の桁である。
    half = space.mask & (rng.random(grid.shape) < 0.5)
    rest = space.mask & ~half
    residuals = []
    for label, bodies, _ in groups:
        for name, shape in bodies:
            parts = [acceptance.intersection_volume(m, grid, shape, cache=document.bbox_cache) for m in (space.mask, half, rest)]
            total = parts[0][0]
            tolerance = max(1e-9, 1e-9 * total)
            new_parts = [overlaps.overlap_volume(m, grid, shape)[1] for m in (space.mask, half, rest)]
            residuals.append(dict(group=label, body=name, tolerance_mm3=tolerance,
                                  old_residual_mm3=parts[0][1] - parts[1][1] - parts[2][1],
                                  new_residual_mm3=new_parts[0] - new_parts[1] - new_parts[2]))
    record["additivity"] = dict(
        rows=residuals,
        old_worst_over_tolerance=max(abs(r["old_residual_mm3"]) / r["tolerance_mm3"] for r in residuals),
        new_worst_over_tolerance=max(abs(r["new_residual_mm3"]) / r["tolerance_mm3"] for r in residuals))

    # 探索の候補と同じ大きさのばらつき: 設計空間の 5〜95% の無作為部分集合を、新経路だけ繰り返して測る
    samples = []
    for _ in range(repeats):
        mask = space.mask & (rng.random(grid.shape) < rng.uniform(0.05, 0.95))
        seconds = 0.0
        for _, bodies, preserve in groups:
            seconds += _timed(lambda: acceptance.region_check(mask, grid, bodies, preserve=preserve,
                                                              overlaps=overlaps))[0]
        samples.append(seconds)
    record["new_repeated"] = dict(repeats=repeats, median_seconds=statistics.median(samples), max_seconds=max(samples))
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--spacing", type=float, required=True)
    parser.add_argument("--repeats", type=int, default=20)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    trace.DEFAULT_ROOT = Path(tempfile.mkdtemp(prefix="region_overlaps_"))  # 念のため。この計測は実行記録を作らない
    record = measure(args.model, args.spacing, repeats=args.repeats, seed=args.seed)
    text = _record.dumps(record, default=str)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"書き出し: {args.out}")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
