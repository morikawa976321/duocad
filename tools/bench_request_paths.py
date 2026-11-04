"""実際の要求の経路(MCP の HTTP・画面の WebSocket)を、本物のサーバを子プロセスで立てて工程ごとに測る。

    uv run python -X utf8 tools/bench_request_paths.py --part small|big|multi [--rounds 4] [--out <json>] [--only <名前の一部>]

子プロセスは `duocad.mcp_server.main` そのもの(`--no-stdio`・使い捨ての `--models-dir`・空いたポート)で、起動の前に
関数へ時計を差し込む(既存のコードは変えない)。時計は `time.perf_counter`(同じ機械ならプロセスをまたいで同じ時計)。

- MCP の確定: HTTP の受信 → `mcp_lock` → ループへ投げ返す待ち → 道具(verb・自動保存・封筒)→ 返事、とその後の配信
  (`state_meta`・`delta`(三角形分割)・`compose`・`encode`・送信)→ 画面の接続に届くまで
- 画面の確定(WebSocket): ゴースト → 確定 → 配信 → `done`、undo / redo
- 読み込み(`open_document`): ファイル・解析・ログの再生(verb ごと)・差し替え・最初の配信
- カメラ(`set_view_resolution`): 測り直しの写し(ループの上)・ワーカーの測定・配り直し
- ループを塞いだ時間: ループの全コールバックの所要(`asyncio.Handle._run`)と、画面の接続から送る軽い照会
  (`ai_get_status`)の往復。照会は画面の接続の受信待ち行列に並ぶので、画面の次のクリックが待つ時間と同じ

自動保存の囲み方は経路で違う(測る条件): 画面の確定は `Server._apply` が `background_autosave` で囲み、ループの上では
JSON 化だけで書き込みは書き手スレッド。MCP の確定は囲まないので、ループの上で書き込みまで済ませる。

部品: small / big は `tools/bench_all_ops.py` の合成の部品(穴 24 / 384)と板を横切る棒。multi は small に移動の複製を
12 個足した 13 ボディ(ボディの数に比例する費用を見る反例)。
"""
from __future__ import annotations

import argparse
import functools
import http.client
import inspect
import json
import shutil
import signal
import socket
import statistics
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

now = time.perf_counter

# ====================================================================================== サーバ側(子プロセス)
_EVENTS: list = []
_LOCK = threading.Lock()
#: 始まって終わっていない区間の数(名前ごと)。測り終えたかの判定に使う
_ACTIVE: dict = {}
#: ループのコールバックの所要の合計と数(小さいものも全部)
_BUSY = [0.0, 0]
_LOOP_THREAD: list = []

#: 終わるまで次の操作へ進まない区間
SETTLING = ("push", "tool.call", "refine.measure", "preview.run", "autosave.write", "on_loop", "open")


def _record(name: str, t0: float, t1: float, extra: Any = None) -> None:
    event = {"n": name, "th": threading.current_thread().name, "t0": t0, "t1": t1}
    if extra is not None:
        event["x"] = extra
    with _LOCK:
        _EVENTS.append(event)


def _wrap(owner: Any, attr: str, name: str, describe: Optional[Callable[..., Any]] = None) -> None:
    raw = inspect.getattr_static(owner, attr)
    rewrap = None
    if isinstance(raw, (staticmethod, classmethod)):
        rewrap = type(raw)
        raw = raw.__func__

    def extra(args: tuple, kwargs: dict) -> Any:
        if describe is None:
            return None
        try:
            return describe(*args, **kwargs)
        except Exception as error:  # noqa: BLE001
            return f"?{type(error).__name__}"

    def enter() -> float:
        with _LOCK:
            _ACTIVE[name] = _ACTIVE.get(name, 0) + 1
        return now()

    def leave(t0: float, args: tuple, kwargs: dict, result: Any = None) -> None:
        t1 = now()
        with _LOCK:
            _ACTIVE[name] -= 1
        _record(name, t0, t1, extra(args, kwargs))

    if inspect.iscoroutinefunction(raw):
        @functools.wraps(raw)
        async def timed(*args: Any, **kwargs: Any) -> Any:
            t0 = enter()
            try:
                return await raw(*args, **kwargs)
            finally:
                leave(t0, args, kwargs)
    else:
        @functools.wraps(raw)
        def timed(*args: Any, **kwargs: Any) -> Any:
            t0 = enter()
            try:
                return raw(*args, **kwargs)
            finally:
                leave(t0, args, kwargs)
    setattr(owner, attr, rewrap(timed) if rewrap is not None else timed)


