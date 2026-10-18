"""`mirror_sketch_entities` / `mirror_body` の契約 —— 「鏡像反転コピーを作る」1点だけの verb。"""
from __future__ import annotations

import math

import pytest

from duocad import kernel, kernel_sketch, query, sketch
from duocad.oplog import verb_handler
from duocad.server import Session
from document_support import corner_set as _corner_set, replay_through_json
from session_support import SIDE, box as _box, cylinder, select as _select


def _params(session: Session, sketch_id: str, entity_id: str) -> dict:
    return sketch.entity_params(session.document, sketch_id, entity_id)


def _mirrored_corner_set(params: dict, ax, ay, ux, uy) -> list:
    """元の4隅を1点ずつ軸へ映した集合(期待値の作り方)。"""
    mirrored = []
    for u, v in kernel_sketch.rectangle_corners(params):
        t = (u - ax) * ux + (v - ay) * uy
        mirrored.append((round(2.0 * (ax + t * ux) - u, 9), round(2.0 * (ay + t * uy) - v, 9)))
    return sorted(mirrored)


def _cylinder(session: Session):
    """非平面の面を持つボディ(立方体から離れた小さな円柱)。"""
    return cylinder(session, 5.0, 5.0, center=(100.0, 100.0), mode="new")


def _curved_face_index(shape) -> int:
    count = kernel.face_count(shape)
    for index in range(1, count + 1):
        if kernel.classify_face(kernel.face_of(shape, index)) != "plane":
            return index
    raise AssertionError("非平面の面が無い(テストの前提が崩れている)")


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
    entity_id = session.add_circle(sketch_id, [3.0, 0.0], 4.0)["entity_id"]
    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": None}])

    applied = session.mirror_sketch_entities({"sketch": sketch_id, "entity": axis_id})

    mirrored = _params(session, sketch_id, applied["created"][0])
    assert mirrored == pytest.approx({"cx": 3.0, "cy": 10.0, "diameter": 4.0})


