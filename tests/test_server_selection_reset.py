"""形を変えるverbの確定後に選択を全リセットする契約(詳細は `duocad/catalog.py` の `resets_selection`
「形を変えるverbの確定後は選択を全リセットする」節)。ここが崩れると、フィレット等の
確定後も壊れた添字を指す選択が残ったまま次のverbへ渡ってしまう。

**失敗したverbが選択を保持すること**は `tests/test_feature_ui.py` が固定しているので、
ここでは重複して確かめない。
"""
from __future__ import annotations

import pytest
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

from duocad import catalog, selection
from duocad.document import Body
from duocad.server import Session
from session_support import select as _select, session_with_box


@pytest.fixture()
def session() -> Session:
    """30×30×40の直方体を1つ持つセッション(空の文書に手で立てる。STEPは読まない)。"""
    return session_with_box()


def _edge(index: int) -> dict:
    return {"body": "box", "kind": "edge", "index": index}


def _face(index: int) -> dict:
    return {"body": "box", "kind": "face", "index": index}


def _assert_cleared(session: Session) -> None:
    selection = session.get_selection()
    assert selection == {"items": [], "count": 0}


# --------------------------------------------------------------------------------------
# 1. 形を変えるverbの確定後、選択は空になる
# --------------------------------------------------------------------------------------
def test_a_shape_changing_verb_must_declare_resets_selection() -> None:
    """落ちたら、形を変える verb を足すときに `resets_selection` を書き忘れても import が通り、
    確定後も消えた面の添字を指したまま選択が残る(issue #51)。"""
    with pytest.raises(ValueError, match="resets_selection"):
        catalog.Command(name="x", category="feature", session="x")
    declared = catalog.Command(name="x", category="feature", session="x", resets_selection=False)
    assert declared.resets_selection is False
    # 形に触らない分類は省略してよい(false になる)
    assert catalog.Command(name="y", category="query", ws="none").resets_selection is False


def test_fillet_clears_the_selection_on_success(session: Session) -> None:
    _select(session, [_edge(1), _edge(2)])
    session.fillet(1.0)
    _assert_cleared(session)


def test_add_shape_verbs_clear_a_pre_existing_3d_selection(session: Session) -> None:
    """スケッチ図形を1つ足すと、**それ以前に選んでいた3Dの面・辺**も空になる。

    「全リセット」は文字どおり全部で、追加した図形の種別に関係なく効く。
    """
    _select(session, [_face(1)])
    sketch_id = session.sketch_create("XY")["sketch_id"]
    # sketch_create 自体は形を変えないので、まだ何も起きていないはず。
    assert session.get_selection()["count"] == 1

    session.add_circle(sketch_id, [0.0, 0.0], 5.0)

    _assert_cleared(session)


def test_set_parameter_clears_a_selected_region_that_the_edit_makes_vanish(
    session: Session,
) -> None:
    """落ちたら、打ち直しで消えた領域を選んだままになり、`App.svelte` の `regionsStillExist` の
    分岐へ届く(`web/scripts/smoke-regions.mjs` 検証6と同じシナリオ)。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_circle(sketch_id, [0.0, 0.0], 15.0)
    session.add_circle(sketch_id, [20.0, 0.0], 15.0)
    sketch_obj = session.document.sketches[-1]
    assert sketch_obj.id == sketch_id
    radius_name = sketch_obj.entities[1].params["radius"]

    from duocad.region import regions as list_regions

    before = {item.index for item in list_regions(session.document, sketch_id)}
    assert before == {1, 2, 3}, f"想定した3領域(1,2,3)にならない: {before}"

    region_ref = selection.select_region(session.document, sketch_id, 3)
    _select(session, [region_ref])
    assert session.get_selection()["count"] == 1

    session.set_parameter(radius_name, 3.0)  # 重なりが解け、添字3が消える

    after = {item.index for item in list_regions(session.document, sketch_id)}
    assert 3 not in after, "半径を縮めても添字3の領域が消えない(検査の前提が崩れている)"
    _assert_cleared(session)


# --------------------------------------------------------------------------------------
# 2. 形を変えない操作は選択に触らない
# --------------------------------------------------------------------------------------
def test_selection_editing_itself_is_not_a_reset(session: Session) -> None:
    """`modify_selection` / `toggle_selection` / `clear_selection` は選択機構そのものであり、
    「形を変えるverbの確定」ではない —— ここで自分自身を巻き込まない。
    """
    session.set_selection(refs=[_edge(1)])
    assert session.get_selection()["count"] == 1
    session.modify_selection([_edge(2)], [])
    assert session.get_selection()["count"] == 2


# --------------------------------------------------------------------------------------
# 3. undo/redo は「何を戻したか」に関わらず常にリセットする
# --------------------------------------------------------------------------------------
def test_undo_of_a_non_shape_verb_still_clears_the_selection(session: Session) -> None:
    """**undo/redoはunconditional**(verbごとの分岐を持たない単純な規則)。

    材料は `sketch_create` —— 形を1つも変えないが操作ログには載るverbである
    (`set_visible` は操作ログに載らないので、ここの材料にはならない)。
    """
    _select(session, [_face(1)])
    session.sketch_create("XY")
    assert session.get_selection()["count"] == 1  # 形を変えないverbは触らない(2.)

    session.undo()

    _assert_cleared(session)


def test_redo_clears_the_selection(session: Session) -> None:
    _select(session, [_face(1)])
    session.extrude(5.0, mode="join")
    session.undo()
    _select(session, [_edge(1)])

    session.redo()

    _assert_cleared(session)


# --------------------------------------------------------------------------------------
# 4. 「手前を隠して奥の面を選ぶ」ワークフローで選択は飛ばない
# --------------------------------------------------------------------------------------
def test_hiding_a_body_to_reach_a_hidden_face_does_not_drop_the_selection(
    session: Session,
) -> None:
    """奥の面を選ぶために手前を隠しても、**その前に選んでいたもの**は消えない。

    ここが落ちると「隠す→選ぶ→もう一度選ぶ」を強いることになる。
    """
    other = Body(
        id="other", name="other", kind="solid", shape=BRepPrimAPI_MakeBox(5.0, 5.0, 5.0).Shape()
    )
    session.document.add_body(other)
    _select(session, [_face(1)])

    session.set_visible("other", False)  # 手前を隠す
    assert session.get_selection()["count"] == 1

    _select(session, [_face(1), _edge(1)])  # 奥の面(と辺)を選び足す
    session.set_visible("other", True)  # 戻す

    assert session.get_selection()["count"] == 2
