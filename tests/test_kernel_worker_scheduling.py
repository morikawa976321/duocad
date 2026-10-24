"""遅延する検証用の子で、優先順位・取消し・待機を含む期限を検査する。"""
from concurrent.futures import ThreadPoolExecutor
import asyncio
import json
import sys
import threading
import time

import pytest
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from duocad import kernel_worker, worker_process
from test_blend_worker import _box
from test_preview_off_the_loop import _run

CHILD = r'''
import json, sys, time
from pathlib import Path
for line in sys.stdin:
    m = json.loads(line)
    Path(sys.argv[1]).write_text(str(m["arguments"].get("tag", "preview")))
    time.sleep(m["arguments"].get("delay", 5))
    result = dict(shape=m["shape"], provenance={}, volume_before=8000, volume_after=8000)
    print(json.dumps(dict(seq=m["seq"], ok=True, result=result)), flush=True)
'''


@pytest.fixture
def delayed(monkeypatch, tmp_path):
    marker = tmp_path / "started"
    with worker_process.WorkerProcess([sys.executable, "-c", CHILD, str(marker)]) as worker:
        monkeypatch.setattr(kernel_worker, "_WORKER", worker)
        yield worker, marker
        _wait(lambda: kernel_worker._ACTIVE is None)


def _wait(predicate):
    deadline = time.monotonic() + 4
    while not predicate():
        assert time.monotonic() < deadline, "検証用の子または待機要求が開始しなかった"
        time.sleep(0.005)


def _call(op, delay, tag, preview=None, timeout=1.5):
    shape = BRepPrimAPI_MakeBox(20, 20, 20).Shape()
    def run():
        return kernel_worker.run("fillet" if op == "blend_edges" else "offset_faces", shape,
                                 {"delay": delay, "tag": tag}, op=op, timeout=timeout)
    if preview is None:
        return run()
    with kernel_worker.preview_context(preview):
        return run()


@pytest.mark.parametrize("op", ["blend_edges", "face_edit"])
@pytest.mark.parametrize("timeout", [1.5, None])
def test_commit_and_replay_preempt_a_computing_preview(delayed, op, timeout):
    worker, marker = delayed
    with ThreadPoolExecutor(1) as pool:
        previous = pool.submit(_call, op, 5, "preview", threading.Event())
        _wait(marker.exists)
        started = time.monotonic()
        _call(op, 0.05, "commit", timeout=timeout)
        assert time.monotonic() - started < 1.0
        with pytest.raises(ValueError, match="取り消した"):
            previous.result(timeout=2)
    assert worker.starts == 2


def test_cancelled_preview_stops_the_child(delayed):
    worker, marker = delayed
    cancel = threading.Event()
    with ThreadPoolExecutor(1) as pool:
        previous = pool.submit(_call, "blend_edges", 5, "preview", cancel)
        _wait(marker.exists)
        cancel.set()
        with pytest.raises(ValueError, match="取り消した"):
            previous.result(timeout=1)
    assert worker._process is None


def test_preview_does_not_stop_commit_and_only_the_latest_waiter_runs(delayed):
    worker, marker = delayed
    with ThreadPoolExecutor(3) as pool:
        commit = pool.submit(_call, "face_edit", 0.6, "commit")
        _wait(marker.exists)
        old = pool.submit(_call, "blend_edges", 0, "old", threading.Event())
        _wait(lambda: kernel_worker._PENDING_PREVIEW is not None)
        latest = pool.submit(_call, "blend_edges", 0, "latest", threading.Event())
        with pytest.raises(ValueError, match="取り消した"):
            old.result(timeout=1)
        commit.result(timeout=2)
        latest.result(timeout=2)
    assert worker.starts == 1
    assert marker.read_text() == "latest"


@pytest.mark.parametrize("preview", [True, False])
def test_deadline_includes_waiting_and_expired_request_is_never_sent(delayed, preview):
    worker, marker = delayed
    with ThreadPoolExecutor(1) as pool:
        commit = pool.submit(_call, "blend_edges", 0.6, "commit")
        _wait(marker.exists)
        started = time.monotonic()
        with pytest.raises(ValueError, match="秒以内"):
            _call("face_edit", 0, "expired", threading.Event() if preview else None, timeout=0.15)
        assert time.monotonic() - started < 0.4
        commit.result(timeout=2)
    assert worker.starts == 1 and marker.read_text() == "commit"


