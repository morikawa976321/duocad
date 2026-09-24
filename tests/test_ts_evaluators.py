"""`web/src/` のTSの純関数を Node で実際に評価する `tests/eval_*.mjs` を走らせる(assert は各 mjs の中)。

落ちたら、その mjs の先頭が名指す純関数が想定と食い違う(出力に失敗した assert が出る)。
**skipしない** —— node が無いときのskipは無人実行では緑と区別が付かない。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent


@pytest.mark.parametrize(
    "evaluator",
    [
        "eval_expression.mjs",
        "eval_grid_scale.mjs",
        "eval_min_width.mjs",
        "eval_screen_pick.mjs",
        "eval_selection_ref_key.mjs",
        "eval_sketch_geometry.mjs",
        "eval_view_cube.mjs",
    ],
)
def test_the_typescript_pure_functions_pass_their_own_assertions(evaluator: str) -> None:
    try:
        completed = subprocess.run(
            ["node", str(TESTS_DIR / evaluator)], capture_output=True, cwd=str(TESTS_DIR.parent), timeout=60
        )
    except FileNotFoundError as error:
        raise AssertionError("node が見つからない。TS側の純関数を評価できないので検査できない") from error
    assert completed.returncode == 0, (completed.stdout + completed.stderr).decode("utf-8", "replace")
