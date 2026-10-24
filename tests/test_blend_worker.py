"""辺の編集の子プロセス境界: 期限・復旧・順序・由来・期限なしの再生。"""
from __future__ import annotations

import asyncio
import json
import math
import sys
import time

import pytest
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeSphere
from OCP.gp import gp_Ax1, gp_Ax2, gp_Dir, gp_Pnt

from duocad import kernel, kernel_worker, query, worker_process
from duocad.document import Body
from duocad.mcp_server import DocumentTools, McpServer
from duocad.verbs._common import blend_edges
from document_support import replay_through_json
from mcp_support import call_error
from session_support import select
from test_fillet_tight import _elliptic_post
from test_fillet_tight_surface import _outline
from test_preview_off_the_loop import _run


def _state(session):
    doc = session.document
    return (json.dumps(doc.log.to_json(), sort_keys=True),
            tuple((body.id, body.shape) for body in doc.bodies), tuple(doc.selection.items))


def _box(session):
    body = Body(id="box", name="box", kind="solid", shape=BRepPrimAPI_MakeBox(20, 20, 20).Shape())
    session.document.add_body(body)
    select(session, [{"body": body.id, "kind": "edge", "index": 1}])
    return body


@pytest.mark.parametrize("route", ["apply", "mcp", "preview", "preview_job", "ws_preview"])
def test_the_slow_dome_is_refused_without_changes_and_the_worker_recovers(session, monkeypatch, route):
    # 実測: この球面の向きは、本来の Build が 600 秒でも返らなかった。
    axes = gp_Ax2(gp_Pnt(0, 0, -20), gp_Dir(0, math.sin(0.03), math.cos(0.03)))
    axes.Rotate(gp_Ax1(axes.Location(), axes.Direction()), math.pi)
    dome = BRepAlgoAPI_Common(_elliptic_post(6, 2, 8), BRepPrimAPI_MakeSphere(axes, 26).Shape()).Shape()
    session.document.add_body(Body(id="dome", name="dome", kind="solid", shape=dome))
    select(session, [{"body": "dome", "kind": "edge", "index": index} for index in _outline(dome, True)])
    kernel_worker.run("fillet", BRepPrimAPI_MakeBox(20, 20, 20).Shape(), {"values": [[1, 2.0]]},
                      op="blend_edges", timeout=20)
    worker = kernel_worker._WORKER
    starts = worker.starts
    before = _state(session)
    monkeypatch.setattr(kernel_worker, "INTERACTIVE_TIMEOUT_S", 3.0)
    started = time.monotonic()
    if route == "mcp":
        error = call_error(McpServer(DocumentTools(session)), "fillet", {"radius": 1.5})
        assert error["code"] == "invalid_argument"
        assert "fillet: 3 秒以内に返事が無かった" in error["message"]
    elif route == "ws_preview":
        async def script(server, client):
            started = time.monotonic()
            await server._dispatch(client, json.dumps({"cmd": "preview", "verb": "fillet", "args": {"radius": 1.5}, "preview_id": 1}))
            await asyncio.sleep(0.2)
            await server._dispatch(client, json.dumps({"cmd": "inspect_selection", "request_id": 9}))
            assert time.monotonic() - started < 1.5
            assert client.of_type("selection_info")
            assert not client.of_type("error")

        _, client = _run(session, script)
        assert "fillet: 3 秒以内に返事が無かった" in client.of_type("error")[0]["message"]
        assert not client.of_type("preview")
    else:
        with pytest.raises(ValueError, match="fillet: 3 秒以内に返事が無かった"):
            if route == "preview_job":
                session.preview_job("fillet", {"radius": 1.5}).run()
            elif route == "preview":
                session.preview("fillet", {"radius": 1.5})
            else:
                session.apply("fillet", {"radius": 1.5})
    assert 2.9 <= time.monotonic() - started < 10
    assert _state(session) == before
    assert worker._process is None
    monkeypatch.setattr(kernel_worker, "INTERACTIVE_TIMEOUT_S", 20.0)
    body = _box(session)
    session.apply("fillet", {"radius": 2.0})
    assert query.volume(body.shape) < 8000
    assert worker.starts == starts + 1


