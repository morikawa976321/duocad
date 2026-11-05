"""押し出し・回転体・ロフトの相手(`duocad.verbs.extrude._place`): `body_id` を省けば、押し出す立体に触れる・重なる、
見えている編集できるソリッドの全部。join は1つに融合し、cut は全部を削る。決まった相手はログに残る。

寸法: 20×20×20 の箱 A(x 0..20)と B(x 30..50)。体積は各 8000。
"""
from __future__ import annotations

import math

import pytest

from duocad import kernel, query
from duocad.document import Document
from duocad.region import region_ref
from duocad.server import Session
from duocad.sketch import add_line, add_rectangle, sketch_create
from duocad.verbs import add_mesh_body, delete, extrude, extrude_faces, revolve
from document_support import assert_matches_replay, replay_through_json, shape_ref
from session_support import select

BOX = 20.0
BOX_VOLUME = BOX**3
TOL = 1e-9


def _box_at(document: Document, x: float) -> str:
    sketch_id = sketch_create(document, "XY")
    add_rectangle(document, sketch_id, (x, 0.0), BOX, BOX)
    return extrude(document, [region_ref(sketch_id, 1)], BOX, mode="new").args["body_id"]


def _two_boxes() -> tuple[Document, str, str]:
    document = Document()
    return document, _box_at(document, 0.0), _box_at(document, 30.0)


def _rectangle(document: Document, corner: tuple, width: float, height: float) -> str:
    sketch_id = sketch_create(document, "XY")
    add_rectangle(document, sketch_id, corner, width, height)
    return sketch_id


def _volume(document: Document, body_id: str) -> float:
    return query.volume(document.get_body(body_id).shape)


def _bridge_cut(document: Document, **options):
    """A と B の両方を貫く 30×5 の溝(各ボディから 10×5×20 = 1000 を削る)。"""
    sketch_id = _rectangle(document, (10.0, 5.0), 30.0, 5.0)
    return extrude(document, [region_ref(sketch_id, 1)], 30.0, mode="cut", **options)


def test_cut_still_finds_the_visible_body_after_the_active_body_was_deleted():
    """落ちたら、最後に作ったボディを消した後(アクティブが空)の切り取りが、見えている箱を前にして
    「cut requires a target body」で断られる(STEP の部品を消した後に実物で起きた)。"""
    document, a, b = _two_boxes()
    delete(document, bodies=[b])
    assert document.active_body() is None
    sketch_id = _rectangle(document, (5.0, 5.0), 5.0, 5.0)

    operation = extrude(document, [region_ref(sketch_id, 1)], 30.0, mode="cut")

    assert operation.args["body_id"] == a and "other_bodies" not in operation.args
    assert math.isclose(_volume(document, a), BOX_VOLUME - 5.0 * 5.0 * BOX, rel_tol=TOL)


def test_cut_carves_every_body_it_reaches_and_records_them():
    """落ちたら、2つの箱を貫く溝が片方しか削らない、または削った相手がログに残らず再生で変わる。"""
    document, a, b = _two_boxes()

    operation = _bridge_cut(document)

    assert operation.args["body_id"] == a and operation.args["other_bodies"] == [b]
    for body_id in (a, b):
        assert math.isclose(_volume(document, body_id), BOX_VOLUME - 1000.0, rel_tol=TOL)
    replayed = replay_through_json(document)
    for body_id in (a, b):
        assert math.isclose(_volume(replayed, body_id), BOX_VOLUME - 1000.0, rel_tol=TOL)

    document.log.undo(document)
    for body_id in (a, b):
        assert math.isclose(_volume(document, body_id), BOX_VOLUME, rel_tol=TOL)
    assert_matches_replay(document)
    document.log.redo(document)
    assert math.isclose(_volume(document, b), BOX_VOLUME - 1000.0, rel_tol=TOL)


