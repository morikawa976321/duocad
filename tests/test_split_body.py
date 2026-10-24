"""`split_body` の契約 —— 平面でボディを割り、番号と名前の引き継ぎ・断り方・undo・再生が決まった通りになる。"""
from __future__ import annotations

import math

import pytest

from duocad import kernel, kernel_split, query
from duocad.document import Body, Document
from duocad.region import region_ref
from duocad.sketch import (
    VERB_SKETCH_CREATE_OFFSET,
    add_circle,
    add_rectangle,
    base_plane,
    offset_plane,
    sketch_create,
)
from duocad.verbs import add_mesh_body, extrude, move_body, split_body
from document_support import assert_matches_replay, box_shape, new_box, replay_through_json, second_body
from duocad.server import Session
from session_support import SIDE, box, face_with_normal, select

from duocad import (
    brep_geometry as _brep_geometry,
    brep_topology as _brep_topology,
)

CUBE = 20.0


def _offset_sketch(doc: Document, height: float, base: str = "XY") -> str:
    return sketch_create(doc, offset_plane(base_plane(base), height), verb=VERB_SKETCH_CREATE_OFFSET)


def _cube() -> tuple:
    doc = Document()
    return doc, new_box(doc, CUBE)


def _state(doc: Document) -> list:
    """ボディごとの (id, 名前, 体積, 重心)。保存して読み直しても同じでなければならない番号と形の対応表。"""
    return [
        (
            body.id,
            body.name,
            round(_brep_geometry.volume(body.shape), 6),
            tuple(round(value, 6) for value in _brep_geometry.centroid(body.shape)),
        )
        for body in doc.bodies
    ]


def _total_volume(doc: Document) -> float:
    return sum(_brep_geometry.volume(body.shape) for body in doc.bodies)


def _z_range(doc: Document, body_id: str) -> tuple:
    box = kernel.bounding_box(doc.get_body(body_id).shape)
    return box["min"][2], box["max"][2]


def _facing_down_at(doc: Document, height: float) -> dict:
    """平面 z=`height` にある、法線が -z の面(薄い板の下面)への参照。板は切る対象とは別のボディ。"""
    plate = second_body(doc)
    move_body(doc, [plate], dx=0.0, dy=0.0, dz=height)
    return {"body": plate, "index": face_with_normal(doc.get_body(plate), 0.0, 0.0, -1.0)}


def _u_plate(doc: Document) -> str:
    """40×20×10 の板の x∈[15,25]・y∈[5,20] を溝に切った U 字(y>5 に2本の腕が立つ)。"""
    body_id = new_box(doc, 40.0, 20.0, 10.0)
    sketch_id = sketch_create(doc, base_plane("XY"))
    add_rectangle(doc, sketch_id, (15.0, 5.0), 10.0, 20.0)
    extrude(doc, [region_ref(sketch_id, 1)], 10.0, mode="cut", body_id=body_id)
    return body_id


def test_a_cube_cut_through_the_middle_gives_two_halves() -> None:
    """落ちたら: 中央の平面で割っても、体積が半分ずつの2つのボディにならない。"""
    doc, cube = _cube()
    before = _total_volume(doc)

    operation = split_body(doc, [cube], sketch_id=_offset_sketch(doc, CUBE / 2))

    assert len(doc.bodies) == 2
    assert [round(_brep_geometry.volume(body.shape), 6) for body in doc.bodies] == [CUBE**3 / 2] * 2
    assert math.isclose(_total_volume(doc), before, rel_tol=1e-6)
    assert operation.args["plane_normal"] == [0.0, 0.0, 1.0]
    assert operation.args["plane_point"][2] == CUBE / 2


def test_the_cut_faces_are_marked_new_in_the_provenance() -> None:
    """落ちたら: 切り口の平らな面が由来の記録の new_faces に載らない(面の添字を追えなくなる)。"""
    doc, cube = _cube()

    operation = split_body(doc, [cube], sketch_id=_offset_sketch(doc, 5.0))

    pieces = operation.provenance["bodies"][0]["pieces"]
    assert [piece["side"] for piece in pieces] == ["positive", "negative"]
    for piece in pieces:
        assert len(piece["new_faces"]) == 1
        face = query.describe_faces(doc.get_body(piece["body"]).shape)[piece["new_faces"][0] - 1]
        assert face["kind"] == "plane"
        assert face["area"] == pytest.approx(CUBE * CUBE)


