"""開発版/安定版の区別(`--channel`)と、安定版の更新の口(`/api/update`)。docs/STABLE.md"""
from __future__ import annotations

import http.client
import json
import sys
import socket
import subprocess
from pathlib import Path

import pytest

from duocad import channel
from duocad.channel import StableUpdater, describe
from duocad.mcp_server import parse_args
from duocad.server import WebHandler, start_http_server


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _request(port: int, method: str, path: str, headers: dict = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
    try:
        conn.request(method, path, body=b"" if method == "POST" else None, headers=headers or {})
        response = conn.getresponse()
        raw = response.read()
        return response.status, (json.loads(raw) if raw and response.status < 300 else None)
    finally:
        conn.close()


class _FakeUpdater:
    def __init__(self) -> None:
        self.started = 0
        self.modes: list[str] = []

    def status(self, force: bool = False) -> dict:
        return {"state": "available", "behind": 2, "commits": ["a", "b"], "force": force}

    def start(self, mode: str = "update"):
        self.started += 1
        self.modes.append(mode)
        return 202, {"started": True}


@pytest.fixture
def serve(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>duocad</title>", encoding="utf-8")
    servers = []

    def _start(channel: str, updater=None) -> int:
        port = _free_port()
        servers.append(
            start_http_server(
                dist, port, "ws://127.0.0.1:1", config=describe(channel, updater), updater=updater
            )
        )
        return port

    yield _start
    for httpd in servers:
        httpd.shutdown()
    WebHandler.updater = None
    WebHandler.page_origins = frozenset()


def test_config_names_the_channel(serve):
    """落ちたら: 画面が開発版か安定版かを知る手段が無くなり、「開発版」の札が出ない。"""
    port = serve("dev")
    status, config = _request(port, "GET", "/duocad-config.json")
    assert status == 200
    assert config["channel"] == "dev"
    assert config["update_supported"] is False
    assert config["ws_url"] == "ws://127.0.0.1:1"
    # 立て直しの終わりを画面が知る印。GPU の部品だけ入れる立て直しはコミットを変えないので、版とは別に要る。
    assert isinstance(config["boot"], str) and config["boot"]


def test_update_endpoint_is_absent_on_dev(serve):
    """落ちたら: 開発版にも更新の口が開いており、開発用のチェックアウトを書き換えうる。"""
    port = serve("dev")
    assert _request(port, "GET", "/api/update")[0] == 404
    assert _request(port, "POST", "/api/update", {"Origin": f"http://127.0.0.1:{port}"})[0] == 404


def test_update_accepts_only_duocads_own_page(serve):
    """落ちたら: よそのページ(またはOrigin無しのクライアント)が安定版を再起動させられる。"""
    updater = _FakeUpdater()
    port = serve("stable", updater)

    status, body = _request(port, "GET", "/api/update?force=1")
    assert status == 200 and body["behind"] == 2 and body["force"] is True

    assert _request(port, "POST", "/api/update")[0] == 403
    assert _request(port, "POST", "/api/update", {"Origin": "http://evil.example"})[0] == 403
    assert _request(port, "POST", "/api/update", {"Origin": "null"})[0] == 403
    assert updater.started == 0

    status, body = _request(port, "POST", "/api/update", {"Origin": f"http://127.0.0.1:{port}"})
    assert status == 202 and body == {"started": True}
    assert updater.started == 1

    assert _request(port, "POST", "/api/update?mode=gpu", {"Origin": "http://evil.example"})[0] == 403
    assert _request(port, "POST", "/api/update?mode=gpu", {"Origin": f"http://127.0.0.1:{port}"})[0] == 202
    assert updater.modes == ["update", "gpu"]


def test_data_dir_moves_models_and_exports_out_of_the_repo(tmp_path):
    """落ちたら: 安定版の制作データがリポジトリ直下(git clean で消える場所)に書かれる。"""
    args = parse_args(["--data-dir", str(tmp_path), "--channel", "stable", "--no-stdio"])
    assert args.models_dir == tmp_path.resolve() / "models"
    assert args.exports_dir == tmp_path.resolve() / "export"
    assert args.channel == "stable" and args.no_stdio

    explicit = parse_args(["--data-dir", str(tmp_path), "--models-dir", str(tmp_path / "m")])
    assert explicit.models_dir == tmp_path / "m"


def test_channel_defaults_to_dev():
    """落ちたら: --channel を付け忘れた開発版が札を出さず、安定版と取り違えられる。"""
    assert parse_args([]).channel == "dev"


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return done.stdout.strip()


def test_updater_status_counts_commits_behind_origin(tmp_path):
    """落ちたら: mainに新しいコミットがあっても更新ボタンが出ない(または件数・一覧が嘘になる)。"""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _git(origin, "config", "user.email", "t@example.com")
    _git(origin, "config", "user.name", "t")
    (origin / "a.txt").write_text("1", encoding="utf-8")
    _git(origin, "add", "a.txt")
    _git(origin, "commit", "-q", "-m", "first")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))

    updater = StableUpdater(
        log_dir=tmp_path / "logs",
        relaunch={},
        update_blocker=lambda: None,
        on_started=lambda: None,
        repo_root=clone,
    )
    assert updater.status(force=True)["state"] == "current"

    (origin / "a.txt").write_text("2", encoding="utf-8")
    _git(origin, "commit", "-q", "-am", "second")
    status = updater.status(force=True)
    assert status["state"] == "available"
    assert status["behind"] == 1 and status["commits"] == ["second"]
    assert status["last_result"] is None

    (clone / "a.txt").write_text("local edit", encoding="utf-8")
    assert updater.status(force=True)["state"] == "blocked"


