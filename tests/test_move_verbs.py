"""`move_sketch_entities` / `move_body` の契約 —— スケッチパラメータパネルの後継。

固定するのは、**選択の新しい種別**と**2つの移動verb**の約束である:

- スケッチの図形は `{"sketch", "entity", "point"}` で選べる(`point=None` が図形全体 = 辺)。
  参照は**添字ではなく `entity_id`** なので、他の図形を足しても平行移動しても失効しない
- 点を動かすと**その点だけ**、辺を動かすと**図形全体**が同じ量だけ動く(形は変わらない)
- 同じ図形に辺と点の両方が選ばれていたら**辺が勝つ**(その点だけ2回動かない)
- `copy=True` は元を残し、**対象が属する図形をまるごと**複製する(点だけは複製できない)
- `move_body` は面/辺/頂点から**ボディを逆引き**して動かす。複数ボディなら全部同じ量
- **どちらも面・辺・頂点の添字と `entity_id` を壊さない**ので、確定後に選択をリセットしない
- undo は移動なら逆向きの平行移動、複製なら作ったものの削除
- 保存 -> 読み直し -> 再生で同じ形になる(ログが唯一の正)
- `move_sketch_entities` が書いた座標は、後の `set_parameter` の `rebuild()` で**消えない**
"""
from __future__ import annotations

import asyncio
import json

import pytest

from duocad import kernel, query, selection, sketch
from duocad.document import Document
from duocad.oplog import OpLog, verb_handler
from duocad.server import Server, Session
from duocad.verbs import move_sketch_entities as move_sketch_entities_verb

DEVIATION = 0.2
SIDE = 20.0


@pytest.fixture()
def session() -> Session:
    return Session(None, DEVIATION)


def _select(session: Session, refs: list) -> None:
    session.clear_selection()
    session.modify_selection(refs, [])


def _line(session: Session):
    sketch_id = session.sketch_create("XY")["sketch_id"]
    entity_id = session.add_line(sketch_id, [0.0, 0.0], [10.0, 0.0])["entity_id"]
    return sketch_id, entity_id


def _params(session: Session, sketch_id: str, entity_id: str) -> dict:
    return sketch.entity_params(session.document, sketch_id, entity_id)


def _box(session: Session):
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_rectangle(sketch_id, [0.0, 0.0], SIDE, SIDE)
    _select(session, [{"sketch": sketch_id, "index": 1}])
    session.extrude(SIDE, mode="join")
    session.clear_selection()
    return session.document.bodies[-1], sketch_id


def _center(body) -> list:
    return kernel.bounding_box(body.shape)["center"]


# --------------------------------------------------------------------------------------
# 選択の新しい種別
# --------------------------------------------------------------------------------------
def test_a_sketch_entity_can_be_selected_as_a_point_or_as_a_whole(session: Session) -> None:
    sketch_id, entity_id = _line(session)
    point = selection.select_sketch_entity(session.document, sketch_id, entity_id, "x0")
    whole = selection.select_sketch_entity(session.document, sketch_id, entity_id)

    assert selection.ref_kind(point) == selection.SKETCH_ENTITY_KIND
    assert selection.ref_kind(whole) == selection.SKETCH_ENTITY_KIND
    assert point["point"] == "x0" and whole["point"] is None
    # **点と辺は別物**(同じ図形を指していても同じ参照ではない)
    assert not selection.same_ref(point, whole)


def test_a_region_reference_is_still_a_region(session: Session) -> None:
    """`entity` を持たない `sketch` つきの参照は今までどおり領域である(見分けの規則)。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_rectangle(sketch_id, [0.0, 0.0], 10.0, 10.0)
    region_ref = selection.select_region(session.document, sketch_id, 1)
    assert selection.ref_kind(region_ref) == selection.REGION_KIND


def test_a_point_that_the_kind_does_not_have_is_refused(session: Session) -> None:
    """半径・幅高さ・yキーは点ではない(寸法の編集は今回のスコープ外)。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]
    entity_id = session.add_circle(sketch_id, [0.0, 0.0], 5.0)["entity_id"]
    for bad in ("radius", "cy", "x0"):
        with pytest.raises(ValueError):
            selection.select_sketch_entity(session.document, sketch_id, entity_id, bad)


def test_selecting_a_sketch_entity_that_does_not_exist_is_an_error(session: Session) -> None:
    sketch_id, _entity_id = _line(session)
    with pytest.raises(KeyError):
        selection.select_sketch_entity(session.document, sketch_id, "entity-nope")
    with pytest.raises(KeyError):
        selection.select_sketch_entity(session.document, "sketch-nope", "entity-x")


def test_the_point_table_covers_every_entity_kind() -> None:
    """**全種別が点の定義を持つ**(表に1行足し忘れると move が黙って何もできなくなる)。

    寸法(半径・幅高さ・角度)は点に入らないことも同時に見る。
    """
    dimensions = {"radius", "width", "height", "start_deg", "end_deg"}
    for kind in sketch.ENTITY_KINDS:
        pairs = sketch.entity_point_keys(kind)
        assert pairs, f"{kind} に動かせる点が1つも無い"
        for x_key, y_key in pairs:
            assert x_key not in dimensions and y_key not in dimensions


# --------------------------------------------------------------------------------------
# move_sketch_entities
# --------------------------------------------------------------------------------------
def test_moving_a_point_moves_only_that_point(session: Session) -> None:
    sketch_id, entity_id = _line(session)
    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": "x0"}])
    session.move_sketch_entities(3.0, 4.0)

    assert _params(session, sketch_id, entity_id) == {
        "x0": 3.0, "y0": 4.0, "x1": 10.0, "y1": 0.0
    }


def test_moving_a_whole_entity_moves_every_point(session: Session) -> None:
    sketch_id, entity_id = _line(session)
    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": None}])
    session.move_sketch_entities(3.0, 4.0)

    assert _params(session, sketch_id, entity_id) == {
        "x0": 3.0, "y0": 4.0, "x1": 13.0, "y1": 4.0
    }


def test_moving_a_circle_leaves_its_radius_alone(session: Session) -> None:
    """寸法は動かさない(平行移動で形は変わらない)。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]
    entity_id = session.add_circle(sketch_id, [1.0, 2.0], 5.0)["entity_id"]
    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": None}])
    session.move_sketch_entities(10.0, 0.0)

    assert _params(session, sketch_id, entity_id) == {"cx": 11.0, "cy": 2.0, "radius": 5.0}


def test_a_whole_entity_wins_over_its_own_points(session: Session) -> None:
    """辺と点を同時に選んでも、その点は2回動かない(形が歪まない)。"""
    sketch_id, entity_id = _line(session)
    _select(session, [
        {"sketch": sketch_id, "entity": entity_id, "point": "x0"},
        {"sketch": sketch_id, "entity": entity_id, "point": None},
    ])
    session.move_sketch_entities(3.0, 0.0)

    assert _params(session, sketch_id, entity_id) == {
        "x0": 3.0, "y0": 0.0, "x1": 13.0, "y1": 0.0
    }
    # **ログにも点は残らない**。座標だけを見ると、いまの実装は「全部の結果を先に作って
    # から書き込む」ので点と辺が同じ値を書き、二重移動は表に出ない —— 逐次書き込みへ
    # 変えた瞬間に静かに壊れる形なので、畳んだこと自体をここで固定する。
    assert session.document.log.entries[-1].args["targets"] == [
        {"sketch": sketch_id, "entity": entity_id, "point": None}
    ]


def test_the_same_point_selected_twice_moves_once(session: Session) -> None:
    """参照は選択集合が重複除去するが、verb側でも畳んでいることを直接見る。"""
    sketch_id, entity_id = _line(session)
    doc = session.document
    targets = [
        {"sketch": sketch_id, "entity": entity_id, "point": "x0"},
        {"sketch": sketch_id, "entity": entity_id, "point": "x0"},
    ]
    move_sketch_entities_verb(doc, targets, 5.0, 0.0)
    assert _params(session, sketch_id, entity_id)["x0"] == 5.0


def test_two_points_of_one_entity_both_move(session: Session) -> None:
    """**同じ図形の複数の点を同時に動かすと、全部動く。**

    ウィンドウ選択で線分をまるごと囲えば両端が選ばれるので、これは普通に踏む経路である。
    対象ごとに「自分だけを動かした完全な `params`」を作って順に書き戻す実装だと、
    後の書き込みが前の書き込みを丸ごと打ち消して**最後の点しか動かない**
    (`_apply` は図形ごとに1つの計画へ併合してから書く)。
    """
    sketch_id, entity_id = _line(session)
    _select(session, [
        {"sketch": sketch_id, "entity": entity_id, "point": "x0"},
        {"sketch": sketch_id, "entity": entity_id, "point": "x1"},
    ])
    session.move_sketch_entities(0.0, 5.0)

    assert _params(session, sketch_id, entity_id) == {
        "x0": 0.0, "y0": 5.0, "x1": 10.0, "y1": 5.0
    }


def test_undo_restores_both_points_of_one_entity(session: Session) -> None:
    """併合した移動のundoも、図形ごとに1つの計画へ併合される(片方だけ戻らない)。"""
    sketch_id, entity_id = _line(session)
    _select(session, [
        {"sketch": sketch_id, "entity": entity_id, "point": "x0"},
        {"sketch": sketch_id, "entity": entity_id, "point": "x1"},
    ])
    session.move_sketch_entities(3.0, 5.0)
    session.undo()

    restored = _params(session, sketch_id, entity_id)
    assert restored["x0"] == pytest.approx(0.0, abs=1e-9)
    assert restored["y0"] == pytest.approx(0.0, abs=1e-9)
    assert restored["x1"] == pytest.approx(10.0, abs=1e-9)
    assert restored["y1"] == pytest.approx(0.0, abs=1e-9)


def test_two_control_points_of_one_bezier_both_move(session: Session) -> None:
    """点が4つある種別(ベジエ)でも併合が効く(線分だけの性質ではない)。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]
    # **ベジエを作るverbは無い**(取り込み専用の種別)ので、モデルへ直接置く。
    doc = session.document
    from duocad.sketch import Entity, get_sketch
    from duocad import parameters as parameters_mod

    params = {
        "x0": 0.0, "y0": 0.0, "cx1": 1.0, "cy1": 2.0,
        "cx2": 3.0, "cy2": 2.0, "x1": 4.0, "y1": 0.0,
    }
    refs = {key: parameters_mod.register_value(doc, value) for key, value in params.items()}
    bezier_id = "entity-bezier-test"
    get_sketch(doc, sketch_id).entities.append(
        Entity(id=bezier_id, kind="bezier", params=refs)
    )
    _select(session, [
        {"sketch": sketch_id, "entity": bezier_id, "point": "cx1"},
        {"sketch": sketch_id, "entity": bezier_id, "point": "cx2"},
    ])
    session.move_sketch_entities(0.0, 1.0)

    moved = _params(session, sketch_id, bezier_id)
    assert moved["cy1"] == pytest.approx(3.0)
    assert moved["cy2"] == pytest.approx(3.0)
    assert moved["y0"] == pytest.approx(0.0)  # 選んでいない点は動かない


