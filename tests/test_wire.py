"""状態とプレビューの配信の形(`duocad/net/wire.py`): 1枚のバイナリフレームと、接続が既に持っているものを省く規則。
issue #166・#170。読む側は `web/src/wire.ts`(画面)と `tests/session_support.FakeConnection`。"""
from __future__ import annotations

import asyncio
import base64
import copy
import json
import struct
from pathlib import Path

from duocad.net import wire
from duocad.server import Server
from node_support import run_node
from session_support import FakeConnection, browser, session_with_box

TS_EVALUATOR = Path(__file__).resolve().parent / "eval_wire.mjs"

#: 箱の面1・2を細かく測る画面から、面1・3を細かく測る画面へ(面2・3だけ測り方が変わる)
CAMERA = {"cmd": "set_view_resolution", "world_per_pixel": 0.02, "size_hint": 400.0}


def _float32(value: float) -> float:
    return struct.unpack("<f", struct.pack("<f", value))[0]


def _with_sketch():
    session = session_with_box()
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_rectangle(sketch_id, [40.0, 0.0], 10.0, 10.0)
    return session


def _camera_then_selection(session):
    async def script(send):
        await send(dict(CAMERA, focus={"box": [1, 2]}))
        await send(dict(CAMERA, focus={"box": [1, 3]}))
        await send({"cmd": "modify_selection", "add": [{"body": "box", "kind": "face", "index": 1}], "remove": []})

    return browser(session, script, connections=1)[1][0]


def _state_frames(client: FakeConnection) -> list:
    return [wire.decode(raw) for raw in client.raw if isinstance(raw, bytes) and wire.decode(raw)["type"] == "state"]


def test_a_frame_round_trips_with_the_rounding_the_browser_used() -> None:
    """落ちたら、三角形の座標が画面でこれまで(JSON の数を `Float32Array` に写していた)と違う値になり、STL の書き出しと
    画面の三角形のビット単位の一致が崩れる(README「ボディの種別」)。"""
    payload = {
        "type": "preview",
        "bodies": [{"geometry": {"faces": [{"index": 1, "positions": [0.1, 1.0 / 3.0, -2.5e-7], "indices": [0, 1, 2]}]}}],
        "label": "日本語",
    }
    before = copy.deepcopy(payload)
    frame = wire.encode(payload)
    assert payload == before, "配りものを書き換えた(覚えている面の三角形を壊す)"
    (length,) = struct.unpack_from("<I", frame, 0)
    assert (4 + length) % 4 == 0, "配列の生バイトが 4 バイト境界から始まらない"
    face = wire.decode(frame)["bodies"][0]["geometry"]["faces"][0]
    assert face["positions"] == [_float32(value) for value in before["bodies"][0]["geometry"]["faces"][0]["positions"]]
    assert face["indices"] == [0, 1, 2]
    assert wire.decode(frame)["label"] == "日本語"


def test_only_the_faces_measured_again_travel() -> None:
    """落ちたら、カメラで測り方の変わった面が一部でも、ボディの全面の三角形を送り直す(PC ケースで 1 回 4.5 MB)。"""
    client = _camera_then_selection(session_with_box())
    after_swap = _state_frames(client)[2]
    faces = after_swap["delta"]["bodies"][0]["geometry"]["faces"]
    assert sorted(face["index"] for face in faces if "positions" in face) == [2, 3]
    restored = [payload for payload in client.payloads if payload["type"] == "state"][1]
    assert all("positions" in face for face in restored["delta"]["bodies"][0]["geometry"]["faces"])


