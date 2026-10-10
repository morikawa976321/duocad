"""verb `delete` の契約。固定するのは5つ。"""
from __future__ import annotations

import pytest

from duocad import region, selection, sketch
from duocad.oplog import OpLog
from duocad.server import Session
from duocad.verbs import delete as delete_verb
from session_support import SIDE, box as _box, edit_parameter, select as _select, send_one

DEVIATION = 0.2


def _body_ids(session: Session) -> list:
    return [body.id for body in session.document.bodies]


def _entity_ids(session: Session, sketch_id: str) -> list:
    return [entity.id for entity in sketch.get_sketch(session.document, sketch_id).entities]


def test_a_face_a_body_and_an_entity_all_resolve_in_one_call(session: Session) -> None:
    """面はボディへ逆引きされ、図形はそのまま。**両方あれば両方返る**。"""
    body, sketch_id = _box(session)
    entity_id = session.add_circle(sketch_id, [5.0, 5.0], 2.0)["entity_id"]
    _select(
        session,
        [
            {"body": body.id, "kind": "face", "index": 1},
            {"body": body.id, "kind": "edge", "index": 1},  # 同じボディは1つに畳む
            {"sketch": sketch_id, "entity": entity_id, "point": None},
        ],
    )

    targets = selection.require_deletable(session.document)
    assert targets.bodies == [body.id]
    assert targets.entities == [{"sketch": sketch_id, "entity": entity_id}]


def test_a_point_reference_folds_into_the_whole_entity(session: Session) -> None:
    """点は図形の一部であって図形ではない。**同じ図形を点と辺で選んでも1つ**。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]
    entity_id = session.add_line(sketch_id, [0.0, 0.0], [10.0, 0.0])["entity_id"]
    _select(
        session,
        [
            {"sketch": sketch_id, "entity": entity_id, "point": "x0"},
            {"sketch": sketch_id, "entity": entity_id, "point": None},
        ],
    )

    targets = selection.require_deletable(session.document)
    assert targets.bodies == []
    assert targets.entities == [{"sketch": sketch_id, "entity": entity_id}]


def test_only_sketch_entities_is_a_valid_selection(session: Session) -> None:
    """**ボディが0件でも通る** —— `require_bodies` を素で呼んでいたらここで壊れる。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]
    entity_id = session.add_circle(sketch_id, [0.0, 0.0], 3.0)["entity_id"]
    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": None}])

    targets = selection.require_deletable(session.document)
    assert targets.bodies == [] and len(targets.entities) == 1


def test_an_empty_confirmed_selection_is_selection_empty(session: Session) -> None:
    _box(session)
    session.clear_selection()
    with pytest.raises(selection.SelectionEmpty) as raised:
        selection.require_deletable(session.document)
    # 空は `SelectionKindMismatch` ではない(MCPのエラーコードが分かれている)
    assert not isinstance(raised.value, selection.SelectionKindMismatch)


def test_a_region_in_the_selection_is_ignored_and_the_rest_is_deleted(session: Session) -> None:
    """領域が選択に混ざっていても、残りは問題なく消える(領域は黙って無視)。"""
    body, sketch_id = _box(session)
    _select(
        session,
        [
            {"body": body.id, "kind": "face", "index": 1},
            {"sketch": sketch_id, "index": 1},
        ],
    )
    before = _body_ids(session)
    assert body.id in before

    session.delete()

    assert body.id not in _body_ids(session)
    assert session.document.log.entries[-1].verb == "delete"


def test_a_selection_of_only_regions_is_refused(session: Session) -> None:
    """領域**だけ**を選んでいるときは、削除できる対象が無いので拒否する。"""
    _, sketch_id = _box(session)
    _select(session, [{"sketch": sketch_id, "index": 1}])
    before = _body_ids(session)
    applied_before = session.applied_count

    with pytest.raises(selection.SelectionKindMismatch) as raised:
        session.delete()

    assert raised.value.present == {"region": 1}
    assert raised.value.kind == "body"
    # 文書もログも動いていない
    assert _body_ids(session) == before
    assert session.applied_count == applied_before