def test_a_copy_whose_second_id_collides_adds_nothing(session: Session) -> None:
    """**IDが衝突したら、図形を1つも足さずに落ちる**(部分更新の残骸を作らない)。

    **2つ目のIDだけを衝突させる**のが要点である。1つ目で落ちる作りの検査だと、
    「検査してから書く」を「書きながら検査する」へ崩しても赤くならない ——
    1つ目の妥当性を通してから2つ目で落ちる形が、部分更新が起きる唯一の順序である。
    """
    from duocad.oplog import Operation
    from duocad.verbs.move_sketch import _do_move_sketch_entities

    sketch_id = session.sketch_create("XY")["sketch_id"]
    first = session.add_circle(session.document.sketches[0].id, [0.0, 0.0], 2.0)["entity_id"]
    second = session.add_circle(sketch_id, [10.0, 0.0], 2.0)["entity_id"]
    before_ids = [entity.id for entity in sketch.get_sketch(session.document, sketch_id).entities]
    before_parameters = dict(session.document.parameters)

    fresh = "entity-brand-new-000"
    operation = Operation(
        seq=99,
        verb="move_sketch_entities",
        args={
            "targets": [
                {"sketch": sketch_id, "entity": first, "point": None},
                {"sketch": sketch_id, "entity": second, "point": None},
            ],
            "dx": 0.0,
            "dy": 20.0,
            "copy": True,
            # 1つ目は新しいID、**2つ目だけが既存のIDと衝突する**
            "created": [fresh, second],
        },
    )
    with pytest.raises(ValueError):
        _do_move_sketch_entities(session.document, operation)

    after_ids = [entity.id for entity in sketch.get_sketch(session.document, sketch_id).entities]
    assert after_ids == before_ids, f"衝突で落ちたのに図形が増えている(部分更新): {after_ids}"
    assert session.document.parameters == before_parameters, "名前つきの値だけが増えている"


def test_moving_two_entities_moves_both(session: Session) -> None:
    sketch_id = session.sketch_create("XY")["sketch_id"]
    first = session.add_circle(sketch_id, [0.0, 0.0], 2.0)["entity_id"]
    second = session.add_circle(sketch_id, [10.0, 0.0], 2.0)["entity_id"]
    _select(session, [
        {"sketch": sketch_id, "entity": first, "point": None},
        {"sketch": sketch_id, "entity": second, "point": None},
    ])
    session.move_sketch_entities(0.0, 7.0)

    assert _params(session, sketch_id, first)["cy"] == 7.0
    assert _params(session, sketch_id, second)["cy"] == 7.0
    # **1回の呼び出し = ログ1行**(点ごとに分解しない)
    assert session.document.log.entries[-1].verb == "move_sketch_entities"
    assert session.document.log.entries[-2].verb == "add_circle"


def test_moving_keeps_the_named_parameter_editable(session: Session) -> None:
    """名前つきの値の側を書き換えるので、`set_parameter` で後から打ち直せる道が残る。"""
    sketch_id, entity_id = _line(session)
    entity = sketch.get_entity(sketch.get_sketch(session.document, sketch_id), entity_id)
    name = entity.params["x0"]
    assert isinstance(name, str)

    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": "x0"}])
    session.move_sketch_entities(3.0, 0.0)
    assert session.document.parameters[name] == 3.0


def test_a_move_that_would_break_the_shape_is_refused(session: Session) -> None:
    """端点が重なる線分は作らせない(作成時と同じ検査を通す)。文書は無傷。"""
    sketch_id, entity_id = _line(session)
    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": "x0"}])
    before = _params(session, sketch_id, entity_id)
    log_length = len(session.document.log.entries)

    with pytest.raises(ValueError):
        session.move_sketch_entities(10.0, 0.0)

    assert _params(session, sketch_id, entity_id) == before
    assert len(session.document.log.entries) == log_length


def test_a_zero_move_is_refused(session: Session) -> None:
    sketch_id, entity_id = _line(session)
    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": None}])
    with pytest.raises(ValueError):
        session.move_sketch_entities(0.0, 0.0)


def test_moving_without_a_sketch_selection_is_refused(session: Session) -> None:
    session.clear_selection()
    with pytest.raises(selection.SelectionEmpty):
        session.move_sketch_entities(1.0, 0.0)


def test_a_face_selection_does_not_feed_the_sketch_move(session: Session) -> None:
    """種別が違えば `SelectionKindMismatch`(黙って0件扱いにしない)。"""
    body, _sketch_id = _box(session)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    with pytest.raises(selection.SelectionKindMismatch):
        session.move_sketch_entities(1.0, 0.0)


def test_copying_an_entity_keeps_the_original(session: Session) -> None:
    sketch_id, entity_id = _line(session)
    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": None}])
    applied = session.move_sketch_entities(0.0, 5.0, copy=True)

    entities = sketch.get_sketch(session.document, sketch_id).entities
    assert len(entities) == 2
    assert _params(session, sketch_id, entity_id) == {
        "x0": 0.0, "y0": 0.0, "x1": 10.0, "y1": 0.0
    }
    (new_id,) = applied["created"]
    assert _params(session, sketch_id, new_id) == {
        "x0": 0.0, "y0": 5.0, "x1": 10.0, "y1": 5.0
    }


def test_copying_a_point_copies_the_whole_entity(session: Session) -> None:
    """点だけの複製は表現できないので、複製の単位は必ず図形1つ(本人明言)。"""
    sketch_id, entity_id = _line(session)
    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": "x0"}])
    applied = session.move_sketch_entities(0.0, 5.0, copy=True)

    (new_id,) = applied["created"]
    assert _params(session, sketch_id, new_id) == {
        "x0": 0.0, "y0": 5.0, "x1": 10.0, "y1": 5.0
    }


def test_copying_two_points_of_one_entity_makes_one_copy(session: Session) -> None:
    sketch_id, entity_id = _line(session)
    _select(session, [
        {"sketch": sketch_id, "entity": entity_id, "point": "x0"},
        {"sketch": sketch_id, "entity": entity_id, "point": "x1"},
    ])
    applied = session.move_sketch_entities(0.0, 5.0, copy=True)
    assert len(applied["created"]) == 1


