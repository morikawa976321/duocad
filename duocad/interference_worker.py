"""干渉チェックの子プロセス(Issue #186)。親側の `WorkerProcess` と、子の本体(`python -m duocad.interference_worker`)が同居する。

子で計算する理由: OCP からは OCCT の計算を途中で止められない(山形が一致したねじ山の2体 General Fuse は 240 秒で終わらなかった)ので、
時間の上限を掛けるには子プロセスごと止めるしかない。止めたら作り直し、形を送り直す(PC ケースで約 2 秒)。

## 線の形

1行 = 1つの JSON(`json.dumps` の ASCII。BRep の文字列は改行を含むので JSON の文字列で包む)。親→子は `seq` を付け、子は同じ `seq` で返す。
`seq` の違う返事は捨てる(止めた直後に遅れて届いた返事を次の要求の答えにしない)。

    {"seq":1,"op":"load","bodies":[{"id":"b1","kind":"brep","brep":"..."} | {"id":"b2","kind":"mesh","positions":[..],"indices":[..]}]}
    {"seq":2,"op":"relation","a":"b1","b":"b2","search":1.0,"deviation":0.05|null}   → result は `kernel_interference.pair_relation`(か `interference_mesh.mesh_pair`)
    {"seq":3,"op":"distance","a":"b1","b":"b2"}                                      → result は `kernel_interference.min_distance`
    返事: {"seq":N,"ok":true,"result":{...}} か {"seq":N,"ok":false,"error":"..."}

子の標準出力は線の専用に取り分け、fd 1 は標準エラーへ向ける(OCCT や下位のライブラリが C のレベルで標準出力へ書いても線が壊れない)。
親は子の標準エラーを読み捨てずに上限つきで受ける(詰まらせない)。
"""
from __future__ import annotations

import collections
import json
import os
import queue
import subprocess
import sys
import threading
import time
from typing import Any, Deque, Dict, List, Optional

from .paths import REPO_ROOT

#: 子の標準エラーを控える上限[バイト]。診断に末尾だけ使う。
STDERR_KEEP_BYTES = 8192

_POLL_SECONDS = 0.05