def test_mirroring_an_arc_reflects_its_centre_and_angles(session: Session) -> None:
    """斜めの軸 y=x に映すと、中心 (0,5)→(5,0)、角度 θ→90°-θ(始点と終点が入れ替わり、反時計回りは保たれる)。
    水平な軸では角度の項が恒等になり見えない。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]
    axis_id = session.add_line(sketch_id, [0.0, 0.0], [1.0, 1.0])["entity_id"]
    entity_id = session.add_arc(sketch_id, [0.0, 5.0], 2.0, 10.0, 50.0)["entity_id"]
    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": None}])

    applied = session.mirror_sketch_entities({"sketch": sketch_id, "entity": axis_id})

    mirrored = _params(session, sketch_id, applied["created"][0])
    assert (mirrored["cx"], mirrored["cy"]) == pytest.approx((5.0, 0.0))
    assert (mirrored["start_deg"] % 360.0, mirrored["end_deg"] % 360.0) == pytest.approx((40.0, 80.0))


def _add_bezier(session: Session, sketch_id: str, params: dict) -> str:
    """ベジエを直接モデルへ置く(**取り込み専用の種別で、作るverbが無い**。
    `tests/test_move_verbs.py::test_two_control_points_of_one_bezier_both_move` と同じ手順)。
    """
    from duocad import parameters as parameters_mod
    from duocad.sketch import Entity, get_sketch

    refs = {key: parameters_mod.register_value(session.document, value) for key, value in params.items()}
    entity_id = f"entity-bezier-{len(get_sketch(session.document, sketch_id).entities)}"
    get_sketch(session.document, sketch_id).entities.append(
        Entity(id=entity_id, kind="bezier", params=refs)
    )
    return entity_id


def test_mirroring_a_bezier_reflects_all_its_control_points(session: Session) -> None:
    sketch_id = session.sketch_create("XY")["sketch_id"]
    axis_id = session.add_line(sketch_id, [0.0, 0.0], [10.0, 0.0])["entity_id"]
    entity_id = _add_bezier(
        session,
        sketch_id,
        {"x0": 0.0, "y0": 1.0, "cx1": 1.0, "cy1": 2.0, "cx2": 3.0, "cy2": 4.0, "x1": 5.0, "y1": 6.0},
    )
    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": None}])

    applied = session.mirror_sketch_entities({"sketch": sketch_id, "entity": axis_id})

    mirrored = _params(session, sketch_id, applied["created"][0])
    # 軸は (0,0)-(10,0) = u軸そのものなので、鏡映は全制御点の y を反転するだけ
    assert mirrored == pytest.approx(
        {"x0": 0.0, "y0": -1.0, "cx1": 1.0, "cy1": -2.0, "cx2": 3.0, "cy2": -4.0, "x1": 5.0, "y1": -6.0}
    )
    # 元は変わっていない(常に複製)
    assert _params(session, sketch_id, entity_id) == pytest.approx(
        {"x0": 0.0, "y0": 1.0, "cx1": 1.0, "cy1": 2.0, "cx2": 3.0, "cy2": 4.0, "x1": 5.0, "y1": 6.0}
    )


@pytest.mark.parametrize(
    "axis_p1, angle",
    [
        ([1.0, 0.0], 0.0),    # 軸が水平(辺と平行)
        ([0.0, 1.0], 0.0),    # 軸が垂直(辺と直交)
        ([1.0, 1.0], 0.0),    # 斜めの軸
        ([2.0, 1.0], 33.0),   # 斜めの軸 × 元から傾いた長方形
    ],
)
def test_mirroring_a_rectangle_stays_one_rectangle(session, axis_p1, angle) -> None:
    """**どんな軸でも長方形1つのまま**鏡映でき、4隅が元の4隅を映した集合と一致する。

    落ちたら、`_mirror_rectangle` が張る4隅が「元の隅を1点ずつ映したもの」からずれている。
    """
    sketch_id = session.sketch_create("XY")["sketch_id"]
    axis_id = session.add_line(sketch_id, [0.0, 0.0], axis_p1)["entity_id"]
    entity_id = session.add_rectangle(sketch_id, [1.0, 1.0], 4.0, 2.0, angle)["entity_id"]
    original = _params(session, sketch_id, entity_id)
    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": None}])

    applied = session.mirror_sketch_entities({"sketch": sketch_id, "entity": axis_id})

    assert len(applied["created"]) == 1
    created = sketch.get_entity(
        sketch.get_sketch(session.document, sketch_id), applied["created"][0]
    )
    assert created.kind == "rectangle"
    mirrored = _params(session, sketch_id, created.id)
    assert (mirrored["width"], mirrored["height"]) == pytest.approx((4.0, 2.0))
    length = math.hypot(axis_p1[0], axis_p1[1])
    assert _corner_set(mirrored) == pytest.approx(
        _mirrored_corner_set(original, 0.0, 0.0, axis_p1[0] / length, axis_p1[1] / length),
        abs=1e-9,
    )
    assert _params(session, sketch_id, entity_id) == pytest.approx(original), "元は動かない"


def test_a_rectangle_from_a_log_without_angle_still_refuses_a_self_overlapping_copy(
    session: Session,
) -> None:
    """`angle` を持たない時代のログから再生した長方形も、元と重なる複製は断る。

    落ちたら、`_mirrored_plan` の重なり検査がキーの数の違いだけで不一致と判定している
    (旧文書の長方形は `params` が4個で、鏡映後は5個になる)。重なった複製を作ると
    領域検出が同じ辺を2本見る。
    """
    document = session.document
    sketch_id = sketch.sketch_create(document, "XY")
    entity_id = "entity-legacy-mirror"
    operation = document.log.append(
        "add_rectangle",
        {
            "sketch_id": sketch_id,
            "entity_id": entity_id,
            "kind": "rectangle",
            "params": {"x": 0.0, "y": 0.0, "width": 10.0, "height": 4.0},
        },
    )
    verb_handler("add_rectangle").do(document, operation)
    # 長方形の水平な中心線を軸にすると、鏡映しても元とぴったり重なる。
    axis_id = session.add_line(sketch_id, [-5.0, 2.0], [15.0, 2.0])["entity_id"]
    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": None}])

    with pytest.raises(ValueError) as error:
        session.mirror_sketch_entities({"sketch": sketch_id, "entity": axis_id})

    assert "元と完全に重なる" in str(error.value)
    assert len(sketch.get_sketch(document, sketch_id).entities) == 2


def test_the_axis_must_be_a_line_entity(session: Session) -> None:
    sketch_id = session.sketch_create("XY")["sketch_id"]
    circle_id = session.add_circle(sketch_id, [0.0, 0.0], 6.0)["entity_id"]
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
    """軸自身が対象の選択に混ざっていてもよい(自分自身を軸にしつつ他を鏡映する)。"""
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

    for source, created_id in zip((first, second), applied["created"]):
        mirrored = kernel.bounding_box(session.document.get_body(created_id).shape)
        original = kernel.bounding_box(source.shape)
        assert mirrored["min"][:2] == pytest.approx(original["min"][:2], abs=1e-4)
        assert (mirrored["min"][2], mirrored["max"][2]) == pytest.approx((-SIDE, 0.0), abs=1e-4)


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


def test_a_mirror_survives_save_and_replay(session: Session) -> None:
    """鏡映は体積を変えないので、再生で位置がずれても体積では分からない。境界箱で見る。"""
    body, _ = _box(session)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    applied = session.mirror_body(base_plane="XY")
    created_id = applied["created"][0]
    box_before = kernel.bounding_box(session.document.get_body(created_id).shape)

    replayed = replay_through_json(session.document)

    box_after = kernel.bounding_box(replayed.get_body(created_id).shape)
    assert box_after["min"] == pytest.approx(box_before["min"])
    assert box_after["max"] == pytest.approx(box_before["max"])
    assert box_after["min"] != pytest.approx(kernel.bounding_box(body.shape)["min"]), "鏡映先が元と同じ位置にある(検査の前提)"


def test_redo_of_a_two_body_mirror_leaves_nothing_behind_when_the_second_copy_fails(
    session: Session, monkeypatch
) -> None:
    """落ちたら、2体の鏡映の再生で2体目の `mirror_shape` が落ちたときに1体目だけ足された文書が残る
    (`_common.add_transformed_copies` は全部の形状を作ってから足す)。"""
    body_a, _ = _box(session)
    body_b = _cylinder(session)
    _select(
        session,
        [
            {"body": body_a.id, "kind": "face", "index": 1},
            {"body": body_b.id, "kind": "face", "index": 1},
        ],
    )
    session.mirror_body(base_plane="YZ")
    session.undo()
    count_before = len(session.document.bodies)

    real_mirror = kernel.mirror_shape
    calls = {"n": 0}

    def flaky_mirror(shape, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("2体目で落ちる")
        return real_mirror(shape, *args, **kwargs)

    monkeypatch.setattr(kernel, "mirror_shape", flaky_mirror)
    with pytest.raises(RuntimeError):
        session.redo()
    assert len(session.document.bodies) == count_before, "1体目だけ足された文書が残っている"


def test_a_duplicate_copy_id_leaves_no_entity_and_no_parameter_behind(session: Session) -> None:
    """落ちたら、鏡映の複製2件のうち2件目の ID が既存と重なるときに1件目だけ足され、名前つきの値も
    増えたまま残る(`sketch.add_entities` は全部の ID を先に検査する)。"""
    from duocad.verbs import mirror_sketch as verb

    sketch_id = session.sketch_create("XY")["sketch_id"]
    first = session.add_circle(sketch_id, [1.0, 2.0], 2.0)["entity_id"]
    second = session.add_circle(sketch_id, [3.0, 4.0], 2.0)["entity_id"]
    doc = session.document
    entities_before = len(doc.sketches[0].entities)
    parameters_before = len(doc.parameters)

    with pytest.raises(ValueError, match="already exists"):
        verb._create_mirrors(doc, [(sketch_id, first), (sketch_id, second)], 0.0, 0.0, 1.0, 0.0, ["fresh", first], fresh=True)

    assert len(doc.sketches[0].entities) == entities_before
    assert len(doc.parameters) == parameters_before