def test_the_copy_is_not_part_of_a_pattern(session: Session) -> None:
    """反復の元を複製しても、複製は素の図形になる(反復には入らない)。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]
    entity_id = session.add_circle(sketch_id, [0.0, 0.0], 2.0)["entity_id"]
    session.pattern_rectangular(sketch_id, [entity_id], 10.0, 10.0, 2, 2)
    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": None}])
    applied = session.move_sketch_entities(0.0, 30.0, copy=True)

    (new_id,) = applied["created"]
    patterns = sketch.get_sketch(session.document, sketch_id).patterns
    assert len(patterns) == 1
    assert new_id not in patterns[0].sources


def test_undo_restores_the_moved_point(session: Session) -> None:
    sketch_id, entity_id = _line(session)
    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": "x0"}])
    session.move_sketch_entities(3.0, 4.0)
    session.undo()

    moved = _params(session, sketch_id, entity_id)
    assert moved["x0"] == pytest.approx(0.0, abs=1e-9)
    assert moved["y0"] == pytest.approx(0.0, abs=1e-9)


def test_undo_removes_the_copy(session: Session) -> None:
    sketch_id, entity_id = _line(session)
    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": None}])
    applied = session.move_sketch_entities(0.0, 5.0, copy=True)
    (new_id,) = applied["created"]
    before_parameters = len(session.document.parameters)

    session.undo()
    entities = sketch.get_sketch(session.document, sketch_id).entities
    assert [entity.id for entity in entities] == [entity_id]
    # **名前つきの値も一緒に消す**(消し忘れると `d1` が増え続ける)
    assert len(session.document.parameters) == before_parameters - 4
    assert new_id not in {entity.id for entity in entities}


def test_redo_puts_the_move_back(session: Session) -> None:
    sketch_id, entity_id = _line(session)
    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": "x0"}])
    session.move_sketch_entities(3.0, 4.0)
    session.undo()
    session.redo()
    assert _params(session, sketch_id, entity_id)["x0"] == pytest.approx(3.0)


def test_the_move_survives_save_and_replay(session: Session) -> None:
    """**ログが唯一の正**。保存したログを再生した文書が同じ座標になる。"""
    sketch_id, entity_id = _line(session)
    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": "x0"}])
    session.move_sketch_entities(3.0, 4.0, copy=True)

    replayed = Document()
    replayed.log = OpLog.from_json(session.document.log.to_json())
    replayed.log.replay(replayed)

    assert [entity.id for entity in replayed.sketches[0].entities] == [
        entity.id for entity in session.document.sketches[0].entities
    ]
    for entity in replayed.sketches[0].entities:
        assert sketch.entity_params(replayed, replayed.sketches[0].id, entity.id) == (
            sketch.entity_params(session.document, sketch_id, entity.id)
        )


def test_the_selection_resets_after_a_sketch_move(session: Session) -> None:
    """確定後に選択を空にする(他の形状変更verbと同じ既定の契約。
    `tests/test_server_selection_reset.py` の RESETTING_VERBS 参照)。
    """
    sketch_id, entity_id = _line(session)
    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": "x0"}])
    session.move_sketch_entities(1.0, 0.0)

    assert session.get_selection()["count"] == 0
    assert _params(session, sketch_id, entity_id)["x0"] == pytest.approx(1.0)


def test_a_later_set_parameter_does_not_erase_the_move(session: Session) -> None:
    """`rebuild()` の値の強制が **move の後まで効かない**ことを固定する。

    `set_parameter` はログの後ろに載るので、再生では「先に最終値を当ててから」進む。
    その強制を当の `set_parameter` より後ろのオペレーションにも掛け続けると、
    後から積んだ `move_sketch_entities` の座標が毎回上書きされて**黙って消える**。
    """
    sketch_id, entity_id = _line(session)
    entity = sketch.get_entity(sketch.get_sketch(session.document, sketch_id), entity_id)
    x0_name = entity.params["x0"]
    y1_name = entity.params["y1"]

    session.set_parameter(x0_name, 1.0)          # 先に x0 を打ち直す
    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": "x0"}])
    session.move_sketch_entities(2.0, 0.0)       # その x0 を move で動かす
    session.set_parameter(y1_name, 5.0)          # 別の値を打ち直す -> rebuild が走る

    moved = _params(session, sketch_id, entity_id)
    assert moved["x0"] == pytest.approx(3.0), "move が rebuild で消えている"
    assert moved["y1"] == pytest.approx(5.0)


def test_set_parameter_still_wins_when_it_comes_last(session: Session) -> None:
    """逆順(move -> set_parameter)なら `set_parameter` の絶対値が最終値になる(既存の意味)。"""
    sketch_id, entity_id = _line(session)
    entity = sketch.get_entity(sketch.get_sketch(session.document, sketch_id), entity_id)
    x0_name = entity.params["x0"]

    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": "x0"}])
    session.move_sketch_entities(2.0, 0.0)
    session.set_parameter(x0_name, 7.0)

    assert _params(session, sketch_id, entity_id)["x0"] == pytest.approx(7.0)


# --------------------------------------------------------------------------------------
# move_body
# --------------------------------------------------------------------------------------
def test_moving_a_body_from_a_face_selection(session: Session) -> None:
    body, _sketch_id = _box(session)
    before = _center(body)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    session.move_body(10.0, 0.0, 5.0)

    after = _center(session.document.get_body(body.id))
    assert after[0] == pytest.approx(before[0] + 10.0)
    assert after[1] == pytest.approx(before[1])
    assert after[2] == pytest.approx(before[2] + 5.0)


def test_moving_a_body_keeps_its_volume_and_face_count(session: Session) -> None:
    body, _sketch_id = _box(session)
    faces = kernel.face_count(body.shape)
    volume = query.volume(body.shape)
    _select(session, [{"body": body.id, "kind": "edge", "index": 1}])
    session.move_body(1.0, 2.0, 3.0)

    moved = session.document.get_body(body.id)
    assert kernel.face_count(moved.shape) == faces
    assert query.volume(moved.shape) == pytest.approx(volume)


def test_a_vertex_selection_also_names_the_body(session: Session) -> None:
    body, _sketch_id = _box(session)
    before = _center(body)
    _select(session, [{"body": body.id, "kind": "vertex", "index": 1}])
    session.move_body(0.0, 0.0, 4.0)
    assert _center(session.document.get_body(body.id))[2] == pytest.approx(before[2] + 4.0)


def test_two_bodies_move_by_the_same_amount(session: Session) -> None:
    first, sketch_id = _box(session)
    session.add_rectangle(sketch_id, [40.0, 0.0], SIDE, SIDE)
    _select(session, [{"sketch": sketch_id, "index": 2}])
    session.extrude(SIDE, mode="new")
    second = session.document.bodies[-1]
    before = (_center(first), _center(second))

    _select(session, [
        {"body": first.id, "kind": "face", "index": 1},
        {"body": second.id, "kind": "face", "index": 2},
    ])
    session.move_body(0.0, 0.0, 6.0)

    assert _center(session.document.get_body(first.id))[2] == pytest.approx(before[0][2] + 6.0)
    assert _center(session.document.get_body(second.id))[2] == pytest.approx(before[1][2] + 6.0)
    # **1呼び出し = 1オペレーション**(ボディごとに分解しない)
    assert session.document.log.entries[-1].verb == "move_body"
    assert len(session.document.log.entries[-1].args["bodies"]) == 2


def test_two_faces_of_one_body_move_it_once(session: Session) -> None:
    body, _sketch_id = _box(session)
    before = _center(body)
    _select(session, [
        {"body": body.id, "kind": "face", "index": 1},
        {"body": body.id, "kind": "face", "index": 2},
    ])
    session.move_body(0.0, 0.0, 3.0)
    assert _center(session.document.get_body(body.id))[2] == pytest.approx(before[2] + 3.0)


def test_copying_a_body_adds_one(session: Session) -> None:
    body, _sketch_id = _box(session)
    before = _center(body)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    applied = session.move_body(50.0, 0.0, 0.0, copy=True)

    (new_id,) = applied["created"]
    assert len(session.document.bodies) == 2
    assert _center(session.document.get_body(body.id)) == pytest.approx(before)
    assert _center(session.document.get_body(new_id))[0] == pytest.approx(before[0] + 50.0)
    # **画面が形を取り直す先**(元 + 複製)がイベントに載っている
    assert set(applied["changed_bodies"]) == {body.id, new_id}


def test_the_copy_is_an_independent_shape(session: Session) -> None:
    """複製元と実体を共有しない(片方をフィレットしても、もう片方は変わらない)。"""
    body, _sketch_id = _box(session)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    (new_id,) = session.move_body(50.0, 0.0, 0.0, copy=True)["created"]
    copy_body = session.document.get_body(new_id)
    assert copy_body.shape is not body.shape
    assert not copy_body.shape.IsPartner(body.shape)


def test_a_zero_body_move_is_refused(session: Session) -> None:
    body, _sketch_id = _box(session)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    with pytest.raises(ValueError):
        session.move_body(0.0, 0.0, 0.0)


def test_moving_a_body_without_a_shape_selection_is_refused(session: Session) -> None:
    body, sketch_id = _box(session)
    _select(session, [{"sketch": sketch_id, "index": 1}])  # 領域はボディを名指ししない
    with pytest.raises(selection.SelectionKindMismatch):
        session.move_body(1.0, 0.0, 0.0)


def test_a_hidden_body_cannot_be_moved(session: Session) -> None:
    """選んだ後に隠された順序を弾く(`fillet` / `chamfer` と同じ門)。"""
    body, _sketch_id = _box(session)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    selection.set_visible(session.document, body.id, False)
    with pytest.raises(ValueError):
        session.move_body(1.0, 0.0, 0.0)


def test_undo_puts_the_body_back_exactly(session: Session) -> None:
    """平行移動の逆変換は厳密に元へ戻る(スナップショットを控えなくてよい理由)。"""
    body, _sketch_id = _box(session)
    before = _center(body)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    session.move_body(12.5, -3.25, 7.75)
    session.undo()

    after = _center(session.document.get_body(body.id))
    assert after == pytest.approx(before, abs=1e-12)


def test_undo_removes_the_copied_body(session: Session) -> None:
    body, _sketch_id = _box(session)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    (new_id,) = session.move_body(50.0, 0.0, 0.0, copy=True)["created"]
    session.undo()
    assert [b.id for b in session.document.bodies] == [body.id]
    assert new_id not in {b.id for b in session.document.bodies}


def test_the_body_move_survives_save_and_replay(session: Session) -> None:
    body, _sketch_id = _box(session)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    session.move_body(10.0, 0.0, 0.0)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])  # move_bodyでリセットされるため選び直す
    session.move_body(0.0, 0.0, 5.0, copy=True)
    expected = [(b.id, _center(b)) for b in session.document.bodies]

    replayed = Document()
    replayed.log = OpLog.from_json(session.document.log.to_json())
    replayed.log.replay(replayed)

    assert [b.id for b in replayed.bodies] == [body_id for body_id, _ in expected]
    for (_body_id, center), rebuilt in zip(expected, replayed.bodies):
        assert _center(rebuilt) == pytest.approx(center)


def test_the_selection_resets_after_a_body_move(session: Session) -> None:
    """確定後に選択を空にする(他の形状変更verbと同じ既定の契約)。"""
    body, _sketch_id = _box(session)
    _select(session, [{"body": body.id, "kind": "face", "index": 3}])
    session.move_body(1.0, 0.0, 0.0)

    assert session.get_selection()["items"] == []


def test_the_face_indices_do_not_move_with_the_body(session: Session) -> None:
    """**添字と面の対応が変わらない**こと自体を、面の法線で確かめる。"""
    body, _sketch_id = _box(session)
    before = {face["index"]: face["normal"] for face in query.describe_faces(body.shape)}
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    session.move_body(3.0, 4.0, 5.0)

    after = {
        face["index"]: face["normal"]
        for face in query.describe_faces(session.document.get_body(body.id).shape)
    }
    assert after.keys() == before.keys()
    for index, normal in before.items():
        assert after[index] == pytest.approx(normal)


def test_the_body_revision_moves_with_the_shape(session: Session) -> None:
    """形状が差し替わるので版が上がる(上がらないと画面が古い位置を描き続ける)。"""
    body, _sketch_id = _box(session)
    before = session.body_revision(body.id)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    session.move_body(5.0, 0.0, 0.0)
    assert session.body_revision(body.id) > before


# --------------------------------------------------------------------------------------
# kernel の平行移動そのもの
# --------------------------------------------------------------------------------------
def test_translating_a_shape_does_not_touch_the_original() -> None:
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

    shape = BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape()
    before = kernel.bounding_box(shape)["center"]
    moved = kernel.translate_shape(shape, 1.0, 2.0, 3.0)

    assert kernel.bounding_box(shape)["center"] == pytest.approx(before)
    assert kernel.bounding_box(moved)["center"] == pytest.approx(
        [before[0] + 1.0, before[1] + 2.0, before[2] + 3.0]
    )


def test_translating_back_is_exact() -> None:
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

    shape = BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape()
    before = kernel.bounding_box(shape)["center"]
    there = kernel.translate_shape(shape, 1.3, -2.7, 3.9)
    back = kernel.translate_shape(there, -1.3, 2.7, -3.9)
    assert kernel.bounding_box(back)["center"] == pytest.approx(before, abs=1e-12)


# --------------------------------------------------------------------------------------
# move_body(mode="rotate")
# --------------------------------------------------------------------------------------
#
# 固定するのは4つ:
#
# - 軸の指し方が**辺でも数値でも同じ結果**になること(辺は「点+向き」に潰されるだけ)
# - 回転中心を省いたら**対象の重心**を通る軸になること(境界箱の中心ではない)
# - 複数対象は**1つの合成重心**まわりに回ること(対象ごとにばらばらの中心を作らない)
# - **並進と回転が排他**であること(片方のモードにもう片方の引数を渡すと必ず落ちる)
def _bbox(body) -> dict:
    return kernel.bounding_box(body.shape)


def _vertical_edge_at(body, u: float, v: float) -> int:
    """(u, v) に立っている**まっすぐな縦の辺**の1始まりの添字。無ければ AssertionError。"""
    for edge in query.describe_edges(body.shape):
        if edge["kind"] != "line":
            continue
        start, end = edge["start"], edge["end"]
        if start[2] == end[2]:
            continue  # 水平な辺
        if (start[0], start[1]) == pytest.approx((u, v)) and (end[0], end[1]) == pytest.approx((u, v)):
            return edge["index"]
    raise AssertionError(f"({u}, {v}) に縦の辺が無い")


def _vertex_at(body, point) -> int:
    """指定座標の頂点の1始まりの添字。無ければ AssertionError。"""
    for vertex in kernel.vertex_positions(body.shape):
        if vertex["point"] == pytest.approx(list(point)):
            return vertex["index"]
    raise AssertionError(f"{point} に頂点が無い")


def test_rotating_a_body_about_a_numeric_axis(session: Session) -> None:
    """原点まわりに90度。20mm角の箱(0..20)は x が負側へ回り込む。"""
    body, _sketch_id = _box(session)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    session.move_body(mode="rotate", axis_point=[0.0, 0.0, 0.0], axis_dir=[0.0, 0.0, 1.0], angle=90.0)

    box = _bbox(session.document.get_body(body.id))
    assert box["min"][:2] == pytest.approx([-SIDE, 0.0], abs=1e-6)
    assert box["max"][:2] == pytest.approx([0.0, SIDE], abs=1e-6)
    assert box["min"][2] == pytest.approx(0.0, abs=1e-6)


def test_an_edge_axis_and_the_same_axis_in_numbers_agree(session: Session) -> None:
    """**辺で指す道と数値で指す道は同じ変換**(辺は「軸上の点+向き」に潰されるだけ)。"""
    body, _sketch_id = _box(session)
    index = _vertical_edge_at(body, 0.0, 0.0)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    session.move_body(
        mode="rotate", axis_edge={"body": body.id, "index": index}, angle=90.0
    )
    by_edge = _bbox(session.document.get_body(body.id))

    other = Session(None, DEVIATION)
    twin, _ = _box(other)
    _select(other, [{"body": twin.id, "kind": "face", "index": 1}])
    other.move_body(
        mode="rotate", axis_point=[0.0, 0.0, 0.0], axis_dir=[0.0, 0.0, 1.0], angle=90.0
    )
    by_numbers = _bbox(other.document.get_body(twin.id))

    assert by_edge["min"] == pytest.approx(by_numbers["min"], abs=1e-9)
    assert by_edge["max"] == pytest.approx(by_numbers["max"], abs=1e-9)
    # **ログに残るのは数値**(参照を残すと、undoのとき辺が既に回った後にいる)
    args = session.document.log.entries[-1].args
    assert args["axis_point"] == pytest.approx([0.0, 0.0, 0.0], abs=1e-9)
    assert args["axis_dir"] == pytest.approx([0.0, 0.0, 1.0], abs=1e-9)
    assert "axis_edge" not in args


def test_a_curved_edge_cannot_be_an_axis(session: Session) -> None:
    """円い辺を軸に読み替えない(人が指した辺と回る軸が食い違うのは例外の出ない事故)。"""
    body, sketch_id = _box(session)
    session.add_circle(sketch_id, [50.0, 50.0], 5.0)
    _select(session, [{"sketch": sketch_id, "index": 2}])
    session.extrude(5.0, mode="new")
    cylinder = session.document.bodies[-1]
    circular = next(
        edge["index"] for edge in query.describe_edges(cylinder.shape) if edge["kind"] == "circle"
    )

    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    with pytest.raises(ValueError) as error:
        session.move_body(
            mode="rotate", axis_edge={"body": cylinder.id, "index": circular}, angle=45.0
        )
    assert "not a straight line" in str(error.value)


def test_the_default_centre_is_the_centroid_not_the_origin(session: Session) -> None:
    """**中心を省くと対象の重心を軸が通る**(45度回しても重心が動かない)。

    45度にしてあるのは、90度だと正方形断面が自分自身に写って「原点まわりに回した」場合と
    見分けが付かないためである。原点まわりに回した場合(下の対照)は重心が動く。
    """
    body, _sketch_id = _box(session)
    before = kernel.centroid(body.shape)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    session.move_body(mode="rotate", axis_dir=[0.0, 0.0, 1.0], angle=45.0)

    after = kernel.centroid(session.document.get_body(body.id).shape)
    assert after == pytest.approx(before, abs=1e-6)
    # ログに残った軸は重心を通っている(境界箱の中心ではない、が言えるのは対称でない形だが、
    # ここでは「原点ではない」ことと「重心と一致する」ことを見る)
    args = session.document.log.entries[-1].args
    assert args["axis_point"][:2] == pytest.approx(before[:2], abs=1e-9)


def test_an_explicit_axis_point_moves_the_body(session: Session) -> None:
    """対照実験 —— 同じ45度でも軸を原点に置けば重心は動く(既定が効いていることの裏)。"""
    body, _sketch_id = _box(session)
    before = kernel.centroid(body.shape)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    session.move_body(
        mode="rotate", axis_point=[0.0, 0.0, 0.0], axis_dir=[0.0, 0.0, 1.0], angle=45.0
    )
    after = kernel.centroid(session.document.get_body(body.id).shape)
    assert after != pytest.approx(before, abs=1e-3)


def test_a_center_vertex_moves_the_axis_to_that_vertex(session: Session) -> None:
    """頂点を中心に指すと、軸の向きはそのままにその点を通る。"""
    body, _sketch_id = _box(session)
    index = _vertex_at(body, [0.0, 0.0, 0.0])
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    session.move_body(
        mode="rotate",
        axis_dir=[0.0, 0.0, 1.0],
        center_vertex={"body": body.id, "index": index},
        angle=90.0,
    )

    box = _bbox(session.document.get_body(body.id))
    assert box["min"][:2] == pytest.approx([-SIDE, 0.0], abs=1e-6)
    assert box["max"][:2] == pytest.approx([0.0, SIDE], abs=1e-6)


def test_rotating_keeps_the_volume_and_the_face_indices(session: Session) -> None:
    """**添字は回転でも変わらない**(`TopoDS_Shape.Moved` は下層の形状を共有する)。"""
    body, _sketch_id = _box(session)
    volume = query.volume(body.shape)
    faces = kernel.face_count(body.shape)
    normals = {face["index"]: face["normal"] for face in query.describe_faces(body.shape)}
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    session.move_body(mode="rotate", axis_dir=[0.0, 0.0, 1.0], angle=90.0)

    rotated = session.document.get_body(body.id)
    assert query.volume(rotated.shape) == pytest.approx(volume)
    assert kernel.face_count(rotated.shape) == faces
    after = {face["index"]: face["normal"] for face in query.describe_faces(rotated.shape)}
    assert after.keys() == normals.keys()
    # **同じ添字が同じ面を指し続ける**: Z向きの面は法線が変わらず、横向きの面は
    # 90度きっかり回った先の法線になっている(添字が振り直されたら両方が崩れる)
    for index, normal in normals.items():
        expected = [-normal[1], normal[0], normal[2]]  # Zまわりに+90度
        assert after[index] == pytest.approx(expected, abs=1e-9), f"面{index}"
    assert any(abs(normal[2]) < 0.5 for normal in normals.values()), "横向きの面が1枚も無い"


def test_two_bodies_rotate_about_one_composite_centre(session: Session) -> None:
    """複数対象の中心は**1つ**(対象ごとに個別の重心で回さない)。

    体積の等しい2つの箱を左右に並べて180度回すと、**入れ替わる**のが合成重心まわりに
    回った証拠である。個々の重心で回していたら、どちらもその場に留まる。
    """
    first, sketch_id = _box(session)
    session.add_rectangle(sketch_id, [40.0, 0.0], SIDE, SIDE)
    _select(session, [{"sketch": sketch_id, "index": 2}])
    session.extrude(SIDE, mode="new")
    second = session.document.bodies[-1]
    before = (kernel.centroid(first.shape), kernel.centroid(second.shape))

    _select(session, [
        {"body": first.id, "kind": "face", "index": 1},
        {"body": second.id, "kind": "face", "index": 2},
    ])
    session.move_body(mode="rotate", axis_dir=[0.0, 0.0, 1.0], angle=180.0)

    after_first = kernel.centroid(session.document.get_body(first.id).shape)
    after_second = kernel.centroid(session.document.get_body(second.id).shape)
    assert after_first[:2] == pytest.approx(before[1][:2], abs=1e-6)
    assert after_second[:2] == pytest.approx(before[0][:2], abs=1e-6)
    # **1呼び出し = 1オペレーション**(ボディごとに分解しない)
    entry = session.document.log.entries[-1]
    assert entry.verb == "move_body" and len(entry.args["bodies"]) == 2


def test_copying_a_body_by_rotation_keeps_the_original(session: Session) -> None:
    body, _sketch_id = _box(session)
    before = _bbox(body)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    applied = session.move_body(
        mode="rotate",
        axis_point=[0.0, 0.0, 0.0],
        axis_dir=[0.0, 0.0, 1.0],
        angle=90.0,
        copy=True,
    )

    (new_id,) = applied["created"]
    assert len(session.document.bodies) == 2
    assert _bbox(session.document.get_body(body.id))["min"] == pytest.approx(before["min"])
    copied = session.document.get_body(new_id)
    assert _bbox(copied)["min"][:2] == pytest.approx([-SIDE, 0.0], abs=1e-6)
    # 複製は**独立した形状**(片方をフィレットしても、もう片方は変わらない)
    assert not copied.shape.IsPartner(body.shape)
    assert set(applied["changed_bodies"]) == {body.id, new_id}


def test_undo_puts_a_rotated_body_back(session: Session) -> None:
    """逆角度の回転で戻る(スナップショットを控えなくてよい理由が回転にも当てはまる)。"""
    body, _sketch_id = _box(session)
    before = _bbox(body)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    session.move_body(
        mode="rotate", axis_point=[3.0, -2.0, 0.0], axis_dir=[1.0, 2.0, 3.0], angle=37.5
    )
    session.undo()

    after = _bbox(session.document.get_body(body.id))
    assert after["min"] == pytest.approx(before["min"], abs=1e-9)
    assert after["max"] == pytest.approx(before["max"], abs=1e-9)


def test_undo_removes_a_rotated_copy(session: Session) -> None:
    body, _sketch_id = _box(session)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    (new_id,) = session.move_body(
        mode="rotate", axis_dir=[0.0, 0.0, 1.0], angle=90.0, copy=True
    )["created"]
    session.undo()
    assert [b.id for b in session.document.bodies] == [body.id]
    assert new_id not in {b.id for b in session.document.bodies}


def test_a_rotation_survives_save_and_replay(session: Session) -> None:
    """**ログが唯一の正**。軸が数値で載っているので、再生でも同じ形になる。"""
    body, _sketch_id = _box(session)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    index = _vertical_edge_at(body, 0.0, 0.0)
    session.move_body(mode="rotate", axis_edge={"body": body.id, "index": index}, angle=30.0)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    session.move_body(mode="rotate", axis_dir=[0.0, 1.0, 0.0], angle=20.0, copy=True)
    expected = [(b.id, kernel.centroid(b.shape)) for b in session.document.bodies]

    replayed = Document()
    replayed.log = OpLog.from_json(session.document.log.to_json())
    replayed.log.replay(replayed)

    assert [b.id for b in replayed.bodies] == [body_id for body_id, _ in expected]
    for (_body_id, center), rebuilt in zip(expected, replayed.bodies):
        assert kernel.centroid(rebuilt.shape) == pytest.approx(center, abs=1e-9)


def test_a_replayed_log_without_a_mode_is_still_a_translation(session: Session) -> None:
    """**2026-09-05より前に保存した文書**(`mode` キーが無い)は平行移動として読む。"""
    from duocad.oplog import Operation
    from duocad.verbs.move_body import _do_move_body

    body, _sketch_id = _box(session)
    before = _bbox(body)["min"]
    _do_move_body(
        session.document,
        Operation(
            seq=99,
            verb="move_body",
            args={"bodies": [body.id], "dx": 4.0, "dy": 0.0, "dz": 0.0, "copy": False},
        ),
    )
    assert _bbox(session.document.get_body(body.id))["min"][0] == pytest.approx(before[0] + 4.0)


def test_the_selection_resets_after_a_body_rotation(session: Session) -> None:
    body, _sketch_id = _box(session)
    _select(session, [{"body": body.id, "kind": "face", "index": 3}])
    session.move_body(mode="rotate", axis_dir=[0.0, 0.0, 1.0], angle=15.0)
    assert session.get_selection()["items"] == []


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"mode": "rotate", "angle": 90.0}, id="軸が無い"),
        pytest.param(
            {"mode": "rotate", "axis_dir": [0.0, 0.0, 1.0]}, id="角度が無い"
        ),
        pytest.param(
            {"mode": "rotate", "axis_dir": [0.0, 0.0, 0.0], "angle": 90.0}, id="零ベクトルの軸"
        ),
        pytest.param(
            {"mode": "rotate", "axis_dir": [0.0, 0.0, 1.0], "angle": 360.0}, id="360度"
        ),
        pytest.param(
            {"mode": "rotate", "axis_dir": [0.0, 0.0, 1.0], "angle": 0.0}, id="0度"
        ),
        pytest.param(
            {"mode": "rotate", "axis_dir": [0.0, 0.0, 1.0], "angle": 90.0, "dx": 1.0},
            id="回転に並進を混ぜる",
        ),
        pytest.param({"mode": "translate", "dx": 1.0, "dy": 0.0, "dz": 0.0, "angle": 90.0},
                     id="並進に角度を混ぜる"),
        pytest.param({"mode": "translate"}, id="並進なのに量が無い"),
        pytest.param({"mode": "spin", "angle": 90.0}, id="知らないmode"),
    ],
)
def test_a_half_specified_rotation_is_refused(session: Session, kwargs: dict) -> None:
    """**黙って片方だけ効くことは無い。** 足りない/混ざった引数は全部 ValueError。"""
    body, _sketch_id = _box(session)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    with pytest.raises(ValueError):
        session.move_body(**kwargs)


def test_an_edge_axis_cannot_be_combined_with_numbers(session: Session) -> None:
    """どちらが勝つかを暗黙にしない(辺は位置も向きも決めてしまう)。"""
    body, _sketch_id = _box(session)
    index = _vertical_edge_at(body, 0.0, 0.0)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    with pytest.raises(ValueError):
        session.move_body(
            mode="rotate",
            axis_edge={"body": body.id, "index": index},
            axis_dir=[0.0, 0.0, 1.0],
            angle=90.0,
        )


def test_an_edge_axis_can_be_combined_with_a_center_vertex(session: Session) -> None:
    """**辺が向きを、頂点が位置を決める組み合わせは意図的に許してある**。

    `axis_point` との同時指定は断るのに、こちらは通す —— 辺の向きだけを借りて別の点を
    通す軸にするのは実際に要る操作(「この稜線と平行な軸で、この角を中心に回す」)である。
    `_rotation_args` の拒否条件を広げすぎるとここが黙って塞がるので、明示的に固定する。
    """
    body, _sketch_id = _box(session)
    edge = _vertical_edge_at(body, 0.0, 0.0)          # (0,0) に立つ縦の辺 = Z方向
    vertex = _vertex_at(body, [SIDE, SIDE, 0.0])      # 対角の角
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    session.move_body(
        mode="rotate",
        axis_edge={"body": body.id, "index": edge},
        center_vertex={"body": body.id, "index": vertex},
        angle=90.0,
    )

    args = session.document.log.entries[-1].args
    assert args["axis_dir"] == pytest.approx([0.0, 0.0, 1.0], abs=1e-9), "辺の向きが使われていない"
    assert args["axis_point"] == pytest.approx([SIDE, SIDE, 0.0], abs=1e-9), "頂点の位置が使われていない"
    # (20,20) まわりに+90度: (0..20, 0..20) -> (20..40, 0..20)
    box = _bbox(session.document.get_body(body.id))
    assert box["min"][:2] == pytest.approx([SIDE, 0.0], abs=1e-6)
    assert box["max"][:2] == pytest.approx([2 * SIDE, SIDE], abs=1e-6)


def test_a_symmetric_body_may_be_rotate_copied_onto_itself(session: Session) -> None:
    """**ボディ側は「元と重なる複製」を断らない**(既知の穴。`move_body` のdocstring)。

    スケッチ側(`test_a_copy_that_lands_on_its_source_is_refused`)は `params` の一致で
    弾けるが、ソリッドには同じだけ安くて健全な判定が無い。**将来うっかり境界箱の一致などの
    不健全な判定を入れると、L字を180度回した「重なっていない」場合まで断ってしまう** ——
    その事故を検知するために、いまの挙動(通す)をそのまま固定しておく。
    """
    body, _sketch_id = _box(session)   # 20mm角の立方体 = Zまわりに90度対称
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    applied = session.move_body(mode="rotate", axis_dir=[0.0, 0.0, 1.0], angle=90.0, copy=True)
    assert len(applied["created"]) == 1
    assert len(session.document.bodies) == 2


def test_an_axis_point_cannot_be_combined_with_a_center_vertex(session: Session) -> None:
    """どちらも軸の位置を決めるので、片方が黙って無視される形にしない。"""
    body, _sketch_id = _box(session)
    index = _vertex_at(body, [0.0, 0.0, 0.0])
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    with pytest.raises(ValueError):
        session.move_body(
            mode="rotate",
            axis_dir=[0.0, 0.0, 1.0],
            axis_point=[5.0, 5.0, 0.0],
            center_vertex={"body": body.id, "index": index},
            angle=90.0,
        )


def test_redo_puts_a_rotation_back(session: Session) -> None:
    """undo -> redo で回った状態へ戻る(`_do_*` と `_undo_*` が同じ変換を通る)。"""
    body, _sketch_id = _box(session)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    session.move_body(
        mode="rotate", axis_point=[0.0, 0.0, 0.0], axis_dir=[0.0, 0.0, 1.0], angle=90.0
    )
    rotated = _bbox(session.document.get_body(body.id))
    session.undo()
    session.redo()

    again = _bbox(session.document.get_body(body.id))
    assert again["min"] == pytest.approx(rotated["min"], abs=1e-9)
    assert again["max"] == pytest.approx(rotated["max"], abs=1e-9)


def test_redo_puts_a_rotated_copy_back(session: Session) -> None:
    """`copy=True` の redo は「作ったIDで作り直す」経路を通る(undoは消すだけなので非対称)。"""
    body, _sketch_id = _box(session)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    (new_id,) = session.move_body(
        mode="rotate", axis_point=[0.0, 0.0, 0.0], axis_dir=[0.0, 0.0, 1.0], angle=90.0, copy=True
    )["created"]
    made = _bbox(session.document.get_body(new_id))
    session.undo()
    session.redo()

    again = session.document.get_body(new_id)  # **同じID**で戻る(ログが持っているため)
    assert _bbox(again)["min"] == pytest.approx(made["min"], abs=1e-9)
    assert _bbox(again)["max"] == pytest.approx(made["max"], abs=1e-9)
    assert len(session.document.bodies) == 2


def test_an_axis_edge_index_out_of_range_is_refused(session: Session) -> None:
    body, _sketch_id = _box(session)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    with pytest.raises(ValueError):
        session.move_body(
            mode="rotate", axis_edge={"body": body.id, "index": 9999}, angle=90.0
        )


def test_an_axis_edge_on_a_missing_body_is_not_found(session: Session) -> None:
    body, _sketch_id = _box(session)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    with pytest.raises(KeyError):
        session.move_body(
            mode="rotate", axis_edge={"body": "body-nope", "index": 1}, angle=90.0
        )


# --------------------------------------------------------------------------------------
# move_sketch_entities(mode="rotate")
# --------------------------------------------------------------------------------------
def test_rotating_a_line_about_a_given_centre(session: Session) -> None:
    """(0,0)-(10,0) を原点まわりに90度回すと (0,0)-(0,10)(u->v が正)。"""
    sketch_id, entity_id = _line(session)
    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": None}])
    session.move_sketch_entities(mode="rotate", angle=90.0, center=[0.0, 0.0])

    moved = _params(session, sketch_id, entity_id)
    assert (moved["x0"], moved["y0"]) == pytest.approx((0.0, 0.0), abs=1e-9)
    assert (moved["x1"], moved["y1"]) == pytest.approx((0.0, 10.0), abs=1e-9)


def test_the_sketch_rotation_turns_the_same_way_as_pattern_circular(session: Session) -> None:
    """**符号の向きが `pattern_circular` の `total_angle` と同じ**であることを直接見る。"""
    sketch_id, entity_id = _line(session)
    params = _params(session, sketch_id, entity_id)
    expected = sketch.rotate_point(params["x1"], params["y1"], 0.0, 0.0, 30.0)

    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": None}])
    session.move_sketch_entities(mode="rotate", angle=30.0, center=[0.0, 0.0])
    moved = _params(session, sketch_id, entity_id)
    assert (moved["x1"], moved["y1"]) == pytest.approx(expected, abs=1e-12)


def test_rotating_one_point_moves_only_that_point(session: Session) -> None:
    sketch_id, entity_id = _line(session)
    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": "x1"}])
    session.move_sketch_entities(mode="rotate", angle=90.0, center=[0.0, 0.0])

    moved = _params(session, sketch_id, entity_id)
    assert (moved["x0"], moved["y0"]) == pytest.approx((0.0, 0.0))
    assert (moved["x1"], moved["y1"]) == pytest.approx((0.0, 10.0), abs=1e-9)


def test_the_default_sketch_centre_is_the_average_of_the_targets(session: Session) -> None:
    """中心を省くと**対象の代表点の平均**まわりに回る(線分は中点)。"""
    sketch_id, entity_id = _line(session)
    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": None}])
    session.move_sketch_entities(mode="rotate", angle=90.0)

    moved = _params(session, sketch_id, entity_id)
    # 中点 (5,0) まわりに90度 -> (5,-5)-(5,5)
    assert (moved["x0"], moved["y0"]) == pytest.approx((5.0, -5.0), abs=1e-9)
    assert (moved["x1"], moved["y1"]) == pytest.approx((5.0, 5.0), abs=1e-9)
    assert session.document.log.entries[-1].args["center"] == pytest.approx([5.0, 0.0])


def test_a_centre_entity_names_an_existing_point(session: Session) -> None:
    """既存の点を中心に指せる(円の中心を指して、その円のまわりに線を回す)。"""
    sketch_id, entity_id = _line(session)
    circle_id = session.add_circle(sketch_id, [0.0, 0.0], 2.0)["entity_id"]
    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": None}])
    session.move_sketch_entities(
        mode="rotate",
        angle=90.0,
        center_entity={"sketch": sketch_id, "entity": circle_id},
    )

    moved = _params(session, sketch_id, entity_id)
    assert (moved["x1"], moved["y1"]) == pytest.approx((0.0, 10.0), abs=1e-9)
    # **ログには数値が入る**(中心に使った図形が後で動いてもundoは同じ点を使う)
    assert session.document.log.entries[-1].args["center"] == pytest.approx([0.0, 0.0])
    assert "center_entity" not in session.document.log.entries[-1].args


def test_rotating_an_arc_turns_its_angles_too(session: Session) -> None:
    """円弧は中心が回るだけでなく、開始角・終了角も同じだけ回る(向きが変わる)。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]
    entity_id = session.add_arc(sketch_id, [10.0, 0.0], 5.0, 0.0, 90.0)["entity_id"]
    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": None}])
    session.move_sketch_entities(mode="rotate", angle=90.0, center=[0.0, 0.0])

    moved = _params(session, sketch_id, entity_id)
    assert (moved["cx"], moved["cy"]) == pytest.approx((0.0, 10.0), abs=1e-9)
    assert moved["start_deg"] == pytest.approx(90.0)
    assert moved["end_deg"] == pytest.approx(180.0)
    assert moved["radius"] == pytest.approx(5.0)


