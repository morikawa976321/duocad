"""常駐の子プロセスで重い形状演算を走らせる口(いまは面の削除・オフセットだけ)。方式と実測は docs/FACE_EDIT.md「子プロセス」。

OCCT の `BOPAlgo_RemoveFeatures` は計算中ずっと GIL を握り(3.04 秒の計算で主スレッドが 3.09 秒止まった)、
`BRepOffset_MakeOffset` はプロセスごと落ちることがある(角ばった実物で 21 件)。同じプロセスで計算すると、その間サーバ全体
(画面・MCP)が止まる・落ちるので、子に計算させ、親はパイプを待つ(待つ間は GIL を放す)。

- 子は1本で、初回の呼び出しで立てる(起動に約 1.6 秒)。形は BinTools のバイト列、引数と結果の由来は JSON で渡す
  (BinTools は往復で幾何が変わらず、面の添字の数え方も保つ。BRep の文字列は 1e-14 ずれる)。
- 実行は同時に1件。走っている計算を止めるのは期限切れだけで、そのときは子を殺し、次の呼び出しで立て直す
  (GIL を握ったまま返らない計算を止める手段は子を殺すことしかない)。
- 期限切れ・異常終了・計算の拒否はどれも `ValueError`(呼び出し側は結果を採らず、文書もログも変えない)。
"""
from __future__ import annotations

import atexit
import io
import json
import os
import queue
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

from OCP.BinTools import BinTools, BinTools_FormatVersion
from OCP.TopoDS import TopoDS_Shape

#: 画面・MCP からの操作で、子の計算(受け渡しを含む)を打ち切るまでの秒数。置いた値(docs/FACE_EDIT.md カード e は本人判断待ち)。
#: 実測の根拠は「合成形状の正当な計算の最長 18 秒(穴100個を一度に消す)」と「角ばった実物の1件は最長約 2.3 秒」だけ。
INTERACTIVE_TIMEOUT_S = 30.0
#: 子が立ち上がるまで待つ秒数(期限には含めない)。置いた値
_STARTUP_TIMEOUT_S = 120.0
_HEADER = struct.Struct(">II")

_LOCK = threading.Lock()
_CHILD: Optional["_Child"] = None


def time_limit(interactive: bool) -> Optional[float]:
    """画面・MCP からの操作なら期限[秒]、ログの再生なら None(再生に期限を掛けると、保存できた文書が負荷しだいで開けなくなる)。"""
    return INTERACTIVE_TIMEOUT_S if interactive else None


def run(verb: str, shape: Any, arguments: dict, *, timeout: Optional[float]) -> dict:
    """子で `kernel_face_edit.compute(verb, shape, arguments)` を走らせ、同じ形の辞書を返す(`"shape"` は親の形)。

    `timeout` は計算の期限[秒](None は期限なし。ログの再生)。前の計算の終わりを待つ時間と子の起動は含めない。
    """
    global _CHILD
    request = {"verb": verb, "arguments": arguments}
    blob = _to_bytes(shape)
    with _LOCK:
        try:
            if _CHILD is None or not _CHILD.alive():
                _CHILD = _Child.start()
            header, result_blob = _CHILD.call(request, blob, timeout)
        except _Stopped as stopped:
            if _CHILD is not None:
                _CHILD.kill()
                _CHILD = None
            raise ValueError(f"{verb}: {stopped}") from None
    if not header["ok"]:
        raise ValueError(header["error"])
    return {
        "shape": _from_bytes(result_blob),
        "provenance": header["provenance"],
        "volume_before": header["volume_before"],
        "volume_after": header["volume_after"],
    }


def stop() -> None:
    """子を止める(終了時。次の `run` で立て直す)。"""
    global _CHILD
    with _LOCK:
        if _CHILD is not None:
            _CHILD.close()
            _CHILD = None


atexit.register(stop)


class _Stopped(Exception):
    """子が期限切れか異常終了で、結果が来なかった。"""


