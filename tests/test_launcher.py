"""起動口(duocad.launcher): 子の即死を待たずに知らせる・窓を二重に開かない・ログの文字コード。"""
from __future__ import annotations

import contextlib
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from duocad import launcher


class _Child:
    def __init__(self, code):
        self.code = code

    def poll(self):
        return self.code


def _serve(body: bytes):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def quiet(monkeypatch):
    """ミューテックスと待ちを外し、ダイアログを記録する。"""
    alerts = []
    monkeypatch.setattr(launcher, "_alert", alerts.append)
    monkeypatch.setattr(launcher, "_single_launch", lambda *a, **k: contextlib.nullcontext(True))
    monkeypatch.setattr(launcher.time, "sleep", lambda _s: None)
    return alerts


def test_server_config_ignores_a_non_object_reply():
    server = _serve(b"[]")
    try:
        assert launcher.server_config(server.server_port) is None
    finally:
        server.shutdown()
    server = _serve(json.dumps({"channel": "stable"}).encode())
    try:
        assert launcher.server_config(server.server_port) == {"channel": "stable"}
    finally:
        server.shutdown()


def test_child_dying_at_once_is_reported_without_the_two_minute_wait(tmp_path, monkeypatch, quiet):
    log = tmp_path / "logs" / "server.log"
    log.parent.mkdir()
    log.write_text("==== start\nOSError: ポートが使用中\n", encoding="utf-8")
    monkeypatch.setattr(launcher, "server_config", lambda _port: None)
    monkeypatch.setattr(launcher, "spawn_server", lambda *a: _Child(1))
    started = time.monotonic()
    code = launcher.main(["--data-dir", str(tmp_path), "--port", "1", "--http-port", "2"])
    assert code == 1
    assert time.monotonic() - started < 5
    assert len(quiet) == 1
    assert "終了コード 1" in quiet[0] and "ポートが使用中" in quiet[0] and str(log) in quiet[0]


def test_a_second_child_that_died_because_the_first_server_is_up_counts_as_started(tmp_path, monkeypatch, quiet):
    answers = iter([None, None, {"channel": "stable"}])
    monkeypatch.setattr(launcher, "server_config", lambda _port: next(answers, {"channel": "stable"}))
    monkeypatch.setattr(launcher, "spawn_server", lambda *a: _Child(1))
    monkeypatch.setattr(launcher, "find_app_window", lambda: 7)
    brought = []
    monkeypatch.setattr(launcher, "bring_to_front", brought.append)
    assert launcher.main(["--data-dir", str(tmp_path)]) == 0
    assert quiet == [] and brought == [7]


def test_spawn_server_returns_the_child_and_logs_in_utf8(tmp_path, monkeypatch):
    captured = {}
    sentinel = object()

    def fake_popen(command, **kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)
    assert launcher.spawn_server("stable", 1, 2, tmp_path) is sentinel
    assert captured["env"]["PYTHONIOENCODING"] == "utf-8"


def test_launcher_waits_for_its_own_window_so_a_second_launch_finds_it(tmp_path, monkeypatch, quiet):
    monkeypatch.setattr(launcher, "server_config", lambda _port: {"channel": "stable"})
    seen = iter([None, None, None, 5])
    monkeypatch.setattr(launcher, "find_app_window", lambda: next(seen))
    opened = []
    monkeypatch.setattr(launcher, "open_app_window", lambda url: opened.append(url) or True)
    assert launcher.main(["--data-dir", str(tmp_path)]) == 0
    assert len(opened) == 1
    assert next(seen, "drained") == "drained"


@pytest.mark.skipif(sys.platform != "win32", reason="名前付きミューテックスは Windows の機能")
def test_second_launch_waits_for_the_first_then_goes_on_after_the_timeout():
    holding, release = threading.Event(), threading.Event()

    def first():
        with launcher._single_launch(54321, 5.0) as acquired:
            assert acquired
            holding.set()
            release.wait(5)

    thread = threading.Thread(target=first)
    thread.start()
    holding.wait(5)
    try:
        started = time.monotonic()
        with launcher._single_launch(54321, 0.3) as acquired:
            assert not acquired
        assert 0.25 < time.monotonic() - started < 3
    finally:
        release.set()
        thread.join()
    with launcher._single_launch(54321, 1.0) as acquired:
        assert acquired