def test_cancelled_websocket_future_stops_the_preview_child(session, delayed):
    worker, marker = delayed
    _box(session)
    async def script(server, client):
        await server._dispatch(client, json.dumps(dict(cmd="preview", verb="fillet", args=dict(radius=2))))
        deadline = time.monotonic() + 4
        while not marker.exists():
            assert time.monotonic() < deadline
            await asyncio.sleep(0.01)
        server._preview_futures[client].cancel()
        while worker._process is not None:
            assert time.monotonic() < deadline
            await asyncio.sleep(0.01)
    _, client = _run(session, script)
    assert not client.of_type("preview")


def test_websocket_keeps_only_latest_preview_across_connections(session, delayed):
    from duocad.session import PreviewJob
    from session_support import FakeConnection
    _, marker = delayed
    async def script(server, client):
        other = FakeConnection()
        server.clients.add(other)
        # 確定が子を所有している間に、異なる接続から3件のプレビューを待たせる。
        with ThreadPoolExecutor(1) as pool:
            commit = pool.submit(_call, "face_edit", 0.6, "commit")
            while not marker.exists():
                await asyncio.sleep(0.005)
            jobs = []
            for connection, tag in [(client, "old"), (other, "middle"), (client, "latest")]:
                job = PreviewJob(lambda tag=tag: (_call("blend_edges", 0, tag) and dict(verb="fillet", bodies=[])), lambda: None)
                jobs.append(job)
                seq = server._supersede_previews(connection)
                server._start_preview(connection, {}, job, seq)
                await asyncio.sleep(0.02)
            while not commit.done():
                await asyncio.sleep(0.01)
            commit.result()
        assert jobs[0]._cancel.is_set() and jobs[1]._cancel.is_set()
    async def scenario():
        from duocad.net.ws import Server
        server = Server(session)
        server.bind_loop(asyncio.get_running_loop())
        client = FakeConnection()
        server.clients.add(client)
        await asyncio.wait_for(script(server, client), timeout=4)
        await asyncio.wait_for(asyncio.gather(*list(server._pending_pushes)), timeout=2)
        return client
    client = asyncio.run(scenario())
    assert marker.read_text() == "latest"
    assert len(client.of_type("preview")) == 1


def test_superseded_job_cannot_enter_the_worker_after_an_inline_preview(session, monkeypatch):
    from duocad import kernel
    from duocad.net.ws import Server
    from session_support import FakeConnection
    _box(session)
    monkeypatch.setattr(kernel, "_RESULTS", type(kernel._RESULTS)())
    entered, release = threading.Event(), threading.Event()
    real = kernel_worker.run
    def pause_old(verb, shape, arguments, **kwargs):
        if arguments["values"][0][1] == 1000:
            entered.set()
            assert release.wait(timeout=4)
        else:
            # 同期のプレビュー中は Future の done callback がまだ走れない。
            assert old_job._cancel.is_set()
            release.set()
        return real(verb, shape, arguments, **kwargs)
    monkeypatch.setattr(kernel_worker, "run", pause_old)
    old_job = session.preview_job("fillet", dict(radius=1000))
    async def scenario():
        server = Server(session)
        server.bind_loop(asyncio.get_running_loop())
        client = FakeConnection()
        server.clients.add(client)
        try:
            seq = server._supersede_previews(client)
            server._start_preview(client, dict(preview_id=1), old_job, seq)
            deadline = time.monotonic() + 4
            while not entered.is_set():
                assert time.monotonic() < deadline
                await asyncio.sleep(0.005)
            monkeypatch.setattr(session, "preview_job", lambda *_args, **_kwargs: None)
            await server._dispatch(client, json.dumps(dict(cmd="preview", verb="fillet", args=dict(radius=2), preview_id=2)))
            await asyncio.wait_for(asyncio.gather(*list(server._pending_pushes)), timeout=4)
            assert [item["preview_id"] for item in client.of_type("preview")] == [2]
            assert not client.of_type("error")
        finally:
            release.set()
            server._preview_worker.shutdown(wait=True)
    asyncio.run(scenario())