def test_a_whole_rectangle_can_be_rotated(session: Session) -> None:
    """長方形をまるごと回すと**長方形のまま**で、4隅が元の4隅を回した集合と一致する。

    落ちたら、回転が長方形の4隅の張り方を壊しているか、種別が変わっている。
    """
    sketch_id = session.sketch_create("XY")["sketch_id"]
    entity_id = session.add_rectangle(sketch_id, [0.0, 0.0], 10.0, 4.0)["entity_id"]
    before = _params(session, sketch_id, entity_id)
    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": None}])

    session.move_sketch_entities(mode="rotate", angle=45.0, center=[0.0, 0.0])

    entity = sketch.get_entity(sketch.get_sketch(session.document, sketch_id), entity_id)
    assert entity.kind == "rectangle", "回しても種別は変わらない"
    after = _params(session, sketch_id, entity_id)
    assert (after["width"], after["height"]) == pytest.approx((10.0, 4.0))
    expected = sorted(
        tuple(round(value, 9) for value in sketch.rotate_point(u, v, 0.0, 0.0, 45.0))
        for u, v in kernel.rectangle_corners(before)
    )
    actual = sorted(
        (round(u, 9), round(v, 9)) for u, v in kernel.rectangle_corners(after)
    )
    assert actual == pytest.approx(expected, abs=1e-9)