def _callback_label(callback: Any) -> str:
    owner = getattr(callback, "__self__", None)
    if owner is not None and hasattr(owner, "get_coro"):
        coro = owner.get_coro()
        return "task:" + getattr(coro, "__qualname__", type(coro).__name__)
    return getattr(callback, "__qualname__", None) or type(callback).__name__


def install_probes() -> None:
    """子プロセスで、`mcp_server.main` を呼ぶ前に時計を差し込む。"""
    import asyncio

    from duocad import autosave, document_store, kernel, mcp_server, oplog, session, view
    from duocad.net import http as http_mod
    from duocad.net import refinement, tools_on_loop, wire, ws

    real_run = asyncio.events.Handle._run

    def run(handle: Any) -> Any:
        t0 = now()
        try:
            return real_run(handle)
        finally:
            t1 = now()
            _BUSY[0] += t1 - t0
            _BUSY[1] += 1
            if t1 - t0 >= 0.0005:
                _record("loop", t0, t1, _callback_label(handle._callback))

    asyncio.events.Handle._run = run

    def body_name(view_self: Any, body: Any) -> str:
        return body.id

    def mcp_message(_self: Any, message: Any) -> Any:
        if isinstance(message, dict):
            params = message.get("params") or {}
            return message.get("method"), params.get("name") if isinstance(params, dict) else None
        return None

    def first_arg(_self: Any, value: Any = None, *rest: Any, **kw: Any) -> Any:
        return value if isinstance(value, (str, int, float)) or value is None else type(value).__name__

    def ws_command(_self: Any, _connection: Any, raw: Any) -> Any:
        try:
            return json.loads(raw).get("cmd")
        except Exception:  # noqa: BLE001
            return None

    def push_target(_self: Any, origin: Any, applied: Any, only: Any = None) -> Any:
        return {"origin": origin is not None, "only": only is not None, "applied": None if applied is None else applied.get("verb") or applied.get("change")}

    def faces_groups(shape: Any, groups: dict, **_kw: Any) -> Any:
        return sum(len(list(indices)) for indices in groups.values())

    def encoded(payload: dict) -> Any:
        return payload.get("type")

    def sent(_connection: Any, payload: dict) -> Any:
        return payload.get("type")

    def jobs(copies: list) -> Any:
        return [(body_id, sum(len(list(v)) for v in groups.values())) for body_id, _s, _c, groups, _cl in copies]

    _wrap(http_mod.WebHandler, "do_POST", "http.post", lambda handler: handler.path)
    _wrap(mcp_server.McpServer, "handle", "mcp.handle", mcp_message)
    _wrap(tools_on_loop.ToolsOnLoop, "_run_on_loop", "on_loop", lambda _self, fn, *a: getattr(fn, "__name__", "?"))
    _wrap(mcp_server.DocumentTools, "call", "tool.call", lambda _self, name, *a, **k: name)
    _wrap(session.Session, "apply", "apply", first_arg)
    _wrap(session.Session, "undo", "undo")
    _wrap(session.Session, "redo", "redo")
    _wrap(session.Session, "autosave", "autosave")
    _wrap(session.Session, "save_document", "save_document")
    _wrap(session.Session, "state_meta", "state_meta")
    _wrap(session.Session, "delta", "delta")
    _wrap(session.Session, "preview_job", "preview.job", first_arg)
    _wrap(session.PreviewJob, "run", "preview.run")
    _wrap(kernel, "adopt_results", "preview.adopt")
    _wrap(view.DocumentView, "seed_faces", "seed_faces", lambda _self, body_id, *a: body_id)
    _wrap(session.Session, "adopt_refinement", "refine.adopt", first_arg)
    _wrap(session.Session, "open_document", "open", first_arg)
    _wrap(session.Session, "_replace_document", "open.replace")
    _wrap(session.Session, "set_view_resolution", "set_view_resolution")
    _wrap(autosave.Autosave, "save_current", "autosave.json")
    _wrap(autosave.Autosave, "_write_queued", "autosave.write")
    _wrap(document_store.DocumentStore, "write_document_file", "file.write")
    _wrap(document_store.DocumentStore, "read_document_file", "open.read")
    _wrap(oplog.OpLog, "from_payload", "open.parse")
    _wrap(oplog.OpLog, "replay", "open.replay")
    _wrap(oplog.OpLog, "to_file_chunks", "log.json")
    _wrap(view.DocumentView, "sketches_state", "sketches")
    _wrap(view.DocumentView, "_tessellate_body", "tessellate_body", body_name)
    _wrap(view.DocumentView, "_borrow_faces", "borrow")
    _wrap(view.DocumentView, "_body_volume", "volume", body_name)
    _wrap(view.DocumentView, "reset", "view.reset")
    _wrap(view.DocumentView, "refinement_jobs", "refine.jobs")
    _wrap(kernel, "tessellate_faces", "tessellate_faces", faces_groups)
    _wrap(kernel, "tessellate", "tessellate")
    _wrap(kernel, "tessellate_reusing", "tessellate_reusing")
    _wrap(kernel, "copy_shape", "copy_shape")
    _wrap(wire.Delivered, "compose", "compose")
    _wrap(wire.Delivered, "record", "record")
    _wrap(wire, "encode", "encode", encoded)
    _wrap(ws, "_send", "send", sent)
    _wrap(ws.Server, "_dispatch", "ws.dispatch", ws_command)
    _wrap(ws.Server, "_push", "push", push_target)
    _wrap(ws.Server, "_on_core_change", "core_change")
    _wrap(ws.Server, "_settle_autosave", "settle_autosave")
    _wrap(refinement.BackgroundRefinement, "schedule", "refine.schedule")
    _wrap(refinement, "_measure_copies", "refine.measure", jobs)

    real_handler = oplog.verb_handler

    def timed_handler(verb: str) -> Any:
        handler = real_handler(verb)

        def timed(step: Callable, label: str) -> Callable:
            def run_step(document: Any, operation: Any) -> Any:
                t0 = now()
                try:
                    return step(document, operation)
                finally:
                    _record(label, t0, now(), verb)
            return run_step

        return oplog.VerbHandler(do=timed(handler.do, "verb.do"), undo=timed(handler.undo, "verb.undo"))

    oplog.verb_handler = timed_handler

    real_get = http_mod.WebHandler.do_GET

    def do_GET(handler: Any) -> None:  # noqa: N802
        if not handler.path.startswith("/__trace"):
            return real_get(handler)
        peek = "peek=1" in handler.path
        with _LOCK:
            events = [] if peek else list(_EVENTS)
            if not peek:
                _EVENTS.clear()
            active = {key: value for key, value in _ACTIVE.items() if value}
        body = json.dumps({"events": events, "active": active, "busy": _BUSY[0], "callbacks": _BUSY[1], "now": now()}).encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    http_mod.WebHandler.do_GET = do_GET


