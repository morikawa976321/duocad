"""実物のClaude APIでエージェントループが回ること(**課金されるので既定はskip**)。

走らせる条件は2つ: キーが置いてある(`DUOCAD_ANTHROPIC_API_KEY` か `duocad_secrets.json`)、
かつ環境変数 `DUOCAD_LIVE_CLAUDE=1`。キーだけでは走らない —— 無人のテスト実行が黙って課金しないため。

    $env:DUOCAD_LIVE_CLAUDE = "1"; .venv\\Scripts\\python.exe -m pytest tests/test_agent_claude_api.py -v

決定的な検査は `tests/test_agent_anthropic.py`(偽transport)が持つ。ここが見るのは偽物では確かめられない
3点だけである: thinkingブロックを送り返した往復が400にならない、ループが代わりに置いた問い合わせ
(思考ブロックの無いtool_use)が受け付けられる、2往復目で tools + system がキャッシュから読まれる。
"""
from __future__ import annotations

import os

import pytest

from duocad.agent import AgentLoop, AnthropicProvider, Limits
from duocad.agent.loop import FIRST_LOOK
from duocad.agent.providers import ANTHROPIC_MAX_TOKENS, default_model, load_api_key
from duocad.mcp_server import DocumentTools
from session_support import session_with_box

#: **収集の時点で読む** —— 各テストの前に `conftest._no_claude_api_key` が読み口を空にするので、
#: テストの中で読むと常に未設定になる。
LIVE_KEY = load_api_key()

pytestmark = [
    pytest.mark.skipif(not LIVE_KEY, reason="ClaudeのAPIキーが置いていない"),
    pytest.mark.skipif(
        os.environ.get("DUOCAD_LIVE_CLAUDE") != "1", reason="課金されるので DUOCAD_LIVE_CLAUDE=1 のときだけ走る"
    ),
]


def test_claude_draws_a_circle_through_several_tool_calls():
    tools = DocumentTools(session_with_box())
    calls: list[tuple[str, bool]] = []
    loop = AgentLoop(
        tools,
        AnthropicProvider(LIVE_KEY, model=default_model("anthropic")),
        limits=Limits(max_tokens=ANTHROPIC_MAX_TOKENS, max_total_seconds=120.0),
        progress=lambda name, args, result, ok: calls.append((name, ok)),
        first_look=FIRST_LOOK,
    )
    run = loop.run("XY平面に新しいスケッチを作って、原点に半径5mmの円を1つ描いて。")

    assert run.status == "completed", f"{run.status}: {run.detail}"
    assert ("add_circle", True) in calls, f"円を描けていない: {calls}"
    assert run.turns >= 2, "1往復で終わった(thinkingブロックを送り返す往復を通っていない)"
    assert run.usage.get("cache_read_input_tokens", 0) > 0, f"キャッシュから読まれていない: {run.usage}"
