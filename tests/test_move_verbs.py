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

import math

import pytest

from duocad import kernel, parameters, query, selection, sketch
from duocad.document import Document
from duocad.oplog import OpLog
from duocad.server import Session
from duocad.verbs import move_body as move_body_verb
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
    (2026-08-30に実際に踏んだ。`_apply` は図形ごとに1つの計画へ併合してから書く)。
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
    """確定後に選択を空にする(他の形状変更verbと同じ既定の契約。catalog の note 参照)。

    2026-08-30時点では「参照は`entity_id`で名指しするので平行移動では失効せず、少しずつ
    動かす使い方が主用途」という理由で例外にしていたが、本人が非として却下(2026-08-31)。
    以後は毎回選び直す(`tests/test_server_selection_reset.py` の RESETTING_VERBS 参照)。
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
    """確定後に選択を空にする(他の形状変更verbと同じ既定の契約。本人が非として却下し2026-08-31に変更)。"""
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