class _Child:
    def __init__(self, process: subprocess.Popen):
        self.process = process
        self.frames: "queue.Queue[Optional[tuple]]" = queue.Queue()
        self.next_id = 0
        threading.Thread(target=self._read_all, name="kernel-worker-reader", daemon=True).start()

    @classmethod
    def start(cls) -> "_Child":
        root = str(Path(__file__).resolve().parents[1])
        env = dict(os.environ)
        env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        process = subprocess.Popen(
            [sys.executable, "-m", "duocad.kernel_worker"],
            cwd=root,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        child = cls(process)
        try:
            ready = child._next_frame(_STARTUP_TIMEOUT_S, "starting")
        except _Stopped:
            child.kill()
            raise
        if ready[0].get("ready") is not True:
            child.kill()
            raise _Stopped("the worker process did not start")
        return child

    def alive(self) -> bool:
        return self.process.poll() is None

    def call(self, request: dict, blob: bytes, timeout: Optional[float]) -> tuple:
        self.next_id += 1
        request = {**request, "id": self.next_id}
        try:
            _write_frame(self.process.stdin, request, blob)
        except OSError as error:
            raise _Stopped(f"the worker process stopped ({error})") from None
        header, result = self._next_frame(timeout, "computing")
        if header.get("id") != self.next_id:
            raise _Stopped(f"the worker answered request {header.get('id')} instead of {self.next_id}")
        return header, result

    def _next_frame(self, timeout: Optional[float], doing: str) -> tuple:
        started = time.monotonic()
        try:
            frame = self.frames.get(timeout=timeout)
        except queue.Empty:
            raise _Stopped(f"gave up after {time.monotonic() - started:.1f} s while {doing} (time limit)") from None
        if frame is None:
            code = self.process.wait()
            raise _Stopped(f"the worker process stopped while {doing} (exit code {code & 0xFFFFFFFF:#x})")
        return frame

    def _read_all(self) -> None:
        stream = self.process.stdout
        while True:
            try:
                frame = _read_frame(stream)
            except (OSError, ValueError):
                frame = None
            self.frames.put(frame)
            if frame is None:
                return

    def kill(self) -> None:
        if self.alive():
            self.process.kill()
        self.process.wait()

    def close(self) -> None:
        try:
            self.process.stdin.close()
            self.process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            self.kill()


# --------------------------------------------------------------------------------------
# 受け渡し(親子共通)
# --------------------------------------------------------------------------------------
def _write_frame(stream: Any, header: dict, blob: bytes = b"") -> None:
    encoded = json.dumps(header).encode("utf-8")
    stream.write(_HEADER.pack(len(encoded), len(blob)) + encoded + blob)
    stream.flush()


def _read_frame(stream: Any) -> Optional[tuple]:
    sizes = _read_exact(stream, _HEADER.size)
    if sizes is None:
        return None
    header_size, blob_size = _HEADER.unpack(sizes)
    header = _read_exact(stream, header_size)
    blob = _read_exact(stream, blob_size)
    if header is None or blob is None:
        return None
    return json.loads(header.decode("utf-8")), blob


def _read_exact(stream: Any, size: int) -> Optional[bytes]:
    chunks, remaining = [], size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _to_bytes(shape: Any) -> bytes:
    stream = io.BytesIO()
    BinTools.Write_s(shape, stream, False, False, BinTools_FormatVersion.BinTools_FormatVersion_CURRENT)
    return stream.getvalue()


def _from_bytes(blob: bytes) -> Any:
    shape = TopoDS_Shape()
    BinTools.Read_s(shape, io.BytesIO(blob))
    return shape


# --------------------------------------------------------------------------------------
# 子
# --------------------------------------------------------------------------------------
def _serve() -> None:
    from . import kernel_face_edit

    # OCCT は C++ の標準出力へ書くことがある。受け渡しには元の標準出力の写しを使い、fd 1 は標準エラーへ向ける
    replies = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    requests = sys.stdin.buffer
    _write_frame(replies, {"ready": True})
    while True:
        frame = _read_frame(requests)
        if frame is None:
            return
        header, blob = frame
        reply: dict = {"id": header.get("id")}
        result_blob = b""
        try:
            result = kernel_face_edit.compute(header["verb"], _from_bytes(blob), header["arguments"])
            result_blob = _to_bytes(result.pop("shape"))
            reply.update(ok=True, **result)
        except ValueError as error:
            reply.update(ok=False, error=str(error))
        except Exception as error:  # noqa: BLE001  OCCT の例外(Standard_Failure 等)も断りとして返す
            reply.update(ok=False, error=f"{header.get('verb')}: OCCT raised {type(error).__name__}: {error}")
        _write_frame(replies, reply, result_blob)


if __name__ == "__main__":
    _serve()