def serve(argv: list) -> int:
    install_probes()
    from duocad import mcp_server

    return mcp_server.main(argv)


# ====================================================================================== 画面と AI の側(このプロセス)
class Screen:
    """画面の WebSocket の接続。届いたものを時刻つきで溜め、軽い照会(`ai_get_status`)を投げられる。"""

    def __init__(self, url: str) -> None:
        from websockets.sync.client import connect

        self.connection = connect(url, max_size=None, open_timeout=30)
        self.messages: list = []
        self.changed = threading.Condition()
        self.probe_reply = threading.Event()
        self.probes: list = []
        self._probing = threading.Event()
        self._client_op = 0
        self.reader = threading.Thread(target=self._read, name="screen-reader", daemon=True)
        self.reader.start()

    def _read(self) -> None:
        try:
            for raw in self.connection:
                arrived = now()
                if isinstance(raw, (bytes, bytearray)):
                    (length,) = struct.unpack_from("<I", raw, 0)
                    header = json.loads(raw[4:4 + length].decode("utf-8"))
                    size = len(raw)
                else:
                    header = json.loads(raw)
                    size = len(raw.encode("utf-8"))
                kind = header.get("type")
                if kind == "ai_status":
                    self.probe_reply.set()
                    continue
                with self.changed:
                    self.messages.append({"t": arrived, "type": kind, "bytes": size, "header": header})
                    self.changed.notify_all()
        except Exception:  # noqa: BLE001  切断
            pass

    def next_op(self) -> int:
        self._client_op += 1
        return self._client_op

    def send(self, command: dict) -> None:
        self.connection.send(json.dumps(command))

    def wait(self, predicate: Callable[[dict], bool], start: int, timeout: float = 900.0) -> dict:
        deadline = now() + timeout
        with self.changed:
            while True:
                for message in self.messages[start:]:
                    if predicate(message):
                        return message
                left = deadline - now()
                if left <= 0:
                    raise TimeoutError("待っていた配信が来ない")
                self.changed.wait(left)

    def start_probing(self, interval: float) -> threading.Thread:
        self._probing.set()
        thread = threading.Thread(target=self._probe, args=(interval,), name="screen-probe", daemon=True)
        thread.start()
        return thread

    def stop_probing(self, thread: threading.Thread) -> list:
        self._probing.clear()
        thread.join(timeout=900)
        taken, self.probes = self.probes, []
        return taken

    def _probe(self, interval: float) -> None:
        while self._probing.is_set():
            self.probe_reply.clear()
            sent = now()
            self.send({"cmd": "ai_get_status"})
            if not self.probe_reply.wait(900):
                break
            self.probes.append((sent, now()))
            time.sleep(interval)

    def close(self) -> None:
        try:
            self.connection.close()
        except Exception:  # noqa: BLE001
            pass


