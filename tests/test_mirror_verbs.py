"""`mirror_sketch_entities` / `mirror_body` の契約 —— 「鏡像反転コピーを作る」1点だけの verb。

固定するのは:

- **どちらも常に複製を作る**(move側と違い in-place反転の口は無い)
- スケッチ: 対象は確定選択の図形そのもの(点単位は扱わない)。軸は既存の線分1本への参照で、
  対象の確定選択とは別引数(共有選択集合には混ぜない)
- スケッチ: 長方形は軸が水平・垂直(辺と平行/垂直)なら鏡映できるが、斜めの軸では種別を
  保てず ValueError(黙って線分4本に化けさせない)
- ソリッド: 対象は選択中の面/辺/頂点が属するボディ。鏡映面は base_plane(XY/XZ/YZ)か
  plane_face(既存の平面な面)のどちらか一方(両方/どちらも無しは ValueError)
- ソリッド: 非平面の面を plane_face に渡すと ValueError
- どちらも undo で複製だけが消え、元は触らない
- 確定後に選択は全リセットする
"""
from __future__ import annotations

import math

import pytest

from duocad import kernel, query, selection, sketch
from duocad.server import Session

SIDE = 20.0


@pytest.fixture()
def session() -> Session:
    return Session(None, 0.2)


def _select(session: Session, refs: list) -> None:
    session.clear_selection()
    session.modify_selection(refs, [])


def _params(session: Session, sketch_id: str, entity_id: str) -> dict:
    return sketch.entity_params(session.document, sketch_id, entity_id)


def _box(session: Session):
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_rectangle(sketch_id, [0.0, 0.0], SIDE, SIDE)
    _select(session, [{"sketch": sketch_id, "index": 1}])
    session.extrude(SIDE, mode="join")
    session.clear_selection()
    return session.document.bodies[-1], sketch_id


def _cylinder(session: Session):
    """円柱を1つ作って返す(非平面の面を持つボディが要るテスト用)。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_circle(sketch_id, [100.0, 100.0], 5.0)
    _select(session, [{"sketch": sketch_id, "index": 1}])
    session.extrude(5.0, mode="new")
    session.clear_selection()
    return session.document.bodies[-1]


def _curved_face_index(shape) -> int:
    count = kernel.face_count(shape)
    for index in range(1, count + 1):
        if kernel.classify_face(kernel.face_of(shape, index)) != "plane":
            return index
    raise AssertionError("非平面の面が無い(テストの前提が崩れている)")


# --------------------------------------------------------------------------------------
# mirror_sketch_entities
# --------------------------------------------------------------------------------------
def test_mirroring_a_line_reflects_its_endpoints(session: Session) -> None:
    sketch_id = session.sketch_create("XY")["sketch_id"]
    axis_id = session.add_line(sketch_id, [0.0, 0.0], [10.0, 0.0])["entity_id"]
    entity_id = session.add_line(sketch_id, [1.0, 2.0], [3.0, 4.0])["entity_id"]
    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": None}])

    applied = session.mirror_sketch_entities({"sketch": sketch_id, "entity": axis_id})

    assert applied["axis_p0"] == [0.0, 0.0]
    assert applied["axis_dir"] == [1.0, 0.0]
    created_id = applied["created"][0]
    mirrored = _params(session, sketch_id, created_id)
    assert mirrored == {"x0": 1.0, "y0": -2.0, "x1": 3.0, "y1": -4.0}
    # 元は変わっていない(常に複製)
    assert _params(session, sketch_id, entity_id) == {"x0": 1.0, "y0": 2.0, "x1": 3.0, "y1": 4.0}


def test_mirroring_a_circle_moves_only_its_center(session: Session) -> None:
    sketch_id = session.sketch_create("XY")["sketch_id"]
    axis_id = session.add_line(sketch_id, [0.0, 5.0], [10.0, 5.0])["entity_id"]
    entity_id = session.add_circle(sketch_id, [3.0, 0.0], 2.0)["entity_id"]
    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": None}])

    applied = session.mirror_sketch_entities({"sketch": sketch_id, "entity": axis_id})

    mirrored = _params(session, sketch_id, applied["created"][0])
    assert mirrored == pytest.approx({"cx": 3.0, "cy": 10.0, "radius": 2.0})


def test_mirroring_an_arc_keeps_its_sweep_and_flips_its_position(session: Session) -> None:
    sketch_id = session.sketch_create("XY")["sketch_id"]
    axis_id = session.add_line(sketch_id, [0.0, 0.0], [1.0, 0.0])["entity_id"]
    entity_id = session.add_arc(sketch_id, [0.0, 5.0], 2.0, 0.0, 90.0)["entity_id"]
    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": None}])

    applied = session.mirror_sketch_entities({"sketch": sketch_id, "entity": axis_id})

    mirrored = _params(session, sketch_id, applied["created"][0])
    assert mirrored["cy"] == pytest.approx(-5.0)
    # 掃引量(終角 - 始角)は保たれる(向きの反転は始点・終点の入れ替えで表す)
    assert mirrored["end_deg"] - mirrored["start_deg"] == pytest.approx(90.0)


def test_mirroring_a_rectangle_across_an_axis_parallel_to_its_edge_keeps_its_kind(
    session: Session,
) -> None:
    """軸が長方形の辺と平行(水平)なら軸平行のまま鏡映できる。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]
    axis_id = session.add_line(sketch_id, [0.0, 0.0], [1.0, 0.0])["entity_id"]
    entity_id = session.add_rectangle(sketch_id, [1.0, 1.0], 4.0, 2.0)["entity_id"]
    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": None}])

    applied = session.mirror_sketch_entities({"sketch": sketch_id, "entity": axis_id})

    mirrored = _params(session, sketch_id, applied["created"][0])
    assert mirrored == pytest.approx({"x": 1.0, "y": -3.0, "width": 4.0, "height": 2.0})


