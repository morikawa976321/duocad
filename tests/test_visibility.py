"""可視性のverbと、**可視性が選択機構の一部であること**の契約。

PCケース(27部品・2835面)の実機テストではピック失敗12回/押し出し7回(失敗率63%)の原因が
**手前の部品が奥の面を隠していた**ことだった。可視性は見た目の設定ではなく**何を指せるかの
定義**であり、見えないものは指せないことをコアで保証する。

ShapeRef の形は `{"body": BodyId, "kind": "face", "index": int, "provenance": None}`。
"""
from __future__ import annotations

import pytest

from duocad import selection, sketch
from duocad.server import Session
from session_support import session_with_box


# --------------------------------------------------------------------------------------
# 可視性は undo / redo の対象外
# --------------------------------------------------------------------------------------
def test_hiding_a_body_is_not_undoable():
    """**Ctrl+Z で表示は戻らない**。undoが戻すのは直前の「形を変えた操作」であり、
    可視性はその対象ではない。
    """
    session = session_with_box()
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
    session = session_with_box()
    session.sketch_create("XY")
    session.undo()
    assert session.document.log.can_redo, "検査の前提: やり直せる末尾があること"

    session.set_visible("box", False)

    assert session.document.log.can_redo, "隠しただけでredoが消えた"
    session.redo()
    assert len(session.document.log.entries) == 1


# --------------------------------------------------------------------------------------
# スケッチの可視性(ボディと同じ契約: undo対象外・保存されない・隠したものは指せない)
# --------------------------------------------------------------------------------------
def _sketch_with_region(session: Session) -> str:
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_rectangle(sketch_id, [0.0, 0.0], 10.0, 10.0)
    return sketch_id


def test_hiding_a_sketch_is_not_undoable_and_is_not_saved():
    """落ちたら: スケッチの可視性が操作ログに乗っている(undoで戻る / 保存に残る)。"""
    session = Session(None, 0.2)
    sketch_id = _sketch_with_region(session)
    entries_before = len(session.document.log.entries)

    applied = session.set_sketch_visible(sketch_id, False)

    assert applied["change"] == "visibility" and applied["sketch_id"] == sketch_id
    assert applied["changed"] is True and applied["previous"] is True
    assert len(session.document.log.entries) == entries_before, "可視性がログに積まれた"
    session.undo()  # 直前のverb(長方形)が戻る。可視性はそのまま
    assert sketch.get_sketch(session.document, sketch_id).visible is False
    assert "visible" not in session.document.log.to_json()


def test_a_hidden_sketch_cannot_be_pointed_at():
    """落ちたら: 見えないスケッチの領域・図形が選べる(見えないものは指せない、の破れ)。"""
    session = Session(None, 0.2)
    sketch_id = _sketch_with_region(session)
    entity_id = sketch.get_sketch(session.document, sketch_id).entities[0].id
    session.set_sketch_visible(sketch_id, False)

    with pytest.raises(ValueError, match="hidden"):
        selection.select_region(session.document, sketch_id, 1)
    with pytest.raises(ValueError, match="hidden"):
        selection.select_sketch_entity(session.document, sketch_id, entity_id)

    session.set_sketch_visible(sketch_id, True)
    assert selection.select_region(session.document, sketch_id, 1)["index"] == 1


def test_sketch_visibility_reaches_the_state_and_the_fingerprint():
    """落ちたら: 画面が非表示を知れない(`sketches[].visible` が無い)か、AIの楽観ロックが
    「人がスケッチを隠した」を見逃す(`visibility_fingerprint` が動かない)。"""
    session = Session(None, 0.2)
    sketch_id = _sketch_with_region(session)
    before = selection.visibility_fingerprint(session.document)
    assert session.state_meta()["sketches"][0]["visible"] is True

    session.set_sketch_visible(sketch_id, False)

    assert session.state_meta()["sketches"][0]["visible"] is False
    assert selection.visibility_fingerprint(session.document) != before
    with pytest.raises(KeyError):
        session.set_sketch_visible("sketch-nope", False)
