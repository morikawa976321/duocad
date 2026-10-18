"""`web/src/` のTSの純関数を Node で実際に評価する `tests/eval_*.mjs` を走らせる(assert は各 mjs の中)。

落ちたら、その mjs の先頭が名指す純関数が想定と食い違う(出力に失敗した assert が出る)。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from node_support import run_node

TESTS_DIR = Path(__file__).resolve().parent

EVALUATORS = [
    "eval_ai_result.mjs",
    "eval_choice_sync.mjs",
    "eval_expression.mjs",
    "eval_frame_loop.mjs",
    "eval_grid_planes.mjs",
    "eval_grid_scale.mjs",
    "eval_headlight.mjs",
    "eval_min_width.mjs",
    "eval_pixel_ratio.mjs",
    "eval_resolution_tracker.mjs",
    "eval_screen_pick.mjs",
    "eval_selection_ref_key.mjs",
    "eval_sketch_geometry.mjs",
    "eval_sketch_intersections.mjs",
    "eval_sketch_snap.mjs",
    "eval_view_cube.mjs",
    "eval_view_cube_overlay.mjs",
]


@pytest.mark.parametrize("evaluator", EVALUATORS)
def test_the_typescript_pure_functions_pass_their_own_assertions(evaluator: str) -> None:
    run_node(TESTS_DIR / evaluator, f"{evaluator} が名指す純関数")


def test_every_evaluator_is_run_by_some_test() -> None:
    """落ちたら、`eval_*.mjs` を足したのにどのテストも走らせていない(足し忘れても緑のまま誰も評価しない)。
    入力を渡して走らせる口(`test_ghost_agreement` など)は、他のテストがファイル名を書いている。"""
    other_tests = "".join(
        path.read_text(encoding="utf-8") for path in TESTS_DIR.glob("test_*.py") if path != Path(__file__)
    )
    orphans = [
        path.name
        for path in TESTS_DIR.glob("eval_*.mjs")
        if path.name not in EVALUATORS and path.name not in other_tests
    ]
    assert not orphans, f"どのテストも走らせない評価口: {orphans}"
