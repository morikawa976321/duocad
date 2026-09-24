"""**`applied_count` はAI(MCP)経路とUI(WebSocket)経路で同じ数を意味する**という契約。

同じ `Session` を、AIは `DocumentTools.describe_document()` から、ブラウザは
`Session.state_meta()` から見る。この2つが返す `applied_count` は**どんな操作順序でも
常に一致**しなければならない —— ログの長さはundoで減るが `Session` の数え上げは減らないため、
**片方だけ `len(document.log.entries)` を返す実装だとundoの直後にずれる**。

**揃える先は「undo / redoで動かない」ほうの意味**である。`web/src/App.svelte` の保存済み
判定が、「undoで保存時点へ戻した文書」と「undoしてから別のverbを打った文書」を見分ける
ために、その性質へ明示的に依存しているため(App.svelte の `savedAt` のコメントが前提と
理由を持っている)。逆向き(ログの長さ)へ揃えるとUIの保存判定を作り直すことになる。

このファイルが固定する2つ:

1. **一致** — verbを適用したあと、2つの経路の値が等しい。
2. **undo / redoで減らない** — undoを挟んでも一致し続け、値が下がらない。undoの後に
   打った新しいverbでは**増える**(App.svelteが見分けに使っているのはこの差)。
"""
from __future__ import annotations

import pytest

from duocad.mcp_server import DocumentTools
from duocad.server import Session


@pytest.fixture()
def session() -> Session:
    """STEPを読まない空のセッション(土台が無いので最初の操作からundoできる)。"""
    return Session(None, 0.2)


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
    sketch_id = tools.sketch_create(base_plane="XY")["sketch_id"]
    tools.add_line(sketch_id=sketch_id, p0=[0, 0], p1=[30, 0])
    tools.add_circle(sketch_id=sketch_id, center=[60, 0], radius=10)
    return sketch_id


# --------------------------------------------------------------------------------------
# 1. 一致
# --------------------------------------------------------------------------------------
def test_both_paths_report_the_same_count_after_applying_verbs(
    session: Session, tools: DocumentTools
):
    """verbを1回ずつ適用するたびに、AI経路とUI経路の数が揃っていること。"""
    assert_agree(session, tools, 0, "何も適用していない時点")

    sketch_id = tools.sketch_create(base_plane="XY")["sketch_id"]
    assert_agree(session, tools, 1, "sketch_create の直後")

    tools.add_line(sketch_id=sketch_id, p0=[0, 0], p1=[30, 0])
    assert_agree(session, tools, 2, "add_line の直後")

    tools.add_circle(sketch_id=sketch_id, center=[60, 0], radius=10)
    assert_agree(session, tools, 3, "add_circle の直後")


def test_verbs_applied_through_the_core_are_counted_the_same_way(
    session: Session, tools: DocumentTools
):
    """**UI経路(`Session` を直に叩く)で適用しても**同じ数が両方から見える。

    ブラウザは `Session.add_line` を直接呼ぶ(MCPを経由しない)ので、数え上げが
    `DocumentTools` 側に寄っていると、この経路だけ数が増えない形の食い違いが起きうる。
    """
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_line(sketch_id, [0, 0], [30, 0])
    assert_agree(session, tools, 2, "UI経路で2回適用したあと")


# --------------------------------------------------------------------------------------
# 2. undo / redo
# --------------------------------------------------------------------------------------
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
    """undoの後の新しいverbでは**増える**。

    `App.svelte` の保存済み判定は、まさにこの増え方で「undoで保存時点へ戻した文書」
    (増えない)と「undoしてから別のverbを打った文書」(増える)を見分けている。
    どちらの経路から見てもその見分けが成り立つこと。
    """
    sketch_id = draw_three_verbs(tools)
    tools.undo()
    assert_agree(session, tools, 3, "undo の直後")

    tools.add_circle(sketch_id=sketch_id, center=[90, 0], radius=5)
    assert_agree(session, tools, 4, "undoしてから別のverbを打ったあと")

    log_length = len(session.document.log.entries)
    assert log_length == 3, "undo→新しいverbでログの長さは戻っただけ(=見分けがつかない)"
