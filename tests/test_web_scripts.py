"""`web/scripts/` の実機検査の道具(MCP のクライアント)を Node で評価する。

落ちたら、MCP のクライアントがサーバの雑音で落ちる。
ブラウザは使わない。
"""
from __future__ import annotations

from pathlib import Path

from node_support import run_node

TESTS_DIR = Path(__file__).resolve().parent


def test_the_mcp_client_turns_server_noise_into_a_rejection() -> None:
    run_node(TESTS_DIR / "eval_mcp_client_noise.mjs", "McpClient の雑音の扱い(eval_mcp_client_noise.mjs)")