def test_a_slanted_plane_given_by_a_face_cuts_the_body() -> None:
    """落ちたら: 斜めの平らな面で指した平面で切れない(法線が面の外向きでなくなる・体積の合計が合わない)。"""
    doc, cube = _cube()
    tilted = kernel.rotate_shape(box_shape(10.0, 10.0, 10.0), [0.0, 0.0, 0.0], [1.0, 0.0, 0.0], 30.0, copy=True)
    doc.add_body(Body(id="tilted", name="tilted", kind="solid", shape=tilted))
    slanted = next(face for face in query.describe_faces(tilted) if face["kind"] == "plane" and face["normal"][2] > 0.5)
    centre = slanted["center"]
    doc.get_body("tilted").shape = kernel.translate_shape(
        tilted, CUBE / 2 - centre[0], CUBE / 2 - centre[1], CUBE / 2 - centre[2], copy=True
    )

    operation = split_body(doc, [cube], plane_face={"body": "tilted", "index": slanted["index"]})

    assert len(doc.bodies) == 3
    assert operation.args["plane_normal"] == pytest.approx(slanted["normal"])
    assert math.isclose(
        sum(_brep_geometry.volume(doc.get_body(body_id).shape) for body_id in [cube, *operation.args["created"][0]]),
        CUBE**3,
        rel_tol=1e-6,
    )
    for body_id in operation.args["created"][0]:
        assert 0.0 < _brep_geometry.volume(doc.get_body(body_id).shape) < CUBE**3


def test_keep_positive_leaves_only_the_side_the_normal_points_to() -> None:
    """落ちたら: keep="positive" が法線の向く側以外を残す。"""
    doc, cube = _cube()

    split_body(doc, [cube], sketch_id=_offset_sketch(doc, 5.0), keep="positive")

    assert [body.id for body in doc.bodies] == [cube]
    assert _z_range(doc, cube) == pytest.approx((5.0, CUBE))


def test_keep_negative_leaves_only_the_opposite_side() -> None:
    """落ちたら: keep="negative" が法線の向く側を残す。"""
    doc, cube = _cube()

    split_body(doc, [cube], sketch_id=_offset_sketch(doc, 5.0), keep="negative")

    assert [body.id for body in doc.bodies] == [cube]
    assert _z_range(doc, cube) == pytest.approx((0.0, 5.0))


def test_the_keep_choice_is_checked() -> None:
    """落ちたら: 知らない keep が黙って both として通る。"""
    doc, cube = _cube()

    with pytest.raises(ValueError, match="keep"):
        split_body(doc, [cube], base_plane="XY", keep="left")


def test_exactly_one_plane_source_is_needed() -> None:
    """落ちたら: 平面の指定が0個・2個のときにどれかが黙って選ばれる。"""
    doc, cube = _cube()

    with pytest.raises(ValueError, match="plane"):
        split_body(doc, [cube])
    with pytest.raises(ValueError, match="同時に渡せない"):
        split_body(doc, [cube], base_plane="XY", sketch_id=_offset_sketch(doc, 5.0))


def test_a_plane_that_misses_the_body_is_refused_and_nothing_changes() -> None:
    """落ちたら: 交わらない平面が成功として通る、または断った後に文書・ログ・採番が変わっている。"""
    doc, cube = _cube()
    sketch_id = _offset_sketch(doc, CUBE + 5.0)
    shape, entries, numbering = doc.get_body(cube).shape, len(doc.log.entries), doc.log._snapshot()[2]

    with pytest.raises(ValueError, match="割れない"):
        split_body(doc, [cube], sketch_id=sketch_id)

    assert doc.get_body(cube).shape is shape
    assert len(doc.bodies) == 1
    assert len(doc.log.entries) == entries
    assert doc.log._snapshot()[2] == numbering


def test_a_plane_that_only_touches_a_face_is_refused() -> None:
    """落ちたら: 面に接しているだけ(切っても片側が空)の平面が成功として通る。"""
    doc, cube = _cube()

    with pytest.raises(ValueError, match="割れない"):
        split_body(doc, [cube], base_plane="XY")
    with pytest.raises(ValueError, match="割れない"):
        split_body(doc, [cube], sketch_id=_offset_sketch(doc, CUBE))

    assert len(doc.bodies) == 1


