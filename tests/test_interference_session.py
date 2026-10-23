"""干渉チェックの口(Session・MCP・WebSocket)。落ちたら、文書を変えている・口によって答えが違う・古い結果が届く、のどれかが起きている。"""
from __future__ import annotations

import asyncio
import json
import sys

import pytest
from OCP.BRep import BRep_Tool
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_VERTEX
from OCP.TopoDS import TopoDS

from duocad import interference, selection
from duocad.document import Body
from duocad.interference_worker import WorkerProcess
from duocad.mcp_server import DocumentTools, McpServer
from duocad.mesh_geometry import subshapes
from duocad.server import Server, Session
from interference_support import InProcessWorker, box
from mcp_support import call, call_error, request
from session_support import FakeConnection


@pytest.fixture(autouse=True)
def _in_process(monkeypatch):
    """既定は子を起こさず同じプロセスで走らせる(子の試験は test_interference_worker.py)。"""
    monkeypatch.setattr(interference, "default_worker", lambda load, cancel: InProcessWorker(load, cancel=cancel))


def make_session() -> Session:
    session = Session(0.2)
    for body_id, shape in (("a", box(0, 0, 0, 10, 10, 10)), ("b", box(5, 0, 0, 10, 10, 10)), ("c", box(40, 0, 0, 10, 10, 10))):
        session.document.add_body(Body(id=body_id, name=body_id, kind="solid", shape=shape))
    return session


def tolerances(session):
    """部分形状ごとの許容差(最大値だけだと、一部の辺だけ書き換わったのを見逃す)。"""
    out = []
    for body in session.document.bodies:
        for kind, cast in ((TopAbs_VERTEX, TopoDS.Vertex_s), (TopAbs_EDGE, TopoDS.Edge_s), (TopAbs_FACE, TopoDS.Face_s)):
            out.append([BRep_Tool.Tolerance_s(cast(sub)) for sub in subshapes(body.shape, kind)])
    return out


def fingerprint(session):
    return (
        session.document.log.to_json(), len(session.document.log.entries), session.applied_count,
        session.get_selection(), [body.id for body in session.document.bodies], tolerances(session),
        [(len(subshapes(b.shape, TopAbs_VERTEX)), len(subshapes(b.shape, TopAbs_EDGE)), len(subshapes(b.shape, TopAbs_FACE))) for b in session.document.bodies],
    )


def test_checking_leaves_the_document_the_log_and_the_selection_untouched():
    session = make_session()
    session.modify_selection([selection.select_body(session.document, "a")], [])
    before = fingerprint(session)
    result = session.check_interference(0.2)
    assert fingerprint(session) == before
    assert result["target"]["mode"] == "one_vs_rest" and result["counts"]["overlap"] == 1


def test_an_mcp_call_returns_the_same_pairs_as_the_session():
    session = make_session()
    server = McpServer(DocumentTools(session))
    names = [tool["name"] for tool in request(server, "tools/list")["result"]["tools"]]
    assert "check_interference" in names
    over_mcp = call(server, "check_interference", {"clearance": 0.2})
    direct = session.check_interference(0.2)
    for key in ("pairs", "counts", "target", "clearance"):
        assert over_mcp[key] == direct[key]
    assert all("render" not in pair for pair in over_mcp["pairs"])
    assert over_mcp["thresholds"] == interference.THRESHOLDS and over_mcp["units"] == "mm"
    assert call(server, "check_interference")["clearance"] == 0.0


def test_mcp_refuses_a_bad_clearance_and_a_document_with_no_pair():
    server = McpServer(DocumentTools(make_session()))
    assert call_error(server, "check_interference", {"clearance": -1})["code"] == "invalid_argument"
    lonely = Session(0.2)
    lonely.document.add_body(Body(id="a", name="a", kind="solid", shape=box(0, 0, 0, 1, 1, 1)))
    assert call_error(McpServer(DocumentTools(lonely)), "check_interference")["code"] == "invalid_argument"


