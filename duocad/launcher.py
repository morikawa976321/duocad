"""安定版の起動口。スタートメニューの「duocad」がこれを `pythonw -m duocad.launcher` で呼ぶ。

1. サーバが立っていなければ、窓の無い pythonw で切り離して立てる(`--no-stdio`)。
2. 立つのを待って、**Edgeのアプリウィンドウ**(`--app=`。タブ・アドレスバー・検索窓の無い
   独立した窓)で画面を開く。

既に立っていれば窓を開くだけ。`--no-window` は窓を開かない(更新スクリプトが立て直すとき ——
画面は開いたまま再接続を待っている)。配置と手順は `docs/STABLE.md`。

**ポートは開発版(8765/8766)と分ける**。開発セッションやエージェントが既定のポートで
サーバを立てても安定版とぶつからず、開発側の `.mcp.json`(8766)が安定版を操作することもない。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path
from typing import Optional

from .channel import CHANNELS
from .paths import REPO_ROOT

STABLE_PORT = 8775
STABLE_HTTP_PORT = 8776
DEFAULT_DATA_DIR = Path.home() / "mf" / "duocad-stable" / "data"

#: サーバが応答するまで待つ上限(秒)。OCCTの読み込みで初回は十数秒かかる。
START_TIMEOUT_SECONDS = 120.0

#: サーバのログ(`<data>/logs/server.log`)をこの大きさで1世代だけ回す。
LOG_ROTATE_BYTES = 5 * 1024 * 1024

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


def server_config(http_port: int) -> Optional[dict]:
    """立っているサーバの `/duocad-config.json`。応答が無ければ None。"""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{http_port}/duocad-config.json", timeout=1.0
        ) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError):
        return None


def _python_without_console() -> str:
    """同じ venv の pythonw.exe(無ければ今の python)。"""
    candidate = Path(sys.executable).with_name("pythonw.exe")
    return str(candidate if candidate.exists() else sys.executable)


def _open_log(log_dir: Path):
    log_dir.mkdir(parents=True, exist_ok=True)
    log = log_dir / "server.log"
    try:
        if log.stat().st_size > LOG_ROTATE_BYTES:
            log.replace(log.with_suffix(".log.1"))
    except OSError:
        pass
    return open(log, "ab")


def spawn_server(channel: str, port: int, http_port: int, data_dir: Path) -> None:
    command = [
        _python_without_console(), "-m", "duocad.mcp_server",
        "--channel", channel,
        "--port", str(port),
        "--http-port", str(http_port),
        "--data-dir", str(data_dir),
        "--no-stdio",
    ]
    log = _open_log(data_dir / "logs")
    try:
        log.write(f"\n==== {time.strftime('%Y-%m-%d %H:%M:%S')} {' '.join(command)}\n".encode("utf-8"))
        log.flush()
        subprocess.Popen(
            command,
            cwd=str(REPO_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            creationflags=_NO_WINDOW | _NEW_GROUP,
            close_fds=True,
        )
    finally:
        log.close()


def wait_for_server(http_port: int) -> bool:
    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if server_config(http_port) is not None:
            return True
        time.sleep(0.5)
    return False


def _edge_path() -> Optional[str]:
    try:
        import winreg

        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(
                    root, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"
                ) as key:
                    value, _ = winreg.QueryValueEx(key, "")
                    if value and Path(value).exists():
                        return str(value)
            except OSError:
                continue
    except ImportError:
        pass
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        if base:
            candidate = Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            if candidate.exists():
                return str(candidate)
    return None


def open_app_window(url: str) -> None:
    """Edgeのアプリウィンドウで開く。Edgeが見つからなければ既定のブラウザで開く。"""
    edge = _edge_path()
    if edge is None:
        webbrowser.open(url)
        return
    subprocess.Popen(
        [edge, f"--app={url}"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


#: 安定版のアプリウィンドウのタイトル。Edgeの `--app` 窓はページの `<title>` だけをタイトルにする
#: (普通のタブの窓は「… - Microsoft Edge」になる)ので、完全一致で安定版の窓だけを拾える。
#: **`web/index.html` の `<title>` と揃える**(開発版は `ChannelBadge.svelte` が `[開発版] ` を前に付けるので一致しない)。
APP_WINDOW_TITLE = "duocad"


def find_app_window() -> Optional[int]:
    """開いている安定版のアプリウィンドウ(msedge.exe の、タイトルが `APP_WINDOW_TITLE` の窓)。"""
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return None
    if not hasattr(ctypes, "windll"):
        return None
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    found: list[int] = []

    def process_name(pid: int) -> str:
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(1024)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return ""
            return Path(buffer.value).name.lower()
        finally:
            kernel32.CloseHandle(handle)

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length != len(APP_WINDOW_TITLE):
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        if buffer.value != APP_WINDOW_TITLE:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if process_name(pid.value) == "msedge.exe":
            found.append(int(hwnd))
            return False
        return True

    user32.EnumWindows(visit, 0)
    return found[0] if found else None


def bring_to_front(hwnd: int) -> None:
    """最小化されていれば戻し、前面に出す。"""
    import ctypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    if user32.SetForegroundWindow(hwnd):
        return
    # Windowsは、いま前面にいない処理からの SetForegroundWindow を断ることがある(フォアグラウンドのロック)。
    # 前面の窓のスレッドに入力を一時的につなぐと通る(広く使われている回避策)。
    foreground = user32.GetForegroundWindow()
    theirs = user32.GetWindowThreadProcessId(foreground, None)
    ours = kernel32.GetCurrentThreadId()
    attached = theirs and theirs != ours and user32.AttachThreadInput(ours, theirs, True)
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(ours, theirs, False)


def _alert(message: str) -> None:
    """pythonw には標準出力が無いので、失敗はメッセージボックスで言う。"""
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, message, "duocad", 0x10)
    except (ImportError, AttributeError, OSError):
        print(message, file=sys.stderr)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="duocad 安定版の起動口(サーバを立てて窓を開く)")
    parser.add_argument("--channel", choices=CHANNELS, default="stable")
    parser.add_argument("--port", type=int, default=STABLE_PORT)
    parser.add_argument("--http-port", type=int, default=STABLE_HTTP_PORT)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--no-window", action="store_true", help="サーバだけ立てて窓は開かない")
    args = parser.parse_args(argv)
    data_dir = args.data_dir.expanduser().resolve()
    url = f"http://127.0.0.1:{args.http_port}/"

    running = server_config(args.http_port)
    if running is not None and running.get("channel") != args.channel:
        # 開発セッションが同じポートで別のduocadを立てている。そちらを安定版として開かない。
        _alert(
            f"ポート {args.http_port} で別の duocad(channel={running.get('channel')})が動いている。\n"
            "それを止めてからもう一度起動すること。"
        )
        return 1
    if running is None:
        spawn_server(args.channel, args.port, args.http_port, data_dir)
        if not wait_for_server(args.http_port):
            _alert(
                f"duocad のサーバが {int(START_TIMEOUT_SECONDS)} 秒以内に立たなかった。\n"
                f"ログ: {data_dir / 'logs' / 'server.log'}"
            )
            return 1
    if not args.no_window:
        # 既に開いていれば新しい窓を作らずにそれを前に出す。2つ開くと、今の文書を開き直す操作が
        # 「別のタブで開いている」で断られる(`duocad/net/ws.py` の `open_document`)。
        existing = find_app_window()
        if existing is not None:
            bring_to_front(existing)
        else:
            open_app_window(url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