def test_the_largest_piece_keeps_the_name() -> None:
    """落ちたら: 大きい側が元の番号・名前を引き継がない(蓋と本体に割ったとき本体の名前が蓋に移る)。"""
    doc, cube = _cube()
    name = doc.get_body(cube).name

    split_body(doc, [cube], sketch_id=_offset_sketch(doc, 5.0))

    assert doc.get_body(cube).name == name
    assert _brep_geometry.volume(doc.get_body(cube).shape) == pytest.approx(CUBE * CUBE * 15.0)
    lid = doc.bodies[1]
    assert lid.name != name
    assert _brep_geometry.volume(lid.shape) == pytest.approx(CUBE * CUBE * 5.0)


def test_the_heir_does_not_change_when_the_normal_is_flipped() -> None:
    """落ちたら: 同じ平面を法線を反転して指すと、引き継ぎ先が入れ替わる。"""
    doc, cube = _cube()
    split_body(doc, [cube], sketch_id=_offset_sketch(doc, 15.0))  # 法線 +z で z=15
    upright = _z_range(doc, cube)

    flipped, flipped_cube = _cube()
    split_body(flipped, [flipped_cube], plane_face=_facing_down_at(flipped, 15.0))  # 法線 -z で z=15

    assert upright == pytest.approx((0.0, 15.0))
    assert _z_range(flipped, flipped_cube) == pytest.approx((0.0, 15.0))


def test_equal_halves_go_to_the_positive_side() -> None:
    """落ちたら: ちょうど半分に割ったとき、引き継ぎ先が法線の向く側に決まらない。"""
    doc, cube = _cube()
    split_body(doc, [cube], sketch_id=_offset_sketch(doc, CUBE / 2))
    assert _z_range(doc, cube) == pytest.approx((CUBE / 2, CUBE))

    flipped, flipped_cube = _cube()
    split_body(flipped, [flipped_cube], plane_face=_facing_down_at(flipped, CUBE / 2))
    assert _z_range(flipped, flipped_cube) == pytest.approx((0.0, CUBE / 2))


def _split_u(doc: Document, plate: str):
    """法線 -y の平面 y=12 で U 字を割る(正側 y<12 は1塊、負側 y>12 は2本の腕)。"""
    return split_body(doc, [plate], sketch_id=_offset_sketch(doc, -12.0, "XZ"))


def test_a_side_in_several_lumps_becomes_one_body_per_lump() -> None:
    """落ちたら: 片側が離れた複数の塊になるとき、複数のソリッドを持つ1つのボディが残る。"""
    doc = Document()
    plate = _u_plate(doc)

    operation = _split_u(doc, plate)

    assert len(doc.bodies) == 3
    assert operation.args["created"] and len(operation.args["created"][0]) == 2
    for body in doc.bodies:
        assert len(_brep_topology.split_solids(body.shape)) == 1
    assert sorted(round(_brep_geometry.volume(body.shape)) for body in doc.bodies) == [1200, 1200, 4100]
    assert math.isclose(_total_volume(doc), 40.0 * 20.0 * 10.0 - 10.0 * 15.0 * 10.0, rel_tol=1e-6)


def test_each_lump_keeps_its_number_through_replay() -> None:
    """落ちたら: 塊とボディの番号の対応が、ログの再生で入れ替わる。"""
    doc = Document()
    _split_u(doc, _u_plate(doc))

    assert _state(replay_through_json(doc)) == _state(doc)


def test_a_body_that_misses_the_plane_refuses_the_whole_operation() -> None:
    """落ちたら: 入力の最後のボディだけ交わらないとき、先のボディが割れたまま残る・採番が進む。"""
    doc, cube = _cube()
    other = second_body(doc)  # z∈[0, 5]
    plane = _offset_sketch(doc, 10.0)
    shapes = [body.shape for body in doc.bodies]
    entries, numbering = len(doc.log.entries), doc.log._snapshot()[2]

    with pytest.raises(ValueError, match="割れない"):
        split_body(doc, [cube, other], sketch_id=plane)

    assert [body.shape for body in doc.bodies] == shapes
    assert all(a is b for a, b in zip((body.shape for body in doc.bodies), shapes))
    assert len(doc.log.entries) == entries
    assert doc.log._snapshot()[2] == numbering