class Tracer:
    def __init__(self, port: int) -> None:
        self.port = port

    def fetch(self, peek: bool = False) -> dict:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        connection.request("GET", "/__trace" + ("?peek=1" if peek else ""))
        payload = json.loads(connection.getresponse().read())
        connection.close()
        return payload


class Mcp:
    def __init__(self, port: int) -> None:
        self.port = port
        self._id = 0

    def call(self, name: str, arguments: Optional[dict] = None) -> dict:
        self._id += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self._id, "method": "tools/call",
                           "params": {"name": name, "arguments": arguments or {}}}).encode("utf-8")
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=900)
        connection.request("POST", "/mcp", body, {"Content-Type": "application/json"})
        response = json.loads(connection.getresponse().read())
        connection.close()
        result = response.get("result") or {}
        text = (result.get("content") or [{}])[0].get("text", "{}")
        payload = json.loads(text)
        if result.get("isError") or "error" in response:
            raise RuntimeError(f"{name}: {json.dumps(payload.get('error') or response.get('error'), ensure_ascii=False)[:300]}")
        return payload


# -------------------------------------------------------------------------------------- 測る1件
THREAD_KIND = (("MainThread", "loop"), ("process_request", "http"), ("duocad-preview", "preview"),
               ("duocad-refine", "refine"), ("duocad-autosave", "autosave"))


def thread_kind(name: str) -> str:
    for prefix, kind in THREAD_KIND:
        if prefix in name:
            return kind
    return name.split("_")[0]


def _ms(seconds: Optional[float]) -> Optional[float]:
    return None if seconds is None else round(seconds * 1000.0, 2)


def summarize(label: str, kind: str, t0: float, t_reply: float, t_end: float, frames: list, events: list,
              probes: list, busy: float, error: Optional[str]) -> dict:
    inside = [e for e in events if e["t1"] >= t0 and e["t0"] <= t_end]
    spans: dict = {}
    for event in inside:
        if event["n"] == "loop":
            continue
        key = f"{event['n']}@{thread_kind(event['th'])}"
        total, count = spans.get(key, (0.0, 0))
        spans[key] = (total + event["t1"] - event["t0"], count + 1)
    loop_events = sorted((e for e in inside if e["n"] == "loop"), key=lambda e: e["t0"])
    longest = sorted(loop_events, key=lambda e: e["t0"] - e["t1"])[:5]
    verbs: dict = {}
    for event in inside:
        if event["n"] in ("verb.do", "verb.undo"):
            key = f"{event['n'][5:]}:{event.get('x')}"
            total, count = verbs.get(key, (0.0, 0))
            verbs[key] = (total + event["t1"] - event["t0"], count + 1)

    def first(name: str, thread: Optional[str] = None, after: float = -1.0) -> Optional[dict]:
        found = [e for e in inside if e["n"] == name and e["t0"] >= after and (thread is None or thread_kind(e["th"]) == thread)]
        return min(found, key=lambda e: e["t0"]) if found else None

    stages: dict = {}
    post = first("http.post")
    if post is not None and kind == "mcp":
        handle, on_loop, tool = first("mcp.handle"), first("on_loop"), first("tool.call")
        stages["http_in"] = _ms(post["t0"] - t0)
        if handle is not None:
            stages["lock_parse"] = _ms(handle["t0"] - post["t0"])
        if on_loop is not None and tool is not None:
            stages["loop_queue"] = _ms(tool["t0"] - on_loop["t0"])
            stages["tool"] = _ms(tool["t1"] - tool["t0"])
            stages["loop_back"] = _ms(on_loop["t1"] - tool["t1"])
        if handle is not None:
            stages["reply_out"] = _ms(post["t1"] - handle["t1"])
        stages["client_in"] = _ms(t_reply - post["t1"])
        if tool is not None:
            push = first("push", after=tool["t1"])
            if push is not None:
                stages["push_wait"] = _ms(push["t0"] - tool["t1"])
                stages["push"] = _ms(push["t1"] - push["t0"])
                arrival = next((f["t"] for f in frames if f["type"] == "state" and f["t"] >= push["t0"]), None)
                if arrival is not None:
                    stages["push_to_screen"] = _ms(arrival - push["t1"])
    probe_latencies = [b - a for a, b in probes]
    state_frames = [f for f in frames if f["type"] == "state"]
    row = {
        "case": label,
        "path": kind,
        "reply_ms": _ms(t_reply - t0),
        "first_state_ms": _ms(state_frames[0]["t"] - t0) if state_frames else None,
        "last_frame_ms": _ms(frames[-1]["t"] - t0) if frames else None,
        "frames": [(f["type"], f["bytes"], _ms(f["t"] - t0)) for f in frames],
        "stages": stages,
        "spans": {key: [_ms(total), count] for key, (total, count) in sorted(spans.items(), key=lambda item: -item[1][0])},
        "verbs": {key: [_ms(total), count] for key, (total, count) in sorted(verbs.items(), key=lambda item: -item[1][0])},
        "loop": {
            "busy_ms": _ms(busy),
            "max_block_ms": _ms(longest[0]["t1"] - longest[0]["t0"]) if longest else 0.0,
            "longest": [(_ms(e["t1"] - e["t0"]), e.get("x"), _ms(e["t0"] - t0)) for e in longest],
        },
        "probes": {
            "n": len(probe_latencies),
            "max_ms": _ms(max(probe_latencies)) if probe_latencies else None,
            "median_ms": _ms(statistics.median(probe_latencies)) if probe_latencies else None,
        },
    }
    if error:
        row["error"] = error
    return row


