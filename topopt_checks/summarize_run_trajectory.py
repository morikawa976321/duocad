"""製品経路の実行記録(`output/optimization_runs/<run>/`)から、評価ごとの安全率・材料セル数・CG の累計秒を並べ、
`binary_search` の `target_tolerance` の値ごとに「どの評価で止まっていたか」を出す(計画書 §12.12 の表)。

    .\\.venv\\Scripts\\python.exe topopt_checks\\summarize_run_trajectory.py output\\optimization_runs\\<run> --target 2.0

評価の番号は 0 が段階3の全材料、1 以降が段階4の候補。安全率は `*-stress-output.json` の `safety_factor`、セル数は同じ記録の
`representative.element_count`、秒は `*-linear-solve.json` の `seconds` の累計(CG だけ。peel・応力・記録の書き出しは含まない)。
記録に探索時の目標が無いので `--target` で渡す。不合格の候補も1行になる(安全率が目標未満の行)。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

TOLERANCES = (0.05, 0.03, 0.02, 0.01, 0.005, 0.001)


def trajectory(run: Path) -> list[dict]:
    rows, seconds = [], 0.0
    for path in sorted(run.glob("[0-9]*-*.json")):
        kind = path.stem.split("-", 1)[1]
        if kind == "linear-solve":
            seconds += float(json.loads(path.read_text(encoding="utf-8"))["seconds"])
        elif kind == "stress-output":
            report = json.loads(path.read_text(encoding="utf-8"))
            rows.append(dict(evaluation=len(rows), safety_factor=float(report["safety_factor"]),
                             cells=int(report["representative"]["element_count"]), cg_seconds=seconds))
    return rows


def summarize(rows: list[dict], target: float) -> dict:
    passing = [row for row in rows if row["safety_factor"] >= target]
    smallest = min(passing, key=lambda row: row["cells"])
    stops = {}
    for tolerance in TOLERANCES:
        row = next((row for row in passing if row["safety_factor"] <= target * (1 + tolerance)), None)
        stops[str(tolerance)] = None if row is None else dict(
            row, cells_over_smallest=row["cells"] / smallest["cells"] - 1,
            later_evaluations=len(rows) - 1 - row["evaluation"],
            later_passed=sum(1 for later in passing if later["evaluation"] > row["evaluation"]))
    return dict(target=target, evaluations=len(rows), passed=len(passing), smallest_passing=smallest,
                last=rows[-1], stops=stops)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--target", type=float, required=True, help="探索時の目標安全率")
    parser.add_argument("--rows", action="store_true", help="評価ごとの行も出す")
    args = parser.parse_args()
    rows = trajectory(args.run)
    if args.rows:
        for row in rows:
            print(json.dumps(row))
    print(json.dumps(summarize(rows, args.target), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
