"""カメラの要求で足りなくなった面を、ループの外で測る(`net/ws.py` の `Server._schedule_refinement`)。"""
from __future__ import annotations

import asyncio
import json
import threading
import time

from duocad import kernel
from duocad.net import wire
from duocad.server import Server
from session_support import FakeConnection, session_with_box

CAMERA = {"cmd": "set_view_resolution", "world_per_pixel": 0.02, "size_hint": 400.0}


def _slow_measurement(monkeypatch, gate: threading.Event):
    """ワーカーでの測り直しを `gate` が開くまで止める(ループの上で測る経路は止めない)。"""
    real = kernel.tessellate_faces

    def measured(shape, groups, **options):
        if threading.current_thread() is not threading.main_thread():
            gate.wait(10)
        return real(shape, groups, **options)

    monkeypatch.setattr(kernel, "tessellate_faces", measured)


def _bodies(frame: dict) -> list:
    return [body["body_id"] for body in frame["delta"]["bodies"]]


def test_a_click_during_the_refinement_is_answered_without_waiting(monkeypatch) -> None:
    """落ちたら、ズームの後の測り直し(733 面で 0.5〜0.9 秒)が終わるまで、その間のクリックの返事が待たされる。"""
    session = session_with_box()
    gate = threading.Event()
    _slow_measurement(monkeypatch, gate)

    async def scenario():
        server = Server(session)
        server.bind_loop(asyncio.get_running_loop())
        client = FakeConnection()
        server.clients.add(client)
        await server._push(client, applied=None, only=client)
        await server._dispatch(client, json.dumps(dict(CAMERA, focus={"box": [1, 2]})))
        started = time.perf_counter()
        await server._dispatch(client, json.dumps({"cmd": "modify_selection", "add": [{"body": "box", "kind": "face", "index": 1}], "remove": []}))
        answered = time.perf_counter() - started
        frames_before = [wire.decode(raw) for raw in client.raw]
        gate.set()
        await server.settled()
        frames_after = [wire.decode(raw) for raw in client.raw]
        return answered, frames_before, frames_after

    answered, before, after = asyncio.run(scenario())
    assert answered < 1.0, "クリックが測り直しを待った"
    assert before[-1]["state"]["selection"]["count"] == 1
    assert all("box" not in _bodies(frame) for frame in before[1:]), "測っている最中のボディの三角形を送った"
    assert "box" in _bodies(after[-1]), "測り終えた三角形を配っていない"


def test_a_body_whose_shape_changes_during_the_refinement_is_delivered_at_once(monkeypatch) -> None:
    """落ちたら、測り直しの最中に操作で形が変わったボディの新しい形が、測り直しが終わるまで画面に出ない(古い形が残る)。"""
    session = session_with_box()
    gate = threading.Event()
    _slow_measurement(monkeypatch, gate)

    async def scenario():
        server = Server(session)
        server.bind_loop(asyncio.get_running_loop())
        client = FakeConnection()
        server.clients.add(client)
        await server._push(client, applied=None, only=client)
        await server._dispatch(client, json.dumps(dict(CAMERA, focus={"box": [1, 2]})))
        await server._dispatch(client, json.dumps({"cmd": "modify_selection", "add": [{"body": "box", "kind": "edge", "index": 1}], "remove": []}))
        await server._dispatch(client, json.dumps({"cmd": "chamfer", "distance": 2.0}))
        frames = [wire.decode(raw) for raw in client.raw]
        gate.set()
        await server.settled()
        return frames

    frames = asyncio.run(scenario())
    applied = next(frame for frame in frames if frame.get("applied", {}).get("verb") == "chamfer")
    assert "box" in _bodies(applied), "形が変わったボディを送らずにおいた"
    assert applied["delta"]["bodies"][0]["face_count"] == session.geometry("box")["face_count"]


def test_the_refined_faces_match_what_the_loop_would_measure(monkeypatch) -> None:
    """落ちたら、写しの上で測った三角形が元の形の別の面に入る(添字の取り違え)。"""
    session = session_with_box()
    twin = session_with_box()

    async def scenario():
        server = Server(session)
        server.bind_loop(asyncio.get_running_loop())
        client = FakeConnection()
        server.clients.add(client)
        await server._push(client, applied=None, only=client)
        await server._dispatch(client, json.dumps(dict(CAMERA, focus={"box": [1, 2]})))
        await server.settled()

    asyncio.run(scenario())
    twin.set_view_resolution(CAMERA["world_per_pixel"], CAMERA["size_hint"], {"box": [1, 2]})
    ours = session.geometry("box")["faces"]
    theirs = twin.geometry("box")["faces"]
    assert [(face["index"], face["detail"], face["positions"]) for face in ours] == [
        (face["index"], face["detail"], face["positions"]) for face in theirs
    ]


def test_a_camera_request_during_the_refinement_is_measured_right_after_it(monkeypatch) -> None:
    """落ちたら、測り直しの最中にまたカメラが動いたとき、その向きの面が次にカメラが動くまで測られず、画面が粗いまま残る。"""
    session = session_with_box()
    gate = threading.Event()
    _slow_measurement(monkeypatch, gate)

    async def scenario():
        server = Server(session)
        server.bind_loop(asyncio.get_running_loop())
        client = FakeConnection()
        server.clients.add(client)
        await server._push(client, applied=None, only=client)
        await server._dispatch(client, json.dumps(dict(CAMERA, focus={"box": [1, 2]})))
        await server._dispatch(client, json.dumps(dict(CAMERA, focus={"box": [3, 4]})))
        gate.set()
        await server.settled()
        return [payload for payload in client.payloads if payload.get("type") == "state" and payload["delta"]["bodies"]]

    states = asyncio.run(scenario())
    last = states[-1]["delta"]["bodies"][0]["geometry"]
    fine = sorted(face["index"] for face in last["faces"] if face["detail"] == "fine")
    assert fine == [3, 4], f"後の向きで測った三角形が届いていない: {fine}"