class Bench:
    def __init__(self, screen: Screen, mcp: Mcp, tracer: Tracer, probe_interval: float, only: str) -> None:
        self.screen = screen
        self.mcp = mcp
        self.tracer = tracer
        self.probe_interval = probe_interval
        self.only = only
        self.rows: list = []

    def settle(self, start: int, idle: float = 0.35, timeout: float = 900.0) -> None:
        """画面に何も届かない間が `idle` 続き、サーバに終わっていない仕事が無くなるまで待つ。"""
        deadline = now() + timeout
        while now() < deadline:
            with self.screen.changed:
                last = self.screen.messages[-1]["t"] if len(self.screen.messages) > start else 0.0
            quiet = now() - last
            if quiet >= idle:
                active = self.tracer.fetch(peek=True)["active"]
                if not any(active.get(name) for name in SETTLING):
                    return
            time.sleep(min(0.05, max(0.0, idle - quiet)))
        raise TimeoutError("サーバが静まらない")

    def measure(self, label: str, kind: str, action: Callable[[int], Any], *, expect_state: bool = True,
                record: bool = True) -> Any:
        """`action(配信の数え始め)` を測る。返事を待つのは `action` の中、配信と静まりはここで待つ。"""
        if self.only and self.only not in label and record:
            record = False
        self.tracer.fetch()
        start = len(self.screen.messages)
        busy0 = self.tracer.fetch(peek=True)["busy"]
        prober = self.screen.start_probing(self.probe_interval) if record else None
        t0 = now()
        error = None
        result = None
        try:
            result = action(start)
            t_reply = now()
            if expect_state:
                self.screen.wait(lambda m: m["type"] in ("state", "error"), start)
        except Exception as failure:  # noqa: BLE001
            t_reply = now()
            error = f"{type(failure).__name__}: {failure}"[:300]
        self.settle(start)
        t_end = now()
        probes = self.screen.stop_probing(prober) if prober is not None else []
        trace = self.tracer.fetch()
        if not record:
            if error:
                print(f"  ({label}: {error})", flush=True)
            return result
        with self.screen.changed:
            frames = [m for m in self.screen.messages[start:] if m["t"] <= t_end + 1.0]
        errors = [m["header"].get("message") for m in frames if m["type"] == "error"]
        if errors and not error:
            error = str(errors[0])[:300]
        row = summarize(label, kind, t0, t_reply, t_end, frames, trace["events"], probes, trace["busy"] - busy0, error)
        if isinstance(result, dict) and "_extra" in result:
            row.update(result["_extra"])
        self.rows.append(row)
        stage = " ".join(f"{k}={v}" for k, v in row["stages"].items())
        print(f"  {label:<34} reply={row['reply_ms']} state={row['first_state_ms']} last={row['last_frame_ms']} "
              f"loop_busy={row['loop']['busy_ms']} max_block={row['loop']['max_block_ms']} probe_max={row['probes']['max_ms']} "
              f"{stage} {'ERR ' + error if error else ''}", flush=True)
        return result

    # ---------------------------------------------------------------- 経路ごとの1手
    def mcp_op(self, label: str, name: str, arguments: Optional[dict] = None, *, expect_state: bool = True,
               record: bool = True) -> Any:
        return self.measure(label, "mcp", lambda _start: self.mcp.call(name, arguments), expect_state=expect_state, record=record)

    def ws_op(self, label: str, command: dict, *, until: str = "applied", record: bool = True) -> Any:
        """画面の操作。`until`: applied(配信に自分の client_op)/ preview / done / state。"""
        def act(start: int) -> Any:
            op = self.screen.next_op()
            message = dict(command, client_op=op, ack_id=op)
            self.screen.send(message)
            if until == "preview":
                self.screen.wait(lambda m: m["type"] in ("preview", "error"), start)
            elif until == "applied":
                self.screen.wait(lambda m: m["type"] == "error" or (m["type"] == "state" and (m["header"].get("applied") or {}).get("client_op") == op), start)
            self.screen.wait(lambda m: m["type"] == "done" and m["header"].get("ack_id") >= op, start)
            return op
        return self.measure(label, "ws", act, expect_state=False, record=record)