def test_rotating_a_rectangle_from_a_log_without_angle(session: Session) -> None:
    """`angle` を持たない時代のログから再生した長方形も、まるごと回せる。

    落ちたら、旧文書の長方形(`params` が4個)へ `angle` を書き戻す道が無い ——
    **再生時に埋めてはいけない**(`d<n>` の割り当てがずれて旧ログの `set_parameter` が
    別の値を指す)ので、書き戻す側で登録する必要がある。
    """
    document = session.document
    sketch_id = sketch.sketch_create(document, "XY")
    entity_id = "entity-legacy-rect"
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
    entity = sketch.get_entity(sketch.get_sketch(document, sketch_id), entity_id)
    assert list(entity.params) == ["x", "y", "width", "height"], "旧ログに angle は無い"

    move_sketch_entities_verb(
        document,
        [{"sketch": sketch_id, "entity": entity_id, "point": None}],
        mode="rotate",
        angle=30.0,
        center=[0.0, 0.0],
    )

    after = sketch.entity_params(document, sketch_id, entity_id)
    assert after["angle"] == pytest.approx(30.0)
    assert isinstance(entity.params["angle"], str), "書き足した angle も名前つきの値であること"


def test_rotating_a_whole_rectangle_is_undoable(session: Session) -> None:
    """回転の undo は「逆向きに同じ角度」で元の `params` へ戻る(名前つき寸法も同じ)。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]
    entity_id = session.add_rectangle(sketch_id, [2.0, 3.0], 10.0, 4.0, 15.0)["entity_id"]
    before = _params(session, sketch_id, entity_id)
    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": None}])
    session.move_sketch_entities(mode="rotate", angle=37.0, center=[1.0, 1.0])

    session.undo()

    assert _params(session, sketch_id, entity_id) == pytest.approx(before)


def test_a_rectangle_corner_can_still_be_rotated(session: Session) -> None:
    """長方形の基準の角だけを回すと、位置だけが動く(向きは変わらない)。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]
    entity_id = session.add_rectangle(sketch_id, [10.0, 0.0], 4.0, 2.0)["entity_id"]
    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": "x"}])
    session.move_sketch_entities(mode="rotate", angle=90.0, center=[0.0, 0.0])

    moved = _params(session, sketch_id, entity_id)
    assert (moved["x"], moved["y"]) == pytest.approx((0.0, 10.0), abs=1e-9)
    assert (moved["width"], moved["height"]) == pytest.approx((4.0, 2.0))


