"""作業空間(起動中のコマンド・モード。`duocad/workspace.py`)の契約(issue #153)。

選択と同じ共有の短命状態で、人(WebSocket)も AI も同じ操作で触る。落ちたら「起動 → 人と AI が選択と引数を
相互に直す → 誰かが確定」の流れのどこかが壊れている。
"""
from __future__ import annotations

import pytest

from duocad.server import Session
from duocad.workspace import NoCommand
from session_support import browser, session_with_box

#: 30×30×40 の直方体で Z に平行な辺(ちょうど4本)。
VERTICAL_EDGES = {"kind": "edge", "where": [{"op": "parallel_to", "axis": "Z"}]}


@pytest.fixture()
def session() -> Session:
    return session_with_box()


def select_vertical_edges(session: Session) -> None:
    session.set_selection(predicate=VERTICAL_EDGES)
    assert session.get_selection()["count"] == 4


def command_of(session: Session):
    return session.state_meta()["command"]


def mode_of(session: Session) -> dict:
    return session.state_meta()["mode"]


# ---------------------------------------------------------------- 起動・引数・中断
def test_begin_set_end_lifecycle(session: Session):
    """起動 → 引数 → 中断。配信される状態(`command` / `mode`)とイベントが同じものを指す。"""
    assert command_of(session) is None
    assert mode_of(session) == {"kind": "idle"}

    event = session.begin_command("fillet", {"radius": 1.0}, client_op=3)
    assert event["change"] == "command" and event["client_op"] == 3
    assert event["command"]["verb"] == "fillet" and event["command"]["args"] == {"radius": 1.0}
    first = event["command"]["revision"]
    assert command_of(session) == event["command"]

    event = session.set_command_args({"radius": 2.0})
    assert event["command"]["args"] == {"radius": 2.0}
    assert event["command"]["revision"] > first

    event = session.set_command_args({"radius": None})
    assert event["command"]["args"] == {}, "null はそのキーを外す(「まだ決めていない」へ戻す)"

    event = session.end_command(client_op=9)
    assert event["change"] == "command" and event["command"] is None and event["client_op"] == 9
    assert command_of(session) is None


def test_only_sketch_and_feature_verbs_can_be_begun(session: Session):
    """起動して引数を集めるのはスケッチとフィーチャの verb だけ。選択・履歴・測定・ファイル操作は構える対象ではない。"""
    for verb in ("set_selection", "undo", "measure", "export_step", "set_visible", "no_such_verb"):
        with pytest.raises(ValueError):
            session.begin_command(verb)
    session.begin_command("extrude")
    session.begin_command("add_line")


def test_staged_args_are_checked_for_keys_and_types_but_not_for_required(session: Session):
    session.begin_command("fillet")
    with pytest.raises(ValueError, match="radios"):
        session.set_command_args({"radios": 1.0})
    with pytest.raises(ValueError, match="radius"):
        session.set_command_args({"radius": "2"})
    assert session.set_command_args({"radius": 2.0})["command"]["args"] == {"radius": 2.0}
    with pytest.raises(ValueError, match="radios"):
        session.begin_command("fillet", {"radios": 1.0})


def test_set_and_apply_need_a_command(session: Session):
    with pytest.raises(NoCommand):
        session.set_command_args({"radius": 1.0})
    with pytest.raises(NoCommand):
        session.apply_command()


# ---------------------------------------------------------------- 選択との関係
def test_switching_to_another_verb_clears_the_selection_but_restarting_keeps_it(session: Session):
    """別の verb へ移るのは前の verb の終わり(選択は空)。同じ verb の起動し直しは引数を置き直すだけ。"""
    select_vertical_edges(session)
    session.begin_command("fillet", {"radius": 1.0})
    assert session.get_selection()["count"] == 4
    session.begin_command("fillet", {"radius": 2.0})
    assert session.get_selection()["count"] == 4
    assert command_of(session)["args"] == {"radius": 2.0}
    session.begin_command("chamfer")
    assert session.get_selection()["count"] == 0
    assert command_of(session)["verb"] == "chamfer"


def test_ending_a_command_clears_the_selection_and_ending_nothing_changes_nothing(session: Session):
    """コマンドが終わったら選択も全解除(2026-10-03)。誰が終えても同じになるようサーバが行う。"""
    select_vertical_edges(session)
    session.begin_command("fillet")
    session.end_command()
    assert session.get_selection()["count"] == 0

    select_vertical_edges(session)
    before = session.workspace.revision
    session.end_command()
    assert session.get_selection()["count"] == 4, "無起動の Esc は選択に触らない"
    assert session.workspace.revision == before