def test_mirroring_a_rectangle_across_a_diagonal_axis_is_refused(session: Session) -> None:
    """軸が斜め(長方形の辺と平行でも垂直でもない)だと種別を保てず ValueError。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]
    axis_id = session.add_line(sketch_id, [0.0, 0.0], [1.0, 1.0])["entity_id"]
    entity_id = session.add_rectangle(sketch_id, [1.0, 1.0], 4.0, 2.0)["entity_id"]
    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": None}])

    before = len(session.document.log.entries)
    with pytest.raises(ValueError) as error:
        session.mirror_sketch_entities({"sketch": sketch_id, "entity": axis_id})
    assert "鏡映できない" in str(error.value)
    # 失敗したら文書にもログにも何も残らない
    assert len(session.document.log.entries) == before
    assert len(sketch.get_sketch(session.document, sketch_id).entities) == 2


def test_the_axis_must_be_a_line_entity(session: Session) -> None:
    sketch_id = session.sketch_create("XY")["sketch_id"]
    circle_id = session.add_circle(sketch_id, [0.0, 0.0], 3.0)["entity_id"]
    entity_id = session.add_line(sketch_id, [1.0, 2.0], [3.0, 4.0])["entity_id"]
    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": None}])

    with pytest.raises(ValueError) as error:
        session.mirror_sketch_entities({"sketch": sketch_id, "entity": circle_id})
    assert "line" in str(error.value)


def test_a_target_lying_on_the_axis_is_refused(session: Session) -> None:
    """鏡映しても動かない対象(軸の上に乗っている)は元と完全に重なる複製になるので断る。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]
    axis_id = session.add_line(sketch_id, [0.0, 0.0], [10.0, 0.0])["entity_id"]
    on_axis_id = session.add_line(sketch_id, [1.0, 0.0], [5.0, 0.0])["entity_id"]
    _select(session, [{"sketch": sketch_id, "entity": on_axis_id, "point": None}])

    with pytest.raises(ValueError) as error:
        session.mirror_sketch_entities({"sketch": sketch_id, "entity": axis_id})
    assert "重なる" in str(error.value)


