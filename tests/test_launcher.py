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
    # 本物のリポジトリへ `git fetch` を打たない。更新の起動は専用のテストで確かめる。
    monkeypatch.setattr(launcher, "update_before_start", lambda *a: False)
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


@pytest.fixture
def stable_win(monkeypatch):
    monkeypatch.setattr(launcher.sys, "platform", "win32")
    spawned = []
    monkeypatch.setattr(launcher, "spawn_update_script", lambda **kw: spawned.append(kw))
    return spawned


def _args(*argv):
    parser_args = ["--data-dir", "D", *argv]
    captured = {}
    real = launcher.argparse.ArgumentParser.parse_args

    def capture(self, args=None, namespace=None):
        captured["ns"] = real(self, args, namespace)
        raise SystemExit

    launcher.argparse.ArgumentParser.parse_args = capture
    try:
        with pytest.raises(SystemExit):
            launcher.main(parser_args)
    finally:
        launcher.argparse.ArgumentParser.parse_args = real
    return captured["ns"]


def test_a_fresh_start_applies_a_pending_update_instead_of_starting_the_server(tmp_path, monkeypatch, stable_win):
    """落ちたら: 起動口を押しても main の新しい版が入らず、古いまま起動する(自動更新が働かない)。"""
    monkeypatch.setattr(launcher, "probe_updates", lambda *a, **k: {"state": "available", "behind": 1, "commits": ["x"]})
    args = _args("--no-window")
    assert launcher.update_before_start(args, tmp_path) is True
    (call,) = stable_win
    assert call["auto"] is True and call["branch"] == "main" and call["log_dir"] == tmp_path / "logs"
    assert call["relaunch"] == {"channel": "stable", "port": 8775, "http_port": 8776, "data_dir": tmp_path}


@pytest.mark.parametrize("state", ["current", "blocked", "unknown"])
def test_no_update_when_nothing_can_be_taken_in(tmp_path, monkeypatch, stable_win, state):
    """落ちたら: オフライン・未コミットの変更・分岐のときに更新が走る、または起動が止まる。"""
    monkeypatch.setattr(launcher, "probe_updates", lambda *a, **k: {"state": state})
    assert launcher.update_before_start(_args(), tmp_path) is False
    assert stable_win == []


def test_update_check_is_skipped_for_dev_and_for_the_relaunch_after_an_update(tmp_path, monkeypatch, stable_win):
    """落ちたら: 開発版の起動で開発用チェックアウトが更新される、または更新が失敗した後の立て直しが更新を起こして回り続ける。"""
    def boom(*a, **k):
        raise AssertionError("確認してはいけない")

    monkeypatch.setattr(launcher, "probe_updates", boom)
    assert launcher.update_before_start(_args("--channel", "dev"), tmp_path) is False
    assert launcher.update_before_start(_args("--no-update-check"), tmp_path) is False
    assert stable_win == []


def test_failing_to_start_the_update_script_still_starts_the_server(tmp_path, monkeypatch):
    """落ちたら: 更新スクリプトを起こせないだけで、安定版が起動しなくなる。"""
    monkeypatch.setattr(launcher.sys, "platform", "win32")
    monkeypatch.setattr(launcher, "probe_updates", lambda *a, **k: {"state": "available"})

    def fail(**kw):
        raise OSError("コピーできない")

    monkeypatch.setattr(launcher, "spawn_update_script", fail)
    assert launcher.update_before_start(_args(), tmp_path) is False
    monkeypatch.setattr(launcher, "spawn_update_script", lambda **kw: "update.ps1 が無い")
    assert launcher.update_before_start(_args(), tmp_path) is False


def test_launch_ends_without_starting_the_server_once_the_update_script_is_up(tmp_path, monkeypatch):
    """落ちたら: 更新スクリプトが走っているのに起動口がサーバも立て、更新とぶつかる。"""
    monkeypatch.setattr(launcher, "_single_launch", lambda *a, **k: contextlib.nullcontext(True))
    monkeypatch.setattr(launcher, "server_config", lambda _port: None)
    monkeypatch.setattr(launcher, "update_before_start", lambda *a: True)

    def no_spawn(*a):
        raise AssertionError("サーバを立ててはいけない")

    monkeypatch.setattr(launcher, "spawn_server", no_spawn)
    assert launcher.main(["--data-dir", str(tmp_path)]) == 0


def test_a_running_server_is_not_checked_for_updates(tmp_path, monkeypatch, quiet):
    """落ちたら: 立っているサーバ(作業中の文書がある)の上で、起動口を押しただけで更新・再起動が走る。"""
    monkeypatch.setattr(launcher, "server_config", lambda _port: {"channel": "stable"})
    monkeypatch.setattr(launcher, "update_before_start", lambda *a: (_ for _ in ()).throw(AssertionError))
    monkeypatch.setattr(launcher, "find_app_window", lambda: 3)
    monkeypatch.setattr(launcher, "bring_to_front", lambda hwnd: None)
    assert launcher.main(["--data-dir", str(tmp_path)]) == 0