def test_several_bodies_are_each_split_by_the_same_plane() -> None:
    """落ちたら: 複数のボディを入力したとき、ボディごとに割られない。"""
    doc, cube = _cube()
    sketch_id = sketch_create(doc, base_plane("XY"))
    add_rectangle(doc, sketch_id, (40.0, 0.0), CUBE, CUBE)
    other = extrude(doc, [region_ref(sketch_id, 1)], CUBE, mode="new").args["body_id"]

    operation = split_body(doc, [cube, other], sketch_id=_offset_sketch(doc, 5.0))

    assert len(doc.bodies) == 4
    assert [len(group) for group in operation.args["created"]] == [1, 1]
    assert math.isclose(_total_volume(doc), 2 * CUBE**3, rel_tol=1e-6)


def test_undo_restores_the_single_body_and_redo_splits_again() -> None:
    """落ちたら: undo で元の1つのボディに戻らない・redo で同じ番号と形にならない。"""
    doc, cube = _cube()
    before = _state(doc)
    split_body(doc, [cube], sketch_id=_offset_sketch(doc, 5.0))
    after = _state(doc)

    doc.log.undo(doc)
    assert _state(doc) == before
    assert doc.log.entries[-1].verb == VERB_SKETCH_CREATE_OFFSET
    assert_matches_replay(doc)
    doc.log.redo(doc)

    assert _state(doc) == after
    assert doc.log.entries[-1].verb == "split_body"


def test_undo_of_a_one_sided_split_and_a_multi_body_split_restores_every_shape() -> None:
    """落ちたら: 片側だけ残した割り・複数ボディの割りの undo が、元の形・ボディの並びに戻らない。"""
    doc, cube = _cube()
    plane = _offset_sketch(doc, 5.0)
    shape = doc.get_body(cube).shape
    split_body(doc, [cube], sketch_id=plane, keep="positive")
    doc.log.undo(doc)
    assert doc.get_body(cube).shape is shape

    other = second_body(doc)  # z∈[0,5] は平面 z=5 に接するだけなので別の平面で
    middle = _offset_sketch(doc, 2.5)
    shapes = {body.id: body.shape for body in doc.bodies}
    split_body(doc, [cube, other], sketch_id=middle)
    assert len(doc.bodies) == 4
    doc.log.undo(doc)

    assert {body.id: body.shape for body in doc.bodies} == shapes
    assert_matches_replay(doc)


def test_the_split_matches_the_replay_after_saving() -> None:
    """落ちたら: 保存して読み直した文書で、番号と形の対応が変わる。"""
    doc, cube = _cube()
    split_body(doc, [cube], sketch_id=_offset_sketch(doc, 5.0))

    assert _state(replay_through_json(doc)) == _state(doc)
    assert_matches_replay(doc)


def test_the_volume_total_is_kept_for_a_curved_body() -> None:
    """落ちたら: 曲面を含むボディを斜めに割ったとき、体積の合計が元と合わない。"""
    doc = Document()
    body_id = new_box(doc, CUBE)
    sketch_id = sketch_create(doc, base_plane("XY"))
    add_circle(doc, sketch_id, (CUBE / 2, CUBE / 2), 8.0)
    extrude(doc, [region_ref(sketch_id, 1)], CUBE, mode="cut", body_id=body_id)
    before = _brep_geometry.volume(doc.get_body(body_id).shape)

    operation = split_body(doc, [body_id], plane_face=_tilted_plane(doc))

    pieces = [body_id, *operation.args["created"][0]]
    assert len(pieces) == 2
    assert math.isclose(sum(_brep_geometry.volume(doc.get_body(piece).shape) for piece in pieces), before, rel_tol=1e-6)


def _tilted_plane(doc: Document) -> dict:
    tilted = kernel.rotate_shape(box_shape(10.0, 10.0, 10.0), [0.0, 0.0, 0.0], [0.0, 1.0, 0.0], 25.0, copy=True)
    doc.add_body(Body(id="tilted", name="tilted", kind="solid", shape=tilted))
    face = next(face for face in query.describe_faces(tilted) if face["kind"] == "plane" and face["normal"][2] > 0.5)
    centre = face["center"]
    doc.get_body("tilted").shape = kernel.translate_shape(
        tilted, CUBE / 2 - centre[0], CUBE / 2 - centre[1], CUBE / 2 - centre[2], copy=True
    )
    return {"body": "tilted", "index": face["index"]}


