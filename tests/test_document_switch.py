"""人が画面で文書を切り替えたことを、AI が知らないまま操作を続けない契約(issue #102)。"""
from __future__ import annotations

import asyncio
import json

from duocad.agent.loop import AgentLoop
from duocad.mcp_server import DocumentTools, McpServer
from duocad.server import Session
from agent_support import RecordedProvider, spoke, tool_use
from mcp_support import call, call_error
from session_support import FakeConnection
from test_agent_loop import SpyTools
from test_agent_ws import _settle, _wait_until, build_server


class SwitchingJustBeforeTheCall(SpyTools):
    """ループの直前チェックを通った後、ツール実行の投げ返しが走る前に人が新規を押す。"""

    def __init__(self, inner: DocumentTools, session: Session) -> None:
        super().__init__(inner)
        self.session = session

    def call_and_seq(self, name, arguments, expected_seq=None):
        self.session.new_document()
        return super().call_and_seq(name, arguments, expected_seq)


def test_generation_moves_on_switch_but_not_on_verbs(session: Session):
    """落ちたら、世代が文書の同一性を表していない(verb で動くと AI が自分の操作で止まる)。"""
    tools = DocumentTools(session)
    session.new_document()
    before = session.generation
    tools.call("sketch_create", {"base_plane": "XY"})
    assert session.generation == before
    session.new_document()
    assert session.generation == before + 1
    session.close_document()
    assert session.generation == before + 2


def test_the_version_changes_between_two_new_documents(session: Session):
    """落ちたら、新規→新規の切り替えをアプリ内AIの楽観ロックがすり抜ける(残りの3つは同値になる)。"""
    tools = DocumentTools(session)
    session.new_document()
    first = tools.current_seq()
    session.new_document()
    assert tools.current_seq() != first


def test_describe_document_names_the_file_and_generation(session: Session):
    session.new_document()
    session.save_document("bracket.json")
    described = DocumentTools(session).describe_document()
    assert described["current_name"] == "bracket.json"
    assert described["generation"] == session.generation


def test_mcp_stops_tools_after_a_switch_until_the_document_is_described_again(session: Session):
    """落ちたら、外部MCPの verb が切り替え後の文書に黙って効く(または確かめても通れない)。"""
    session.new_document()
    server = McpServer(DocumentTools(session))
    call(server, "describe_document")
    call(server, "sketch_create", {"base_plane": "XY"})

    session.new_document()
    error = call_error(server, "sketch_create", {"base_plane": "XY"})
    assert error["code"] == "document_changed"
    # 止めただけでは解かない: 確かめずに打ち直しても通らない。
    assert call_error(server, "sketch_create", {"base_plane": "XY"})["code"] == "document_changed"
    assert session.document.sketches == []

    call(server, "describe_document")
    call(server, "sketch_create", {"base_plane": "XY"})
    assert len(session.document.sketches) == 1


def test_mcp_does_not_check_before_the_client_has_looked(session: Session):
    """落ちたら、describe_document を呼ばないクライアントが切り替えと無関係に止まる。"""
    session.new_document()
    server = McpServer(DocumentTools(session))
    session.new_document()
    call(server, "sketch_create", {"base_plane": "XY"})


def test_a_switch_between_the_check_and_the_call_does_not_reach_the_new_document(session: Session):
    """落ちたら、ループの照合とツール実行の隙間に入った切り替えで、古い前提の verb が新しい文書に効く(sol の指摘)。"""
    session.new_document()
    tools = SwitchingJustBeforeTheCall(DocumentTools(session), session)
    provider = RecordedProvider(
        [tool_use("sketch_create", {"base_plane": "XY"}, call_id="call_1"), spoke("止まった")]
    )
    run = AgentLoop(tools, provider).run("スケッチを作って")

    assert session.document.sketches == []
    assert run.tool_calls == ()


def test_switching_documents_stops_a_running_in_app_ai(monkeypatch, box_session: Session):
    """落ちたら、アプリ内AIが前の依頼を切り替え後の文書で続ける。"""
    monkeypatch.setattr(
        "duocad.server.build_provider",
        lambda *a, **k: RecordedProvider(
            [
                tool_use("describe_document", {}, call_id="call_1"),
                tool_use("sketch_create", {"base_plane": "XY"}, call_id="call_2"),
                spoke("最後まで到達したら失敗"),
            ],
            delay=0.08,
        ),
    )

    async def scenario():
        server = build_server(box_session, asyncio.get_running_loop())
        watcher = FakeConnection()
        server.clients.add(watcher)
        await server._dispatch(watcher, json.dumps({"cmd": "ai_send", "message": "スケッチを作って"}))
        await _wait_until(lambda: watcher.of_type("ai_tool_call"))
        await server._dispatch(watcher, json.dumps({"cmd": "new_document"}))
        await _wait_until(lambda: watcher.of_type("ai_result"))
        await _settle(server)
        return watcher

    watcher = asyncio.run(scenario())
    assert watcher.of_type("ai_result")[-1]["status"] == "cancelled"
    assert box_session.document.sketches == []