def test_mcp_keeps_the_contacts_and_tiny_overlaps_it_does_not_hide_them():
    session = Session(0.2)
    for body_id, shape in (("a", box(0, 0, 0, 10, 10, 10)), ("touch", box(10, 0, 0, 10, 10, 10)), ("thin", box(0, 9.9995, 0, 10, 10, 10))):
        session.document.add_body(Body(id=body_id, name=body_id, kind="solid", shape=shape))
    relations = {(p["a"], p["b"]): p["relation"] for p in call(McpServer(DocumentTools(session)), "check_interference")["pairs"]}
    assert relations[("a", "touch")] == "contact" and relations[("a", "thin")] == "tiny_overlap"


# ---------------------------------------------------------------------------------------------- WebSocket
def run_ws(session, script):
    async def scenario():
        server = Server(session)
        server.bind_loop(asyncio.get_running_loop())
        clients = [FakeConnection(), FakeConnection()]
        for client in clients:
            server.clients.add(client)
            await server._push(client, applied=None, only=client)
            client.payloads.clear()

        async def send(command, index=0):
            await server._dispatch(clients[index], json.dumps(command))

        async def until(predicate, seconds=20.0):
            for _ in range(int(seconds / 0.01)):
                if predicate():
                    return
                await asyncio.sleep(0.01)
            raise AssertionError("待っても届かない")

        await script(server, clients, send, until)
        return clients

    return asyncio.run(scenario())


def interference_payloads(client):
    return [p for p in client.payloads if p["type"] == "interference"]


def test_the_ws_reply_goes_only_to_the_requester_with_render_data_and_the_revision():
    session = make_session()

    async def script(server, clients, send, until):
        await send({"cmd": "check_interference", "clearance": 0.2, "client_op": 41})
        await until(lambda: interference_payloads(clients[0]))

    clients = run_ws(session, script)
    payload = interference_payloads(clients[0])[0]
    assert payload["client_op"] == 41 and payload["request_id"] == 41
    assert payload["document_revision"] == f"{session.generation}.{session.applied_count}.{len(session.document.log.entries)}"
    overlap = next(p for p in payload["pairs"] if p["relation"] == "overlap")
    assert overlap["render"]["solid"]["indices"]
    assert not interference_payloads(clients[1]) and all(p["type"] != "interference" for p in clients[1].payloads)


def test_a_refused_request_comes_back_as_an_error_for_that_operation():
    session = Session(0.2)
    session.document.add_body(Body(id="a", name="a", kind="solid", shape=box(0, 0, 0, 1, 1, 1)))

    async def script(server, clients, send, until):
        await send({"cmd": "check_interference", "client_op": 9})

    clients = run_ws(session, script)
    error = next(p for p in clients[0].payloads if p["type"] == "error")
    assert error["where"] == "check_interference" and error["client_op"] == 9


def test_a_newer_request_from_the_same_connection_cancels_the_older_one(monkeypatch):
    made = []

    def factory(load, cancel):
        if not made:  # 1回目の検査は答えない子
            worker = WorkerProcess(load, command=[sys.executable, "-c", "import time; time.sleep(600)"], cancel=cancel)
        else:
            worker = InProcessWorker(load, cancel=cancel)
        made.append(worker)
        return worker

    monkeypatch.setattr(interference, "default_worker", factory)
    session = make_session()

    async def script(server, clients, send, until):
        await send({"cmd": "check_interference", "client_op": 1})
        await until(lambda: made)
        await send({"cmd": "check_interference", "client_op": 2})
        await until(lambda: interference_payloads(clients[0]))
        await asyncio.sleep(0.3)

    clients = run_ws(session, script)
    assert [p["client_op"] for p in interference_payloads(clients[0])] == [2]
    assert made[0]._process is None


def test_a_disconnect_cancels_the_run_and_nothing_is_sent(monkeypatch):
    made = []

    def factory(load, cancel):
        worker = WorkerProcess(load, command=[sys.executable, "-c", "import time; time.sleep(600)"], cancel=cancel)
        made.append(worker)
        return worker

    monkeypatch.setattr(interference, "default_worker", factory)

    async def script(server, clients, send, until):
        await send({"cmd": "check_interference", "client_op": 1})
        await until(lambda: made)
        server.clients.discard(clients[0])
        server._interference.cancel(clients[0])
        await asyncio.sleep(0.5)

    clients = run_ws(make_session(), script)
    assert not interference_payloads(clients[0]) and made[0]._process is None