def test_a_mesh_body_is_refused_with_the_reason() -> None:
    """落ちたら: メッシュのボディが割れてしまう・断る文にできることの一覧が付かない。"""
    doc = Document()
    mesh = add_mesh_body(doc, [0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1], [0, 2, 1, 0, 1, 3, 0, 3, 2, 1, 2, 3])

    with pytest.raises(ValueError, match="mesh body"):
        split_body(doc, [mesh], base_plane="XY")


# --------------------------------------------------------------------------------------
# Session(入口・プレビュー)
# --------------------------------------------------------------------------------------
def _session_cube():
    session = Session(0.2)
    body, _ = box(session)
    select(session, [{"body": body.id, "kind": "face", "index": 1}])
    return session, body


def test_the_preview_shows_the_cut_shape_and_leaves_the_document_alone() -> None:
    """落ちたら: プレビューが切った後の形を返さない・文書・ログ・採番を変える(確定した番号が飛ぶ)。"""
    session, body = _session_cube()
    sketch_id = session.sketch_create_offset(SIDE / 2, "XY")["sketch_id"]
    select(session, [{"body": body.id, "kind": "face", "index": 1}])
    entries, bodies = len(session.document.log.entries), len(session.document.bodies)
    numbering = session.document.log._snapshot()[2]

    preview = session.preview("split_body", {"sketch_id": sketch_id, "keep": "both"})

    assert sorted(round(item["volume"]) for item in preview["bodies"]) == [SIDE**3 / 2] * 2
    assert len(session.document.log.entries) == entries
    assert len(session.document.bodies) == bodies
    assert session.document.log._snapshot()[2] == numbering
    applied = session.split_body(sketch_id=sketch_id)
    assert len(applied["created"]) == 1


def test_a_sketch_figure_reference_selects_the_sketch_plane() -> None:
    """落ちたら: 画面が送る図形の参照 `{"sketch", "entity"}` でスケッチの平面を指せない。"""
    session, body = _session_cube()
    sketch_id = session.sketch_create_offset(SIDE / 2, "XY")["sketch_id"]
    entity_id = session.add_circle(sketch_id, [1.0, 1.0], 2.0)["entity_id"]
    select(session, [{"body": body.id, "kind": "face", "index": 1}])

    applied = session.split_body(sketch_id={"sketch": sketch_id, "entity": entity_id, "point": None}, keep="negative")

    assert applied["created"] == []
    assert applied["keep"] == "negative"
    assert applied["plane_point"][2] == SIDE / 2


def test_a_failed_split_leaves_the_session_untouched() -> None:
    """落ちたら: 交わらない平面で断った後も、ログに操作が積まれる・選択が消える。"""
    session, body = _session_cube()
    sketch_id = session.sketch_create_offset(SIDE * 2, "XY")["sketch_id"]
    select(session, [{"body": body.id, "kind": "face", "index": 1}])
    entries = len(session.document.log.entries)

    with pytest.raises(ValueError, match="割れない"):
        session.split_body(sketch_id=sketch_id)

    assert len(session.document.log.entries) == entries
    assert len(session.document.bodies) == 1


def test_a_plane_tangent_to_a_curved_body_inside_its_bounding_box_is_refused() -> None:
    """落ちたら: 境界箱の内側で円柱に接するだけの平面が成功として通る(片側に塊が無いのに OCCT は IsDone を返す)。"""
    doc = Document()
    sketch_id = sketch_create(doc, base_plane("XY"))
    add_circle(doc, sketch_id, (0.0, 0.0), 10.0)
    cylinder = extrude(doc, [region_ref(sketch_id, 1)], 10.0, mode="new").args["body_id"]
    reach = 5.0 / math.sqrt(2.0)
    plane = {"plane_point": [reach, reach, 5.0], "plane_normal": [1.0, 1.0, 0.0]}

    with pytest.raises(ValueError, match="割れない"):
        kernel_split.split_by_plane(doc.get_body(cylinder).shape, plane["plane_point"], plane["plane_normal"])


