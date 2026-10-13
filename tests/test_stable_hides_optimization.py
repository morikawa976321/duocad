"""安定版(制作用)では形状最適化を出さない(本人決定 2026-10-13、docs/TOPOPT_CONVERGENCE.md 9節・docs/STABLE.md)。

落ちたら、安定版のAIの道具・WebSocketの口・画面へ渡す設定のどれかに形状最適化が残っているか、隠すつもりの無い命令まで隠している。
"""
from __future__ import annotations

from duocad import catalog, channel
from duocad.mcp_server import DocumentTools, McpServer
from duocad.server import Session
from session_support import browser

OPTIMIZATION_TOOLS = {"set_preserve_bodies", "set_exclude_bodies", "set_fixtures", "set_loads", "clear_loads"}
OPTIMIZATION_SOCKET_ONLY = {"optimize_conditions", "optimize_resolution", "run_optimization", "cancel_optimization",
                            "finish_optimization", "optimize_steps", "optimize_step_voxels"}


def test_only_the_shape_optimization_commands_are_hidden_in_stable():
    hidden = channel.hidden_features("stable")
    assert channel.hidden_features("dev") == frozenset()
    assert catalog.feature_command_names(hidden) == OPTIMIZATION_TOOLS | OPTIMIZATION_SOCKET_ONLY
    assert set(catalog.tool_names()) - set(catalog.tool_names(hidden)) == OPTIMIZATION_TOOLS


def test_stable_ai_tools_leave_out_the_optimization_conditions_and_count_what_is_left():
    hidden = channel.hidden_features("stable")
    tools = DocumentTools(session=None, hidden_features=hidden)  # type: ignore[arg-type]
    names = {d["name"] for d in tools.descriptors()}
    assert not names & OPTIMIZATION_TOOLS and len(names) == len(catalog.tool_names()) - len(OPTIMIZATION_TOOLS)
    try:
        tools.call("set_fixtures", {})
    except KeyError:
        pass
    else:
        raise AssertionError("安定版で set_fixtures が呼べた")
    reply = McpServer(tools).handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert f"公開ツールは{len(names)}個" in reply["result"]["instructions"]


def test_stable_socket_refuses_the_optimization_commands_and_dev_still_answers(tmp_path):
    def session() -> Session:
        return Session(None, 0.2, models_dir=tmp_path / "models", exports_dir=tmp_path / "export")

    async def script(send):
        refused = await send({"cmd": "optimize_conditions"})
        state = await send({"cmd": "get_state"})
        return refused, state

    (refused, state), _ = browser(session(), script, connections=1, hidden_features=channel.hidden_features("stable"))
    assert refused["type"] == "error" and refused["where"] == "optimize_conditions" and "この版では使えない" in refused["message"]
    assert state["type"] != "error"
    (answered, _), _ = browser(session(), script, connections=1)
    assert answered["type"] != "error"


def test_the_page_is_told_over_the_socket_whether_shape_optimization_is_available(tmp_path):
    """画面は接続のたびに届く `optimize_status` の `available` で入口とステップの取り直しを決める(設定ファイルの取得に依らない)。"""
    from duocad.server import Server

    session = Session(None, 0.2, models_dir=tmp_path / "models", exports_dir=tmp_path / "export")
    assert Server(session, hidden_features=channel.hidden_features("stable"))._optimize_status()["available"] is False
    assert Server(session)._optimize_status()["available"] is True
