"""models/*.json の同じ条件で二値探索(削除・追加・置換)を実測する。

    .\\.venv\\Scripts\\python.exe topopt_checks\\measure_binary_search.py --model hanger_test.json --spacing 2.4 --targets 2 3 4

荷重の大きさ・方向は文書から読み、塊は製品と同じく固定凸包で1回導出する。
候補評価数・FEM回数(evaluationsに対応する試行数)・最良体積の履歴を書き出す。

旧`measure_safety_search.py`(二分探索 vs Illinois法の比較)の後継。二値探索は連続値の
材料量を探索しない(候補は削除・追加・置換で生成する)ので、比較する探索器そのものが
無くなっている——ここでは新ループの候補評価数・体積推移だけを実測する。

**未実機検証(2026-09-17)。** このリポジトリに `models/*.json` が無いため実行できておらず、
構文・importの確認までしかしていない。実際に使う前に手元の `models/` で1回通すこと。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

import _bootstrap  # noqa: F401  リポジトリ直下を sys.path へ足す(理由はそのモジュール)
from duocad.topopt import OUTPUT_DIR
from duocad.topopt import binary_search as bs
from duocad.topopt import design_space as ds

OUT_PATH = OUTPUT_DIR / "binary_search_measurements.json"


def run_case(session, space, bundle, *, target: float, max_evaluations: int) -> dict:
    from duocad import optimize_run

    opts = optimize_run.OptimizeOptions(target_safety_factor=target)
    conditions = optimize_run._build_search_conditions(session.document, space, bundle, opts)
    payload: dict = {
        "target_safety_factor": target,
        "max_evaluations": max_evaluations,
    }
    started = time.perf_counter()
    result = bs.find_best_feasible(space.mask, conditions, max_evaluations=max_evaluations)
    seconds = time.perf_counter() - started
    history = [
        {
            "index": trial.index,
            "operation": trial.operation,
            "outcome": trial.outcome,
            "material_mm3": (
                None if trial.material_mm3 is None else float(trial.material_mm3)
            ),
            "safety_factor": (
                None if trial.safety_factor is None else float(trial.safety_factor)
            ),
        }
        for trial in result.history
    ]
    best_volume_history = [h["material_mm3"] for h in history if h["outcome"] == "ok"]
    payload.update(
        {
            "status": result.status,
            "termination": result.termination,
            "evaluations": int(result.evaluations),
            "fem_solves": sum(1 for h in history if h["outcome"] in ("ok", "infeasible")),
            "best_material_mm3": (
                None if result.best_feasible is None else float(result.best_feasible.material_mm3)
            ),
            "best_volume_history": best_volume_history,
            "history": history,
            "seconds": seconds,
        }
    )
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="二値探索(削除・追加・置換)の実測")
    parser.add_argument("--model", required=True, help="models/*.json のファイル名")
    parser.add_argument("--spacing", type=float, default=2.4)
    parser.add_argument("--max-evaluations", type=int, default=15)
    parser.add_argument("--out", type=Path, default=OUT_PATH)
    parser.add_argument("--targets", type=float, nargs="+", required=True)
    args = parser.parse_args()

    from duocad.optimize_run import build_conditions
    from duocad.session import Session

    session = Session(None, 0.2)
    session.open_document(args.model)
    bundle = build_conditions(session.document)
    space = ds.derive(bundle.conditions, spacing=args.spacing)
    print(
        f"{args.model} 塊 {space.element_count} 要素 / spacing={args.spacing} "
        f"/ max_evaluations={args.max_evaluations}",
        flush=True,
    )

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    cases: list[dict] = []
    for target in args.targets:
        print(f"  目標 sf={target}", flush=True)
        case = run_case(session, space, bundle, target=target, max_evaluations=args.max_evaluations)
        cases.append(case)
        # **1ケース終わるたびに書き出す。** 途中で落ちてもそこまでが残る。
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(
                {
                    "model": args.model,
                    "spacing_mm": args.spacing,
                    "derivation": ds.DERIVATION,
                    "design_elements": int(space.element_count),
                    "cases": cases,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    print(f"\n書き出し: {args.out}", flush=True)
    print(f"{'目標':>8} {'状態':>20} {'評価':>4} {'材料[mm^3]':>12} {'秒':>6}")
    for case in cases:
        best = case["best_material_mm3"]
        print(
            f"{case['target_safety_factor']:>8.0f} {case['status']:>20} {case['evaluations']:>4} "
            f"{best if best is not None else float('nan'):>12.0f} "
            f"{case['seconds']:>6.1f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
