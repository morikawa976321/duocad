"""原点の7パーツ(`duocad/datum.py`)を、ボディやスケッチの点・線・面と同じく選び・測り・verb の引数に使えること。"""
from __future__ import annotations

import math

import pytest

from duocad import datum, kernel, query, selection, sketch
from duocad.document import Document
from duocad.region import region_ref, regions
from duocad.server import Session
from duocad.sketch import add_point, add_rectangle, sketch_create
from duocad.verbs import loft, mirror_body, mirror_sketch_entities, move_body, move_sketch_entities, revolve, split_body
from document_support import new_box, replay_through_json
from session_support import box as _box, select as _select


def _rectangle(document: Document, corner, width, height, plane="XY"):
    sketch_id = sketch_create(document, plane)
    entity = add_rectangle(document, sketch_id, corner, width, height)
    found = regions(document, sketch_id)
    return sketch_id, entity, [region_ref(sketch_id, found[-1].index)]


def _box_bounds(document: Document, body_id: str) -> tuple:
    box = kernel.bounding_box(document.get_body(body_id).shape)
    return tuple(round(value, 6) for value in box["min"]), tuple(round(value, 6) for value in box["max"])


# --------------------------------------------------------------------------------------
# 選択
# --------------------------------------------------------------------------------------
def test_every_datum_can_be_selected_and_is_one_item_per_name(session: Session) -> None:
    """落ちたら、原点パーツを選択集合へ入れられないか、同じパーツが2回入る。"""
    _select(session, [{"datum": name} for name in datum.NAMES] + [{"datum": "X"}])
    items = session.get_selection()["items"]
    assert [item["datum"] for item in items] == list(datum.NAMES)
    assert {selection.ref_kind(item) for item in items} == {selection.DATUM_KIND}


def test_an_unknown_datum_name_is_refused_without_changing_the_selection(session: Session) -> None:
    """落ちたら、綴り違いの原点パーツが黙って入る(どの軸を指したか分からない参照が残る)。"""
    _select(session, [{"datum": "X"}])
    with pytest.raises(ValueError, match="unknown datum"):
        session.modify_selection([{"datum": "W"}], [])
    assert session.get_selection()["items"] == [{"datum": "X", "provenance": None}]


def test_a_selected_datum_is_toggled_like_any_other_item(session: Session) -> None:
    """落ちたら、画面のクリック(トグル)で原点パーツを外せない。"""
    session.toggle_selection([{"datum": "XY"}])
    session.toggle_selection([{"datum": "XY"}])
    assert session.get_selection()["items"] == []


def test_datums_do_not_count_as_bodies_to_move(session: Session) -> None:
    """落ちたら、原点パーツを一緒に選ぶと「動かすボディ」に原点が数えられて壊れる。"""
    body, _ = _box(session)
    _select(session, [{"body": body.id, "kind": "body"}, {"datum": "Z"}])
    assert selection.require_bodies(session.document) == [body.id]
    _select(session, [{"datum": "Z"}])
    with pytest.raises(selection.SelectionKindMismatch):
        selection.require_bodies(session.document)


def test_a_selected_datum_plane_is_where_the_sketch_goes(session: Session) -> None:
    """落ちたら、原点の平面を選んでスケッチを作っても、基準平面と同じ場所・向きに貼られない。"""
    _select(session, [{"datum": "XZ"}])
    created = session.sketch_create()
    expected = session.sketch_create("XZ")
    assert created["frame"] == expected["frame"]


def test_a_face_and_a_datum_plane_together_are_ambiguous_for_a_sketch(session: Session) -> None:
    """落ちたら、面と原点の平面を両方選んだのに、どちらかへ黙って貼られる。"""
    body, _ = _box(session)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}, {"datum": "XY"}])
    with pytest.raises(selection.SelectionAmbiguous):
        session.sketch_create()


# --------------------------------------------------------------------------------------
# 測定
# --------------------------------------------------------------------------------------
def test_a_vertex_is_measured_from_the_origin(session: Session) -> None:
    """落ちたら、原点と頂点の距離が頂点の座標の長さにならない。"""
    body, _ = _box(session)
    far = max(session.vertices(body.id)["vertices"], key=lambda item: sum(value * value for value in item["point"]))
    _select(session, [{"datum": "origin"}, {"body": body.id, "kind": "vertex", "index": far["index"]}])
    measured = session.measure()
    assert measured["items"][0] == {"kind": "datum", "datum": "origin", "datum_kind": "point", "point": [0.0, 0.0, 0.0]}
    assert measured["between"]["distance"] == pytest.approx(math.sqrt(sum(value * value for value in far["point"])))