# -------------------------------------------------------------------------------------- 文書と場面
def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def build_document(part: str, models_dir: Path) -> dict:
    """部品を作って `models_dir` へ保存し、場面に要る添字(サーバが開く文書と同じ。再生は同じ添字を作る)を返す。"""
    import bench_all_ops as base
    from duocad import brep_topology, kernel
    from duocad.session import Session

    session = Session(kernel.DEFAULT_DEVIATION, models_dir=models_dir)
    size = "small" if part == "multi" else part
    t0 = now()
    body_id = base.build_part(session, **base.PARTS[size])
    session.apply("sketch_create", {"base_plane": "XZ"})
    sid = base.last_sketch(session)
    session.apply("add_rectangle", {"sketch_id": sid, "corner": [-70, 2], "width": 140, "height": 3})
    session.leave_sketch()
    base.select(session, [{"sketch": sid, "index": 1}])
    session.apply("extrude", {"distance": 30, "mode": "new"})
    tool = session.document.bodies[-1].id
    if part == "multi":
        for k in range(12):
            base.select(session, [base.body_ref(body_id)])
            session.apply("move_body", {"dx": 0.0, "dy": 100.0 * (k + 1), "dz": 0.0, "copy": True})
    session.clear_selection()
    built = now() - t0
    planes = base.faces_of_kind(session, body_id, "plane")
    top = max(planes, key=lambda f: f["centroid"][2])
    lines = base.blendable(session, body_id, base.edges_of_kind(session, body_id, "line", 3.0), 1)
    bbox = session.geometry(body_id)["bbox"]
    name = f"paths_{part}.json"
    session.rename_document(name)
    return {
        "name": name,
        "body": body_id,
        "tool": tool,
        "top": top["index"],
        "edge": lines[0]["index"],
        "diag": bbox["diagonal"],
        "faces": brep_topology.face_count(session.document.get_body(body_id).shape),
        "bodies": len(session.document.bodies),
        "log": len(session.document.log.entries),
        "file_bytes": (models_dir / name).stat().st_size,
        "build_ms": _ms(built),
    }


def fillet_then_look(mcp: Mcp, radius: float) -> dict:
    """AI の続け方: verb の返事を受けたらすぐ次の照会を送る(その照会は verb の配信が済むまでループで待つ)。"""
    mcp.call("fillet", {"radius": radius})
    replied = now()
    mcp.call("describe_document")
    return {"_extra": {"next_call_ms": _ms(now() - replied)}}