class WorkerError(Exception):
    """子プロセスへの要求が答えを返せなかった。`reason` は結果に載せる文言。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class WorkerTimeout(WorkerError):
    """上限の時間内に返事が無かった(子は止めて作り直す必要がある)。"""


class WorkerFailure(WorkerError):
    """子が途中で終わった・壊れた返事を返した・起動できなかった(子は作り直す必要がある)。"""


class WorkerRemoteError(WorkerError):
    """子は生きていて、計算が例外で終わったと返した(子はそのまま使える)。"""


class WorkerCancelled(Exception):
    """要求した側が取り消した。"""


# ------------------------------------------------------------------------------------------------
# 親側
# ------------------------------------------------------------------------------------------------
class WorkerProcess:
    """子プロセス1つ。`request` は1回に1件(並行して投げない)。止めたら `restart` で作り直し、`load` の中身を送り直す。"""

    def __init__(
        self,
        load_message: Dict[str, Any],
        *,
        command: Optional[List[str]] = None,
        cancel: Optional[threading.Event] = None,
        startup_seconds: float = 60.0,
    ) -> None:
        self._load = load_message
        self._command = command or [sys.executable, "-m", "duocad.interference_worker"]
        self._cancel = cancel
        self._startup_seconds = startup_seconds
        self._process: Optional[subprocess.Popen] = None
        self._lines: "queue.Queue[Optional[bytes]]" = queue.Queue()
        self._outgoing: "queue.Queue[Optional[bytes]]" = queue.Queue()
        self._stderr: Deque[bytes] = collections.deque()
        self._stderr_bytes = 0
        self._stderr_lock = threading.Lock()
        self._seq = 0
        self.starts = 0

    def __enter__(self) -> "WorkerProcess":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    # ---- 要求
    def request(self, message: Dict[str, Any], timeout: float) -> Dict[str, Any]:
        """子へ要求を送り、返事の `result` を返す。無ければ起動して `load` を送る(その時間も `timeout` の内)。"""
        deadline = time.monotonic() + timeout
        if self._process is None:
            self._spawn()
            self._exchange(self._load, min(self._startup_seconds, max(deadline - time.monotonic(), 0.0)))
        return self._exchange(message, max(deadline - time.monotonic(), 0.0))

    def restart(self) -> None:
        """子を止めて、次の要求で作り直させる。"""
        self._kill()

    def close(self) -> None:
        self._kill()

    def stderr_tail(self) -> str:
        with self._stderr_lock:
            return b"".join(self._stderr).decode("utf-8", errors="replace")

    # ---- 中身
    def _spawn(self) -> None:
        self._lines = queue.Queue()
        self._outgoing = queue.Queue()
        with self._stderr_lock:
            self._stderr.clear()
            self._stderr_bytes = 0
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO_ROOT), environment.get("PYTHONPATH")]))
        try:
            process = subprocess.Popen(
                self._command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(REPO_ROOT),
                env=environment,
            )
        except OSError as error:
            raise WorkerFailure(f"子プロセスを起動できない: {error}") from error
        self._process = process
        self.starts += 1
        lines, outgoing = self._lines, self._outgoing
        threading.Thread(target=self._pump_stdout, args=(process, lines), daemon=True).start()
        threading.Thread(target=self._pump_stderr, args=(process,), daemon=True).start()
        threading.Thread(target=self._pump_stdin, args=(process, outgoing), daemon=True).start()

    @staticmethod
    def _pump_stdout(process: subprocess.Popen, lines: "queue.Queue[Optional[bytes]]") -> None:
        try:
            for line in process.stdout:  # type: ignore[union-attr]
                lines.put(line)
        except (OSError, ValueError):
            pass
        lines.put(None)

    def _pump_stderr(self, process: subprocess.Popen) -> None:
        try:
            while True:
                chunk = process.stderr.read1(4096)  # type: ignore[union-attr]
                if not chunk:
                    return
                with self._stderr_lock:
                    self._stderr.append(chunk)
                    self._stderr_bytes += len(chunk)
                    while self._stderr_bytes > STDERR_KEEP_BYTES and len(self._stderr) > 1:
                        self._stderr_bytes -= len(self._stderr.popleft())
        except (OSError, ValueError):
            return

    @staticmethod
    def _pump_stdin(process: subprocess.Popen, outgoing: "queue.Queue[Optional[bytes]]") -> None:
        """書き込みは別スレッドで行う(大きな `load` を、読まない子へ書いて親が塞がらないように)。"""
        try:
            while True:
                data = outgoing.get()
                if data is None:
                    return
                process.stdin.write(data)  # type: ignore[union-attr]
                process.stdin.flush()  # type: ignore[union-attr]
        except (OSError, ValueError):
            return

    def _exchange(self, message: Dict[str, Any], timeout: float) -> Dict[str, Any]:
        self._seq += 1
        seq = self._seq
        self._outgoing.put((json.dumps({**message, "seq": seq}) + "\n").encode("ascii"))
        deadline = time.monotonic() + timeout
        while True:
            if self._cancel is not None and self._cancel.is_set():
                raise WorkerCancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WorkerTimeout(f"{timeout:.0f} 秒以内に返事が無かった")
            try:
                line = self._lines.get(timeout=min(_POLL_SECONDS, remaining))
            except queue.Empty:
                continue
            if line is None:
                raise WorkerFailure(self._death_reason())
            try:
                reply = json.loads(line)
            except ValueError:
                raise WorkerFailure("子の返事が壊れている(JSON ではない)") from None
            if not isinstance(reply, dict):
                raise WorkerFailure("子の返事が壊れている(辞書ではない)")
            if reply.get("seq") != seq:
                continue  # 止める前の要求への遅れた返事
            if reply.get("ok") is True and isinstance(reply.get("result"), dict):
                return reply["result"]
            if reply.get("ok") is False and isinstance(reply.get("error"), str):
                raise WorkerRemoteError(reply["error"])
            raise WorkerFailure("子の返事が壊れている(ok / result が無い)")

    def _death_reason(self) -> str:
        process = self._process
        code = None
        if process is not None:
            try:
                code = process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                code = None
        tail = self.stderr_tail().strip().splitlines()[-1:] or [""]
        return f"子プロセスが途中で終了した(終了コード {code}){(': ' + tail[0][:200]) if tail[0] else ''}"

    def _kill(self) -> None:
        process, self._process = self._process, None
        self._outgoing.put(None)
        if process is None:
            return
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                stream.close()  # type: ignore[union-attr]
            except (OSError, ValueError):
                pass


class InProcessWorker:
    """同じ要求を呼び出し元のプロセスで処理する。`WorkerProcess` と同じ口(時間の上限は掛からない。軽い形のテスト用)。"""

    def __init__(self, load_message: Dict[str, Any], *, cancel: Optional[threading.Event] = None) -> None:
        self._load = load_message
        self._cancel = cancel
        self._handler: Optional[Handler] = None
        self.starts = 0

    def request(self, message: Dict[str, Any], timeout: float) -> Dict[str, Any]:  # noqa: ARG002
        if self._cancel is not None and self._cancel.is_set():
            raise WorkerCancelled()
        if self._handler is None:
            self._handler = Handler()
            self._handler.handle(self._load)
            self.starts += 1
        try:
            return self._handler.handle(message)
        except Exception as error:  # noqa: BLE001
            raise WorkerRemoteError(f"{type(error).__name__}: {error}") from error

    def restart(self) -> None:
        self._handler = None

    def close(self) -> None:
        self._handler = None

    def __enter__(self) -> "InProcessWorker":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


# ------------------------------------------------------------------------------------------------
# 子の本体
# ------------------------------------------------------------------------------------------------
class Handler:
    """要求 1 件を処理する。形と、メッシュ経路の manifold を覚える。"""

    def __init__(self) -> None:
        self._bodies: Dict[str, Dict[str, Any]] = {}
        self._manifolds: Dict[str, Any] = {}

    def handle(self, message: Dict[str, Any]) -> Dict[str, Any]:
        operation = message.get("op")
        if operation == "load":
            return self._load(message["bodies"])
        if operation == "relation":
            return self._relation(message["a"], message["b"], float(message["search"]), message.get("deviation"))
        if operation == "distance":
            from . import kernel_interference

            return kernel_interference.min_distance(self._shape(message["a"]), self._shape(message["b"]))
        raise ValueError(f"知らない op: {operation!r}")

    def _load(self, bodies: List[Dict[str, Any]]) -> Dict[str, Any]:
        from . import kernel_interference

        self._bodies, self._manifolds = {}, {}
        for body in bodies:
            if body["kind"] == "brep":
                self._bodies[body["id"]] = {"kind": "brep", "shape": kernel_interference.decode_brep_unchecked(body["brep"])}
            else:
                self._bodies[body["id"]] = {"kind": "mesh", "positions": body["positions"], "indices": body["indices"]}
        return {"loaded": len(self._bodies)}

    def _shape(self, body_id: str) -> Any:
        body = self._bodies[body_id]
        if body["kind"] != "brep":
            raise ValueError(f"{body_id} は B-rep ではない")
        return body["shape"]

    def _relation(self, first: str, second: str, search: float, deviation: Optional[float]) -> Dict[str, Any]:
        if self._bodies[first]["kind"] == "brep" and self._bodies[second]["kind"] == "brep":
            from . import kernel_interference

            return kernel_interference.pair_relation(self._bodies[first]["shape"], self._bodies[second]["shape"], deviation)
        from . import interference_mesh

        return interference_mesh.mesh_pair(self._manifold(first), self._manifold(second), search, deviation)

    def _manifold(self, body_id: str) -> Any:
        if body_id not in self._manifolds:
            from . import interference_mesh

            body = self._bodies[body_id]
            if body["kind"] == "brep":
                self._manifolds[body_id] = interference_mesh.manifold_of_shape(body["shape"])[0]
            else:
                self._manifolds[body_id] = interference_mesh.manifold_of_triangles(body["positions"], body["indices"])[0]
        return self._manifolds[body_id]


def serve(lines: Any, out: Any) -> None:
    handler = Handler()
    for line in lines:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
            seq = message.get("seq")
        except ValueError:
            out.write(json.dumps({"seq": None, "ok": False, "error": "要求が JSON ではない"}) + "\n")
            continue
        if message.get("op") == "exit":
            return
        try:
            reply = {"seq": seq, "ok": True, "result": handler.handle(message)}
        except Exception as error:  # noqa: BLE001  計算の失敗は親へ返し、子は次の要求へ進む
            reply = {"seq": seq, "ok": False, "error": f"{type(error).__name__}: {error}"}
        out.write(json.dumps(reply) + "\n")
        out.flush()


def main() -> None:
    protocol_fd = os.dup(1)
    os.dup2(2, 1)
    out = os.fdopen(protocol_fd, "w", encoding="ascii", buffering=1)
    sys.stdin.reconfigure(encoding="ascii")  # type: ignore[attr-defined]
    serve(sys.stdin, out)


if __name__ == "__main__":
    main()
