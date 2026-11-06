"""予備の昇格・補充と、起動中を含む終了時の子の寿命を検査する。"""
from concurrent.futures import ThreadPoolExecutor
import sys
import threading
import time

import pytest
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

from duocad import kernel_worker as kw, worker_process


CHILD = r'''
import json, os, sys, time
from pathlib import Path
root = Path(sys.argv[1])
for line in sys.stdin:
    m = json.loads(line)
    if m.get("op") == "ping":
        (root / "ping").write_text(str(os.getpid()))
        while (root / "hold-ping").exists():
            time.sleep(.005)
        result = {"pid": os.getpid()}
    else:
        args = m["arguments"]
        (root / args["tag"]).write_text(str(os.getpid()))
        if args.get("crash"):
            os._exit(17)
        while args.get("hold") and not (root / "release").exists():
            time.sleep(.005)
        result = {"shape": m["shape"], "pid": os.getpid()}
    print(json.dumps({"seq": m["seq"], "ok": True, "result": result}), flush=True)
'''


def wait(predicate):
    deadline = time.monotonic() + 15
    while not predicate():
        assert time.monotonic() < deadline, "子の状態が検査の待ち合わせに達しない"
        time.sleep(.005)


@pytest.fixture
def workers(monkeypatch, tmp_path):
    kw.stop()
    wait(lambda: kw._ACTIVE is None)
    monkeypatch.setattr(kw, "_CHILD_COMMAND", [sys.executable, "-c", CHILD, str(tmp_path)])
    processes = []
    live_counts = []
    warmers = []
    spawn = kw._KernelWorker._spawn
    warm = kw._warm_spare

    def observe_spawn(self):
        with kw._CONDITION:
            spawn(self)
            processes.append(self._process)
            live_counts.append(sum(p.poll() is None for p in processes))

    def observe_warm(worker):
        done = threading.Event()
        warmers.append(done)
        try:
            warm(worker)
        finally:
            done.set()

    monkeypatch.setattr(kw._KernelWorker, "_spawn", observe_spawn)
    monkeypatch.setattr(kw, "_warm_spare", observe_warm)
    yield tmp_path, processes
    kw.stop()
    wait(lambda: kw._ACTIVE is None and all(done.is_set() for done in warmers))
    assert all(p.poll() is not None for p in processes)
    assert all(count <= 2 for count in live_counts)


def call(tag, *, hold=False, cancel=None, timeout=15, crash=False):
    def run():
        return kw.run("fillet", BRepPrimAPI_MakeBox(20, 20, 20).Shape(),
                      {"tag": tag, "hold": hold, "crash": crash}, op="blend_edges", timeout=timeout)
    if cancel is None:
        return run()
    with kw.preview_context(cancel):
        return run()


def ready():
    kw.warm_up_in_background()
    wait(lambda: kw._ACTIVE is None and kw._SPARE is not None)
    return kw._SPARE.request({"op": "ping"}, 10)["pid"]


def preempt(root, tag="preview"):
    with ThreadPoolExecutor(1) as pool:
        old = pool.submit(call, tag, hold=True, cancel=threading.Event())
        wait(lambda: (root / tag).exists())
        result = call("commit-" + tag)
        with pytest.raises(ValueError, match="取り消した"):
            old.result(timeout=15)
    return result


def test_cancel_promotes_the_ready_pid_and_replenishes_at_most_two_children(workers):
    root, processes = workers
    spare_pid = ready()
    for i in range(3):
        old_process = kw._WORKER._process
        assert preempt(root, f"preview-{i}")["pid"] == spare_pid
        assert old_process.poll() is not None
        wait(lambda: kw._SPARE is not None)
        spare_pid = kw._SPARE.request({"op": "ping"}, 10)["pid"]
        assert sum(p.poll() is None for p in processes) == 2
    assert len(processes) == 5