def run_scenes(bench: Bench, doc: dict, rounds: int) -> None:
    body, tool = doc["body"], doc["tool"]
    edge = {"body": body, "kind": "edge", "index": doc["edge"]}
    top = {"body": body, "kind": "face", "index": doc["top"]}
    whole = {"body": body, "kind": "body", "index": None}
    bar = {"body": tool, "kind": "body", "index": None}
    diag = doc["diag"]
    overview = {"cmd": "set_view_resolution", "world_per_pixel": diag / 1200.0, "size_hint": diag}
    focus = list(range(1, min(doc["faces"], 30) + 1))

    print("\n### 読み込み(冷えた状態)", flush=True)
    bench.ws_op("load open_document #0", {"cmd": "open_document", "name": doc["name"]})
    bench.ws_op("load camera overview #0", overview, until="done")
    bench.mcp_op("mcp describe_document #0", "describe_document", expect_state=False)

    for r in range(rounds):
        tag = f"#{r}"
        print(f"\n### 回 {r}", flush=True)
        bench.mcp_op(f"mcp set_selection edge {tag}", "set_selection", {"refs": [edge]})
        bench.mcp_op(f"mcp fillet {tag}", "fillet", {"radius": 0.5 + 0.01 * r})
        bench.mcp_op(f"mcp undo fillet {tag}", "undo")
        bench.mcp_op(f"mcp redo fillet {tag}", "redo")
        bench.mcp_op(f"mcp undo fillet(2) {tag}", "undo", record=False)
        bench.mcp_op(f"mcp set_selection edge(2) {tag}", "set_selection", {"refs": [edge]}, record=False)
        bench.measure(f"mcp fillet→すぐ describe {tag}", "mcp", lambda _s, r=r: fillet_then_look(bench.mcp, 0.503 + 0.01 * r))
        bench.mcp_op(f"mcp undo fillet(3) {tag}", "undo", record=False)
        bench.mcp_op(f"mcp set_selection face {tag}", "set_selection", {"refs": [top]}, record=False)
        bench.mcp_op(f"mcp extrude face join {tag}", "extrude", {"distance": 0.5 + 0.01 * r, "mode": "join"})
        bench.mcp_op(f"mcp undo extrude {tag}", "undo")
        bench.mcp_op(f"mcp set_selection bar {tag}", "set_selection", {"refs": [bar]}, record=False)
        bench.mcp_op(f"mcp boolean cut {tag}", "boolean_body", {"target_ref": top, "mode": "cut"})
        bench.mcp_op(f"mcp undo boolean {tag}", "undo")
        bench.mcp_op(f"mcp set_selection body {tag}", "set_selection", {"refs": [whole]}, record=False)
        bench.mcp_op(f"mcp move_body {tag}", "move_body", {"dx": 1.0 + 0.01 * r, "dy": 0.0, "dz": 0.0})
        bench.mcp_op(f"mcp undo move {tag}", "undo")
        bench.mcp_op(f"mcp set_selection body(2) {tag}", "set_selection", {"refs": [whole]}, record=False)
        bench.mcp_op(f"mcp mirror_body {tag}", "mirror_body", {"base_plane": "YZ"})
        bench.mcp_op(f"mcp undo mirror {tag}", "undo")
        bench.mcp_op(f"mcp set_selection bar(2) {tag}", "set_selection", {"refs": [bar]}, record=False)
        bench.mcp_op(f"mcp delete bar {tag}", "delete")
        bench.mcp_op(f"mcp undo delete {tag}", "undo")
        bench.mcp_op(f"mcp describe_document {tag}", "describe_document", expect_state=False)

        bench.ws_op(f"ws modify_selection edge {tag}", {"cmd": "modify_selection", "add": [edge], "remove": []})
        bench.ws_op(f"ws preview fillet {tag}", {"cmd": "preview", "verb": "fillet", "args": {"radius": 0.505 + 0.01 * r}, "preview_id": r}, until="preview")
        bench.ws_op(f"ws confirm fillet {tag}", {"cmd": "fillet", "radius": 0.505 + 0.01 * r})
        bench.ws_op(f"ws undo {tag}", {"cmd": "undo"})
        bench.ws_op(f"ws redo {tag}", {"cmd": "redo"})
        bench.ws_op(f"ws undo(2) {tag}", {"cmd": "undo"}, record=False)
        bench.ws_op(f"ws clear_selection {tag}", {"cmd": "clear_selection"}, record=False)

        bench.ws_op(f"camera zoom in(30面) {tag}", dict(overview, world_per_pixel=diag / 12000.0, focus={body: focus}), until="done")
        bench.ws_op(f"camera zoom out {tag}", overview, until="done")

    for r in range(1, rounds):
        print(f"\n### 読み込み(開き直し {r})", flush=True)
        bench.ws_op(f"load open_document #{r}", {"cmd": "open_document", "name": doc["name"]})
        bench.ws_op(f"load camera overview #{r}", overview, until="done")
        bench.mcp_op(f"mcp describe_document(after open) #{r}", "describe_document", expect_state=False, record=False)