def test_a_top_face_is_measured_from_the_xy_plane_and_an_edge_against_an_axis(session: Session) -> None:
    """落ちたら、原点の平面・軸が平面・直線として測られていない(面の高さや辺の角度が出ない)。"""
    body, _ = _box(session)
    top = max(
        (item for item in query.describe_faces(body.shape) if item["kind"] == "plane"),
        key=lambda item: item["center"][2],
    )
    _select(session, [{"datum": "XY"}, {"body": body.id, "kind": "face", "index": top["index"]}])
    assert session.measure()["between"]["distance"] == pytest.approx(20.0)

    vertical = next(
        item for item in session.edges(body.id)["edges"]
        if item["kind"] == "line" and abs(item["points"][2] - item["points"][-1]) > 1.0
    )
    _select(session, [{"datum": "X"}, {"body": body.id, "kind": "edge", "index": vertical["index"]}])
    assert session.measure()["between"]["angle_deg"] == pytest.approx(90.0)


# --------------------------------------------------------------------------------------
# ボディの verb
# --------------------------------------------------------------------------------------
def test_a_body_turns_around_an_origin_axis_given_as_the_axis_edge() -> None:
    """落ちたら、回転軸に指した原点の軸が、原点を通るその向きの直線として使われていない。"""
    document = Document()
    body_id = new_box(document, 10.0)
    before_min, before_max = _box_bounds(document, body_id)
    move_body(document, [body_id], mode="rotate", angle=90.0, axis_edge={"datum": "Z"})
    after_min, after_max = _box_bounds(document, body_id)
    assert after_min == pytest.approx((-before_max[1], before_min[0], before_min[2]), abs=1e-6)
    assert after_max == pytest.approx((-before_min[1], before_max[0], before_max[2]), abs=1e-6)


def test_the_origin_as_the_centre_vertex_moves_the_axis_through_the_origin() -> None:
    """落ちたら、回転中心に原点を指しても軸が原点へ寄らない。"""
    document = Document()
    body_id = new_box(document, 10.0)
    move_body(document, [body_id], mode="rotate", angle=180.0, axis_dir=[0, 0, 1], center_vertex={"datum": "origin"})
    after_min, after_max = _box_bounds(document, body_id)
    assert after_max[0] == pytest.approx(0.0, abs=1e-6) and after_max[1] == pytest.approx(0.0, abs=1e-6)


def test_a_point_is_not_an_axis() -> None:
    """落ちたら、原点の点を回転軸に指しても断られない(向きの無い軸で回る)。"""
    document = Document()
    body_id = new_box(document, 10.0)
    with pytest.raises(ValueError, match="not an axis"):
        move_body(document, [body_id], mode="rotate", angle=90.0, axis_edge={"datum": "origin"})


@pytest.mark.parametrize("name", ["XY", "XZ", "YZ"])
def test_a_datum_plane_mirrors_like_the_base_plane_of_the_same_name(name: str) -> None:
    """落ちたら、鏡映面に指した原点の平面が base_plane の同じ名前と別の面になる。"""
    by_datum, by_name = Document(), Document()
    for document in (by_datum, by_name):
        new_box(document, 10.0)
    datum_args = mirror_body(by_datum, [by_datum.bodies[0].id], plane_face={"datum": name}).args
    name_args = mirror_body(by_name, [by_name.bodies[0].id], base_plane=name).args
    assert datum_args["plane_point"] == name_args["plane_point"]
    assert datum_args["plane_normal"] == name_args["plane_normal"]


def test_a_datum_plane_cuts_like_the_base_plane_of_the_same_name() -> None:
    """落ちたら、分割面に指した原点の平面が base_plane の同じ名前と別の面になる(残す側が入れ替わる)。"""
    by_datum, by_name = Document(), Document()
    for document in (by_datum, by_name):
        body_id = new_box(document, 10.0)
        move_body(document, [body_id], dx=-5.0, dy=-5.0, dz=-5.0)
    datum_args = split_body(by_datum, [by_datum.bodies[0].id], plane_face={"datum": "XZ"}, keep="positive").args
    name_args = split_body(by_name, [by_name.bodies[0].id], base_plane="XZ", keep="positive").args
    assert datum_args["plane_normal"] == name_args["plane_normal"]
    assert _box_bounds(by_datum, by_datum.bodies[0].id) == _box_bounds(by_name, by_name.bodies[0].id)


