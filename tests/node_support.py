"""`tests/eval_*.mjs` のような Node の評価器を走らせるテストの道具。"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from duocad.paths import REPO_ROOT


def run_node(evaluator: Path, what: str, *, stdin=None, json_out: bool = False, timeout: int = 60):
    """`node <evaluator>` をリポジトリ直下で走らせ、stdout を返す(`json_out` なら JSON で読む)。"""
    assert evaluator.exists(), f"評価の口が無い: {evaluator}"
    try:
        completed = subprocess.run(
            ["node", str(evaluator)],
            input=None if stdin is None else json.dumps(stdin).encode("utf-8"),
            capture_output=True,
            cwd=str(REPO_ROOT),
            timeout=timeout,
        )
    except FileNotFoundError as error:
        raise AssertionError(f"node が見つからない。{what}を検査できない") from error
    if completed.returncode != 0:
        raise AssertionError(
            f"{what}の評価に失敗した(Node が .ts を読めない可能性がある。23.6 以降なら既定で読める):\n"
            + (completed.stdout + completed.stderr).decode("utf-8", "replace")
        )
    return json.loads(completed.stdout.decode("utf-8")) if json_out else completed.stdout
