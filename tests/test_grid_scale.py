"""基準グリッドの範囲・刻みを内容の大きさから導く純関数(`web/src/gridScale.ts`)の検査。

**Python側の対応物は無い**(このロジックはTSにしか無く、`web/src/adaptiveResolution.ts` の
ような2言語の一致検査は要らない)。`tests/eval_adaptive_resolution.mjs` と同じやり方 ——
ソースの文字列比較ではなく、Node で実際にTSの純関数を評価する —— で、失敗したら非ゼロで
終わるスクリプト(`tests/eval_grid_scale.mjs`)を走らせるだけにする。**skipしない** ——
足場が無いときのskipは無人実行では緑と区別が付かない。

グリッドの見た目そのもの(格子が実際に組み直るか・ヒステリシスが点滅を防ぐか)は
pytest では判定できない —— `web/scripts/` の実機smokeが持つ。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TS_EVALUATOR = Path(__file__).resolve().parent / "eval_grid_scale.mjs"


def test_the_grid_scale_pure_functions_pass_their_own_assertions() -> None:
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
        "web/src/gridScale.ts の純関数がここでの想定と食い違う:\n"
        + completed.stdout.decode("utf-8", "replace")
        + completed.stderr.decode("utf-8", "replace")
    )
