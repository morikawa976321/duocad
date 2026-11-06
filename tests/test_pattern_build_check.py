"""矩形反復の画面側の事前検査(`PATTERN_RECTANGULAR.build`)。"""
from __future__ import annotations

from pathlib import Path

from node_support import run_node


def test_a_zero_spacing_in_one_direction_is_refused_before_sending() -> None:
    """落ちたら、片方向だけ間隔0で個数2以上の指定が画面を通り、サーバが内部IDつきの文で断る。"""
    run_node(Path(__file__).resolve().parent / "eval_pattern_build.mjs", "矩形反復の事前検査")