def medians(rows: list) -> list:
    """同じ場面(`#n` を除いた名前)の、最初の回(冷えた)と2回目以降の中央値。"""
    groups: dict = {}
    for row in rows:
        base = row["case"].rsplit(" #", 1)[0]
        groups.setdefault(base, []).append(row)
    table = []
    for base, items in groups.items():
        def pick(key: str, sub: Optional[str] = None, rows_: list = items) -> list:
            values = []
            for row in rows_:
                value = row.get(key) if sub is None else (row.get(key) or {}).get(sub)
                if isinstance(value, (int, float)):
                    values.append(value)
            return values

        warm = items[1:] or items
        entry = {"case": base, "n": len(items), "errors": sum(1 for row in items if row.get("error"))}
        for key, sub, name in (("reply_ms", None, "reply"), ("first_state_ms", None, "state"), ("last_frame_ms", None, "last"),
                               ("loop", "busy_ms", "loop_busy"), ("loop", "max_block_ms", "max_block"), ("probes", "max_ms", "probe_max"),
                               ("next_call_ms", None, "next_call")):
            cold = pick(key, sub, items[:1])
            values = pick(key, sub, warm)
            entry[f"{name}_cold"] = cold[0] if cold else None
            entry[f"{name}_warm"] = round(statistics.median(values), 1) if values else None
        stage_names = sorted({name for row in warm for name in row["stages"]})
        entry["stages_warm"] = {name: round(statistics.median([row["stages"][name] for row in warm if name in row["stages"]]), 1) for name in stage_names}
        table.append(entry)
    return table


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--serve", action="store_true", help="(内部用)時計を差し込んだサーバとして立つ。残りの引数は mcp_server へ")
    ap.add_argument("--part", choices=("small", "big", "multi"), default="small")
    ap.add_argument("--rounds", type=int, default=4)
    ap.add_argument("--probe-interval", type=float, default=0.02)
    ap.add_argument("--only", default="")
    ap.add_argument("--out", type=Path, default=REPO / "output" / "bench_request_paths" / "paths.json")
    args, rest = ap.parse_known_args()
    if args.serve:
        raise SystemExit(serve(rest))

    work = Path(tempfile.mkdtemp(prefix="duocad-paths-"))
    models = work / "models"
    dist = work / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>bench</title>", encoding="utf-8")
    print(f"work={work} python={sys.version.split()[0]} part={args.part}", flush=True)
    doc = build_document(args.part, models)
    print(f"  文書: {json.dumps(doc, ensure_ascii=False)}", flush=True)
    port, http_port = free_port(), free_port()
    log = (work / "server.log").open("w", encoding="utf-8")
    server = subprocess.Popen(
        [sys.executable, "-X", "utf8", str(Path(__file__).resolve()), "--serve", "--no-stdio",
         "--port", str(port), "--http-port", str(http_port), "--models-dir", str(models),
         "--exports-dir", str(work / "export"), "--web-dist", str(dist)],
        stdout=log, stderr=subprocess.STDOUT, cwd=str(REPO),
    )
    screen = None
    rows: list = []
    try:
        tracer = Tracer(http_port)
        for _ in range(600):
            try:
                tracer.fetch(peek=True)
                break
            except OSError:
                time.sleep(0.1)
        screen = Screen(f"ws://127.0.0.1:{port}")
        screen.wait(lambda m: m["type"] == "state", 0, timeout=60)
        time.sleep(3.0)  # 子の先立て(`kernel_worker.warm_up_in_background`)が済むまで
        bench = Bench(screen, Mcp(http_port), tracer, args.probe_interval, args.only)
        try:
            run_scenes(bench, doc, args.rounds)
        finally:
            rows = bench.rows
    finally:
        if screen is not None:
            screen.close()
        server.send_signal(signal.SIGINT)
        try:
            server.wait(timeout=20)
        except subprocess.TimeoutExpired:
            server.kill()
        log.close()
        table = medians(rows)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps({"python": sys.version, "part": args.part, "document": doc, "summary": table, "rows": rows},
                                       ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\nwrote {args.out}")
        print("\n| 場面 | 返事 冷/温 | 配信が届く 冷/温 | ループ占有 温 | 最長の塞ぎ 温 | 照会の最大 温 | 失敗 |")
        print("|---|---|---|---|---|---|---|")
        for entry in table:
            print(f"| {entry['case']} | {entry['reply_cold']} / {entry['reply_warm']} | {entry['state_cold']} / {entry['state_warm']} | "
                  f"{entry['loop_busy_warm']} | {entry['max_block_warm']} | {entry['probe_max_warm']} | {entry['errors']} |")
        shutil.copy(work / "server.log", args.out.with_suffix(".server.log"))
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
