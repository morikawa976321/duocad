"""保存 → 開くで、ボディの形(体積・面の数と種別・外接箱)まで元と一致する。

落ちたら: 開いた文書の形が保存前と違う(再生で形を作り直す verb のどれかが、その場の結果と別の形を作っている)。
`document_state` は図形の `params` までで形を見ないので、ここでは形そのものを照合する。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from duocad import kernel, query
from duocad.server import Session
from session_support import box, cylinder, pad, select

from duocad import brep_topology as _brep_topology


def _shape_state(session: Session) -> dict:
    """ボディごとの形の写し(体積・面数・面の種別ごとの枚数・外接箱・名前・表示)。"""
    state = {}
    for body in session.document.bodies:
        described = query.describe_shape(body.shape)
        bounds = kernel.bounding_box(body.shape, cache=None)
        state[body.id] = {
            "name": body.name,
            "visible": body.visible,
            "volume": query.volume(body.shape),
            "faces": _brep_topology.face_count(body.shape),
            "kinds": {kind: count for kind, count in described["faces_by_type"].items() if count},
            "min": bounds["min"],
            "max": bounds["max"],
        }
    return state


def _assert_same_shapes(actual: dict, expected: dict) -> None:
    assert list(actual) == list(expected), "ボディの並びが違う"
    for body_id, want in expected.items():
        got = actual[body_id]
        assert got["name"] == want["name"], body_id
        assert got["visible"] == want["visible"], body_id
        assert got["faces"] == want["faces"], body_id
        assert got["kinds"] == want["kinds"], body_id
        assert got["volume"] == pytest.approx(want["volume"], rel=1e-9), body_id
        assert got["min"] == pytest.approx(want["min"], abs=1e-9), body_id
        assert got["max"] == pytest.approx(want["max"], abs=1e-9), body_id


def _edge(session: Session, body_id: str, *, start: tuple, end: tuple) -> dict:
    """端点(順不同)で指す辺の参照。形が変わるたびに添字は振り直されるので、その都度引く。"""
    for edge in query.describe_edges(session.document.get_body(body_id).shape):
        points = (tuple(edge["start"]), tuple(edge["end"]))
        if all(
            any(pytest.approx(want, abs=1e-6) == got for got in points) for want in (start, end)
        ):
            return {"body": body_id, "kind": "edge", "index": edge["index"]}
    raise AssertionError(f"辺が無い: {start} - {end}")


def _circle_edge(session: Session, body_id: str, z: float) -> dict:
    for edge in query.describe_edges(session.document.get_body(body_id).shape):
        if edge["kind"] == "circle" and pytest.approx(edge["start"][2], abs=1e-6) == z:
            return {"body": body_id, "kind": "edge", "index": edge["index"]}
    raise AssertionError(f"z={z} の円い辺が無い")


def _top_face(session: Session, body_id: str, z: float) -> dict:
    for face in query.describe_faces(session.document.get_body(body_id).shape):
        if face["normal"][2] > 0.99 and pytest.approx(face["center"][2], abs=1e-6) == z:
            return {"body": body_id, "kind": "face", "index": face["index"]}
    raise AssertionError(f"z={z} の上向きの面が無い")


def _reopen(session: Session, name: str) -> Session:
    session.save_document(name)
    fresh = Session(0.2, models_dir=session.models_dir)
    fresh.open_document(name)
    return fresh


def test_a_chain_of_boolean_chamfer_fillet_and_face_offset_reopens_to_the_same_shapes(session: Session) -> None:
    """ブーリアン → 面取り → フィレット → 面のオフセットに、穴を持たず別に立つ円柱(フィレット・面取り)を加えた文書を保存して開く。"""
    target, _ = box(session)
    tool = pad(session, 10.0, 10.0, 30.0, corner=(15.0, 5.0))
    select(session, [{"body": tool.id, "kind": "body", "index": None}])
    session.boolean_body({"body": target.id, "kind": "body", "index": None}, "union")
    assert query.volume(session.document.get_body(target.id).shape) == pytest.approx(10000.0)

    select(session, [_edge(session, target.id, start=(0.0, 0.0, 0.0), end=(0.0, 0.0, 20.0))])
    session.chamfer(2.0)
    select(session, [_edge(session, target.id, start=(15.0, 15.0, 30.0), end=(25.0, 15.0, 30.0))])
    session.fillet(1.5)
    select(session, [_top_face(session, target.id, 30.0)])
    session.offset_faces(2.0)

    post = cylinder(session, 6.0, 10.0, center=(100.0, 0.0), mode="new")
    select(session, [_circle_edge(session, post.id, 10.0)])
    session.fillet(1.0)
    select(session, [_circle_edge(session, post.id, 0.0)])
    session.chamfer(1.0)
    session.clear_selection()

    expected = _shape_state(session)
    chained = expected[target.id]
    assert chained["volume"] != pytest.approx(10000.0, rel=1e-3), "前提: 連鎖の各 verb が形を変えている"
    assert chained["max"][2] == pytest.approx(32.0), "前提: 上の面のオフセットが効いている"
    assert len(expected) == 2
    assert {"cone", "torus"} <= set(expected[post.id]["kinds"]), "前提: 円柱に面取りと丸めが付いている"

    reopened = _reopen(session, "chain.json")

    _assert_same_shapes(_shape_state(reopened), expected)
    assert [entry.verb for entry in reopened.document.log.entries] == [
        entry.verb for entry in session.document.log.entries
    ]


def test_a_body_with_a_hole_and_a_fillet_reopens_to_the_same_shape(session: Session) -> None:
    """貫通穴を切った箱にフィレットを掛けた形(円筒面を含む)を保存して開く。"""
    body, _ = box(session)
    hole = cylinder(session, 4.0, 20.0, center=(10.0, 10.0), mode="cut")
    assert hole.id == body.id
    select(session, [_edge(session, body.id, start=(0.0, 0.0, 0.0), end=(20.0, 0.0, 0.0))])
    session.fillet(3.0)
    session.clear_selection()

    expected = _shape_state(session)
    assert expected[body.id]["kinds"].get("cylinder_full") == 1, "前提: 穴の円筒面がある"

    reopened = _reopen(session, "hole.json")

    _assert_same_shapes(_shape_state(reopened), expected)