def test_updater_refuses_outside_the_update_branch(tmp_path):
    """落ちたら: 開発用のチェックアウト(develop)で更新ボタンが押せ、作業ツリーでビルドと再起動が走る。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "develop")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("1", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "first")
    started = []
    updater = StableUpdater(
        log_dir=tmp_path / "logs",
        relaunch={},
        update_blocker=lambda: None,
        on_started=lambda: started.append(1),
        repo_root=repo,
    )
    assert updater.status(force=True)["state"] == "blocked"
    status, _body = updater.start()
    assert status == 409 and not started


def test_updater_treats_a_clone_ahead_of_main_as_current(tmp_path):
    """落ちたら: developのコミットから導入した安定版に「更新できない」の札が出続ける。"""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _git(origin, "config", "user.email", "t@example.com")
    _git(origin, "config", "user.name", "t")
    (origin / "a.txt").write_text("1", encoding="utf-8")
    _git(origin, "add", "a.txt")
    _git(origin, "commit", "-q", "-m", "first")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    _git(clone, "config", "user.email", "t@example.com")
    _git(clone, "config", "user.name", "t")
    (clone / "b.txt").write_text("ahead", encoding="utf-8")
    _git(clone, "add", "b.txt")
    _git(clone, "commit", "-q", "-m", "develop commit")

    updater = StableUpdater(
        log_dir=tmp_path / "logs", relaunch={}, update_blocker=lambda: None, on_started=lambda: None,
        repo_root=clone,
    )
    assert updater.status(force=True)["state"] == "current"

    # mainがPRを取り込んだマージコミットだけを持っている(developには無い)状態は「最新」。
    _git(origin, "checkout", "-q", "-b", "feature")
    (origin / "c.txt").write_text("feature", encoding="utf-8")
    _git(origin, "add", "c.txt")
    _git(origin, "commit", "-q", "-m", "feature")
    _git(origin, "checkout", "-q", "main")
    _git(origin, "merge", "-q", "--no-ff", "-m", "merge PR", "feature")
    _git(clone, "fetch", "-q", "origin", "feature")
    _git(clone, "merge", "-q", "--no-ff", "-m", "develop takes feature", "FETCH_HEAD")
    assert updater.status(force=True)["state"] == "current"

    (origin / "a.txt").write_text("2", encoding="utf-8")
    _git(origin, "commit", "-q", "-am", "diverging")
    assert updater.status(force=True)["state"] == "blocked"


def test_update_status_rejects_cross_site_embeds_and_foreign_hosts(serve):
    """落ちたら: よそのページの埋め込みやDNSリバインディングで、安定版に git fetch を連打させられる。"""
    port = serve("stable", _FakeUpdater())
    assert _request(port, "GET", "/api/update", {"Sec-Fetch-Site": "cross-site"})[0] == 403
    assert _request(port, "GET", "/api/update", {"Host": "evil.example:80"})[0] == 403
    assert _request(port, "GET", "/api/update", {"Sec-Fetch-Site": "same-origin"})[0] == 200


def test_update_is_refused_while_the_server_cannot_stop_cleanly(tmp_path):
    """落ちたら: 形状最適化・AIの実行中や状態不明のまま更新が始まり、強制終了で結果が消える。"""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _git(origin, "config", "user.email", "t@example.com")
    _git(origin, "config", "user.name", "t")
    (origin / "a.txt").write_text("1", encoding="utf-8")
    _git(origin, "add", "a.txt")
    _git(origin, "commit", "-q", "-m", "first")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    (origin / "a.txt").write_text("2", encoding="utf-8")
    _git(origin, "commit", "-q", "-am", "second")

    started = []

    def make(blocker):
        return StableUpdater(
            log_dir=tmp_path / "logs", relaunch={}, update_blocker=blocker,
            on_started=lambda: started.append(1), repo_root=clone,
        )

    status, body = make(lambda: "形状最適化が走っている").start()
    assert status == 409 and "形状最適化" in body["error"]

    def broken():
        raise TimeoutError

    assert make(broken).start()[0] == 503
    assert not started


def test_installing_the_gpu_parts_does_not_need_new_commits(tmp_path):
    """落ちたら、設定の「GPU の部品を入れる」が main に新しいコミットの無い安定版で断られる(更新を待たないと入らない)、
    または知らない mode で更新スクリプトが走る。"""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _git(origin, "config", "user.email", "t@example.com")
    _git(origin, "config", "user.name", "t")
    (origin / "a.txt").write_text("1", encoding="utf-8")
    _git(origin, "add", "a.txt")
    _git(origin, "commit", "-q", "-m", "first")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    asked = []

    def blocker():
        asked.append(1)
        return "テスト: ここで止める(スクリプトは起こさない)"

    updater = StableUpdater(
        log_dir=tmp_path / "logs", relaunch={}, update_blocker=blocker, on_started=lambda: None, repo_root=clone,
    )
    assert updater.status(force=True)["state"] == "current"
    assert updater.start()[0] == 409 and not asked
    assert updater.start(mode="mystery")[0] == 400 and not asked
    if sys.platform == "win32":
        status, body = updater.start(mode="gpu")
        assert status == 409 and body["error"].startswith("テスト") and asked == [1]


@pytest.mark.skipif(sys.platform != "win32", reason="自己更新は Windows だけ")
def test_the_update_script_does_not_inherit_the_servers_python_home(tmp_path, monkeypatch):
    """落ちたら、uv の venv の python が渡す PYTHONHOME を更新スクリプトが引き継ぎ、Python の版を上げる更新で
    新しい版の python が古い版の標準ライブラリを読んで uv sync が落ちる(元の版へ戻され、上げられない)。"""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _git(origin, "config", "user.email", "t@example.com")
    _git(origin, "config", "user.name", "t")
    (origin / "a.txt").write_text("1", encoding="utf-8")
    _git(origin, "add", "a.txt")
    _git(origin, "commit", "-q", "-m", "first")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    (clone / "tools" / "stable").mkdir(parents=True)
    (clone / "tools" / "stable" / "update.ps1").write_text("# テスト", encoding="utf-8")
    (origin / "a.txt").write_text("2", encoding="utf-8")
    _git(origin, "commit", "-q", "-am", "second")

    launched = []
    real_popen = channel.subprocess.Popen

    def popen(command, **kwargs):  # git(subprocess.run)は通し、更新スクリプトの起動だけ横取りする
        if command[0] != "powershell.exe":
            return real_popen(command, **kwargs)
        launched.append(kwargs)

    monkeypatch.setattr(channel.subprocess, "Popen", popen)
    monkeypatch.setenv("PYTHONHOME", r"C:\python311")
    monkeypatch.setenv("UV_INTERNAL__PYTHONHOME", r"C:\python311")
    monkeypatch.setenv("DUOCAD_TEST_KEEP", "1")
    updater = StableUpdater(
        log_dir=tmp_path / "logs",
        relaunch={"channel": "stable", "port": 1, "http_port": 2, "data_dir": tmp_path},
        update_blocker=lambda: None, on_started=lambda: None, repo_root=clone,
    )
    assert updater.start()[0] == 202
    (kwargs,) = launched
    assert "PYTHONHOME" not in kwargs["env"] and "UV_INTERNAL__PYTHONHOME" not in kwargs["env"]
    assert kwargs["env"]["DUOCAD_TEST_KEEP"] == "1"
