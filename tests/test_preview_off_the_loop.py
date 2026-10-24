"""プレビューをワーカースレッドで計算し、その間もイベントループが他の要求を捌く(issue #168)。受信待ち行列の集約。"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from duocad import kernel, kernel_worker
from duocad.net.ws import Server, _Inbox
from duocad.server import Session
from session_support import FakeConnection, box as _box, select as _select

from duocad import (
    brep_geometry as _brep_geometry,
)

RADIUS = 2.0


def test_the_inbox_keeps_only_the_newest_queued_preview_and_resolution() -> None:
    """落ちたら、打ち直した半径の数だけ1秒のプレビューが順に走る・カメラを止めた回数だけ 27 部品を測り直す。"""

    async def drain() -> list:
        inbox = _Inbox()
        for message in (
            {"cmd": "preview", "verb": "fillet", "args": {"radius": 1}},
            {"cmd": "set_view_resolution", "world_per_pixel": 1.0},
            {"cmd": "modify_selection", "add": []},
            {"cmd": "preview", "verb": "fillet", "args": {"radius": 2}},
            {"cmd": "set_view_resolution", "world_per_pixel": 2.0},
        ):
            inbox.put(json.dumps(message))
        inbox.close()
        out = []
        while (raw := await inbox.get()) is not None:
            out.append(json.loads(raw))
        return out

    got = asyncio.run(drain())
    assert [item["cmd"] for item in got] == ["modify_selection", "preview", "set_view_resolution"]
    assert got[1]["args"]["radius"] == 2 and got[2]["world_per_pixel"] == 2.0


def test_a_job_on_the_fork_gives_the_same_ghost_and_leaves_the_document_alone(session: Session) -> None:
    """落ちたら、ワーカーで写しの上で計算したゴーストがその場の投機実行と違う、または計算が元の文書を変える。"""
    body, _ = _box(session)
    _select(session, [{"body": body.id, "kind": "edge", "index": 1}])
    shape, entries, selection = body.shape, len(session.document.log.entries), list(session.document.selection.items)
    direct = session.preview("fillet", {"radius": RADIUS})
    job = session.preview_job("fillet", {"radius": RADIUS})
    forked = job.run()
    assert body.shape is shape and len(session.document.log.entries) == entries
    assert session.document.selection.items == selection
    assert [item["body_id"] for item in forked["bodies"]] == [item["body_id"] for item in direct["bodies"]]
    for ours, theirs in zip(forked["bodies"], direct["bodies"]):
        assert ours["face_count"] == theirs["face_count"]
        assert ours["volume"] == pytest.approx(theirs["volume"], rel=1e-12)
        assert [len(face["indices"]) for face in ours["geometry"]["faces"]] == [
            len(face["indices"]) for face in theirs["geometry"]["faces"]
        ]


def test_the_commit_reuses_what_the_worker_computed(session: Session, monkeypatch) -> None:
    """落ちたら、ワーカーが写しの上で求めたフィレットを確定が使えず、同じ計算をもう一度する(#169 の使い回しが消える)。"""
    calls = []
    real = kernel_worker.run
    monkeypatch.setattr(kernel_worker, "run", lambda verb, *args, **kwargs: calls.append(verb) or real(verb, *args, **kwargs))
    monkeypatch.setattr(kernel, "_RESULTS", type(kernel._RESULTS)())
    body, _ = _box(session)
    _select(session, [{"body": body.id, "kind": "edge", "index": 1}])
    job = session.preview_job("fillet", {"radius": RADIUS})
    preview = job.run()
    job.adopt()
    session.fillet(RADIUS)
    assert calls == ["fillet"]
    assert _brep_geometry.volume(body.shape) == pytest.approx(preview["bodies"][0]["volume"])


def _slow_blends(monkeypatch, seconds: float) -> None:
    real = kernel_worker.run

    def slow(verb, *args, **kwargs):
        time.sleep(seconds)
        return real(verb, *args, **kwargs)

    monkeypatch.setattr(kernel_worker, "run", slow)
    monkeypatch.setattr(kernel, "_RESULTS", type(kernel._RESULTS)())


def _run(session: Session, script):
    async def scenario():
        server = Server(session)
        server.bind_loop(asyncio.get_running_loop())
        client = FakeConnection()
        server.clients.add(client)
        await server._push(client, applied=None, only=client)
        client.payloads.clear()
        result = await script(server, client)
        while server._pending_pushes:
            await asyncio.gather(*list(server._pending_pushes), return_exceptions=True)
        return result, client

    return asyncio.run(scenario())


def test_light_requests_are_answered_while_the_preview_computes(session: Session, monkeypatch) -> None:
    """落ちたら、プレビューの計算(大きな部品で1秒)の間、クリック・選択の解除などの軽い要求まで止まる。"""
    body, _ = _box(session)
    _select(session, [{"body": body.id, "kind": "edge", "index": 1}])
    _slow_blends(monkeypatch, 0.5)

    async def script(server, client):
        started = time.perf_counter()
        await server._dispatch(client, json.dumps({"cmd": "preview", "verb": "fillet", "args": {"radius": RADIUS}, "preview_id": 1, "ack_id": 5}))
        await server._dispatch(client, json.dumps({"cmd": "inspect_selection", "request_id": 9, "ack_id": 6}))
        return time.perf_counter() - started

    (elapsed, client) = _run(session, script)
    types = [payload["type"] for payload in client.payloads]
    assert elapsed < 0.3, f"軽い要求がプレビューの計算を待った({elapsed:.2f} 秒)"
    assert types.index("selection_info") < types.index("preview")
    assert client.of_type("preview")[0]["preview_id"] == 1 and client.of_type("preview")[0]["bodies"]
    # 画面は `done` の番号「以下」をまとめて消すので、プレビュー(5)が済むまで後の要求(6)の `done` も出さない
    assert [item["ack_id"] for item in client.of_type("done")] == [6]
    assert types.index("done") > types.index("preview")


def test_a_superseded_preview_result_is_not_sent(session: Session, monkeypatch) -> None:
    """落ちたら、打ち直す前の半径のゴースト(古い結果)まで送られ、画面が解析して捨てる(大きな部品で 338 KB)。"""
    body, _ = _box(session)
    _select(session, [{"body": body.id, "kind": "edge", "index": 1}])
    _slow_blends(monkeypatch, 0.2)

    async def script(server, client):
        for preview_id, radius in ((1, 1.0), (2, 1.5), (3, 2.0)):
            await server._dispatch(client, json.dumps({"cmd": "preview", "verb": "fillet", "args": {"radius": radius}, "preview_id": preview_id, "ack_id": preview_id}))

    (_none, client) = _run(session, script)
    assert [item["preview_id"] for item in client.of_type("preview")] == [3]
    assert client.of_type("done")[-1]["ack_id"] == 3


def test_a_failed_preview_still_reports_and_acknowledges(session: Session) -> None:
    """落ちたら、ワーカーで失敗したプレビューの理由が画面へ届かない・待ちの印(`done`)が残り続ける。"""
    body, _ = _box(session)
    _select(session, [{"body": body.id, "kind": "edge", "index": 1}])

    async def script(server, client):
        await server._dispatch(client, json.dumps({"cmd": "preview", "verb": "fillet", "args": {"radius": 1000.0}, "preview_id": 1, "ack_id": 4, "client_op": 8}))

    (_none, client) = _run(session, script)
    errors = client.of_type("error")
    assert errors and errors[0]["where"] == "preview" and errors[0]["client_op"] == 8
    assert client.of_type("preview") == [] and [item["ack_id"] for item in client.of_type("done")] == [4]


def test_an_inline_preview_supersedes_a_running_worker_preview(session: Session, monkeypatch) -> None:
    """落ちたら、ワーカーで計算中の古いプレビューの結果・失敗が、その場で計算した新しいプレビューの後に届き、画面が新しいゴーストを消す。"""
    body, _ = _box(session)
    _select(session, [{"body": body.id, "kind": "edge", "index": 1}])
    _slow_blends(monkeypatch, 0.3)

    async def script(server, client):
        await server._dispatch(client, json.dumps({"cmd": "preview", "verb": "fillet", "args": {"radius": 1000.0}, "preview_id": 1, "ack_id": 1}))
        # 次のプレビューはその場で計算する経路(`preview_session` の verb と同じ)
        monkeypatch.setattr(session, "preview_job", lambda *_args, **_kwargs: None)
        await server._dispatch(client, json.dumps({"cmd": "preview", "verb": "fillet", "args": {"radius": RADIUS}, "preview_id": 2, "ack_id": 2}))

    (_none, client) = _run(session, script)
    assert [item["preview_id"] for item in client.of_type("preview")] == [2]
    assert client.of_type("error") == []
    assert client.of_type("done")[-1]["ack_id"] == 2


def test_a_preview_finishing_after_a_disconnect_does_not_keep_the_connection(session: Session, monkeypatch) -> None:
    """落ちたら、プレビューの計算中に画面を読み込み直すたびに、切れた接続が `done` の表に残り続ける(接続ごと保持が増える)。"""
    body, _ = _box(session)
    _select(session, [{"body": body.id, "kind": "edge", "index": 1}])
    _slow_blends(monkeypatch, 0.2)

    async def script(server, client):
        await server._dispatch(client, json.dumps({"cmd": "preview", "verb": "fillet", "args": {"radius": RADIUS}, "preview_id": 1, "ack_id": 1}))
        server.clients.discard(client)  # `handle` の後始末と同じ
        server._acks.pop(client, None)
        while server._pending_pushes:
            await asyncio.gather(*list(server._pending_pushes), return_exceptions=True)
        return dict(server._acks)

    (acks, _client) = _run(session, script)
    assert acks == {}
