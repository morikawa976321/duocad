"""`web/src/` のTSの純関数を Node で実際に評価する `tests/eval_*.mjs` を走らせる(assert は各 mjs の中)。

落ちたら、その mjs の先頭が名指す純関数が想定と食い違う(出力に失敗した assert が出る)。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from node_support import run_node

TESTS_DIR = Path(__file__).resolve().parent


@pytest.mark.parametrize(
    "evaluator",
    [
        "eval_choice_sync.mjs",
        "eval_expression.mjs",
        "eval_grid_scale.mjs",
        "eval_min_width.mjs",
        "eval_screen_pick.mjs",
        "eval_selection_ref_key.mjs",
        "eval_sketch_geometry.mjs",
        "eval_sketch_intersections.mjs",
        "eval_sketch_snap.mjs",
        "eval_view_cube.mjs",
    ],
)
def test_the_typescript_pure_functions_pass_their_own_assertions(evaluator: str) -> None:
    run_node(TESTS_DIR / evaluator, f"{evaluator} が名指す純関数")