def test_join_merges_every_body_it_touches_into_the_first():
    """落ちたら、2つの箱をつなぐ板が片方にしか足されない(もう片方と重なったまま別ボディで残る)、
    または undo で消えたボディが元の並び・アクティブに戻らない。"""
    document, a, b = _two_boxes()
    assert document.active_body().id == b
    sketch_id = _rectangle(document, (15.0, 5.0), 20.0, 5.0)

    operation = extrude(document, [region_ref(sketch_id, 1)], 10.0, mode="join")

    assert operation.args["body_id"] == a and operation.args["other_bodies"] == [b]
    assert [body.id for body in document.bodies] == [a]
    assert document.active_body().id == a
    # 板 20×5×10 のうち各箱と重なる 5×5×10 を引く
    assert math.isclose(_volume(document, a), 2 * BOX_VOLUME + 1000.0 - 2 * 250.0, rel_tol=TOL)
    assert [body.id for body in replay_through_json(document).bodies] == [a]

    document.log.undo(document)
    assert [body.id for body in document.bodies] == [a, b]
    assert document.active_body().id == b
    assert math.isclose(_volume(document, a), BOX_VOLUME, rel_tol=TOL)
    assert_matches_replay(document)


def test_join_that_touches_nothing_makes_a_new_body_even_with_an_active_body():
    """落ちたら、離れた場所の結合がアクティブなボディへ飛び地として足される(1つのボディが2つの塊になる)。"""
    document, a, b = _two_boxes()
    sketch_id = _rectangle(document, (100.0, 0.0), 5.0, 5.0)

    operation = extrude(document, [region_ref(sketch_id, 1)], 5.0, mode="join")

    created = operation.args["body_id"]
    assert created not in (a, b) and len(document.bodies) == 3
    assert math.isclose(_volume(document, b), BOX_VOLUME, rel_tol=TOL)


def test_join_that_only_touches_a_face_still_joins():
    """落ちたら、面に貼ったスケッチから外へ足す(箱の上面に接するだけの)板が、箱に足されず別のボディになる。"""
    document, a, _b = _two_boxes()
    sketch_id = sketch_create(document, {"kind": "offset", "base": {"kind": "base", "name": "XY"}, "distance": BOX})
    add_rectangle(document, sketch_id, (5.0, 5.0), 5.0, 5.0)

    operation = extrude(document, [region_ref(sketch_id, 1)], 5.0, mode="join")

    assert operation.args["body_id"] == a and "other_bodies" not in operation.args
    assert math.isclose(_volume(document, a), BOX_VOLUME + 125.0, rel_tol=TOL)


def test_a_hidden_body_is_left_alone():
    """落ちたら、隠したボディが見えないところで削られる(見えないものは変えない)。"""
    document, a, b = _two_boxes()
    document.get_body(b).visible = False

    operation = _bridge_cut(document)

    assert operation.args["body_id"] == a and "other_bodies" not in operation.args
    assert math.isclose(_volume(document, b), BOX_VOLUME, rel_tol=TOL)


def test_cut_that_reaches_no_visible_body_fails_and_changes_nothing():
    """落ちたら、隠したボディだけを通る・面に触れるだけの切り取りが、何も削らずに成功として通る。"""
    document, a, b = _two_boxes()
    document.get_body(a).visible = False
    sketch_id = _rectangle(document, (5.0, 5.0), 5.0, 5.0)
    entries = len(document.log.entries)

    with pytest.raises(ValueError, match="cut reaches no visible body"):
        extrude(document, [region_ref(sketch_id, 1)], 30.0, mode="cut")
    with pytest.raises(ValueError, match="正の距離は面の外向き"):
        extrude_faces(document, [shape_ref(document, b, "face", 1)], 5.0, mode="cut")

    assert len(document.log.entries) == entries
    assert math.isclose(_volume(document, a), BOX_VOLUME, rel_tol=TOL)