def test_an_axis_is_not_a_plane() -> None:
    """落ちたら、軸を鏡映面に指しても断られない。"""
    document = Document()
    new_box(document, 10.0)
    with pytest.raises(ValueError, match="not a plane"):
        mirror_body(document, [document.bodies[0].id], plane_face={"datum": "X"})


# --------------------------------------------------------------------------------------
# スケッチの verb
# --------------------------------------------------------------------------------------
def test_a_sketch_entity_mirrors_over_an_origin_axis_on_its_plane() -> None:
    """落ちたら、スケッチ平面の上にある原点の軸で鏡映できない。"""
    document = Document()
    sketch_id = sketch_create(document, "XY")
    entity = add_point(document, sketch_id, (3.0, 4.0))
    mirror_sketch_entities(document, [{"sketch": sketch_id, "entity": entity, "point": None}], {"datum": "Y"})
    copied = sketch.get_sketch(document, sketch_id).entities[-1]
    values = sketch.entity_params(document, sketch_id, copied.id)
    assert (values["cx"], values["cy"]) == pytest.approx((-3.0, 4.0))


def test_an_origin_axis_off_the_sketch_plane_is_refused_as_a_mirror_axis() -> None:
    """落ちたら、平面の上に無い軸を平面へ落として黙って別の線で鏡映する。"""
    document = Document()
    sketch_id = sketch_create(document, sketch.offset_plane(sketch.base_plane("XY"), 5.0))
    entity = add_point(document, sketch_id, (3.0, 4.0))
    with pytest.raises(ValueError, match="平面の上に無い"):
        mirror_sketch_entities(document, [{"sketch": sketch_id, "entity": entity, "point": None}], {"datum": "Y"})


def test_a_region_revolves_around_an_origin_axis_and_replays_the_same() -> None:
    """落ちたら、原点の軸で回転体が作れないか、保存して開き直すと別の形になる。"""
    document = Document()
    _sketch_id, _, refs = _rectangle(document, (2.0, 0.0), 3.0, 10.0, plane="XZ")
    revolve(document, refs, {"datum": "Z"})
    expected = math.pi * (5.0**2 - 2.0**2) * 10.0
    assert query.volume(document.bodies[-1].shape) == pytest.approx(expected, rel=1e-9)
    restored = replay_through_json(document)
    assert query.volume(restored.bodies[-1].shape) == pytest.approx(expected, rel=1e-9)


def test_an_origin_axis_off_the_sketch_plane_is_refused_as_a_revolve_axis() -> None:
    """落ちたら、平面の上に無い軸を平面へ落とした線のまわりに黙って回す。"""
    document = Document()
    _sketch_id, _, refs = _rectangle(document, (2.0, 0.0), 3.0, 10.0, plane="XY")
    with pytest.raises(ValueError, match="平面の上に無い"):
        revolve(document, refs, {"datum": "Z"})


def test_a_sketch_rotates_around_the_origin_dropped_onto_an_offset_plane() -> None:
    """落ちたら、回転中心に指した原点がスケッチ平面へ垂直に落とした点にならない。"""
    document = Document()
    sketch_id = sketch_create(document, sketch.offset_plane(sketch.base_plane("XY"), 5.0))
    entity = add_point(document, sketch_id, (3.0, 0.0))
    move_sketch_entities(
        document, [{"sketch": sketch_id, "entity": entity, "point": None}], mode="rotate", angle=90.0,
        center_entity={"datum": "origin"},
    )
    values = sketch.entity_params(document, sketch_id, entity)
    assert (values["cx"], values["cy"]) == pytest.approx((0.0, 3.0), abs=1e-9)


def test_a_loft_can_close_at_the_origin_and_replays_the_same() -> None:
    """落ちたら、ロフトの収束点に原点を指せないか、開き直すと別の形になる。"""
    document = Document()
    sketch_id = sketch_create(document, sketch.offset_plane(sketch.base_plane("XY"), 30.0))
    add_rectangle(document, sketch_id, (-10.0, -10.0), 20.0, 20.0)
    found = regions(document, sketch_id)
    loft(document, [region_ref(sketch_id, found[0].index)], mode="new", end_point={"datum": "origin"})
    expected = 20.0 * 20.0 * 30.0 / 3.0
    assert query.volume(document.bodies[-1].shape) == pytest.approx(expected, rel=1e-6)
    assert query.volume(replay_through_json(document).bodies[-1].shape) == pytest.approx(expected, rel=1e-6)
