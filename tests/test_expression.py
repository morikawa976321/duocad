"""数値入力欄の式評価(`web/src/expression.ts`)の検査。

**Python側の対応物は無い**(式を読むのはブラウザ側だけ)。`tests/test_grid_scale.py` と
同じやり方で、Node で実際にTSの純関数を評価する。**skipしない** —— 足場が無いときの
skipは無人実行では緑と区別が付かない。

落ちたときの意味: 打った式が別の数になる(または通るべき式が拒まれる)。素の数値が
`Number.parseFloat` と食い違う場合は、実機検査(`web/scripts/*.mjs`)も一緒に壊れている。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TS_EVALUATOR = Path(__file__).resolve().parent / "eval_expression.mjs"


def test_the_expression_parser_passes_its_own_assertions() -> None:
    assert TS_EVALUATOR.exists(), f"評価の口が無い: {TS_EVALUATOR}"
    try:
        completed = subprocess.run(
            ["node", str(TS_EVALUATOR)],
            capture_output=True,
            cwd=str(REPO_ROOT),
            timeout=60,
        )
    except FileNotFoundError as error:  # noqa: PERF203
        raise AssertionError(
            "node が見つからない。TS側の純関数を評価できないので検査できない"
        ) from error
    assert completed.returncode == 0, (
        "web/src/expression.ts の式評価がここでの想定と食い違う:\n"
        + completed.stdout.decode("utf-8", "replace")
        + completed.stderr.decode("utf-8", "replace")
    )