def test_deleting_a_body_and_an_entity_is_one_operation_and_one_undo(session: Session) -> None:
    """**混在した選択でも1回のverb呼び出し・1行のログ・1回のundo**。"""
    body, sketch_id = _box(session)
    base = _entity_ids(session, sketch_id)  # `_box` が引いた長方形
    kept = session.add_circle(sketch_id, [5.0, 5.0], 2.0)["entity_id"]
    doomed = session.add_circle(sketch_id, [12.0, 12.0], 3.0)["entity_id"]
    _select(
        session,
        [
            {"body": body.id, "kind": "face", "index": 1},
            {"sketch": sketch_id, "entity": doomed, "point": None},
        ],
    )
    log_before = len(session.document.log.entries)

    applied = session.delete()

    assert applied["verb"] == "delete"
    assert len(session.document.log.entries) == log_before + 1  # 1行だけ
    assert body.id not in _body_ids(session)
    assert _entity_ids(session, sketch_id) == base + [kept]
    # 形を変えるverbなので選択は空になる
    assert session.get_selection() == {"items": [], "count": 0}

    session.undo()  # **1回で両方戻る**
    assert body.id in _body_ids(session)
    assert _entity_ids(session, sketch_id) == base + [kept, doomed]

    session.redo()
    assert body.id not in _body_ids(session)
    assert _entity_ids(session, sketch_id) == base + [kept]


def test_undo_puts_the_body_back_where_it_was(session: Session) -> None:
    """**並びと active も戻る** —— 末尾へ足し直すだけだと順序だけが黙って変わる。"""
    first, sketch_id = _box(session)
    _select(session, [{"sketch": sketch_id, "index": 1}])
    session.extrude(SIDE, mode="new")
    session.clear_selection()
    second = session.document.bodies[-1]
    order = _body_ids(session)
    assert order == [first.id, second.id]
    session.document.set_active_body(second.id)

    _select(session, [{"body": first.id, "kind": "body", "index": None}])
    session.delete()
    assert _body_ids(session) == [second.id]
    # 消したのは active ではないので active は動かない
    assert session.document.active_body().id == second.id

    session.undo()
    assert _body_ids(session) == order
    assert session.document.active_body().id == second.id
    # 形も同じ実体が戻っている(作り直しではない)
    assert session.document.get_body(first.id).shape is first.shape


def test_undo_restores_the_active_body_that_was_deleted(session: Session) -> None:
    body, _ = _box(session)
    session.document.set_active_body(body.id)
    _select(session, [{"body": body.id, "kind": "body", "index": None}])

    session.delete()
    assert session.document.active_body() is None

    session.undo()
    assert session.document.active_body().id == body.id