# ---------------------------------------------------------------- 確定
def test_apply_needs_the_required_args_and_keeps_the_command_when_it_fails(session: Session):
    """必須が無い・OCCT が失敗した、のどちらでもコマンドは構えたまま残る(近くの値で打ち直す)。"""
    select_vertical_edges(session)
    session.begin_command("fillet")
    staged = command_of(session)
    with pytest.raises(ValueError, match="radius"):
        session.apply_command()
    assert command_of(session) == staged
    assert session.get_selection()["count"] == 4

    session.set_command_args({"radius": 1000.0})
    log_before = len(session.document.log.entries)
    with pytest.raises(Exception):
        session.apply_command()
    assert command_of(session)["args"] == {"radius": 1000.0}
    assert len(session.document.log.entries) == log_before

    applied = session.apply_command({"radius": 2.0})
    assert applied["verb"] == "fillet"
    assert command_of(session) is None, "確定の成功でコマンドは無くなる(「確定した」の唯一の表現)"
    assert len(session.document.log.entries) == log_before + 1
    assert session.get_selection()["count"] == 0


def test_a_one_shot_verb_with_the_same_name_closes_the_staged_command(session: Session):
    """`fillet(radius)` の一発呼び出し(MCP・画面の Enter)が、同じ verb の構えを確定として畳む。別の verb は触らない。"""
    select_vertical_edges(session)
    session.begin_command("fillet")
    session.apply("fillet", {"radius": 2.0})
    assert command_of(session) is None

    fresh = session_with_box()  # 丸めた後の直方体は Z に平行な辺が 8 本になるので、2つ目は素の直方体で見る
    fresh.begin_command("chamfer")
    select_vertical_edges(fresh)
    fresh.apply("fillet", {"radius": 1.0})
    assert command_of(fresh)["verb"] == "chamfer", "別の verb の確定で、人が起動したコマンドを勝手に閉じない"


def test_apply_fills_sketch_id_from_the_editing_sketch(session: Session):
    created = session.apply("sketch_create", {"base_plane": "XY"})
    session.begin_command("add_rectangle", {"corner": [0.0, 0.0], "width": 10.0, "height": 5.0})
    applied = session.apply_command()
    assert applied["verb"] == "add_rectangle"
    sketch = next(item for item in session.document.sketches if item.id == created["sketch_id"])
    assert len(sketch.entities) == 1
    assert command_of(session) is None


def test_preview_does_not_fold_the_staged_command(session: Session):
    """投機実行で通った verb は確定ではない。構えを畳むと、確定していないのに画面の欄が消える。"""
    select_vertical_edges(session)
    session.begin_command("fillet", {"radius": 2.0})
    session.preview("fillet", {"radius": 2.0})
    assert command_of(session)["verb"] == "fillet"
    assert session.get_selection()["count"] == 4


# ---------------------------------------------------------------- モード
def test_creating_a_sketch_enters_it_for_everyone(session: Session):
    """スケッチを作ったら誰が作っても入る(画面が自分の操作の成功でだけ入っていた分をサーバへ寄せる)。"""
    created = session.apply("sketch_create", {"base_plane": "XY"})
    assert mode_of(session) == {"kind": "sketch", "sketch_id": created["sketch_id"]}
    event = session.leave_sketch(client_op=5)
    assert event["change"] == "mode" and event["mode"] == {"kind": "idle"} and event["client_op"] == 5
    assert session.enter_sketch(created["sketch_id"])["mode"]["sketch_id"] == created["sketch_id"]
    with pytest.raises(KeyError):
        session.enter_sketch("no-such-sketch")


def test_losing_the_editing_sketch_returns_to_idle(session: Session):
    created = session.apply("sketch_create", {"base_plane": "XY"})
    session.undo()
    assert mode_of(session) == {"kind": "idle"}
    session.redo()
    assert mode_of(session) == {"kind": "idle"}, "redo で勝手に入り直さない"

    created = session.apply("sketch_create", {"base_plane": "XY"})
    session.apply("delete_items", {"sketches": [created["sketch_id"]]})
    assert mode_of(session) == {"kind": "idle"}


