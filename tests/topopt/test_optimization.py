"""実行の寿命、配送の控え、仕上げ受付の境界を外から確かめる。"""
from __future__ import annotations

import asyncio
import json
import threading

import pytest

from duocad import optimize_run
from duocad.autosave import Autosave
from duocad.document import Document
from duocad.document_store import DocumentStore
from duocad.optimization import Optimization, OptimizationResults, OptimizationRun
from duocad.server import Server
from duocad.sketch import sketch_create
from duocad.verbs.add_mesh import add_mesh_body
from session_support import FakeConnection, four_conditions


def test_ending_a_run_closes_requests_and_a_new_run_starts_without_them():
    """終了した実行は要求を受け付けず、中断と仕上げ要求を次の実行へ持ち越さない。"""
    owner = Optimization()
    document = Document()
    owner.begin(document, "a.json", 123.0)
    run = owner.active
    run.cancel()
    assert run.request_finish(2) is True
    assert run.status() == {"running": True, "cancelling": True, "finishing": True}
    owner.end()
    assert run.request_finish(1) is False
    assert not owner.visible(document, "a.json")
    owner.begin(document, "a.json", 123.0)
    assert owner.active.status() == {"running": True}
    assert owner.active.request_finish(None) is True
    assert owner.active.read_finish_request(final=True) == -1
    assert owner.active.request_finish(0) is False


def test_steps_survive_the_run_and_are_replaced_by_the_next_search():
    """終了後と同名の開き直しではステップを保ち、次の探索を始めると前の記録を捨てる。"""
    owner = Optimization()
    document = Document()
    owner.begin(document, "a.json", None)
    run = owner.prepare_search()
    assert owner.steps_for(document, "a.json") is None
    run.start_search()
    steps = owner.steps_for(document, "a.json")
    owner.end()
    assert steps is not None and owner.steps_for(Document(), "a.json") is steps
    assert owner.steps_for(Document(), "b.json") is None
    owner.begin(Document(), "b.json", None)
    owner.prepare_search().start_search()
    assert owner.steps_for(document, "a.json") is None
    assert owner.steps_for(Document(), "b.json") is not steps


def test_failed_delivery_keeps_each_document_and_created_time(tmp_path, monkeypatch):
    """連続して書けなくても両方の結果を保ち、通知と文書はそれぞれ一度だけ取り出せる。"""
    store = DocumentStore(tmp_path / "models", tmp_path / "exports")
    results = OptimizationResults(store, Autosave(store))

    def fail(*args):
        raise PermissionError("使用中")

    monkeypatch.setattr(store, "write_document_file", fail)
    for name, created in (("a.json", 123.0), ("b.json", 456.0)):
        run = OptimizationRun(Document(), name, created)
        sketch_create(run.work, "XY")
        run.adopt_results()
        result = results.deliver_elsewhere(run, {"state": "done"})
        assert result["file"] == name and result["elsewhere"] is True
        assert "PermissionError" in result["warning"]
        results.keep_notice(name, result)
    for name, created in (("a.json", 123.0), ("b.json", 456.0)):
        document, kept_created = results.take_document(name)
        assert kept_created == created and len(document.sketches) == 1
        assert results.take_document(name) is None
        assert results.take_notice(name)["file"] == name
        assert results.take_notice(name) is None