def test_a_plane_between_the_lumps_of_one_body_is_refused() -> None:
    """落ちたら: 離れた2つの塊を持つ1つのボディを、塊の間を通る(どちらとも交わらない)平面で「割れた」ことにしている。"""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
    from OCP.gp import gp_Pnt


    doc = Document()
    lumps = _brep_topology.compound([BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 10, 10, 10).Shape(), BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 30), 10, 10, 10).Shape()])
    doc.add_body(Body(id="lumps", name="lumps", kind="solid", shape=lumps))
    before = _state(doc)
    with pytest.raises(ValueError, match="切り口ができない"):
        split_body(doc, [doc.bodies[0].id], sketch_id=_offset_sketch(doc, 20.0))
    assert _state(doc)[: len(before)] == before


def test_equal_volumes_are_ordered_by_x_even_when_x_differs_below_the_rounding() -> None:
    """落ちたら: 同体積の塊の並びが重心の x の小さい方にならない(6桁の丸めで x の差が消える・差の許容の比較が循環する)。"""
    a = {"side": "positive", "centroid": [5.0, 25.0, 7.5]}
    b = {"side": "positive", "centroid": [5.0000004, 5.0, 7.5]}
    assert sorted([b, a], key=kernel_split._piece_order)[0] is a
    c = {"side": "negative", "centroid": [-100.0, 0.0, 0.0]}
    assert sorted([c, b], key=kernel_split._piece_order)[0] is b, "正側が先"
    # 差の許容で比べると A>B・B>C・C>A と循環する3つ(x の差 0.75e-9 ずつ)。どの並びから並べても A が先
    trio = [{"side": "positive", "centroid": [x, y, 0.0]} for x, y in ((0.0, 40.0), (0.75e-9, 20.0), (1.5e-9, 0.0))]
    for order in ([0, 1, 2], [2, 1, 0], [1, 2, 0], [2, 0, 1]):
        assert sorted([trio[i] for i in order], key=kernel_split._piece_order)[0] is trio[0]


def test_replay_follows_the_recorded_centroids_not_the_ordering_rule() -> None:
    """落ちたら: 再生で塊と番号を並びの規則で決め直していて、計算の誤差で並びが変わると番号の対応が入れ替わる
    (記録した重心に最も近い塊へ対応づける)。記録の重心の並びを入れ替えたログを再生して、記録どおりになることを見る。"""
    doc = Document()
    plate = _u_plate(doc)
    operation = split_body(doc, [plate], sketch_id=_offset_sketch(doc, -12.0, "XZ"), keep="negative")
    assert len(operation.args["centroids"][0]) == 2, "腕2本(同じ体積)"
    heir, other = operation.args["centroids"][0]
    operation.args["centroids"][0] = [other, heir]
    replayed = replay_through_json(doc)
    centroid_of = {body.id: _brep_geometry.centroid(body.shape) for body in replayed.bodies}
    assert centroid_of[plate] == pytest.approx(other, abs=1e-9)
    (new_id,) = operation.args["created"][0]
    assert centroid_of[new_id] == pytest.approx(heir, abs=1e-9)


def test_a_wall_and_a_central_post_with_the_same_centroid_keep_their_ids_through_replay() -> None:
    """落ちたら: 重心が一致する外壁と中央の柱(体積が違う)を、再生で取り違えて ID が入れ替わる(体積で見分ける)。"""
    doc = Document()
    body_id = new_box(doc, 60.0, 60.0, 30.0)
    cut_sketch = _offset_sketch(doc, 2.0)
    add_rectangle(doc, cut_sketch, (3.0, 3.0), 54.0, 54.0)
    add_rectangle(doc, cut_sketch, (15.0, 15.0), 30.0, 30.0)
    from duocad import region as regions

    ring = region_ref(cut_sketch, max(regions.regions(doc, cut_sketch), key=lambda found: found.area).index)  # 溝になる環
    extrude(doc, [ring], 28.0, mode="cut", body_id=body_id)
    split_body(doc, [body_id], sketch_id=_offset_sketch(doc, 8.0))
    before = {body.id: round(_brep_geometry.volume(body.shape), 3) for body in doc.bodies}
    replayed = replay_through_json(doc)
    assert {body.id: round(_brep_geometry.volume(body.shape), 3) for body in replayed.bodies} == before
