"""配信の三角形の使い回し(`view.DocumentView` の面のキャッシュ)の取りこぼし。ゴーストを出し直す・形が変わる・複製する
ときに、変わっていない面を測り直さない。"""
from __future__ import annotations

import pytest

from duocad import kernel
from duocad.server import Session

DEVIATION = 0.2


@pytest.fixture()
def counted(monkeypatch):
    """`kernel.tessellate_faces` が測った面の添字(呼び出しごと。形を丸ごと測った `kernel.tessellate` は全面)。"""
    calls: list = []
    real = kernel.tessellate_faces
    whole = kernel.tessellate

    def counting(shape, groups, **options):
        calls.append(sorted(index for indices in groups.values() for index in indices))
        return real(shape, groups, **options)

    def counting_whole(shape, *args, **options):
        result = whole(shape, *args, **options)
        calls.append(list(range(1, result["face_count"] + 1)))
        return result

    monkeypatch.setattr(kernel, "tessellate_faces", counting)
    monkeypatch.setattr(kernel, "tessellate", counting_whole)
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
    measured = {index for call in counted for index in call}
    assert measured and measured <= {2, 3}, "測り方の変わった面(2: 細→粗、3: 粗→細)のほかは測らない"


@pytest.mark.parametrize("verb, args", [
    ("mirror_body", {"base_plane": "YZ"}),
    ("move_body", {"dx": 100.0, "dy": 0.0, "dz": 0.0, "copy": True}),
])
def test_a_rigid_copy_takes_closedness_from_its_source(boxed, monkeypatch, verb, args) -> None:
    """落ちたら、複製・鏡映のたびに、元と同じつながりの形が閉じた立体かを数え直す(穴 384 個の板で 8〜12 ms)。"""
    session, body = boxed
    session.geometry(body)
    session.modify_selection([{"body": body, "kind": "body", "index": None}], [])
    calls: list = []
    real = kernel.is_closed_shell
    monkeypatch.setattr(kernel, "is_closed_shell", lambda shape: calls.append(shape) or real(shape))
    session.apply(verb, args)
    copy = session.document.bodies[-1]
    geometry = session.geometry(copy.id)
    assert calls == []
    assert geometry["closed"] is real(copy.shape)


def test_a_ghost_typed_again_measures_only_the_changed_faces(boxed, counted) -> None:
    """落ちたら、画面が粗く測っている面(背を向けた面・画面外の面)を、半径を打ち直すたびのゴーストが細かく測り直す
    (--part big の上から見た画面で 73 面・1 回 2〜4 秒)。"""
    session, body = boxed
    session.set_view_resolution(0.02, 400.0, {body: []})
    session.geometry(body)
    _ghosts(session, body, (1.0,))
    counted.clear()
    job = session.preview_job("fillet", {"radius": 1.5})
    ghost = job.run()["bodies"][0]["geometry"]
    job.adopt()
    session.fillet(1.5)
    shape = session.document.get_body(body).shape
    kept = {position for position, _index in kernel.kept_faces(shape)[1]}
    assert kept, "題材の前提(丸めても残る面がある)が崩れた"
    assert counted == [sorted(set(range(1, ghost["face_count"] + 1)) - kept)]
    fresh = kernel.tessellate(shape, session.view.view_deviation, session.view.view_angular_deflection)
    assert [face["positions"] for face in ghost["faces"]] == [face["positions"] for face in fresh["faces"]]