def test_the_axis_may_be_one_of_the_targets(session: Session) -> None:
    """軸自身が対象の選択に混ざっていてもよい(自分自身を軸にしつつ他を鏡映する)。

    軸に使った図形自身は対象から黙って外れる(鏡映しても自分自身と重なるだけなので)。
    """
    sketch_id = session.sketch_create("XY")["sketch_id"]
    axis_id = session.add_line(sketch_id, [0.0, 0.0], [10.0, 0.0])["entity_id"]
    other_id = session.add_line(sketch_id, [1.0, 2.0], [3.0, 4.0])["entity_id"]
    _select(
        session,
        [
            {"sketch": sketch_id, "entity": axis_id, "point": None},
            {"sketch": sketch_id, "entity": other_id, "point": None},
        ],
    )

    applied = session.mirror_sketch_entities({"sketch": sketch_id, "entity": axis_id})

    # 軸自身は対象から外れているので、複製は other_id の分だけ(1個)
    assert len(applied["created"]) == 1
    mirrored = _params(session, sketch_id, applied["created"][0])
    assert mirrored == {"x0": 1.0, "y0": -2.0, "x1": 3.0, "y1": -4.0}
    # 軸に使った図形自体は変わらず残っている
    assert _params(session, sketch_id, axis_id) == {"x0": 0.0, "y0": 0.0, "x1": 10.0, "y1": 0.0}


def test_mirroring_without_a_sketch_selection_is_refused(session: Session) -> None:
    sketch_id = session.sketch_create("XY")["sketch_id"]
    axis_id = session.add_line(sketch_id, [0.0, 0.0], [10.0, 0.0])["entity_id"]
    session.clear_selection()
    with pytest.raises(ValueError):
        session.mirror_sketch_entities({"sketch": sketch_id, "entity": axis_id})


def test_undo_removes_the_mirrored_sketch_copy(session: Session) -> None:
    sketch_id = session.sketch_create("XY")["sketch_id"]
    axis_id = session.add_line(sketch_id, [0.0, 0.0], [10.0, 0.0])["entity_id"]
    entity_id = session.add_line(sketch_id, [1.0, 2.0], [3.0, 4.0])["entity_id"]
    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": None}])
    applied = session.mirror_sketch_entities({"sketch": sketch_id, "entity": axis_id})
    created_id = applied["created"][0]
    assert sketch.get_entity(sketch.get_sketch(session.document, sketch_id), created_id)

    session.undo()

    with pytest.raises(KeyError):
        sketch.get_entity(sketch.get_sketch(session.document, sketch_id), created_id)
    # 元は残っている
    assert _params(session, sketch_id, entity_id) == {"x0": 1.0, "y0": 2.0, "x1": 3.0, "y1": 4.0}


def test_the_selection_resets_after_a_sketch_mirror(session: Session) -> None:
    sketch_id = session.sketch_create("XY")["sketch_id"]
    axis_id = session.add_line(sketch_id, [0.0, 0.0], [10.0, 0.0])["entity_id"]
    entity_id = session.add_line(sketch_id, [1.0, 2.0], [3.0, 4.0])["entity_id"]
    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": None}])

    session.mirror_sketch_entities({"sketch": sketch_id, "entity": axis_id})

    assert session.get_selection()["items"] == []


# --------------------------------------------------------------------------------------
# mirror_body
# --------------------------------------------------------------------------------------
def test_mirroring_a_body_with_a_base_plane(session: Session) -> None:
    body, _ = _box(session)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])

    applied = session.mirror_body(base_plane="YZ")

    assert applied["plane_point"] == [0.0, 0.0, 0.0]
    assert applied["plane_normal"] == pytest.approx([1.0, 0.0, 0.0])
    assert applied["created"] == applied["changed_bodies"]
    mirrored = session.document.get_body(applied["created"][0])
    # 元は 0..20 / 鏡映後は -20..0(YZ平面=x=0に対する鏡映)
    box = kernel.bounding_box(mirrored.shape)
    assert box["min"][0] == pytest.approx(-SIDE, abs=1e-4)
    assert box["max"][0] == pytest.approx(0.0, abs=1e-4)
    # 体積は保たれる
    assert query.volume(mirrored.shape) == pytest.approx(query.volume(body.shape))
    # 元のボディは動いていない
    original_box = kernel.bounding_box(body.shape)
    assert original_box["min"][0] == pytest.approx(0.0, abs=1e-4)


