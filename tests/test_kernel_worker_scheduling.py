"""遅延する検証用の子で、優先順位・取消し・待機を含む期限を検査する。"""
from concurrent.futures import ThreadPoolExecutor
import asyncio
import json
import sys
import threading
import time
from types import SimpleNamespace

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
    if m.get("op") == "ready":
        print(json.dumps(dict(seq=m["seq"], ok=True, result={})), flush=True)
        continue
    tag = str(m["arguments"].get("tag", "preview"))
    Path(sys.argv[1]).write_text(tag)
    delay = m["arguments"].get("delay")
    if delay is None:
        release = Path(sys.argv[1]).with_suffix("." + tag + ".release")
        while not release.exists():
            time.sleep(0.005)
    else:
        time.sleep(delay)
    with Path(sys.argv[1]).with_suffix(".completed").open("a") as completed:
        completed.write(tag + "\n")
    result = dict(shape=m["shape"], provenance={}, volume_before=8000, volume_after=8000)
    print(json.dumps(dict(seq=m["seq"], ok=True, result=result)), flush=True)
'''


@pytest.fixture
def delayed(monkeypatch, tmp_path):
    marker = tmp_path / "started"
    with worker_process.WorkerProcess([sys.executable, "-c", CHILD, str(marker)]) as worker:
        # 短い期限で子の起動速度を検査しない。受信ループからの返答を先に待つ。
        worker.request({"op": "ready"}, timeout=10)
        monkeypatch.setattr(kernel_worker, "_WORKER", worker)
        yield worker, marker
        _wait(lambda: kernel_worker._ACTIVE is None)


def _wait(predicate):
    deadline = time.monotonic() + 10
    while not predicate():
        assert time.monotonic() < deadline, "検証用の子または待機要求が開始しなかった"
        time.sleep(0.005)


def _release(marker, tag):
    marker.with_suffix(f".{tag}.release").touch()


def _observe_waiters(monkeypatch, *cancels):
    """各要求が実際に計算枠を待つまで、先行する計算を解放しない。"""
    waiting = {cancel: threading.Event() for cancel in cancels}
    real_wait = kernel_worker._CONDITION.wait
    def wait(*args, **kwargs):
        event = waiting.get(kernel_worker._PREVIEW.get())
        if event is not None:
            event.set()
        return real_wait(*args, **kwargs)
    monkeypatch.setattr(kernel_worker._CONDITION, "wait", wait)
    return waiting


def _call(op, delay, tag, preview=None, timeout=10):
    shape = BRepPrimAPI_MakeBox(20, 20, 20).Shape()
    def run():
        return kernel_worker.run("fillet" if op == "blend_edges" else "offset_faces", shape,
                                 {"delay": delay, "tag": tag}, op=op, timeout=timeout)
    if preview is None:
        return run()
    with kernel_worker.preview_context(preview):
        return run()


@pytest.mark.parametrize("op", ["blend_edges", "face_edit"])
@pytest.mark.parametrize("timeout", [10, None])
def test_commit_and_replay_preempt_a_computing_preview(delayed, op, timeout):
    worker, marker = delayed
    with ThreadPoolExecutor(1) as pool:
        previous = pool.submit(_call, op, None, "preview", threading.Event())
        _wait(marker.exists)
        _call(op, 0, "commit", timeout=timeout)
        with pytest.raises(ValueError, match="取り消した"):
            previous.result(timeout=10)
    assert worker.starts == 2


def test_cancelled_preview_stops_the_child(delayed):
    worker, marker = delayed
    cancel = threading.Event()
    with ThreadPoolExecutor(1) as pool:
        previous = pool.submit(_call, "blend_edges", None, "preview", cancel)
        _wait(marker.exists)
        cancel.set()
        with pytest.raises(ValueError, match="取り消した"):
            previous.result(timeout=10)
    assert worker._process is None


def test_previews_do_not_stop_commit_or_cancel_other_waiters(delayed, monkeypatch):
    worker, marker = delayed
    old_cancel, latest_cancel = threading.Event(), threading.Event()
    waiting = _observe_waiters(monkeypatch, old_cancel, latest_cancel)
    with ThreadPoolExecutor(3) as pool:
        commit = pool.submit(_call, "face_edit", None, "commit")
        _wait(marker.exists)
        old = pool.submit(_call, "blend_edges", 0, "old", old_cancel)
        latest = pool.submit(_call, "blend_edges", 0, "latest", latest_cancel)
        _wait(lambda: all(event.is_set() for event in waiting.values()))
        _release(marker, "commit")
        commit.result(timeout=10)
        old.result(timeout=10)
        latest.result(timeout=10)
    assert worker.starts == 1
    completed = marker.with_suffix(".completed").read_text().splitlines()
    assert completed[0] == "commit" and set(completed[1:]) == {"old", "latest"}


def test_new_preview_waits_for_running_preview_without_restarting_child(delayed, monkeypatch):
    worker, marker = delayed
    cancel = threading.Event()
    latest_cancel = threading.Event()
    waiting = _observe_waiters(monkeypatch, latest_cancel)
    with ThreadPoolExecutor(2) as pool:
        old = pool.submit(_call, "blend_edges", None, "old", cancel)
        _wait(marker.exists)
        latest = pool.submit(_call, "blend_edges", 0, "latest", latest_cancel)
        _wait(waiting[latest_cancel].is_set)
        assert not latest.done() and not cancel.is_set()
        _release(marker, "old")
        old.result(timeout=10)
        latest.result(timeout=10)
    assert not cancel.is_set()
    assert worker.starts == 1
    assert marker.with_suffix(".completed").read_text().splitlines() == ["old", "latest"]


def test_waiting_preview_deadline_does_not_stop_running_preview(delayed):
    worker, marker = delayed
    with ThreadPoolExecutor(1) as pool:
        old = pool.submit(_call, "blend_edges", None, "old", threading.Event())
        _wait(marker.exists)
        with pytest.raises(ValueError, match="秒以内"):
            _call("blend_edges", 0, "expired", threading.Event(), timeout=0.15)
        _release(marker, "old")
        old.result(timeout=10)
    assert worker.starts == 1
    assert marker.with_suffix(".completed").read_text().splitlines() == ["old"]


def test_running_preview_deadline_stops_child_and_next_request_recovers(delayed, monkeypatch):
    worker, marker = delayed
    expired = threading.Event()
    clock = SimpleNamespace(monotonic=lambda: time.monotonic() + (20 if expired.is_set() else 0))
    monkeypatch.setattr(kernel_worker, "time", clock)
    monkeypatch.setattr(worker_process, "time", clock)
    with ThreadPoolExecutor(1) as pool:
        running = pool.submit(_call, "blend_edges", None, "expired", threading.Event())
        _wait(marker.exists)  # 子が要求を受信し、計算中になってから期限を進める。
        expired.set()
        with pytest.raises(ValueError, match="秒以内"):
            running.result(timeout=10)
    assert marker.exists() and worker._process is None
    expired.clear()
    _call("blend_edges", 0, "next", threading.Event())
    assert worker.starts == 2
    assert marker.with_suffix(".completed").read_text().splitlines() == ["next"]


@pytest.mark.parametrize("preview", [True, False])
def test_deadline_includes_waiting_and_expired_request_is_never_sent(delayed, preview):
    worker, marker = delayed
    with ThreadPoolExecutor(1) as pool:
        commit = pool.submit(_call, "blend_edges", None, "commit")
        _wait(marker.exists)
        with pytest.raises(ValueError, match="秒以内"):
            _call("face_edit", 0, "expired", threading.Event() if preview else None, timeout=0.15)
        _release(marker, "commit")
        commit.result(timeout=10)
    assert worker.starts == 1 and marker.read_text() == "commit"


def test_cancelled_websocket_future_stops_the_preview_child(session, delayed):
    worker, marker = delayed
    _box(session)
    async def script(server, client):
        await server._dispatch(client, json.dumps(dict(cmd="preview", verb="fillet", args=dict(radius=2))))
        deadline = time.monotonic() + 10
        while not marker.exists():
            assert time.monotonic() < deadline
            await asyncio.sleep(0.01)
        server._preview_futures[client].cancel()
        while worker._process is not None:
            assert time.monotonic() < deadline
            await asyncio.sleep(0.01)
    _, client = _run(session, script)
    assert not client.of_type("preview")


@pytest.mark.parametrize("superseded", [False, True])
def test_disconnect_stops_even_a_superseded_running_preview(session, delayed, monkeypatch, superseded):
    from duocad.net.ws import Server
    from duocad.session import PreviewJob
    from session_support import FakeConnection
    worker, marker = delayed
    jobs = []
    def make_job(_verb, args):
        tag = str(args["radius"])
        job = PreviewJob(lambda: (_call("blend_edges", None, tag) and dict(verb="fillet", bodies=[])), lambda: None)
        jobs.append(job)
        return job
    monkeypatch.setattr(session, "preview_job", make_job)
    async def scenario():
        disconnected = asyncio.Event()
        class Connection(FakeConnection):
            async def __aiter__(self):
                await disconnected.wait()
                return
                yield  # 非同期イテレータ。切断までは受信を待つ。
        server = Server(session)
        server.bind_loop(asyncio.get_running_loop())
        client = Connection()
        handler = asyncio.create_task(server.handle(client))
        deadline = time.monotonic() + 10
        try:
            while client not in server.clients:
                assert time.monotonic() < deadline
                await asyncio.sleep(0.005)
            async def preview(value):
                await server._dispatch(client, json.dumps(dict(cmd="preview", verb="fillet",
                                        args=dict(radius=value), preview_id=value, ack_id=value)))
            await preview(1)
            while not marker.exists():
                assert time.monotonic() < deadline
                await asyncio.sleep(0.005)
            if superseded:
                await preview(2)
            disconnected.set()
            await asyncio.wait_for(handler, timeout=10)
            while worker._process is not None:
                assert time.monotonic() < deadline
                await asyncio.sleep(0.005)
            async with asyncio.timeout(10):
                while server._pending_pushes:
                    await asyncio.gather(*list(server._pending_pushes))
                    await asyncio.sleep(0)  # 完了済みでも、集合から除くコールバックを実行させる。
            assert all(job._cancel.is_set() for job in jobs)
            assert not server._preview_jobs and not server._preview_futures
            assert client not in server._acks
            assert not client.of_type("preview") and not client.of_type("error")
        finally:
            disconnected.set()
            await handler
            if server._preview_worker is not None:
                server._preview_worker.shutdown(wait=True)
    asyncio.run(scenario())
    assert worker.starts == 1
    assert not marker.with_suffix(".completed").exists()


def test_websocket_supersedes_only_queued_previews_on_the_same_connection(session, delayed, monkeypatch):
    from duocad.session import PreviewJob
    from session_support import FakeConnection
    worker, marker = delayed
    jobs = []
    def make_job(_verb, args):
        tag = str(args["radius"])
        job = PreviewJob(lambda: (_call("blend_edges", None if tag == "1" else 0, tag)
                                 and dict(verb="fillet", bodies=[])), lambda: None)
        jobs.append(job)
        return job
    monkeypatch.setattr(session, "preview_job", make_job)
    async def script(server, client):
        other = FakeConnection()
        server.clients.add(other)
        async def preview(connection, value):
            await server._dispatch(connection, json.dumps(dict(cmd="preview", verb="fillet",
                                    args=dict(radius=value), preview_id=value, ack_id=value)))
        await preview(client, 1)
        deadline = time.monotonic() + 10
        while not marker.exists():
            assert time.monotonic() < deadline
            await asyncio.sleep(0.005)
        await preview(other, 2)
        await preview(client, 3)
        await preview(client, 4)
        assert not client.of_type("done")  # 実行中の古い要求が済む前に後続を完了扱いにしない。
        _release(marker, "1")
        return other, server
    (other, server), client = _run(session, script)
    assert [job._cancel.is_set() for job in jobs] == [False, False, True, False]
    assert worker.starts == 1
    assert marker.with_suffix(".completed").read_text().splitlines() == ["1", "2", "4"]
    assert [item["preview_id"] for item in client.of_type("preview")] == [4]
    assert [item["preview_id"] for item in other.of_type("preview")] == [2]
    assert client.of_type("done")[-1]["ack_id"] == 4
    assert not client.of_type("error") and not other.of_type("error")
    assert not server._preview_jobs and not server._preview_futures


def test_superseded_running_job_can_finish_after_an_inline_preview(session, monkeypatch):
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
            assert release.wait(timeout=10)
        else:
            # 同期のプレビュー中は Future の done callback がまだ走れない。
            assert not old_job._cancel.is_set()
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
            deadline = time.monotonic() + 10
            while not entered.is_set():
                assert time.monotonic() < deadline
                await asyncio.sleep(0.005)
            monkeypatch.setattr(session, "preview_job", lambda *_args, **_kwargs: None)
            await server._dispatch(client, json.dumps(dict(cmd="preview", verb="fillet", args=dict(radius=2), preview_id=2)))
            await asyncio.wait_for(asyncio.gather(*list(server._pending_pushes)), timeout=10)
            assert [item["preview_id"] for item in client.of_type("preview")] == [2]
            assert not client.of_type("error")
        finally:
            release.set()
            server._preview_worker.shutdown(wait=True)
    asyncio.run(scenario())