def test_without_warm_up_cancellation_keeps_the_single_worker_behavior(workers):
    root, processes = workers
    first = call("first")
    worker = kw._WORKER
    assert preempt(root)["pid"] != first["pid"]
    assert kw._WORKER is worker and worker.starts == 2
    assert kw._SPARE is None and kw._STARTING_SPARE is None
    assert sum(p.poll() is None for p in processes) == 1


def test_cancellation_before_the_spare_is_ready_falls_back_without_a_third_child(workers):
    root, processes = workers
    first = call("first")
    worker = kw._WORKER
    (root / "hold-ping").touch()
    kw.warm_up_in_background()
    wait(lambda: (root / "ping").exists())
    assert kw._SPARE is None and kw._STARTING_SPARE is not None
    assert preempt(root)["pid"] != first["pid"]
    assert kw._WORKER is worker and worker.starts == 2
    (root / "hold-ping").unlink()
    wait(lambda: kw._SPARE is not None)
    assert sum(p.poll() is None for p in processes) == 2


def test_another_cancellation_during_replenishment_falls_back(workers):
    root, processes = workers
    spare_pid = ready()
    (root / "ping").unlink()
    (root / "hold-ping").touch()
    assert preempt(root, "first-preview")["pid"] == spare_pid
    promoted = kw._WORKER
    wait(lambda: (root / "ping").exists())
    assert kw._SPARE is None and kw._STARTING_SPARE is not None
    assert preempt(root, "second-preview")["pid"] != spare_pid
    assert kw._WORKER is promoted and promoted.starts == 2
    (root / "hold-ping").unlink()
    wait(lambda: kw._SPARE is not None)
    assert sum(p.poll() is None for p in processes) == 2


@pytest.mark.parametrize("failure", ["timeout", "crash", "idle-death"])
def test_timeout_and_child_death_also_promote_the_ready_spare(workers, monkeypatch, failure):
    root, _ = workers
    spare_pid = ready()
    if failure == "idle-death":
        kw._WORKER._process.kill()
        kw._WORKER._process.wait(timeout=10)
    elif failure == "crash":
        with pytest.raises(ValueError, match="終了"):
            call("crash", crash=True)
    else:
        expired = threading.Event()
        clock = type("Clock", (), {"monotonic": staticmethod(lambda: time.monotonic() + (30 if expired.is_set() else 0))})
        monkeypatch.setattr(kw, "time", clock)
        monkeypatch.setattr(worker_process, "time", clock)
        with ThreadPoolExecutor(1) as pool:
            running = pool.submit(call, "timeout", hold=True)
            wait(lambda: (root / "timeout").exists())
            expired.set()
            with pytest.raises(kw.TimedOut, match="返事が無かった"):
                running.result(timeout=15)
        expired.clear()
    assert call("next")["pid"] == spare_pid


def test_a_spare_killed_externally_does_not_fail_the_next_request(workers):
    root, _ = workers
    ready()
    kw._SPARE._process.kill()
    kw._SPARE._process.wait(timeout=10)
    assert preempt(root)["pid"]
    wait(lambda: kw._SPARE is not None)
    assert call("after")["pid"]


def test_failed_spare_startup_does_not_fail_requests(workers, monkeypatch):
    root, _ = workers
    call("first")
    spawn = kw._KernelWorker._spawn
    failed = threading.Event()

    def fail_spare(self):
        if self is kw._STARTING_SPARE:
            failed.set()
            raise worker_process.WorkerFailure("injected startup failure")
        spawn(self)

    monkeypatch.setattr(kw._KernelWorker, "_spawn", fail_spare)
    kw.warm_up_in_background()
    wait(lambda: failed.is_set() and kw._STARTING_SPARE is None)
    assert preempt(root)["pid"]
    assert call("after")["pid"]


def test_failed_warm_up_threads_do_not_block_requests(workers, monkeypatch):
    start = threading.Thread.start

    def fail_warmer(self):
        if self.name.startswith("kernel-worker-"):
            raise RuntimeError("injected thread startup failure")
        start(self)

    monkeypatch.setattr(threading.Thread, "start", fail_warmer)
    kw.warm_up_in_background()
    assert kw._ACTIVE is None
    assert call("next")["pid"]
    assert kw._STARTING_SPARE is None


