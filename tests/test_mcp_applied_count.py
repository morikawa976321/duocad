"""**`applied_count` はAI(MCP)経路とUI(WebSocket)経路で同じ数を意味する**という契約。"""
from __future__ import annotations

import pytest

from duocad.mcp_server import DocumentTools
from duocad.server import Session


@pytest.fixture()
def tools(session: Session) -> DocumentTools:
    return DocumentTools(session)


def both_counts(session: Session, tools: DocumentTools) -> tuple[int, int]:
    """(AIが見る数, ブラウザが見る数)。**この2つが食い違うのがこのファイルの検知対象**。"""
    return (
        tools.describe_document()["applied_count"],
        session.state_meta()["applied_count"],
    )


def assert_agree(session: Session, tools: DocumentTools, expected: int, where: str) -> None:
    ai, ui = both_counts(session, tools)
    assert ai == ui, f"{where}: MCP(AI)は {ai}、WebSocket(UI)は {ui} —— 経路で意味が食い違っている"
    assert ai == expected, f"{where}: 適用したverbは {expected} 回のはずが {ai}"


def draw_three_verbs(tools: DocumentTools) -> str:
    """スケッチ1つ＋図形2つ = 3回のverb。**形状を作らない**ので体積計算に依存しない。"""
    sketch_id = tools.call("sketch_create", {"base_plane": "XY"})["sketch_id"]
    tools.call("add_line", {"sketch_id": sketch_id, "p0": [0, 0], "p1": [30, 0]})
    tools.call("add_circle", {"sketch_id": sketch_id, "center": [60, 0], "diameter": 20})
    return sketch_id


def test_both_paths_report_the_same_count_after_applying_verbs(
    session: Session, tools: DocumentTools
):
    """verbを1回ずつ適用するたびに、AI経路とUI経路の数が揃っていること。"""
    assert_agree(session, tools, 0, "何も適用していない時点")

    sketch_id = tools.call("sketch_create", {"base_plane": "XY"})["sketch_id"]
    assert_agree(session, tools, 1, "sketch_create の直後")

    tools.call("add_line", {"sketch_id": sketch_id, "p0": [0, 0], "p1": [30, 0]})
    assert_agree(session, tools, 2, "add_line の直後")

    tools.call("add_circle", {"sketch_id": sketch_id, "center": [60, 0], "diameter": 20})
    assert_agree(session, tools, 3, "add_circle の直後")


def test_undo_and_redo_do_not_move_the_count_but_a_new_verb_after_them_does(
    session: Session, tools: DocumentTools
):
    """**undoで減らない**(減っていたのが直したバグそのもの)、redoでも動かない、undoの後の新しいverbでは増える。
    undo/redo は MCP 経由と UI 経由の両方で通す。"""
    sketch_id = draw_three_verbs(tools)
    assert_agree(session, tools, 3, "3回適用したあと")

    tools.undo()
    assert_agree(session, tools, 3, "MCP経由でundoした直後")
    tools.undo()
    session.undo()
    assert_agree(session, tools, 3, "3回undoし終えた直後")

    tools.redo()
    session.redo()
    assert_agree(session, tools, 3, "2回redoした直後")

    tools.call("add_circle", {"sketch_id": sketch_id, "center": [90, 0], "diameter": 10})
    assert_agree(session, tools, 4, "undo/redoしてから別のverbを打ったあと")
    assert len(session.document.log.entries) == 3, "ログの長さは戻っただけ(=適用回数とは見分けがつく)"
