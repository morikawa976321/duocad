"""形を変えるverbの確定後に選択を全リセットする統一契約。

**実装の置き場**: 選択集合はコアにあるので、リセットもコア側で行う。`duocad/catalog.py` の
`Command.resets_selection` が**単一の宣言**で、`Session._reset_selection_if_needed` がそれを
見て確定後に1箇所で発火する。`Session` はWebSocketとMCPの**どちらも同じインスタンスを叩く**
唯一の適用パイプラインなので、ここで検証すれば両方をカバーする。

固定する契約:

1. `resets_selection=True` はカタログの中で正確にこの13verbだけである
2. 形を変えるverbが成功で確定すると、選択は空になる(`items=[]` / `origin="user"`)
3. 形を変えない操作(可視性・選択操作そのもの)は選択に触らない
4. undo/redo は「何を戻したか」に関わらず常にリセットする(単純な規則を保つため)
5. 「手前を隠して奥の面を選ぶ」ワークフロー(可視性切替)で選択は飛ばない

**失敗したverbが選択を保持すること**は `tests/test_feature_ui.py` が固定しているので、
ここでは重複して確かめない。
"""
from __future__ import annotations

import pytest
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

from duocad import catalog
from duocad.document import Body
from duocad.server import Session

BOX_X, BOX_Y, BOX_Z = 30.0, 30.0, 40.0

#: 「形を変える」と宣言されている verb 名。**この集合が変わったら `duocad/catalog.py` の
#: 宣言を見てここも一緒に見直すこと**(このテストは宣言そのものではなく「この集合で合って
#: いるか」を固定する)。
RESETTING_VERBS = frozenset(
    {
        "extrude",
        "extrude_face",
        "fillet",
        "chamfer",
        "pattern_rectangular",
        "pattern_circular",
        "add_line",
        "add_circle",
        "add_rectangle",
        "add_arc",
        # SVG読み込みは**図形を足すverbの仲間**なので `add_*` と同じく選択を空にする。
        # 貼る先に使った面が選ばれたまま残ると、次に打つ `extrude` が領域ではなく面を
        # 掴んだ状態で走り、`selection_kind_mismatch` で止まる。
        "import_svg",
        "set_parameter",
        "undo",
        "redo",
    }
)


def test_resets_selection_is_declared_on_exactly_these_verbs() -> None:
    """カタログの `resets_selection=True` が RESETTING_VERBS と過不足なく一致する。"""
    flagged = {command.name for command in catalog.COMMANDS if command.resets_selection}
    assert flagged == RESETTING_VERBS


@pytest.fixture()
def session() -> Session:
    """30×30×40の直方体を1つ持つセッション(空の文書に手で立てる。STEPは読まない)。"""
    made = Session(None, 0.2)
    made.document.add_body(
        Body(id="box", name="box", kind="solid", shape=BRepPrimAPI_MakeBox(BOX_X, BOX_Y, BOX_Z).Shape())
    )
    return made


def _select(session: Session, refs: list) -> None:
    """選択を `refs` で置き換える(人がクリックで選び直すのと同じ入口)。"""
    session.clear_selection()
    session.modify_selection(refs, [])


def _edge(index: int) -> dict:
    return {"body": "box", "kind": "edge", "index": index}


def _face(index: int) -> dict:
    return {"body": "box", "kind": "face", "index": index}


def _assert_cleared(session: Session) -> None:
    selection = session.get_selection()
    assert selection == {"items": [], "origin": "user", "count": 0}


# --------------------------------------------------------------------------------------
# 2. 形を変えるverbの確定後、選択は空になる
# --------------------------------------------------------------------------------------
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


def test_set_parameter_clears_the_selection_on_success(session: Session) -> None:
    """`set_parameter` はrebuildで形が変わりうるので全リセットの対象(タスクファイルの明示)。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_circle(sketch_id, [0.0, 0.0], 5.0)
    # add_circle は cx/cy/radius の3つを名前つき(d1..d3)にする(tests/test_parameters.py)。
    # どれを打ち直しても rebuild が走るので、ここではどれか1つで足りる。
    name = next(iter(session.document.parameters))
    _select(session, [_face(1)])

    session.set_parameter(name, 8.0)

    _assert_cleared(session)


# --------------------------------------------------------------------------------------
# 3. 形を変えない操作は選択に触らない
# --------------------------------------------------------------------------------------
def test_set_visible_does_not_touch_the_selection(session: Session) -> None:
    _select(session, [_face(1)])
    session.set_visible("box", False)
    assert session.get_selection()["count"] == 1


def test_selection_editing_itself_is_not_a_reset(session: Session) -> None:
    """`modify_selection` / `commit_selection` / `clear_selection` は選択機構そのものであり、
    「形を変えるverbの確定」ではない —— ここで自分自身を巻き込まない。
    """
    session.propose_selection(refs=[_edge(1)])
    session.commit_selection()
    assert session.get_selection()["count"] == 1
    session.modify_selection([_edge(2)], [])
    assert session.get_selection()["count"] == 2


# --------------------------------------------------------------------------------------
# 4. undo/redo は「何を戻したか」に関わらず常にリセットする
# --------------------------------------------------------------------------------------
def test_undo_of_a_shape_changing_verb_clears_the_selection(session: Session) -> None:
    _select(session, [_face(1)])
    session.extrude(5.0)  # ここで既に空になっている
    _select(session, [_edge(1)])  # 次の一手として選び直した状態を作る

    session.undo()

    _assert_cleared(session)


def test_undo_of_a_non_shape_verb_still_clears_the_selection(session: Session) -> None:
    """**undo/redoはunconditional**(verbごとの分岐を持たない単純な規則)。

    材料は `sketch_create` —— 形を1つも変えないが操作ログには載るverbである
    (`set_visible` は2026-08-26に操作ログから外れたので、ここの材料にはならない)。
    """
    _select(session, [_face(1)])
    session.sketch_create("XY")
    assert session.get_selection()["count"] == 1  # 形を変えないverbは触らない(契約3)

    session.undo()

    _assert_cleared(session)


def test_redo_clears_the_selection(session: Session) -> None:
    _select(session, [_face(1)])
    session.extrude(5.0)
    session.undo()
    _select(session, [_edge(1)])

    session.redo()

    _assert_cleared(session)


# --------------------------------------------------------------------------------------
# 5. 「手前を隠して奥の面を選ぶ」ワークフローで選択は飛ばない
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