def test_rotating_two_entities_uses_one_centre(session: Session) -> None:
    """2つの円を180度回すと**入れ替わる**(平均が中心だから)。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]
    first = session.add_circle(sketch_id, [0.0, 0.0], 2.0)["entity_id"]
    second = session.add_circle(sketch_id, [10.0, 0.0], 2.0)["entity_id"]
    _select(session, [
        {"sketch": sketch_id, "entity": first, "point": None},
        {"sketch": sketch_id, "entity": second, "point": None},
    ])
    session.move_sketch_entities(mode="rotate", angle=180.0)

    assert _params(session, sketch_id, first)["cx"] == pytest.approx(10.0, abs=1e-9)
    assert _params(session, sketch_id, second)["cx"] == pytest.approx(0.0, abs=1e-9)
    entry = session.document.log.entries[-1]
    assert entry.verb == "move_sketch_entities" and len(entry.args["targets"]) == 2


def test_copying_a_sketch_entity_by_rotation(session: Session) -> None:
    sketch_id, entity_id = _line(session)
    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": None}])
    applied = session.move_sketch_entities(
        mode="rotate", angle=90.0, center=[0.0, 0.0], copy=True
    )

    (new_id,) = applied["created"]
    assert _params(session, sketch_id, entity_id) == {
        "x0": 0.0, "y0": 0.0, "x1": 10.0, "y1": 0.0
    }  # 元はそのまま
    copied = _params(session, sketch_id, new_id)
    assert (copied["x1"], copied["y1"]) == pytest.approx((0.0, 10.0), abs=1e-9)


def test_a_copy_that_lands_on_its_source_is_refused(session: Session) -> None:
    """円をその中心まわりに回した複製は元と完全に重なる(領域検出が同じ辺を2本見る)。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]
    entity_id = session.add_circle(sketch_id, [3.0, 4.0], 2.0)["entity_id"]
    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": None}])
    with pytest.raises(ValueError):
        session.move_sketch_entities(
            mode="rotate", angle=90.0, center=[3.0, 4.0], copy=True
        )
    assert len(sketch.get_sketch(session.document, sketch_id).entities) == 1


