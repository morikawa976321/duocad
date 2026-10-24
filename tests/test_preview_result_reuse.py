"""プレビューで求めた形を確定と redo が使い回す(`kernel.remembered`)、カメラ由来の配信で形の版が動かない。issue #169。"""
from __future__ import annotations

import pytest

from duocad import kernel, kernel_worker
from duocad.server import Session
from session_support import box as _box, select as _select

from duocad import (
    brep_geometry as _brep_geometry,
)

RADIUS = 2.0


@pytest.fixture
def blends(monkeypatch):
    """同一プロセスの計算と子への要求の回数。覚えた結果は空から始める。"""
    calls = []
    real = kernel._blend_edges
    monkeypatch.setattr(kernel, "_blend_edges", lambda shape, values, verb: calls.append(verb) or real(shape, values, verb=verb))
    run = kernel_worker.run
    monkeypatch.setattr(kernel_worker, "run", lambda verb, *args, **kwargs: calls.append(verb) or run(verb, *args, **kwargs))
    monkeypatch.setattr(kernel, "_RESULTS", type(kernel._RESULTS)())
    return calls


def _edge(body) -> list:
    return [{"body": body.id, "kind": "edge", "index": 1}]


def test_commit_and_redo_reuse_the_previewed_fillet(session: Session, blends) -> None:
    """落ちたら、確定(と redo)がプレビューと同じフィレットをもう一度計算する(PC ケースの最大ボディで 0.88 秒)。"""
    body, _ = _box(session)
    _select(session, _edge(body))
    preview = session.preview("fillet", {"radius": RADIUS})
    assert blends == ["fillet"]
    session.fillet(RADIUS)
    assert blends == ["fillet"], "確定がプレビューの結果を使わなかった"
    committed = body.shape
    assert preview["bodies"][0]["volume"] == pytest.approx(_brep_geometry.volume(committed))
    session.undo()
    session.redo()
    assert blends == ["fillet"], "redo が同じ形へ同じフィレットを掛け直した"
    assert body.shape is committed


def test_a_different_radius_or_shape_is_computed_again(session: Session, blends) -> None:
    """落ちたら、半径や形が違うのに前の結果が返り、プレビューと違う形が確定する。"""
    body, _ = _box(session)
    _select(session, _edge(body))
    session.preview("fillet", {"radius": RADIUS})
    session.preview("fillet", {"radius": RADIUS + 1.0})
    session.preview("chamfer", {"distance": RADIUS})
    assert blends == ["fillet", "fillet", "chamfer"]
    session.fillet(RADIUS + 1.0)
    _select(session, _edge(body))
    session.fillet(RADIUS)  # 丸めた後の形(別の形)へは計算し直す
    assert blends == ["fillet", "fillet", "chamfer", "fillet"]


def test_the_order_of_the_edges_is_part_of_the_key(session: Session, blends) -> None:
    """落ちたら、辺を足す順だけが違うフィレットに前の結果が返る(OCCT は順で結果が変わり、保存を開き直すと形が変わる)。"""
    body, _ = _box(session)
    kernel.fillet_edges(body.shape, {1: 1.0, 5: 2.0})
    kernel.fillet_edges(body.shape, {5: 2.0, 1: 1.0})
    assert blends == ["fillet", "fillet"]


def test_a_failure_is_not_remembered(session: Session, blends, monkeypatch) -> None:
    """落ちたら、一度失敗した引数が覚えられ、条件が同じでも計算せずに失敗を返す(または失敗が成功として返る)。"""
    body, _ = _box(session)
    _select(session, _edge(body))
    with pytest.raises(Exception):
        session.preview("fillet", {"radius": 1000.0})
    with pytest.raises(Exception):
        session.fillet(1000.0)
    assert blends == ["fillet", "fillet"]


def test_callers_get_their_own_result_dicts(session: Session, blends) -> None:
    """落ちたら、呼び手が結果の辞書を書き換えると、覚えた結果まで変わり次の呼び手が壊れた形を受け取る。"""
    body, _ = _box(session)
    first = kernel.fillet_edges(body.shape, {1: RADIUS})
    first["shape"] = None
    again = kernel.fillet_edges(body.shape, {1: RADIUS})
    assert again["shape"] is not None and blends == ["fillet"]


def test_the_shape_revision_ignores_camera_driven_coarseness(session: Session) -> None:
    """落ちたら、カメラで粗さが変わっただけの配信でも形の版が上がり、画面が1秒かかるプレビューを投げ直す。"""
    body, _ = _box(session)

    def meta() -> dict:
        return next(item for item in session.state_meta()["bodies"] if item["body_id"] == body.id)

    before = meta()
    session.set_view_resolution(0.001, 40.0)
    after_zoom = meta()
    assert after_zoom["revision"] > before["revision"], "粗さが変われば配る形(版)は変わる"
    assert after_zoom["shape_revision"] == before["shape_revision"]
    _select(session, _edge(body))
    session.fillet(RADIUS)
    assert meta()["shape_revision"] > after_zoom["shape_revision"]
    session.set_visible(body.id, False)
    assert meta()["shape_revision"] > after_zoom["shape_revision"] + 1, "隠すとプレビューの対象が変わる"
