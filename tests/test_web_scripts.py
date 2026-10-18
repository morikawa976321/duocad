"""`web/scripts/` の実機検査の道具(サーバの起動・MCP のクライアント・撮影台の距離決め)を Node で評価する。

落ちたら、検査の道具が失敗の後始末をしない(node が終わらない・python のサーバが残る)か、撮影台が NaN で落ちる。
ブラウザは使わない。
"""
from __future__ import annotations

from pathlib import Path

from node_support import run_node

TESTS_DIR = Path(__file__).resolve().parent


def test_the_framing_of_the_comparison_shots_survives_a_vertical_view() -> None:
    run_node(TESTS_DIR / "eval_shoot_framing.mjs", "撮影台の距離決め(eval_shoot_framing.mjs)")


def test_the_mcp_client_turns_server_noise_into_a_rejection() -> None:
    run_node(TESTS_DIR / "eval_mcp_client_noise.mjs", "McpClient の雑音の扱い(eval_mcp_client_noise.mjs)")


def test_start_server_stops_the_python_server_when_the_start_fails() -> None:
    """空きポートで python のサーバを起動し、HTTP が上がる前に期限を切らせる。"""
    run_node(
        TESTS_DIR / "eval_start_server_failure.mjs", "startServer の失敗時の後始末(eval_start_server_failure.mjs)",
        timeout=90,
    )
