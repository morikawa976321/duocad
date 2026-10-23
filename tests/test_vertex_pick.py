"""頂点のピック/ハイライトの土台(頂点の3D座標を引ける)。"""
from __future__ import annotations

import pytest

from duocad import kernel
from duocad.server import Session
from session_support import SIDE, box as _box, browser as _browser

from duocad import (
    brep_topology as _brep_topology,
)


def test_vertex_positions_use_the_same_index_as_the_selection(session: Session) -> None:
    """`vertex_positions` の添字は `count_subshapes(shape, "vertex")` の体系と同じ。

    落ちたら、選択で通した頂点の添字で座標が引けない
    (= 選択に入った頂点を画面に出せない)。
    """
    body, _ = _box(session)
    described = kernel.vertex_positions(body.shape)

    assert len(described) == _brep_topology.count_subshapes(body.shape, "vertex") == 8
    assert [item["index"] for item in described] == list(range(1, 9))
    for item in described:
        assert len(item["point"]) == 3
        # 20mm立方体なので、全頂点が 0 か 20(の丸め誤差内)に居る
        assert all(min(abs(value), abs(value - SIDE)) < 1e-6 for value in item["point"])


def test_an_empty_shape_has_no_vertex_map() -> None:
    """空の形状は ValueError(黙って0件を返すと「頂点が無いボディ」に見える)。"""
    with pytest.raises(ValueError):
        kernel.vertex_positions(None)


def test_vertices_are_sent_only_to_the_connection_that_asked(session: Session) -> None:
    """`get_vertices` は要求した接続にだけ返る(`get_edges` と同じ扱い)。"""
    body, _ = _box(session)

    async def script(send):
        return await send({"cmd": "get_vertices", "body_id": body.id})

    (answer, clients) = _browser(session, script)

    assert answer["type"] == "vertices"
    assert answer["body_id"] == body.id
    assert len(answer["vertices"]) == 8
    assert clients[1].payloads == [], "頂点は全接続へ配らない(要求した接続だけ)"


def test_the_vertex_revision_follows_the_shape(session: Session) -> None:
    """形が変われば `revision` が上がる。**古い添字の座標を描き続けさせない**ための番号。"""
    body, _ = _box(session)
    before = session.vertices(body.id)
    session.clear_selection()
    session.modify_selection([{"body": body.id, "kind": "edge", "index": 1}], [])
    session.fillet(3.0)
    after = session.vertices(body.id)

    assert after["revision"] > before["revision"]
    assert len(after["vertices"]) != len(before["vertices"])


def test_edges_and_vertices_carry_the_shape_revision(session: Session) -> None:
    """辺・頂点の応答に形の版が載り、形が変わったときだけ上がる。落ちたら、画面が粗さだけ変わった版(カメラ由来)で
    取り寄せた添字を古い形のものと取り違える(または形の変わった古い添字を使い続ける)。"""
    body, _ = _box(session)
    edges = session.edges(body.id)
    vertices = session.vertices(body.id)
    assert edges["shape_revision"] == vertices["shape_revision"] == session.view.shape_revision(body.id)

    session.modify_selection([{"body": body.id, "kind": "edge", "index": 1}], [])
    session.fillet(3.0)

    assert session.edges(body.id)["shape_revision"] > edges["shape_revision"]
    assert session.vertices(body.id)["shape_revision"] > vertices["shape_revision"]


def test_the_vertices_of_a_hidden_body_are_refused(session: Session) -> None:
    """隠したBodyの頂点は配らない(`edges()` / `geometry()` と同じ規律)。"""
    body, _ = _box(session)
    session.set_visible(body.id, False)

    with pytest.raises(ValueError):
        session.vertices(body.id)


def test_asking_for_the_vertices_of_an_unknown_body_is_an_error(session: Session) -> None:
    _box(session)

    async def script(send):
        return await send({"cmd": "get_vertices", "body_id": "body-does-not-exist"})

    (answer, _clients) = _browser(session, script)
    assert answer["type"] == "error"
    assert answer["where"] == "get_vertices"