def test_undo_restores_a_rotated_sketch_entity(session: Session) -> None:
    sketch_id, entity_id = _line(session)
    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": None}])
    session.move_sketch_entities(mode="rotate", angle=37.0, center=[2.0, -1.0])
    session.undo()

    restored = _params(session, sketch_id, entity_id)
    assert (restored["x0"], restored["y0"]) == pytest.approx((0.0, 0.0), abs=1e-9)
    assert (restored["x1"], restored["y1"]) == pytest.approx((10.0, 0.0), abs=1e-9)


def test_a_sketch_rotation_survives_save_and_replay(session: Session) -> None:
    sketch_id, entity_id = _line(session)
    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": None}])
    session.move_sketch_entities(mode="rotate", angle=25.0, center=[1.0, 2.0])
    expected = _params(session, sketch_id, entity_id)

    replayed = Document()
    replayed.log = OpLog.from_json(session.document.log.to_json())
    replayed.log.replay(replayed)
    assert sketch.entity_params(replayed, sketch_id, entity_id) == pytest.approx(expected)


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"mode": "rotate"}, id="角度が無い"),
        pytest.param({"mode": "rotate", "angle": 360.0}, id="360度"),
        pytest.param({"mode": "rotate", "angle": 0.0}, id="0度"),
        pytest.param({"mode": "rotate", "angle": 90.0, "dx": 1.0}, id="回転に並進を混ぜる"),
        pytest.param(
            {"mode": "rotate", "angle": 90.0, "center": [0.0, 0.0],
             "center_entity": {"sketch": "s", "entity": "e"}},
            id="中心を2通りで指す",
        ),
        pytest.param({"mode": "translate", "dx": 1.0, "dy": 0.0, "angle": 90.0},
                     id="並進に角度を混ぜる"),
        pytest.param({"mode": "translate"}, id="並進なのに量が無い"),
        pytest.param({"mode": "spin", "angle": 90.0}, id="知らないmode"),
    ],
)
def test_a_half_specified_sketch_rotation_is_refused(session: Session, kwargs: dict) -> None:
    sketch_id, entity_id = _line(session)
    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": None}])
    with pytest.raises(ValueError):
        session.move_sketch_entities(**kwargs)


def test_redo_puts_a_sketch_rotation_back(session: Session) -> None:
    sketch_id, entity_id = _line(session)
    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": None}])
    session.move_sketch_entities(mode="rotate", angle=90.0, center=[0.0, 0.0])
    rotated = _params(session, sketch_id, entity_id)
    session.undo()
    session.redo()
    assert _params(session, sketch_id, entity_id) == pytest.approx(rotated)


def test_a_replayed_sketch_log_without_a_mode_is_still_a_translation(session: Session) -> None:
    """**2026-09-05より前に保存した文書**(`mode` キーが無い)は平行移動として読む。

    `move_body` 側と対にしてある —— 後方互換は2つの verb が別々に持っているので、
    片方だけ壊れても気づけるように両方を見る。
    """
    from duocad.oplog import Operation
    from duocad.verbs.move_sketch import _do_move_sketch_entities

    sketch_id, entity_id = _line(session)
    _do_move_sketch_entities(
        session.document,
        Operation(
            seq=99,
            verb="move_sketch_entities",
            args={
                "targets": [{"sketch": sketch_id, "entity": entity_id, "point": None}],
                "dx": 2.0,
                "dy": 3.0,
                "copy": False,
            },
        ),
    )
    assert _params(session, sketch_id, entity_id) == {
        "x0": 2.0, "y0": 3.0, "x1": 12.0, "y1": 3.0
    }


