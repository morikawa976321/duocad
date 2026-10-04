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
from pathlib import Path
from typing import Any, Callable, Optional

from .paths import REPO_ROOT

CHANNELS = ("dev", "stable")

#: 更新元の枝。
UPDATE_BRANCH = "main"

#: `git fetch` を叩く間隔の下限(秒)。画面は開くたびと定期的に状態を聞くので、
#: そのたびにGitHubへ行かない。
STATUS_CACHE_SECONDS = 60.0

#: 一覧に出す新しいコミットの件数の上限。
MAX_LISTED_COMMITS = 20

#: `update.ps1` が結果を書くファイル名(`log_dir` 直下)。画面が更新後の再読み込みで
#: 成否を知るのに使う(`last_result`)。
RESULT_FILE = "last-update-result.json"

#: **pythonw から console のプログラム(git)を呼ぶと、そのたびに黒い窓が一瞬出る**。
#: 安定版は窓の無い pythonw で動くので、子プロセスはすべてこれで起こす。
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
                # 開発用のチェックアウト(develop)で `--channel stable` を立てた場合など。
                # ここで止めないと update.ps1 が開発中の作業ツリーでビルドと再起動をする。
                return {
                    **base,
                    "state": "blocked",
                    "reason": f"{self.branch} 以外の枝({branch.stdout.strip()})にいる",
                }
            dirty = _git("status", "--porcelain", "--untracked-files=no", cwd=self.repo_root)
            if dirty.returncode != 0:
                return {**base, "state": "unknown", "reason": dirty.stderr.strip() or "git status に失敗"}
            if dirty.stdout.strip():
                # 安定版のcloneを手で書き換えた形跡。取り込みで潰さない(update.ps1 も同じ検査で止まる)。
                return {**base, "state": "blocked", "reason": "安定版のフォルダに未コミットの変更がある"}
            fetch = _git("fetch", "--quiet", "origin", self.branch, timeout=60.0, cwd=self.repo_root)
            if fetch.returncode != 0:
                return {**base, "state": "unknown", "reason": fetch.stderr.strip() or "git fetch に失敗"}
            remote = f"origin/{self.branch}"
            ancestor = _git("merge-base", "--is-ancestor", "HEAD", remote, cwd=self.repo_root)
            if ancestor.returncode != 0:
                # developのコミットから導入した場合(install.ps1 -Commit)。mainにだけあるのが
                # PRのマージコミットだけなら取り込むものは無い。次に develop → main がマージされると
                # HEAD がmainの祖先になり、早送りで取り込めるようになる。
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

    def start(self) -> tuple[int, dict]:
        """更新スクリプトを切り離して起動し、サーバの終了を予約する。(HTTPステータス, 本文) を返す。"""
        with self._lock:
            if self._started:
                return 409, {"error": "更新はすでに始まっている"}
            if sys.platform != "win32":
                return 501, {"error": "自己更新は Windows だけ"}
            state = self._probe()
            self._cached, self._cached_at = state, time.monotonic()
            if state["state"] != "available":
                return 409, {"error": state.get("reason") or "取り込む変更が無い"}
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
            ]
            # CREATE_NO_WINDOW: 見えないコンソールを持たせる(DETACHED_PROCESS にすると、
            # スクリプトが呼ぶ git / uv / npm がそれぞれ自分の窓を開いてしまう)。
            # Windowsの子プロセスは親が終わっても残るので、切り離しはこれで足りる。
            subprocess.Popen(
                command,
                cwd=str(self.repo_root),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=_NO_WINDOW | _NEW_GROUP,
                close_fds=True,
            )
            self._started = True
        self.on_started()
        return 202, {"started": True}


def describe(channel: str, updater: Optional[StableUpdater]) -> dict[str, Any]:
    """`/duocad-config.json` に載せる分(`ws_url` の隣)。"""
    return {
        "channel": channel,
        "version": version_info(),
        "update_supported": updater is not None,
    }