@pytest.mark.parametrize("before_final_read", [True, False])
def test_finish_requests_around_the_final_read_reach_the_screen_and_result(
    session, monkeypatch, before_final_read
):
    """最後の読み取り前に受け付けた番号を結果に使い、読み取り後の要求は実行中表示を保って断る。"""
    four_conditions(session)
    before_read = threading.Event()
    allow_read = threading.Event()
    after_read = threading.Event()
    allow_finish = threading.Event()

    def fake_run(document, options, *, finish_request, **kwargs):
        assert finish_request() is None
        before_read.set()
        assert allow_read.wait(10)
        selected = finish_request(final=True)
        after_read.set()
        assert allow_finish.wait(10)
        body_id = add_mesh_body(document, [0., 0., 0., 1., 0., 0., 0., 1., 0.], [0, 1, 2], name="結果")
        return {"state": "done", "body_id": body_id, "name": "結果", "triangle_count": 1,
                "finished_step": 3 if selected is None else selected}

    monkeypatch.setattr(optimize_run, "run", fake_run)

    async def scenario():
        server = Server(session)
        server.bind_loop(asyncio.get_running_loop())
        connection = FakeConnection()
        server.clients.add(connection)
        try:
            await server._dispatch(connection, json.dumps({"cmd": "run_optimization"}))
            assert await asyncio.to_thread(before_read.wait, 10)
            if before_final_read:
                await server._dispatch(connection, json.dumps({"cmd": "finish_optimization", "step": 1}))
                assert connection.last("optimize_status")["finishing"] is True
            allow_read.set()
            assert await asyncio.to_thread(after_read.wait, 10)
            await server._dispatch(connection, json.dumps({"cmd": "finish_optimization", "step": 0}))
            error = connection.last("optimize_error")
            assert error["already_running"] is True and "探索はもう終わって" in error["message"]
            assert connection.last("optimize_status")["running"] is True
        finally:
            allow_read.set()
            allow_finish.set()
            await asyncio.gather(*server._pending_optimize_runs)
        result = connection.last("optimize_result")
        assert result["finished_step"] == (1 if before_final_read else 3)
        assert connection.last("optimize_status")["running"] is False

    asyncio.run(scenario())
    assert session.document.bodies[-1].name == "結果"
    assert json.loads((session.models_dir / session.current_name).read_text(encoding="utf-8"))["operations"][-1]["verb"] == "add_mesh_body"


def test_a_failed_delivery_is_reopened_and_announced_once_to_all_connections(session, monkeypatch):
    """別文書の画面へ結果を出さず、保存失敗後に戻ると結果を再保存して全接続へ一度だけ通知する。"""
    four_conditions(session)
    session.rename_document("a.json")
    session.copy_document("a.json", "b.json")
    original = session.document
    entered = threading.Event()
    release = threading.Event()
    loop_thread = threading.get_ident()
    real_write = session.store.write_document_file
    refuse = True

    def write(target, payload):
        if refuse and target.name == "a.json":
            assert threading.get_ident() == loop_thread
            raise PermissionError("使用中")
        real_write(target, payload)

    def fake_run(document, options, **kwargs):
        assert threading.get_ident() != loop_thread
        assert all(body.shape is not original.get_body(body.id).shape for body in document.bodies)
        entered.set()
        assert release.wait(10)
        body_id = add_mesh_body(document, [0., 0., 0., 1., 0., 0., 0., 1., 0.], [0, 1, 2], name="結果")
        return {"state": "done", "body_id": body_id, "name": "結果", "triangle_count": 1}

    monkeypatch.setattr(session.store, "write_document_file", write)
    monkeypatch.setattr(optimize_run, "run", fake_run)

    async def scenario():
        nonlocal refuse
        server = Server(session)
        server.bind_loop(asyncio.get_running_loop())
        connections = [FakeConnection(), FakeConnection()]
        server.clients.update(connections)

        async def send(command):
            await server._dispatch(connections[0], json.dumps(command))

        try:
            await send({"cmd": "run_optimization"})
            assert await asyncio.to_thread(entered.wait, 10)
            await send({"cmd": "open_document", "name": "b.json"})
            assert all(body.name != "結果" for body in original.bodies)
        finally:
            release.set()
            await asyncio.gather(*server._pending_optimize_runs)
        assert session.current_name == "b.json"
        assert all(not connection.of_type("optimize_result") for connection in connections)
        assert all(body.name != "結果" for body in session.document.bodies)
        assert "add_mesh_body" not in (session.models_dir / "a.json").read_text(encoding="utf-8")
        refuse = False
        await send({"cmd": "open_document", "name": "a.json"})
        assert session.document.bodies[-1].name == "結果"
        if session.pending_write is not None:
            await asyncio.wrap_future(session.pending_write)
            session.settle_autosave()
        assert "add_mesh_body" in (session.models_dir / "a.json").read_text(encoding="utf-8")
        session.sketch_create("XY")
        await send({"cmd": "open_document", "name": "b.json"})
        await send({"cmd": "open_document", "name": "a.json"})
        for connection in connections:
            [result] = connection.of_type("optimize_result")
            assert result["file"] == "a.json" and "PermissionError" in result["warning"]

    asyncio.run(scenario())
