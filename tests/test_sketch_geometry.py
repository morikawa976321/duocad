"""スケッチの描く4verbが点と寸法から出す形(`web/src/commands/sketchGeometry.ts`)の契約。

**落ちたときの意味**: クリックで置ける形・打鍵で置ける形・円弧の掃引の向きのどれかが
変わった。とくに円弧は `start_deg <= end_deg` を破ると、コア(`kernel.py` の
`(end_deg - start_deg) % 360`)とブラウザの幽霊(`entityKinds.ts` の `arcPoints`)が
**別の弧を描く**。

中身は `tests/eval_sketch_geometry.mjs` にあり、pytestは `node` で走らせて終了コードだけを
見る(`test_view_cube.py` / `test_grid_scale.py` と同じやり方)。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

EVALUATOR = Path(__file__).resolve().parent / "eval_sketch_geometry.mjs"


def test_the_sketch_geometry_matches_the_hand_computed_values() -> None:
    """TS側の純関数を **Nodeで実際に評価**する。**skipしない** —— 足場が無いときのskipは
    無人実行では緑と区別が付かない。"""
    assert EVALUATOR.exists(), f"評価の口が無い: {EVALUATOR}"
    try:
        completed = subprocess.run(
            ["node", str(EVALUATOR)],
            capture_output=True,
            cwd=str(EVALUATOR.parent.parent),
            timeout=60,
        )
    except FileNotFoundError as error:
        raise AssertionError(
            "node が見つからない。TS側の幾何を評価できないので検査できない"
        ) from error
    assert completed.returncode == 0, (
        "web/src/commands/sketchGeometry.ts が期待する形を出さない:\n"
        + completed.stdout.decode("utf-8", "replace")
        + completed.stderr.decode("utf-8", "replace")
    )