@pytest.mark.parametrize("verb,value_name", [("fillet", "radius"), ("chamfer", "distance")])
def test_order_and_results_survive_the_worker_and_replay_has_no_deadline(session, monkeypatch, verb, value_name):
    body = _box(session)
    original = body.shape
    select(session, [{"body": body.id, "kind": "edge", "index": index} for index in [5, 1]])
    values = {5: 1.0, 1: 2.0}
    expected = kernel._blend_edges(original, values, verb=verb)
    calls = []
    run = kernel_worker.run

    def observe(verb, shape, arguments, **kwargs):
        calls.append((arguments, kwargs))
        return run(verb, shape, arguments, **kwargs)

    monkeypatch.setattr(kernel_worker, "run", observe)
    session.apply(verb, {value_name: [1.0, 2.0]})
    assert calls == [({"values": ((5, 1.0), (1, 2.0))},
                      {"op": "blend_edges", "timeout": kernel_worker.INTERACTIVE_TIMEOUT_S})]
    assert session.document.log.entries[-1].provenance == expected["provenance"]
    assert query.describe_faces(body.shape) == query.describe_faces(expected["shape"])
    assert query.volume(body.shape) == pytest.approx(expected["volume_after"], rel=1e-12)
    session.undo()
    assert body.shape is original
    monkeypatch.setattr(kernel, "_RESULTS", type(kernel._RESULTS)())
    monkeypatch.setattr(kernel_worker, "INTERACTIVE_TIMEOUT_S", 1e-6)
    session.redo()
    restored = type(session.document)()
    restored.add_body(Body(id=body.id, name=body.name, kind="solid", shape=kernel.copy_shape(original)))
    replay_through_json(session.document, into=restored)
    assert [kwargs["timeout"] for _, kwargs in calls[1:]] == [None, None]
    assert query.describe_faces(restored.get_body(body.id).shape) == query.describe_faces(body.shape)
    assert query.volume(restored.get_body(body.id).shape) == pytest.approx(expected["volume_after"], rel=1e-12)


@pytest.mark.parametrize("verb,value_name", [("fillet", "radius"), ("chamfer", "distance")])
def test_a_child_failure_keeps_the_document_and_log_and_allows_retry(session, monkeypatch, tmp_path, verb, value_name):
    child = '''
import os, sys
from pathlib import Path
marker = Path(sys.argv[1])
if not marker.exists():
    marker.write_text(sys.stdin.readline(), encoding="utf-8")
    os._exit(3)
from duocad import kernel_worker
kernel_worker.child_main()
'''
    _box(session)
    before = _state(session)
    marker = tmp_path / "request"
    with worker_process.WorkerProcess([sys.executable, "-c", child, str(marker)]) as worker:
        monkeypatch.setattr(kernel_worker, "_WORKER", worker)
        with pytest.raises(ValueError, match=f"{verb}: .*途中で終了"):
            session.apply(verb, {value_name: 2.0})
        assert json.loads(marker.read_text(encoding="utf-8"))["op"] == "blend_edges"
        assert _state(session) == before
        assert worker._process is None
        session.apply(verb, {value_name: 2.0})
        assert worker.starts == 2


@pytest.mark.parametrize("verb,value_name", [("fillet", "radius"), ("chamfer", "distance")])
def test_adopted_preview_is_reused_for_commit_and_redo(session, monkeypatch, verb, value_name):
    body = _box(session)
    job = session.preview_job(verb, {value_name: 2.0})
    preview = job.run()
    job.adopt()

    def unexpected(*args, **kwargs):
        pytest.fail("確定・redo がプレビューで計算した結果を使わなかった")

    monkeypatch.setattr(kernel_worker, "run", unexpected)
    session.apply(verb, {value_name: 2.0})
    committed = body.shape
    assert query.volume(committed) == pytest.approx(preview["bodies"][0]["volume"])
    session.undo()
    session.redo()
    assert body.shape is committed


def test_face_edits_and_blends_share_one_worker():
    shape = BRepPrimAPI_MakeBox(20, 20, 20).Shape()
    kernel_worker.run("offset_faces", shape, {"faces": [1], "distance": 1.0}, timeout=20)
    worker = kernel_worker._WORKER
    starts = worker.starts
    blend_edges(shape, {1: 2.0}, verb="chamfer")
    assert kernel_worker._WORKER is worker and worker.starts == starts


def test_worker_imports_the_worktree_code():
    from duocad.paths import REPO_ROOT

    child = "from duocad import kernel_worker; kernel_worker.worker_process.run_child(lambda message: {'root': str(kernel_worker.__file__)})"
    with worker_process.WorkerProcess([sys.executable, "-c", child]) as worker:
        result = worker.request({}, 20)
    assert result["root"] == str(REPO_ROOT / "duocad" / "kernel_worker.py")
