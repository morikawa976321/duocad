"""自動保存の要求順序・省略・失敗回収・流し切りを、保存先への書き込みで確かめる。"""
from __future__ import annotations

import concurrent.futures
import json
import threading

import pytest

from duocad.autosave import Autosave
from duocad.document_store import DocumentStore
from duocad.oplog import OpLog


@pytest.fixture
def autosaver(tmp_path):
    saver = Autosave(DocumentStore(tmp_path / "models", tmp_path / "exports"))
    yield saver
    saver.drain()


def _log(count: int) -> OpLog:
    log = OpLog()
    for _ in range(count):
        log.append("sketch_create", {"base_plane": "XY"})
    return log


class HeldWrite:
    def __init__(self, store, monkeypatch, *, fail=False):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.writes = []
        self.real = store.write_document_file
        self.fail = fail
        monkeypatch.setattr(store, "write_document_file", self.write)

    def write(self, target, payload):
        count = len(json.loads(b"".join(payload))["operations"])
        self.writes.append((target.name, count))
        if len(self.writes) == 1:
            self.entered.set()
            assert self.release.wait(10), "止めた書き込みが解放されなかった"
            if self.fail:
                raise OSError("disk full")
        self.real(target, payload)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.release.set()


def test_writes_follow_request_order(autosaver, monkeypatch):
    with HeldWrite(autosaver.store, monkeypatch) as held:
        with autosaver.background():
            autosaver.save_current("current.json", _log(1), None)
            assert held.entered.wait(10)
            first = autosaver.pending_write
            autosaver.save_current("current.json", _log(2), None)
            last = autosaver.pending_write
        assert first is not last and not last.done()
    autosaver.drain()
    assert held.writes == [("current.json", 1), ("current.json", 2)]
    assert first.done() and last.done() and autosaver.pending_write is None


def test_a_newer_request_skips_an_older_waiting_request(autosaver, monkeypatch):
    with HeldWrite(autosaver.store, monkeypatch) as held:
        with autosaver.background():
            autosaver.save_current("current.json", _log(1), None)
            assert held.entered.wait(10)
            autosaver.save_current("current.json", _log(2), None)
            skipped = autosaver.pending_write
            autosaver.save_current("current.json", _log(3), None)
    autosaver.drain()
    assert skipped.result() is None
    assert held.writes == [("current.json", 1), ("current.json", 3)]
    payload = autosaver.store.read_document_file(autosaver.store.models_dir / "current.json")
    assert len(payload["operations"]) == 3


def test_the_payload_and_destination_are_fixed_before_returning(autosaver, monkeypatch):
    log = _log(1)
    with HeldWrite(autosaver.store, monkeypatch) as held:
        with autosaver.background():
            autosaver.save_current("first.json", _log(0), None)
            assert held.entered.wait(10)
            autosaver.save_current("second.json", log, 123.5)
        log.append("sketch_create", {"base_plane": "YZ"})
    autosaver.drain()
    payload = autosaver.store.read_document_file(autosaver.store.models_dir / "second.json")
    assert len(payload["operations"]) == 1 and payload["created"] == 123.5
    assert held.writes == [("first.json", 0), ("second.json", 1)]


def test_failure_is_collected_once_and_a_later_write_recovers(autosaver, monkeypatch):
    with HeldWrite(autosaver.store, monkeypatch, fail=True) as held:
        with autosaver.background():
            autosaver.save_current("current.json", _log(1), None)
        assert held.entered.wait(10)
        assert autosaver.settle() is True and autosaver.saved
        future = autosaver.pending_write
        held.release.set()
        concurrent.futures.wait([future])
    assert autosaver.saved and autosaver.store.warnings == []
    assert autosaver.settle() is False and not autosaver.saved
    assert autosaver.pending_write is None
    assert autosaver.store.warnings == ["自動保存できなかった: OSError: disk full"]
    assert autosaver.settle() is True and not autosaver.saved
    with autosaver.background():
        autosaver.save_current("current.json", _log(2), None)
    autosaver.drain()
    assert autosaver.saved and len(autosaver.store.warnings) == 1


@pytest.mark.parametrize("fail", [False, True])
def test_drain_waits_and_collects_the_last_result(autosaver, monkeypatch, fail):
    started = threading.Event()
    finished = threading.Event()

    def drain():
        started.set()
        autosaver.drain()
        finished.set()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as caller:
        with HeldWrite(autosaver.store, monkeypatch, fail=fail) as held:
            with autosaver.background():
                autosaver.save_current("current.json", _log(1), None)
            assert held.entered.wait(10)
            result = caller.submit(drain)
            assert started.wait(10) and not finished.is_set()
        result.result(timeout=10)
    assert finished.is_set() and autosaver.pending_write is None
    assert autosaver.saved is not fail
    assert bool(autosaver.store.warnings) is fail


def test_synchronous_save_follows_the_queued_write(session, monkeypatch):
    """製品の同期保存が、それ以前の背景保存を追い越さない。"""
    session.sketch_create("XY")
    started = threading.Event()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as caller:
        with HeldWrite(session.store, monkeypatch) as held:
            with session.background_autosave():
                session.sketch_create("YZ")
            assert held.entered.wait(10)

            def release():
                assert started.wait(10)
                held.release.set()

            released = caller.submit(release)
            started.set()
            assert session.autosave()
        released.result(timeout=10)
    assert held.writes == [(session.current_name, 2), (session.current_name, 2)]
    assert session.pending_write is None and session.saved


def test_nested_background_context_restores_synchronous_saving(session, monkeypatch):
    """背景保存の入れ子を例外で抜けても、Session の通常の保存経路へ戻る。"""
    session.sketch_create("XY")
    with HeldWrite(session.store, monkeypatch) as held:
        with session.background_autosave():
            with pytest.raises(ValueError), session.background_autosave():
                raise ValueError("中断")
            session.sketch_create("YZ")
            assert held.entered.wait(10)
            assert session.pending_write is not None
    assert session.autosave()
    assert session.pending_write is None
    assert held.writes == [(session.current_name, 2), (session.current_name, 2)]