def test_deleting_an_entity_releases_its_parameters_and_undo_restores_the_names(
    session: Session,
) -> None:
    """**`release_param_refs` を対にする** —— 忘れると名前つきの値が誰にも参照されず残る。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]
    kept = session.add_line(sketch_id, [0.0, 0.0], [10.0, 0.0])["entity_id"]
    doomed = session.add_circle(sketch_id, [4.0, 4.0], 2.0)["entity_id"]
    before = dict(session.document.parameters)
    doomed_names = set(
        sketch.get_sketch(session.document, sketch_id).entities[1].params.values()
    )
    assert doomed_names  # 図形は名前つきの値を持っている(この検査の前提)

    _select(session, [{"sketch": sketch_id, "entity": doomed, "point": None}])
    session.delete()

    assert set(session.document.parameters) == set(before) - doomed_names

    session.undo()
    assert session.document.parameters == before
    assert _entity_ids(session, sketch_id) == [kept, doomed]
    assert sketch.entity_params(session.document, sketch_id, doomed) == {
        "cx": 4.0,
        "cy": 4.0,
        "radius": 2.0,
    }


def test_deleting_an_imported_curve_does_not_turn_it_into_named_values(
    session: Session, tmp_path
) -> None:
    """取り込んだベジエは**素の数値のまま**戻る(undoで `d1` が数千個生えない)。"""
    svg = tmp_path / "curve.svg"
    svg.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
        '<path d="M 10 10 C 20 20 30 20 40 10"/></svg>',
        encoding="utf-8",
    )
    sketch_id = session.sketch_create("XY")["sketch_id"]
    entity_id = session.import_svg(sketch_id, str(svg))["entity_ids"][0]
    before_params = dict(session.document.parameters)
    before = sketch.entity_params(session.document, sketch_id, entity_id)

    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": None}])
    session.delete()
    session.undo()

    restored = sketch.get_sketch(session.document, sketch_id).entities[0]
    assert all(not isinstance(value, str) for value in restored.params.values())
    assert session.document.parameters == before_params
    assert sketch.entity_params(session.document, sketch_id, entity_id) == before


def test_deleting_nothing_is_refused(session: Session) -> None:
    """verbを直に呼んだときも、対象0件は通さない(undoを1手消費する空振りを作らない)。"""
    with pytest.raises(ValueError):
        delete_verb(session.document)


def test_a_hidden_body_is_refused(session: Session) -> None:
    """隠したボディは指せない(`fillet` / `move_body` と同じ門番)。"""
    body, _ = _box(session)
    _select(session, [{"body": body.id, "kind": "body", "index": None}])
    session.set_visible(body.id, False)

    with pytest.raises(ValueError, match="hidden"):
        session.delete()
    assert body.id in _body_ids(session)


def test_the_document_survives_save_reopen_and_can_still_be_undone(session: Session) -> None:
    """再生が同じ結果を作り、**再生後は undo も通る**(スナップショットは再生が埋める)。"""
    body, sketch_id = _box(session)
    entity_id = session.add_circle(sketch_id, [5.0, 5.0], 2.0)["entity_id"]
    _select(
        session,
        [
            {"body": body.id, "kind": "face", "index": 1},
            {"sketch": sketch_id, "entity": entity_id, "point": None},
        ],
    )
    session.delete()
    session.save_document("deleted.json")

    session.open_document("deleted.json")
    assert _body_ids(session) == []
    assert entity_id not in _entity_ids(session, sketch_id)

    session.undo()
    assert len(_body_ids(session)) == 1
    assert entity_id in _entity_ids(session, sketch_id)


def test_undo_without_replay_fails_loudly_for_bodies(session: Session) -> None:
    """読んだだけのログでボディの削除を undo すると **KeyError**(黙って空振りしない)。"""
    body, _ = _box(session)
    _select(session, [{"body": body.id, "kind": "body", "index": None}])
    session.delete()

    log = OpLog.from_json(session.document.log.to_json())
    fresh = Session(None, DEVIATION).document
    with pytest.raises(KeyError):
        log.undo(fresh)


def test_a_parameter_edit_after_a_delete_replays_the_delete(session: Session) -> None:
    """`set_parameter` はログを頭から再生する。**削除も再生される**(消したものは戻らない)。"""
    body, sketch_id = _box(session)
    session.add_circle(sketch_id, [5.0, 5.0], 2.0)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    session.delete()

    radius = next(
        name for name, value in session.document.parameters.items() if value == 2.0
    )
    edit_parameter(session, radius, 3.0)

    assert _body_ids(session) == []
    assert session.document.parameters[radius] == 3.0


def test_deleting_the_source_of_a_pattern_does_not_break_the_state(session: Session) -> None:
    """反復の元を消しても落ちない(依存グラフは作らない。undoで元どおり展開される)。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]
    entity_id = session.add_circle(sketch_id, [0.0, 0.0], 2.0)["entity_id"]
    session.pattern_rectangular(sketch_id, [entity_id], 10.0, 10.0, 2, 2)
    assert len(region.regions(session.document, sketch_id)) == 4

    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": None}])
    session.delete()

    assert region.regions(session.document, sketch_id) == []
    session.state_meta()  # 配りものが落ちない(毎回この経路を通る)

    session.undo()
    assert len(region.regions(session.document, sketch_id)) == 4


def test_deleting_a_body_leaves_a_sketch_pasted_on_its_face_alone(session: Session) -> None:
    """スケッチ平面は作成時点で焼き付けてあるので、貼った面のボディが消えても壊れない。"""
    body, _ = _box(session)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    on_face = session.sketch_create()["sketch_id"]
    session.add_circle(on_face, [3.0, 3.0], 1.0)

    _select(session, [{"body": body.id, "kind": "body", "index": None}])
    session.delete()

    state = session.state_meta()
    assert state["bodies"] == []
    frame = next(item for item in state["sketches"] if item["sketch_id"] == on_face)
    assert frame["frame"] is not None


def test_the_browser_can_delete_what_the_ai_selected(session: Session) -> None:
    """AIが選んだ集合を、ブラウザからそのまま消せる。"""
    body, _ = _box(session)
    session.set_selection([{"body": body.id, "kind": "face", "index": 1}])

    answer = send_one(session, {"cmd": "delete", "client_op": 7})
    assert answer["type"] == "state", answer
    assert answer["applied"]["verb"] == "delete"
    assert answer["applied"]["bodies"] == [body.id]
    assert _body_ids(session) == []
