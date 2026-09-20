"""最小幅の判定と表示(`web/src/minWidth.ts`)の検査。表示した下限・上限がそのまま入力して通ること。

`tests/test_grid_scale.py` と同じやり方で、node が `tests/eval_min_width.mjs` を走らせる。skipしない。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TS_EVALUATOR = Path(__file__).resolve().parent / "eval_min_width.mjs"


def test_the_shown_min_width_bounds_are_valid_inputs() -> None:
    assert TS_EVALUATOR.exists(), f"評価の口が無い: {TS_EVALUATOR}"
    try:
        completed = subprocess.run(["node", str(TS_EVALUATOR)], capture_output=True, cwd=str(REPO_ROOT), timeout=60)
    except FileNotFoundError as error:
        raise AssertionError("node が見つからない。TS側の純関数を評価できないので検査できない") from error
    assert completed.returncode == 0, (
        "web/src/minWidth.ts の判定と表示が食い違う:\n"
        + completed.stdout.decode("utf-8", "replace") + completed.stderr.decode("utf-8", "replace")
    )
