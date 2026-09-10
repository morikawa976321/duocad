"""当たり判定の距離計算(`web/src/screenPick.ts`)の検査。

**Python側の対応物は無い**(当たり判定はTSにしか無い)。`tests/test_grid_scale.py` と同じ
やり方 —— ソースの文字列比較ではなく、Node で実際にTSの純関数を評価する —— で、失敗したら
非ゼロで終わるスクリプト(`tests/eval_screen_pick.mjs`)を走らせるだけにする。**skipしない**
—— 足場が無いときのskipは無人実行では緑と区別が付かない。

ここが落ちたら「画面上の半径[px]で hit/miss を決める規則」が壊れている。**実際に狙った物が
取れるか**(射影・優先順位・ズーム後の感触)は pytest では判定できない —— `web/scripts/` の
実機smokeが持つ。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TS_EVALUATOR = Path(__file__).resolve().parent / "eval_screen_pick.mjs"


def test_the_screen_pick_pure_functions_pass_their_own_assertions() -> None:
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
        "web/src/screenPick.ts の純関数がここでの想定と食い違う:\n"
        + completed.stdout.decode("utf-8", "replace")
        + completed.stderr.decode("utf-8", "replace")
    )