def test_an_unchanged_log_and_sketch_are_left_out() -> None:
    """落ちたら、領域を1つ選ぶだけで全操作ログと全スケッチ(領域の三角形込み)が毎回流れる(長方形 150 個で 1 回 123 KB)。"""
    session = _with_sketch()
    client = _camera_then_selection(session)
    selection = _state_frames(client)[-1]
    assert "log" not in selection["state"]
    assert selection["state"]["sketches"] == [{"sketch_id": session.document.sketches[0].id, "unchanged": True}]
    restored = [payload for payload in client.payloads if payload["type"] == "state"][-1]["state"]
    assert restored["log"] == session.state_meta()["log"]
    assert restored["sketches"][0]["sketch_id"] == session.document.sketches[0].id
    assert restored["sketches"][0]["regions"], "省かれたスケッチを埋め戻していない"


def test_the_browser_decoder_restores_what_the_server_meant() -> None:
    """落ちたら、画面の読み方(`web/src/wire.ts`)がサーバの省き方・フレームの形と食い違い、面が欠けるか別の面の三角形を描く。"""
    session = _with_sketch()
    sketch_id = session.document.sketches[0].id

    async def script(send):
        await send(dict(CAMERA, focus={"box": [1, 2]}))
        await send(dict(CAMERA, focus={"box": [1, 3]}))
        await send({"cmd": "modify_selection", "add": [{"body": "box", "kind": "face", "index": 1}], "remove": []})
        await send({"cmd": "add_rectangle", "sketch_id": sketch_id, "corner": [60.0, 0.0], "width": 5.0, "height": 5.0})
        await send({"cmd": "undo"})

    client = browser(session, script, connections=1)[1][0]
    frames = [
        {"binary": True, "data": base64.b64encode(raw).decode("ascii")} if isinstance(raw, bytes)
        else {"binary": False, "data": raw}
        for raw in client.raw
    ]
    fresh = FakeConnection()
    for raw in client.raw:
        asyncio.run(fresh.send(raw))
    from_ts = run_node(TS_EVALUATOR, "画面の読み方", stdin=frames, json_out=True, timeout=120)
    assert from_ts == json.loads(json.dumps(fresh.payloads))


def test_a_new_connection_receives_everything_again() -> None:
    """落ちたら、再読み込み・繋ぎ直した画面へ前の接続が受け取った面を省いて送り、形が欠ける。"""
    session = session_with_box()

    async def scenario():
        server = Server(session)
        server.bind_loop(asyncio.get_running_loop())
        first = FakeConnection()
        server.clients.add(first)
        await server._push(first, applied=None, only=first)
        await server._push(first, applied=None, only=first)
        second = FakeConnection()
        server.clients.add(second)
        await server._push(second, applied=None, only=second)
        return first, second

    first, second = asyncio.run(scenario())
    again = wire.decode(first.raw[-1])
    assert again["delta"]["bodies"] == [] and "log" not in again["state"], "同じ接続へは何も送り直さない"
    fresh = wire.decode(second.raw[-1])
    assert "log" in fresh["state"]
    assert all("positions" in face for face in fresh["delta"]["bodies"][0]["geometry"]["faces"])


def test_an_operation_sends_only_the_end_of_the_log() -> None:
    """落ちたら、操作1回ごとに全操作ログが流れる(長方形 150 個のスケッチで 1 回 33 KB)か、undo で消えた末尾が画面に残る。"""
    session = _with_sketch()
    sketch_id = session.document.sketches[0].id
    before = len(session.state_meta()["log"])

    async def script(send):
        await send({"cmd": "add_rectangle", "sketch_id": sketch_id, "corner": [60.0, 0.0], "width": 5.0, "height": 5.0})
        await send({"cmd": "undo"})

    client = browser(session, script, connections=1)[1][0]
    added, undone = _state_frames(client)[-2:]
    assert added["state"]["log_keep"] == before and len(added["state"]["log_tail"]) == 1
    assert undone["state"]["log_keep"] == before and undone["state"]["log_tail"] == []
    restored = [payload for payload in client.payloads if payload["type"] == "state"]
    assert len(restored[-2]["state"]["log"]) == before + 1
    assert restored[-1]["state"]["log"] == session.state_meta()["log"]