def test_a_mode_change_folds_the_staged_command(session: Session):
    """モードをまたいで集めかけの引数を持ち越さない(画面の `{#key mode.kind}` と同じ)。"""
    select_vertical_edges(session)
    session.begin_command("fillet", {"radius": 1.0})
    session.enter_optimize()
    assert mode_of(session) == {"kind": "optimize"}
    assert command_of(session) is None
    session.leave_optimize()
    assert mode_of(session) == {"kind": "idle"}
    session.leave_sketch()
    assert mode_of(session) == {"kind": "idle"}


def test_beginning_a_sketch_verb_with_a_sketch_id_enters_that_sketch(session: Session):
    """AI がスケッチの verb を構えると、人の画面がそのスケッチへ入る。"""
    created = session.apply("sketch_create", {"base_plane": "XY"})
    session.leave_sketch()
    event = session.begin_command("add_rectangle", {"sketch_id": created["sketch_id"]})
    assert event["command"]["verb"] == "add_rectangle"
    assert mode_of(session) == {"kind": "sketch", "sketch_id": created["sketch_id"]}
    with pytest.raises(KeyError):
        session.begin_command("add_line", {"sketch_id": "no-such-sketch"})


def test_switching_the_document_resets_the_workspace(session: Session):
    session.begin_command("fillet", {"radius": 1.0})
    before = session.workspace.revision
    session.new_document()
    assert command_of(session) is None
    assert mode_of(session) == {"kind": "idle"}
    assert session.workspace.revision > before


# ---------------------------------------------------------------- WebSocket
def test_the_workspace_travels_over_websocket_to_every_screen(session: Session):
    """人の画面の起動・引数・確定が、他の画面(と AI が読む状態)へ届く。`applied` は要求した画面にだけ付く。"""

    select_vertical_edges(session)  # 先に選ぶ(台本の中で直接 `set_selection` すると、その配信が後から割り込む)

    async def script(send):
        began = await send({"cmd": "begin_command", "verb": "fillet", "args": {"radius": 1.0}, "client_op": 11})
        edited = await send({"cmd": "set_command_args", "args": {"radius": 2.0}, "client_op": 12})
        applied = await send({"cmd": "apply_command", "client_op": 13})
        return began, edited, applied

    (began, edited, applied), clients = browser(session, script, connections=2)
    assert began["applied"]["change"] == "command" and began["applied"]["client_op"] == 11
    assert began["state"]["command"]["verb"] == "fillet"
    assert edited["state"]["command"]["args"] == {"radius": 2.0}
    assert applied["applied"]["verb"] == "fillet" and applied["applied"]["client_op"] == 13
    assert applied["state"]["command"] is None

    other = [payload for payload in clients[1].payloads if payload["type"] == "state"]
    assert [payload["state"]["command"] and payload["state"]["command"]["verb"] for payload in other] == [
        "fillet",
        "fillet",
        None,
    ]
    assert all("applied" not in payload for payload in other)


def test_mode_changes_travel_over_websocket(session: Session):
    created = session.apply("sketch_create", {"base_plane": "XY"})

    async def script(send):
        left = await send({"cmd": "leave_sketch", "client_op": 1})
        entered = await send({"cmd": "enter_sketch", "sketch_id": created["sketch_id"], "client_op": 2})
        return left, entered

    (left, entered), _clients = browser(session, script, connections=1)
    assert left["applied"] == {"change": "mode", "mode": {"kind": "idle"}, "client_op": 1}
    assert entered["state"]["mode"] == {"kind": "sketch", "sketch_id": created["sketch_id"]}


def test_errors_keep_the_workspace_and_reach_the_screen(session: Session):
    async def script(send):
        unknown = await send({"cmd": "begin_command", "verb": "no_such_verb"})
        nothing = await send({"cmd": "apply_command"})
        return unknown, nothing

    (unknown, nothing), _clients = browser(session, script, connections=1)
    assert unknown["type"] == "error" and "no_such_verb" in unknown["message"]
    assert nothing["type"] == "error" and "起動中のコマンドが無い" in nothing["message"]
    assert command_of(session) is None


def test_hidden_features_cannot_be_begun_or_entered(session: Session):
    """安定版(形状最適化を隠す版)では、最適化の verb を構えることも最適化モードに入ることもできない。"""

    async def script(send):
        begun = await send({"cmd": "begin_command", "verb": "set_fixtures"})
        entered = await send({"cmd": "enter_optimize"})
        return begun, entered

    (begun, entered), _clients = browser(
        session, script, connections=1, hidden_features=frozenset({"shape_optimization"})
    )
    assert begun["type"] == "error" and entered["type"] == "error"
    assert command_of(session) is None and mode_of(session) == {"kind": "idle"}