def test_stop_closes_primary_and_ready_spare(workers):
    _, processes = workers
    ready()
    assert sum(p.poll() is None for p in processes) == 2
    kw.stop()
    assert all(p.poll() is not None for p in processes)
    assert kw._WORKER is kw._SPARE is kw._STARTING_SPARE is None
    call("restart")
    assert kw._SPARE is kw._STARTING_SPARE is None
    assert sum(p.poll() is None for p in processes) == 1


@pytest.mark.parametrize("stage", ["encoding", "computing"])
def test_stop_during_a_request_does_not_restart_any_child(workers, monkeypatch, stage):
    root, processes = workers
    ready()
    entered, release = threading.Event(), threading.Event()
    encode = kw._encode

    def paused(shape):
        entered.set()
        assert release.wait(timeout=15)
        return encode(shape)

    if stage == "encoding":
        monkeypatch.setattr(kw, "_encode", paused)
    with ThreadPoolExecutor(1) as pool:
        running = pool.submit(call, "running", hold=True)
        try:
            if stage == "encoding":
                assert entered.wait(timeout=15)
            else:
                wait(lambda: (root / "running").exists())
            kw.stop()
            assert all(p.poll() is not None for p in processes)
        finally:
            release.set()
        with pytest.raises(ValueError):
            running.result(timeout=15)
    assert kw._WORKER is kw._SPARE is kw._STARTING_SPARE is None


@pytest.mark.parametrize("stage", ["before-spawn", "during-ping", "after-ping"])
def test_stop_during_spare_startup_leaves_no_child(workers, monkeypatch, stage):
    root, processes = workers
    call("first")
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    request = kw._KernelWorker.request

    def paused(self, message, timeout, **kwargs):
        if self is not kw._STARTING_SPARE:
            return request(self, message, timeout, **kwargs)
        try:
            if stage == "after-ping":
                result = request(self, message, timeout, **kwargs)
            entered.set()
            assert release.wait(timeout=15)
            return result if stage == "after-ping" else request(self, message, timeout, **kwargs)
        finally:
            done.set()

    if stage == "during-ping":
        (root / "hold-ping").touch()
    else:
        monkeypatch.setattr(kw._KernelWorker, "request", paused)
    try:
        kw.warm_up_in_background()
        if stage == "during-ping":
            wait(lambda: (root / "ping").exists())
        else:
            assert entered.wait(timeout=15)
        kw.stop()
        assert all(p.poll() is not None for p in processes)
    finally:
        release.set()
    if stage != "during-ping":
        assert done.wait(timeout=15)
    assert kw._SPARE is kw._STARTING_SPARE is None


def test_stop_during_primary_warm_up_cannot_publish_a_child(workers, monkeypatch):
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    request = kw._KernelWorker.request

    def paused(self, *args, **kwargs):
        entered.set()
        try:
            assert release.wait(timeout=15)
            return request(self, *args, **kwargs)
        finally:
            done.set()

    monkeypatch.setattr(kw._KernelWorker, "request", paused)
    try:
        kw.warm_up_in_background()
        assert entered.wait(timeout=15)
        kw.stop()
    finally:
        release.set()
    assert done.wait(timeout=15)
    wait(lambda: kw._ACTIVE is None)
    assert kw._WORKER is kw._SPARE is kw._STARTING_SPARE is None


def test_enabling_spares_during_a_running_request_starts_them_after_it_finishes(workers):
    root, _ = workers
    with ThreadPoolExecutor(1) as pool:
        running = pool.submit(call, "running", hold=True)
        wait(lambda: (root / "running").exists())
        kw.warm_up_in_background()
        assert kw._SPARE is kw._STARTING_SPARE is None
        (root / "release").touch()
        running.result(timeout=15)
    wait(lambda: kw._SPARE is not None)