def test_mirroring_a_body_with_an_existing_face(session: Session) -> None:
    body, _ = _box(session)
    top_face = next(
        face["index"]
        for face in query.describe_faces(body.shape)
        if face["normal"][2] > 0.9
    )
    _select(session, [{"body": body.id, "kind": "edge", "index": 1}])

    applied = session.mirror_body(plane_face={"body": body.id, "index": top_face})

    assert applied["plane_normal"] == pytest.approx([0.0, 0.0, 1.0])
    mirrored = session.document.get_body(applied["created"][0])
    box = kernel.bounding_box(mirrored.shape)
    assert box["min"][2] == pytest.approx(SIDE) and box["max"][2] == pytest.approx(2 * SIDE)


def test_both_plane_arguments_is_refused(session: Session) -> None:
    body, _ = _box(session)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    with pytest.raises(ValueError) as error:
        session.mirror_body(base_plane="XY", plane_face={"body": body.id, "index": 1})
    assert "同時に渡せない" in str(error.value)


def test_neither_plane_argument_is_refused(session: Session) -> None:
    body, _ = _box(session)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    with pytest.raises(ValueError) as error:
        session.mirror_body()
    assert "mirror plane" in str(error.value)


def test_a_non_planar_face_is_refused(session: Session) -> None:
    cylinder = _cylinder(session)
    curved = _curved_face_index(cylinder.shape)
    _select(session, [{"body": cylinder.id, "kind": "face", "index": curved}])

    with pytest.raises(ValueError) as error:
        session.mirror_body(plane_face={"body": cylinder.id, "index": curved})
    assert "平面" in str(error.value)


def test_a_hidden_body_cannot_be_mirrored(session: Session) -> None:
    body, _ = _box(session)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    session.set_visible(body.id, False)
    with pytest.raises(ValueError):
        session.mirror_body(base_plane="XY")


def test_two_bodies_mirror_across_the_same_plane(session: Session) -> None:
    first, sketch_id = _box(session)
    session.add_rectangle(sketch_id, [40.0, 0.0], SIDE, SIDE)
    _select(session, [{"sketch": sketch_id, "index": 2}])
    session.extrude(SIDE, mode="new")
    second = session.document.bodies[-1]

    _select(
        session,
        [
            {"body": first.id, "kind": "face", "index": 1},
            {"body": second.id, "kind": "face", "index": 1},
        ],
    )

    applied = session.mirror_body(base_plane="XY")

    assert len(applied["created"]) == 2


def test_undo_removes_the_mirrored_body(session: Session) -> None:
    body, _ = _box(session)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    applied = session.mirror_body(base_plane="XY")
    created_id = applied["created"][0]
    assert session.document.get_body(created_id)
    before = len(session.document.bodies)

    session.undo()

    with pytest.raises(KeyError):
        session.document.get_body(created_id)
    assert len(session.document.bodies) == before - 1
    # 元のボディはそのまま
    assert session.document.get_body(body.id) is body


def test_the_selection_resets_after_a_body_mirror(session: Session) -> None:
    body, _ = _box(session)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])

    session.mirror_body(base_plane="XY")

    assert session.get_selection()["items"] == []


def test_mirroring_without_a_body_selection_is_refused(session: Session) -> None:
    _box(session)
    session.clear_selection()
    with pytest.raises(ValueError):
        session.mirror_body(base_plane="XY")


def test_redo_puts_a_mirrored_body_back(session: Session) -> None:
    body, _ = _box(session)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    applied = session.mirror_body(base_plane="XY")
    created_id = applied["created"][0]

    session.undo()
    session.redo()

    assert session.document.get_body(created_id)


def test_a_mirror_survives_save_and_replay(session: Session) -> None:
    body, _ = _box(session)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    applied = session.mirror_body(base_plane="XY")
    created_id = applied["created"][0]
    volume_before = query.volume(session.document.get_body(created_id).shape)

    from duocad.document import Document
    from duocad.oplog import OpLog

    replayed = Document()
    replayed.log = OpLog.from_json(session.document.log.to_json())
    replayed.log.replay(replayed)

    volume_after = query.volume(replayed.get_body(created_id).shape)
    assert volume_after == pytest.approx(volume_before)
