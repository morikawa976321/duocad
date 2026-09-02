"""ビューキューブの26領域・視点方向を導く純関数(`web/src/viewCube.ts`)の検査。

**Python側の対応物は無い**(ビューキューブはTSにしか描かれない)。`tests/eval_grid_scale.mjs` /
`tests/test_grid_scale.py` と同じやり方 —— ソースの文字列比較ではなく、Node で実際にTSの
純関数を評価する —— で、失敗したら非ゼロで終わるスクリプト(`tests/eval_view_cube.mjs`)を
走らせるだけにする。**skipしない** —— 足場が無いときのskipは無人実行では緑と区別が付かない。

描画(three.js の第2パス描画)・Pointer Lockドラッグ・視点アニメーションは pytest では
判定できない —— `web/src/viewport.ts` 側の実機確認が持つ。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TS_EVALUATOR = Path(__file__).resolve().parent / "eval_view_cube.mjs"


def test_the_view_cube_pure_functions_pass_their_own_assertions() -> None:
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
        "web/src/viewCube.ts の純関数がここでの想定と食い違う:\n"
        + completed.stdout.decode("utf-8", "replace")
        + completed.stderr.decode("utf-8", "replace")
    )
