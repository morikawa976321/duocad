"""ブラウザのファイル本文を、既存の取り込み verb に渡すための一時置き場。"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile
import threading
import time
import unicodedata
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .http import WebHandler

UPLOAD_HTTP_PATH = "/api/upload"
UPLOAD_MAX_BYTES = 128 * 1024 * 1024
UPLOAD_TTL_SECONDS = 60 * 60
UPLOAD_CHUNK_BYTES = 64 * 1024


def validate_filename(name: str) -> None:
    reserved = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$", "CLOCK$"}
    reserved.update(f"{prefix}{n}" for prefix in ("COM", "LPT") for n in "123456789¹²³")
    if (
        not name
        or any(c in '<>:"/\\|?*' or unicodedata.category(c) in ("Cc", "Cs") for c in name)
        or name.endswith((".", " "))
        or name.split(".", 1)[0].rstrip(" ").upper() in reserved
        or len(name.encode("utf-16-le")) > 240
        or len(name.encode("utf-8")) > 240
    ):
        raise ValueError("ファイル名に使えない文字や予約名がある")


class UploadStore:
    def __init__(self) -> None:
        # 消せないファイル(Windows では、別の読み手が開いたままのもの)が残っても、サーバの終了を例外で止めない。
        self._temp = tempfile.TemporaryDirectory(prefix="duocad-upload-", ignore_cleanup_errors=True)
        self.root = Path(self._temp.name).resolve()
        self._condition = threading.Condition()
        self._entries: dict[Path, float | None] = {}
        self._closed = False
        atexit.register(self.close)

    def begin(self, name: str) -> Path:
        with self._condition:
            if self._closed:
                raise RuntimeError("サーバを終了している")
            now = time.monotonic()
            for directory, completed in list(self._entries.items()):
                if completed is not None and now - completed >= UPLOAD_TTL_SECONDS:
                    try:
                        shutil.rmtree(directory)
                    except OSError:
                        continue
                    del self._entries[directory]
            directory = Path(tempfile.mkdtemp(dir=self.root))
            self._entries[directory] = None
            return directory / name

    def finish(self, path: Path, *, keep: bool) -> None:
        with self._condition:
            self._entries[path.parent] = time.monotonic()
            try:
                if not keep:
                    shutil.rmtree(path.parent)
                    del self._entries[path.parent]
            finally:
                self._condition.notify_all()

    def close(self) -> None:
        with self._condition:
            self._closed = True
            # HTTP の受信中はファイルが開いている。Windows では開いたファイルを消せない。
            self._condition.wait_for(lambda: all(t is not None for t in self._entries.values()))
            self._temp.cleanup()
            self._entries.clear()
            atexit.unregister(self.close)


def post_upload(handler: WebHandler) -> None:
    def reply(status: int, code: str, message: str) -> None:
        handler._send_json(status, {"error": code, "message": message})

    handler.close_connection = True
    handler.connection.settimeout(30)
    lengths = handler.headers.get_all("Content-Length", [])
    if len(lengths) != 1 or handler.headers.get("Transfer-Encoding") is not None:
        reply(411, "length_required", "ファイルの大きさが指定されていない")
        return
    try:
        length = int(lengths[0])
        if length < 0:
            raise ValueError
    except ValueError:
        reply(400, "invalid_length", "ファイルの大きさの指定が正しくない")
        return

    if length > UPLOAD_MAX_BYTES:
        reply(413, "too_large", f"ファイルが大きすぎる(上限 {UPLOAD_MAX_BYTES // (1024 * 1024)} MiB)")
        return

    error = None
    host = (handler.headers.get("Host") or "").strip().lower()
    if handler.headers.get("Origin") not in handler.page_origins or host not in handler.mcp_hosts:
        error = (403, "forbidden", "duocad が配信する画面からしか送信できない")

    name = ""
    if error is None:
        try:
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query, errors="strict")
            names = query.get("filename", [])
            if len(names) != 1:
                raise ValueError("ファイル名が 1 つ指定されていない")
            name = names[0]
            validate_filename(name)
        except ValueError:
            error = (400, "invalid_filename", "ファイル名に使えない文字や予約名がある(名前を変えてから落とす)")
        if error is None and not name.lower().endswith((".step", ".stp", ".svg")):
            error = (415, "unsupported_extension", "取り込めるのは STEP(.step・.stp)と SVG(.svg)だけ")

    path = None
    output = None
    store = handler.upload_store
    try:
        if error is None:
            try:
                path = store.begin(name)
                if os.name == "nt" and len(str(path).encode("utf-16-le")) > 480:
                    error = (400, "invalid_filename", "ファイル名が長すぎる(名前を短くしてから落とす)")
                else:
                    output = path.open("xb")
            except RuntimeError:
                error = (503, "shutting_down", "サーバを終了しているので受け取れない")
            except OSError:
                error = (500, "storage_error", "一時ファイルを保存できない(空き容量と書き込み権限を確かめる)")

        # 拒否時も本文を小分けに読み捨てる。送信中に接続を閉じるとブラウザには拒否理由が届かない。
        remaining = length
        while remaining:
            chunk = handler.rfile.read(min(UPLOAD_CHUNK_BYTES, remaining))
            if not chunk:
                error = (400, "incomplete_body", "ファイルの送信が途中で切れた(もう一度落とす)")
                break
            remaining -= len(chunk)
            if output is not None and error is None:
                try:
                    output.write(chunk)
                except OSError:
                    error = (500, "storage_error", "一時ファイルを保存できない(空き容量を確かめる)")
    except (OSError, TimeoutError):
        error = (408, "receive_failed", "ファイルを受信できなかった(接続を確かめて、もう一度落とす)")
    finally:
        if output is not None:
            try:
                output.close()
            except OSError:
                error = (500, "storage_error", "一時ファイルを保存できない(空き容量を確かめる)")
        if path is not None:
            try:
                store.finish(path, keep=error is None)
            except OSError:
                error = (500, "storage_error", "一時ファイルを片付けられない(書き込み権限を確かめる)")
    if error is not None:
        reply(*error)
    else:
        handler._send_json(200, {"path": str(path)})