def test_a_given_body_id_limits_the_target_to_that_body():
    """落ちたら、MCP が行き先を指したのに、触れている他のボディまで削られる。"""
    document, a, b = _two_boxes()

    operation = _bridge_cut(document, body_id=b)

    assert operation.args["body_id"] == b and "other_bodies" not in operation.args
    assert math.isclose(_volume(document, a), BOX_VOLUME, rel_tol=TOL)
    assert math.isclose(_volume(document, b), BOX_VOLUME - 1000.0, rel_tol=TOL)


def test_a_face_extrusion_joins_the_other_body_it_runs_into():
    """落ちたら、面を伸ばして隣の箱にめり込ませても、めり込んだ箱が別ボディのまま重なって残る。"""
    document, a, b = _two_boxes()
    right = next(face["index"] for face in kernel.tessellate(document.get_body(a).shape)["faces"] if face["normal"][0] > 0.999)

    operation = extrude_faces(document, [shape_ref(document, a, "face", right)], 15.0, mode="join")

    assert operation.args["body_id"] == a and operation.args["other_bodies"] == [b]
    assert [body.id for body in document.bodies] == [a]
    # A は x 0..35、B は x 30..50 で、重なり 5×20×20 を1回だけ数える
    assert math.isclose(_volume(document, a), 50.0 * BOX * BOX, rel_tol=TOL)


def test_a_mesh_body_is_not_a_target():
    """落ちたら、重なっているメッシュボディを相手にしようとして断られ、ソリッドへの切り取りまで止まる。"""
    document, a, b = _two_boxes()
    add_mesh_body(document, [5.0, 5.0, 5.0, 15.0, 5.0, 5.0, 5.0, 15.0, 5.0, 5.0, 5.0, 15.0], [0, 2, 1, 0, 1, 3, 0, 3, 2, 1, 2, 3])
    sketch_id = _rectangle(document, (5.0, 5.0), 5.0, 5.0)

    operation = extrude(document, [region_ref(sketch_id, 1)], 30.0, mode="cut")

    assert operation.args["body_id"] == a and "other_bodies" not in operation.args


def test_revolve_cuts_every_body_it_reaches():
    """落ちたら、回転体の切り取りが押し出しと違う相手の決め方をしている。"""
    document, a, b = _two_boxes()
    sketch_id = sketch_create(document, "XZ")
    # x 15..24・z 5..10 の長方形を x=25 の縦の線のまわりに回す(半径 1..10 の輪が両方の箱をかすめる)
    add_rectangle(document, sketch_id, (15.0, 5.0), 9.0, 5.0)
    line = add_line(document, sketch_id, (25.0, -10.0), (25.0, 30.0))

    operation = revolve(document, [region_ref(sketch_id, 1)], {"sketch": sketch_id, "entity": line}, 360.0, mode="cut")

    assert sorted([operation.args["body_id"], *operation.args["other_bodies"]]) == sorted([a, b])
    for body_id in (a, b):
        assert _volume(document, body_id) < BOX_VOLUME


def test_the_session_reports_and_previews_every_body():
    """落ちたら、画面のプレビューが削れる2つのボディの片方しか見せない、または応答の changed_bodies /
    removed_bodies が実際に変わった・消えたボディと食い違う。"""
    session = Session(0.2)
    document = session.document
    a, b = _box_at(document, 0.0), _box_at(document, 30.0)
    sketch_id = _rectangle(document, (10.0, 5.0), 30.0, 5.0)

    select(session, [{"sketch": sketch_id, "index": 1}])
    preview = session.preview_job("extrude", {"distance": 30.0, "mode": "cut"}).run()
    assert sorted(body["body_id"] for body in preview["bodies"]) == sorted([a, b])
    result = session.apply("extrude", {"distance": 30.0, "mode": "cut"})
    assert sorted(result["changed_bodies"]) == sorted([a, b]) and result["removed_bodies"] == []

    joined = _rectangle(document, (15.0, 12.0), 20.0, 5.0)
    select(session, [{"sketch": joined, "index": 1}])
    result = session.apply("extrude", {"distance": 10.0, "mode": "join"})
    assert result["changed_bodies"] == [a] and result["removed_bodies"] == [b]
