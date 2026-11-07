"""背景の測り直しが、ボディの形ごとの写しを持ち回す(`net/refinement.py`)。"""
from __future__ import annotations

import asyncio
import json

from duocad import kernel
from duocad.server import Server
from session_support import FakeConnection, session_with_box


def _camera(world_per_pixel: float, focus: list) -> str:
    return json.dumps({"cmd": "set_view_resolution", "world_per_pixel": world_per_pixel, "size_hint": 400.0, "focus": {"box": focus}})


def test_the_worker_copy_is_made_once_per_shape_and_still_measures_the_same(monkeypatch) -> None:
    """落ちたら、ズームのたびに全ボディの写しをループの上で作り直す(27 部品で 1 回 60〜90 ms)か、
    持ち回した写しの上で測った三角形がループの測ったものと食い違う。"""
    session = session_with_box()
    twin = session_with_box()
    copies: list = []
    real = kernel.copy_shape
    monkeypatch.setattr(kernel, "copy_shape", lambda shape: copies.append(shape) or real(shape))

    async def scenario():
        server = Server(session)
        server.bind_loop(asyncio.get_running_loop())
        client = FakeConnection()
        server.clients.add(client)
        await server._push(client, applied=None, only=client)
        for per_pixel, focus in ((0.02, [1, 2]), (0.01, [1, 2, 3]), (0.03, [4])):
            await server._dispatch(client, _camera(per_pixel, focus))
            await server.settled()

    asyncio.run(scenario())
    assert len(copies) == 1, f"写しを {len(copies)} 回作った"
    twin.set_view_resolution(0.03, 400.0, {"box": [4]})
    ours = session.geometry("box")["faces"]
    theirs = twin.geometry("box")["faces"]
    assert [(face["index"], face["detail"], face["positions"]) for face in ours] == [
        (face["index"], face["detail"], face["positions"]) for face in theirs
    ]
