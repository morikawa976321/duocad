"""開発版/安定版の区別と、安定版の自己更新(`/api/update`)。配置・手順・判断は `docs/STABLE.md`。

安定版の更新はサーバの外で行う: サーバが `tools/stable/update.ps1` を切り離して起動して終了し、
スクリプトが取り込み・ビルドして `duocad.launcher` で立て直す。**Windowsでは動いている
`.venv` の python.exe を置き換えられない**ため。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from .paths import REPO_ROOT

CHANNELS = ("dev", "stable")

#: 安定版(制作用)で出さない機能(`catalog.FEATURES`)。形状最適化は凍結した(本人決定 2026-10-13、
#: docs/TOPOPT_CONVERGENCE.md 9節)。開発版では今までどおり使える。
HIDDEN_IN_STABLE = frozenset({"shape_optimization"})

#: 更新元の枝。
UPDATE_BRANCH = "main"

#: `git fetch` を叩く間隔の下限(秒)。
STATUS_CACHE_SECONDS = 60.0

#: 一覧に出す新しいコミットの件数の上限。
MAX_LISTED_COMMITS = 20

#: `update.ps1` が結果を書くファイル名(`log_dir` 直下。`last_result` が読む)。
RESULT_FILE = "last-update-result.json"

#: このサーバのプロセスの起動ごとに変わる印(`/duocad-config.json` の `boot`)。画面は立て直しの終わりを
#: これの変化で知る —— GPU の部品だけを入れる立て直しはコミットを変えないので、版では見分けられない。
BOOT_ID = uuid.uuid4().hex

#: 更新スクリプトへ引き継がない環境変数。**uv の venv の python は、起動時に `PYTHONHOME` を自分の基の
#: python(例: 3.11)へ向けて子に渡す**。それを引き継いだ `uv sync` が新しい版(例: 3.14)の venv で duocad を
#: ビルドすると、3.14 が 3.11 の標準ライブラリを読んで落ちる(`SRE module mismatch`。2026-10-10 の 3.11 → 3.14 で実測)。
_INHERITED_PYTHON_ENV = ("PYTHONHOME", "UV_INTERNAL__PYTHONHOME", "PYTHONPATH", "__PYVENV_LAUNCHER__", "VIRTUAL_ENV")

# pythonw から console のプログラム(git)を呼ぶと黒い窓が一瞬出る。子プロセスはすべてこれで起こす。
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


def _git(*args: str, timeout: float = 30.0, cwd: Path = REPO_ROOT) -> subprocess.CompletedProcess:
    # 認証やホスト鍵を聞かれても答える人がいない(窓が無い)ので、聞かずに失敗させる。
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_SSH_COMMAND": os.environ.get("GIT_SSH_COMMAND", "ssh -o BatchMode=yes"),
    }
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
        stdin=subprocess.DEVNULL,
        creationflags=_NO_WINDOW,
    )


def version_info(cwd: Path = REPO_ROOT) -> dict:
    """今動いているコードのコミット(短いハッシュとコミット日時)。gitが無ければ空。"""
    try:
        done = _git("log", "-1", "--format=%h%x09%cI", timeout=10.0, cwd=cwd)
    except (OSError, subprocess.SubprocessError):
        return {}
    if done.returncode != 0 or "\t" not in done.stdout:
        return {}
    commit, date = done.stdout.strip().split("\t", 1)
    return {"commit": commit, "date": date}


class StableUpdater:
    """安定版の更新状態を答え、更新を始める。`WebHandler`(`/api/update`)から呼ばれる。

    `status()` も `start()` も HTTP のワーカースレッドで走る。`Session` には触らず、
    今止めてよいかを `update_blocker`(イベントループへ投げ返す呼び出し。止められない理由か None)で聞く。
    """

    def __init__(
        self,
        *,
        log_dir: Path,
        relaunch: dict,
        update_blocker: Callable[[], Optional[str]],
        on_started: Callable[[], None],
        repo_root: Path = REPO_ROOT,
        branch: str = UPDATE_BRANCH,
    ) -> None:
        self.log_dir = Path(log_dir)
        #: 立て直すときに `duocad.launcher` へ渡すもの(`channel` / `port` / `http_port` / `data_dir`)。
        self.relaunch = relaunch
        self.update_blocker = update_blocker
        self.on_started = on_started
        self.repo_root = Path(repo_root)
        self.branch = branch
        self._lock = threading.Lock()
        self._cached: Optional[dict] = None
        self._cached_at = 0.0
        self._started = False

    # ---- 状態 ----

    def status(self, force: bool = False) -> dict:
        with self._lock:
            now = time.monotonic()
            if not force and self._cached is not None and now - self._cached_at < STATUS_CACHE_SECONDS:
                payload = dict(self._cached)
            else:
                payload = self._probe()
                self._cached, self._cached_at = payload, now
                payload = dict(payload)
        payload["last_result"] = self._last_result()
        return payload

    def _probe(self) -> dict:
        current = version_info(self.repo_root)
        base = {"current": current, "behind": 0, "commits": []}
        try:
            branch = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=self.repo_root)
            if branch.returncode == 0 and branch.stdout.strip() != self.branch:
                # 開発用チェックアウトで `--channel stable` を立てた場合など。止めないと update.ps1 が開発中の作業ツリーを再起動する。
                return {
                    **base,
                    "state": "blocked",
                    "reason": f"{self.branch} 以外の枝({branch.stdout.strip()})にいる",
                }
            dirty = _git("status", "--porcelain", "--untracked-files=no", cwd=self.repo_root)
            if dirty.returncode != 0:
                return {**base, "state": "unknown", "reason": dirty.stderr.strip() or "git status に失敗"}
            if dirty.stdout.strip():
                # 取り込みで潰さない(update.ps1 も同じ検査で止まる)。
                return {**base, "state": "blocked", "reason": "安定版のフォルダに未コミットの変更がある"}
            fetch = _git("fetch", "--quiet", "origin", self.branch, timeout=60.0, cwd=self.repo_root)
            if fetch.returncode != 0:
                return {**base, "state": "unknown", "reason": fetch.stderr.strip() or "git fetch に失敗"}
            remote = f"origin/{self.branch}"
            ancestor = _git("merge-base", "--is-ancestor", "HEAD", remote, cwd=self.repo_root)
            if ancestor.returncode != 0:
                # developのコミットから導入した場合(install.ps1 -Commit)。mainにだけあるのがマージコミットだけなら取り込むものは無い。
                only_main = _git(
                    "rev-list", "--count", "--no-merges", f"HEAD..{remote}", cwd=self.repo_root
                )
                if only_main.returncode == 0 and only_main.stdout.strip() == "0":
                    return {**base, "state": "current"}
                return {**base, "state": "blocked", "reason": f"HEAD と {remote} が分岐している(早送りできない)"}
            log = _git(
                "log", f"--max-count={MAX_LISTED_COMMITS}", "--format=%s", f"HEAD..{remote}",
                cwd=self.repo_root,
            )
            count = _git("rev-list", "--count", f"HEAD..{remote}", cwd=self.repo_root)
        except (OSError, subprocess.SubprocessError) as error:
            return {**base, "state": "unknown", "reason": str(error)}
        behind = int(count.stdout.strip() or 0) if count.returncode == 0 else 0
        commits = [line for line in log.stdout.splitlines() if line.strip()]
        return {
            **base,
            "state": "available" if behind > 0 else "current",
            "behind": behind,
            "commits": commits,
        }

    def _last_result(self) -> Optional[dict]:
        try:
            return json.loads((self.log_dir / RESULT_FILE).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return None

    # ---- 更新 ----

    def start(self, mode: str = "update") -> tuple[int, dict]:
        """更新スクリプトを切り離して起動し、サーバの終了を予約する。(HTTPステータス, 本文) を返す。

        `mode="gpu"` は取り込みもビルドもせず、GPU の部品(extra gpu)だけを入れて立て直す
        (main に新しいコミットが無くても走る)。
        """
        if mode not in ("update", "gpu"):
            return 400, {"error": f"未知の mode: {mode!r}"}
        with self._lock:
            if self._started:
                return 409, {"error": "更新はすでに始まっている"}
            if sys.platform != "win32":
                return 501, {"error": "自己更新は Windows だけ"}
            state = self._probe()
            self._cached, self._cached_at = state, time.monotonic()
            if mode == "update" and state["state"] != "available":
                return 409, {"error": state.get("reason") or "取り込む変更が無い"}
            if mode == "gpu" and state["state"] == "blocked":
                return 409, {"error": state.get("reason")}
            try:
                reason = self.update_blocker()
            except Exception as error:  # noqa: BLE001  (イベントループが塞がっている等。HTTPスレッドへ漏らさない)
                return 503, {"error": f"サーバの状態を確かめられなかった: {error!r}"}
            if reason is not None:
                return 409, {"error": reason}
            script = self.repo_root / "tools" / "stable" / "update.ps1"
            if not script.exists():
                return 500, {"error": f"{script} が無い"}
            self.log_dir.mkdir(parents=True, exist_ok=True)
            # **更新でスクリプト自身が書き換わる**ので、一時フォルダへ写してから走らせる。
            runner = Path(tempfile.gettempdir()) / f"duocad-update-{os.getpid()}.ps1"
            shutil.copyfile(script, runner)
            command = [
                "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(runner),
                "-RepoRoot", str(self.repo_root),
                "-Branch", self.branch,
                "-ServerPid", str(os.getpid()),
                "-LogDir", str(self.log_dir),
                "-Channel", str(self.relaunch["channel"]),
                "-Port", str(self.relaunch["port"]),
                "-HttpPort", str(self.relaunch["http_port"]),
                "-DataDir", str(self.relaunch["data_dir"]),
                *(["-InstallGpu"] if mode == "gpu" else []),
            ]
            # CREATE_NO_WINDOW: DETACHED_PROCESS にするとスクリプトが呼ぶ git / uv / npm が自分の窓を開く。
            # Windowsの子プロセスは親が終わっても残るので、切り離しはこれで足りる。
            subprocess.Popen(
                command,
                cwd=str(self.repo_root),
                env={k: v for k, v in os.environ.items() if k.upper() not in _INHERITED_PYTHON_ENV},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=_NO_WINDOW | _NEW_GROUP,
                close_fds=True,
            )
            self._started = True
        self.on_started()
        return 202, {"started": True}


def hidden_features(channel: str) -> frozenset:
    """その版で隠す機能(`catalog.FEATURES` の部分集合)。"""
    return HIDDEN_IN_STABLE if channel == "stable" else frozenset()


def describe(channel: str, updater: Optional[StableUpdater]) -> dict[str, Any]:
    """`/duocad-config.json` に載せる分(`ws_url` の隣)。"""
    return {
        "channel": channel,
        "version": version_info(),
        "boot": BOOT_ID,
        "update_supported": updater is not None,
    }
