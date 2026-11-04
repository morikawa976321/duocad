"""配信の三角形の使い回し(`view.DocumentView` の面のキャッシュ)の取りこぼし。ゴーストを出し直す・形が変わる・複製する
ときに、変わっていない面を測り直さない。"""
from __future__ import annotations

import pytest

from duocad import kernel
from duocad.server import Session

DEVIATION = 0.2


@pytest.fixture()
def counted(monkeypatch):
    """`kernel.tessellate_faces` が測った面の添字(呼び出しごと)。"""
    calls: list = []
    real = kernel.tessellate_faces

    def counting(shape, groups, **options):
        calls.append(sorted(index for indices in groups.values() for index in indices))
        return real(shape, groups, **options)

    monkeypatch.setattr(kernel, "tessellate_faces", counting)
    return calls


@pytest.fixture()
def boxed() -> tuple:
    """直方体1つのセッション。"""
    session = Session(DEVIATION)
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_rectangle(sketch_id, [0.0, 0.0], 30.0, 20.0)
    session.modify_selection([{"sketch": sketch_id, "index": 1}], [])
    session.extrude(10.0, mode="new")
    session.clear_selection()
    return session, session.document.bodies[-1].id


def _ghosts(session, body: str, radii) -> None:
    session.modify_selection([{"body": body, "kind": "edge", "index": 1}], [])
    for radius in radii:
        job = session.preview_job("fillet", {"radius": radius})
        job.run()
        job.adopt()


def test_ghosts_typed_again_keep_the_shape_on_screen(boxed, counted) -> None:
    """落ちたら、半径を打ち直してゴーストを出すたびに画面に出ている形の面が追い出され、確定の後の undo で全面を測り直す
    (穴 384 個の板で 10 秒)。"""
    session, body = boxed
    session.geometry(body)
    _ghosts(session, body, (1.0, 1.5, 2.0, 2.5))
    session.fillet(2.5)
    session.geometry(body)
    counted.clear()
    session.undo()
    session.geometry(body)
    assert counted == []


def test_cancelled_ghosts_keep_the_faces_the_camera_needs(boxed, counted) -> None:
    """落ちたら、ゴーストを何度か出して取り消した後にカメラが動くと、画面に出ている形を丸ごと測り直す。"""
    session, body = boxed
    session.set_view_resolution(0.02, 400.0, {body: [1, 2]})
    session.geometry(body)
    _ghosts(session, body, (1.0, 1.5, 2.0, 2.5))
    session.clear_selection()
    counted.clear()
    session.set_view_resolution(0.02, 400.0, {body: [1, 3]})
    session.geometry(body)
    assert counted == [[2, 3]], "測り方の変わった面(2: 細→粗、3: 粗→細)だけを測る"
