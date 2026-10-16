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


def test_verbs_applied_through_the_core_are_counted_the_same_way(
    session: Session, tools: DocumentTools
):
    """**UI経路(`Session` を直に叩く)で適用しても**同じ数が両方から見える。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_line(sketch_id, [0, 0], [30, 0])
    assert_agree(session, tools, 2, "UI経路で2回適用したあと")


def test_undo_does_not_move_the_count_on_either_path(session: Session, tools: DocumentTools):
    """**undoで減らない**。減っていたのが直したバグそのもの。"""
    draw_three_verbs(tools)
    assert_agree(session, tools, 3, "3回適用したあと")

    tools.undo()
    assert_agree(session, tools, 3, "MCP経由でundoした直後")

    tools.undo()
    assert_agree(session, tools, 3, "2回undoした直後")

    session.undo()
    assert_agree(session, tools, 3, "UI経由でundoした直後(3回目)")


def test_redo_does_not_move_the_count_on_either_path(session: Session, tools: DocumentTools):
    """redoでも動かない(undoで減らないのだから、戻しても増えない)。"""
    draw_three_verbs(tools)
    tools.undo()
    tools.undo()
    assert_agree(session, tools, 3, "2回undoしたあと")

    tools.redo()
    assert_agree(session, tools, 3, "MCP経由でredoした直後")

    session.redo()
    assert_agree(session, tools, 3, "UI経由でredoした直後")


def test_a_new_verb_after_undo_increases_the_count_on_both_paths(
    session: Session, tools: DocumentTools
):
    """undoの後の新しいverbでは**増える**。"""
    sketch_id = draw_three_verbs(tools)
    tools.undo()
    assert_agree(session, tools, 3, "undo の直後")

    tools.call("add_circle", {"sketch_id": sketch_id, "center": [90, 0], "diameter": 10})
    assert_agree(session, tools, 4, "undoしてから別のverbを打ったあと")

    log_length = len(session.document.log.entries)
    assert log_length == 3, "undo→新しいverbでログの長さは戻っただけ(=見分けがつかない)"
