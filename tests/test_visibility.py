"""可視性のverbと、**可視性が選択機構の一部であること**の契約。

PCケース(27部品・2835面)の実機テストではピック失敗12回/押し出し7回(失敗率63%)の原因が
**手前の部品が奥の面を隠していた**ことだった。可視性は見た目の設定ではなく**何を指せるかの
定義**であり、見えないものは指せないことをコアで保証する。

ShapeRef の形は `{"body": BodyId, "kind": "face", "index": int, "provenance": None}`。
"""
from __future__ import annotations

from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

from duocad.document import Body
from duocad.server import Session


# --------------------------------------------------------------------------------------
# 可視性は undo / redo の対象外
# --------------------------------------------------------------------------------------
def _box_session() -> Session:
    """30×30×40 の直方体を1つ持つセッション(操作ログは空の状態から始まる)。"""
    session = Session(None, 0.2)
    session.document.add_body(
        Body(id="box", name="box", kind="solid", shape=BRepPrimAPI_MakeBox(30.0, 30.0, 40.0).Shape())
    )
    return session


def test_hiding_a_body_is_not_undoable():
    """**Ctrl+Z で表示は戻らない**。undoが戻すのは直前の「形を変えた操作」であり、
    可視性はその対象ではない。
    """
    session = _box_session()
    session.sketch_create("XY")  # 戻る先の本物のverbを1つ積んでおく

    session.set_visible("box", False)
    assert session.document.get_body("box").visible is False

    session.undo()

    assert session.document.get_body("box").visible is False, "undoが可視性まで戻している"
    assert session.document.log.entries == [], "undoが戻したのは直前のverbのはず"


def test_hiding_a_body_does_not_discard_the_redo_stack():
    """隠しただけでredoが消えない。

    `OpLog.append` は**redo可能だった末尾を新しい操作で捨てる**。可視性がverbとして
    ログに乗ると、隠しただけで「やり直せるはずのもの」が黙って消える。
    """
    session = _box_session()
    session.sketch_create("XY")
    session.undo()
    assert session.document.log.can_redo, "検査の前提: やり直せる末尾があること"

    session.set_visible("box", False)

    assert session.document.log.can_redo, "隠しただけでredoが消えた"
    session.redo()
    assert len(session.document.log.entries) == 1
