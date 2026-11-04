"""要求の経路の計測(`tools/bench_request_paths.py`)と、undo / redo と配信の競合の検査(`tools/probe_undo_delivery.py`)の道具。

落ちたら、検査が食い違いを見逃す(古い形の三角形を描いている画面を一致と言う)か、計測が工程を取り違えて報告する。
"""
from __future__ import annotations

import asyncio
import sys

from duocad import view
from duocad.server import Server
from script_support import load_tool
from session_support import session_with_box


def _tool(monkeypatch, name: str):
    monkeypatch.setattr(sys, "path", list(sys.path))  # 道具は読み込むときに `tools/` を前へ足す
    return load_tool(monkeypatch, name)


def _run_box_scene(probe) -> list:
    session = session_with_box()
    diag = session.geometry("box")["bbox"]["diagonal"]

    async def scene() -> list:
        server = Server(session)
        server.bind_loop(asyncio.get_running_loop())
        lab = probe.Lab(session, server, diag)
        screen = await lab.connect("A")
        lab.scene = "box"
        before = probe.fingerprint(session.document.get_body("box").shape)
        await lab.step("select", lambda: lab.ws(screen, {"cmd": "modify_selection", "add": [{"body": "box", "kind": "edge", "index": 1}], "remove": []}))
        await lab.step("chamfer", lambda: lab.ws(screen, {"cmd": "chamfer", "distance": 2.0}))
        await lab.step("undo", lambda: lab.ws(screen, {"cmd": "undo"}), before=before, body_id="box")
        await lab.step("redo", lambda: lab.ws(screen, {"cmd": "redo"}))
        await lab.disconnect(screen)
        await asyncio.gather(*lab.tasks, return_exceptions=True)
        return lab.results

    return asyncio.run(scene())


def test_the_probe_finds_no_mismatch_in_a_plain_undo_redo(monkeypatch) -> None:
    """落ちたら、検査が正しい配信を食い違いと言う(報告の「食い違い 0」が当てにならない)。"""
    probe = _tool(monkeypatch, "probe_undo_delivery")
    results = _run_box_scene(probe)
    assert [row["step"] for row in results] == ["select", "chamfer", "undo", "redo"]
    assert all(not row["problems"] and not row["violations"] and "error" not in row for row in results), results


def test_the_probe_flags_triangles_left_over_from_an_older_shape(monkeypatch) -> None:
    """落ちたら、形が替わったのに版が上がらず古い三角形が画面に残る食い違いを、検査が見逃す。"""
    probe = _tool(monkeypatch, "probe_undo_delivery")
    real = view.DocumentView.body_revision
    frozen: dict = {}

    def stale(self, body_id: str) -> int:
        if body_id not in frozen:
            frozen[body_id] = real(self, body_id)
        return frozen[body_id]

    monkeypatch.setattr(view.DocumentView, "body_revision", stale)
    results = _run_box_scene(probe)
    chamfer = next(row for row in results if row["step"] == "chamfer")
    assert chamfer["violations"] and chamfer["problems"]


def test_the_bench_splits_an_mcp_request_into_its_stages(monkeypatch) -> None:
    """落ちたら、MCP の1要求の内訳(ループの待ち・道具・配信)を別の工程の時間で報告する。"""
    bench = _tool(monkeypatch, "bench_request_paths")

    def event(name: str, thread: str, t0: float, t1: float) -> dict:
        return {"n": name, "th": thread, "t0": t0, "t1": t1}

    http_thread = "Thread-3 (process_request_thread)"
    events = [
        event("http.post", http_thread, 1.001, 1.120),
        event("mcp.handle", http_thread, 1.002, 1.110),
        event("on_loop", http_thread, 1.003, 1.105),
        event("tool.call", "MainThread", 1.010, 1.100),
        event("push", "MainThread", 1.101, 1.300),
        event("loop", "MainThread", 1.010, 1.100),
        event("loop", "MainThread", 1.101, 1.300),
    ]
    frames = [{"t": 1.302, "type": "state", "bytes": 10}]
    row = bench.summarize("mcp fillet", "mcp", 1.000, 1.121, 1.4, frames, events, [(1.05, 1.31)], 0.29, None)
    assert row["stages"] == {
        "http_in": 1.0, "lock_parse": 1.0, "loop_queue": 7.0, "tool": 90.0, "loop_back": 5.0, "reply_out": 10.0,
        "client_in": 1.0, "push_wait": 1.0, "push": 199.0, "push_to_screen": 2.0,
    }
    assert row["loop"]["max_block_ms"] == 199.0
    assert row["probes"]["max_ms"] == 260.0
    assert row["first_state_ms"] == 302.0