def test_the_selection_resets_after_a_sketch_rotation(session: Session) -> None:
    sketch_id, entity_id = _line(session)
    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": None}])
    session.move_sketch_entities(mode="rotate", angle=15.0, center=[0.0, 0.0])
    assert session.get_selection()["items"] == []


# --------------------------------------------------------------------------------------
# kernel の回転そのもの
# --------------------------------------------------------------------------------------
def test_rotating_a_shape_does_not_touch_the_original() -> None:
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

    shape = BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape()
    before = kernel.bounding_box(shape)["center"]
    rotated = kernel.rotate_shape(shape, [0.0, 0.0, 0.0], [0.0, 0.0, 1.0], 90.0)

    assert kernel.bounding_box(shape)["center"] == pytest.approx(before)
    assert kernel.bounding_box(rotated)["center"] == pytest.approx(
        [-before[1], before[0], before[2]], abs=1e-6
    )


def test_rotating_back_returns_to_the_start() -> None:
    """逆角度で戻る(`move_body` の undo がスナップショットを控えない根拠)。"""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

    shape = BRepPrimAPI_MakeBox(10.0, 20.0, 30.0).Shape()
    before = kernel.bounding_box(shape)
    there = kernel.rotate_shape(shape, [1.0, 2.0, 3.0], [1.0, 1.0, 1.0], 41.7)
    back = kernel.rotate_shape(there, [1.0, 2.0, 3.0], [1.0, 1.0, 1.0], -41.7)
    assert kernel.bounding_box(back)["min"] == pytest.approx(before["min"], abs=1e-9)
    assert kernel.bounding_box(back)["max"] == pytest.approx(before["max"], abs=1e-9)


def test_a_degenerate_axis_is_refused() -> None:
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

    shape = BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape()
    with pytest.raises(ValueError):
        kernel.rotate_shape(shape, [0.0, 0.0, 0.0], [0.0, 0.0, 0.0], 30.0)
    with pytest.raises(ValueError):
        kernel.rotate_shape(shape, [0.0, 0.0], [0.0, 0.0, 1.0], 30.0)


def test_the_centroid_is_the_volume_centre_not_the_bounding_box_centre() -> None:
    """**L字**では重心と境界箱の中心が違う(既定の回転中心が重心であることの土台)。"""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

    tall = BRepPrimAPI_MakeBox(2.0, 2.0, 20.0).Shape()
    arm = kernel.translate_shape(BRepPrimAPI_MakeBox(20.0, 2.0, 2.0).Shape(), 0.0, 0.0, 0.0)
    shape = BRepAlgoAPI_Fuse(tall, arm).Shape()

    centroid = kernel.centroid(shape)
    box_centre = kernel.bounding_box(shape)["center"]
    assert centroid != pytest.approx(box_centre, abs=1e-3)


# --------------------------------------------------------------------------------------
# WebSocketの口 —— **画面から回転を踏めるようになった**(2026-09-06)
# --------------------------------------------------------------------------------------
#
# 移動の2つは表(`SKETCH_COMMANDS` / `FEATURE_COMMANDS`)に載らず `_dispatch` が個別に捌く
# (`catalog` の `ws="special"`)。表は「載せた引数は全部必須」なので、`mode` で要る引数が
# 入れ替わり中心も省略できるこの2つは置けない。**個別分岐は静かに壊れる** ——
# 表と違って導出の突き合わせが効かず、引数を1つ書き忘れても `unknown command` にすらならず、
# 「省略した扱い」で通ってしまう。ここがその受け皿になる。
class _FakeConnection:
    """`Server` が使うのは `send` だけ(`test_feature_ui.py` と同じ harness)。"""

    def __init__(self) -> None:
        self.payloads: list = []


    async def send(self, raw: str) -> None:
        self.payloads.append(json.loads(raw))


def _browser(session: Session, command: dict) -> dict:
    """1コマンドをWebSocketの口へ流し、最後に届いた封筒を返す。"""

    async def scenario():
        server = Server(session)
        server.bind_loop(asyncio.get_running_loop())
        client = _FakeConnection()
        server.clients.add(client)
        await server._push(client, applied=None, only=client)
        client.payloads.clear()
        await server._dispatch(client, json.dumps(command))
        return client.payloads[-1]

    return asyncio.run(scenario())


def test_the_browser_rotates_a_body_around_a_picked_edge(session: Session) -> None:
    """**辺への参照をそのまま送れる**(画面が指した辺 = `axis_edge`)。

    画面は辺を座標へ潰さずに送る —— 辺が乗る直線を厳密に出せるのはコアだけである
    (`kernel.edge_axis`)。応答には**解決後の数値**が載り、undo と再生はそちらを使う。
    """
    body, _ = _box(session)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    before = _center(body)

    answer = _browser(
        session,
        {
            "cmd": "move_body",
            "mode": "rotate",
            "angle": 90.0,
            "axis_edge": {"body": body.id, "kind": "edge", "index": 1},
            "copy": False,
        },
    )

    assert answer["type"] == "state", answer
    applied = answer["applied"]
    assert applied["verb"] == "move_body"
    assert applied["mode"] == "rotate"
    # **参照ではなく数値**が返る(`Session.move_body` の契約)。
    point, direction = kernel.edge_axis(body.shape, 1)
    assert applied["axis_point"] == pytest.approx(point)
    assert applied["axis_dir"] == pytest.approx(direction)
    assert _center(session.document.get_body(body.id)) != pytest.approx(before, abs=1e-6)


def test_the_browser_can_move_the_axis_onto_a_picked_vertex(session: Session) -> None:
    """`center_vertex` を添えると**軸の位置だけ**がその頂点へ寄る(向きは辺のまま)。"""
    body, _ = _box(session)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    vertex = kernel.vertex_positions(body.shape)[2]

    answer = _browser(
        session,
        {
            "cmd": "move_body",
            "mode": "rotate",
            "angle": 90.0,
            "axis_edge": {"body": body.id, "kind": "edge", "index": 1},
            "center_vertex": {"body": body.id, "kind": "vertex", "index": vertex["index"]},
        },
    )

    applied = answer["applied"]
    _point, direction = kernel.edge_axis(body.shape, 1)
    assert applied["axis_point"] == pytest.approx(list(vertex["point"]))
    assert applied["axis_dir"] == pytest.approx(direction)


def test_the_browser_still_translates_a_body_without_a_mode(session: Session) -> None:
    """**`mode` を省いた古い形の呼び出しは並進のまま**(保存済みの経路を壊さない)。"""
    body, _ = _box(session)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    before = _center(body)

    answer = _browser(session, {"cmd": "move_body", "dx": 5.0, "dy": 0.0, "dz": 0.0})

    assert answer["applied"]["mode"] == "translate"
    after = _center(session.document.get_body(body.id))
    assert after[0] == pytest.approx(before[0] + 5.0, abs=1e-6)


def test_the_browser_cannot_mix_the_two_modes(session: Session) -> None:
    """回転に並進の引数を添えたら**エラーで返る**(黙って片方だけ効かない)。

    `_dispatch` が省略された引数を既定値で埋めていると、`dx=0` が「渡された」ことになって
    ここが通ってしまう —— **`None` のまま渡している**ことの検査でもある。
    """
    body, _ = _box(session)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])

    answer = _browser(
        session,
        {
            "cmd": "move_body",
            "mode": "rotate",
            "angle": 90.0,
            "axis_edge": {"body": body.id, "kind": "edge", "index": 1},
            "dx": 5.0,
        },
    )

    assert answer["type"] == "error", answer


def test_the_browser_rotates_sketch_entities_around_a_picked_point(session: Session) -> None:
    """スケッチ側は**図形の点への参照**(`center_entity`)で中心を指す。"""
    sketch_id, entity_id = _line(session)
    anchor = session.add_circle(sketch_id, [10.0, 0.0], 2.0)["entity_id"]
    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": None}])

    answer = _browser(
        session,
        {
            "cmd": "move_sketch_entities",
            "mode": "rotate",
            "angle": 90.0,
            "center_entity": {"sketch": sketch_id, "entity": anchor, "point": "cx"},
        },
    )

    assert answer["type"] == "state", answer
    assert answer["applied"]["center"] == pytest.approx([10.0, 0.0])
    moved = _params(session, sketch_id, entity_id)
    # 中心 (10,0) まわりに90度(u->v が反時計正): (0,0) -> (10,-10) / (10,0) -> (10,0)
    assert [moved["x0"], moved["y0"]] == pytest.approx([10.0, -10.0], abs=1e-9)
    assert [moved["x1"], moved["y1"]] == pytest.approx([10.0, 0.0], abs=1e-9)


def test_the_browser_may_omit_the_sketch_rotation_centre(session: Session) -> None:
    """中心を送らなければ**コアの既定**(対象の代表点の平均)になる。

    画面は指されなかった中心を**引数ごと送らない**ので、既定の規則がコアの1箇所に
    とどまる(ブラウザ側に写すと、片方だけ変わったときに幽霊と結果が別の場所へ出る)。
    """
    sketch_id, entity_id = _line(session)
    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": None}])

    answer = _browser(
        session, {"cmd": "move_sketch_entities", "mode": "rotate", "angle": 90.0}
    )

    assert answer["applied"]["center"] == pytest.approx([5.0, 0.0])  # 線分の中点
