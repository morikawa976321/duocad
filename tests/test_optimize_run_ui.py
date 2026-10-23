"""形状最適化を**画面から走らせる**ための配線(`20260904-1705`)の契約。"""
from __future__ import annotations

import asyncio
import json
import threading

import pytest

from duocad import optimize_run
from duocad.server import Server, Session
from duocad.verbs.add_mesh import add_mesh_body
from session_support import FakeConnection, browser, four_conditions

pytestmark = pytest.mark.usefixtures("finished_shape_check_at_a_third_of_the_test_element")


def test_steps_start_at_the_coarse_end(session: Session) -> None:
    """**添字0が上限側**(＝既定＝最も安い設定)。ここが逆を向くと、画面のスライダーは
    「既定より粗い設定」を用意することになり、条件が黙って消える側へ人を送り出す。
    """
    four_conditions(session)
    limits = optimize_run.resolution_limits(session.document)
    steps = limits["steps"]
    assert steps, "目盛りが1段も無い"
    assert steps[0]["element_size_mm"] == pytest.approx(limits["max_element_size_mm"])
    assert steps[0]["element_size_mm"] == pytest.approx(limits["default_element_size_mm"])
    assert steps[-1]["element_size_mm"] == pytest.approx(limits["min_element_size_mm"])


def test_steps_get_finer_and_costlier(session: Session) -> None:
    """落ちたら、目盛りを進めても要素サイズが細かく・要素数が多くならない段がある。"""
    four_conditions(session)
    steps = optimize_run.resolution_limits(session.document)["steps"]
    assert len(steps) >= 2
    for previous, current in zip(steps, steps[1:]):
        assert current["index"] == previous["index"] + 1
        assert current["element_size_mm"] < previous["element_size_mm"]
        assert current["design_elements_estimate"] > previous["design_elements_estimate"]
        assert current["design_elements_estimate"] <= optimize_run.MAX_DESIGN_ELEMENTS


def _dispatch(session: Session, message: dict) -> FakeConnection:
    """1コマンドを流して応答を集める。**探索が回らない**コマンドだけをここで使う。"""
    return browser(session, lambda send: send(message), connections=1)[1][0]


def test_conditions_command_returns_the_problems_verbatim(session: Session) -> None:
    """**足りない条件は文のまま画面へ渡す。** 画面が文言を組み立て直すと、
    コアの制限(この版は拘束面1枚だけ、等)が変わっても古い説明を出し続ける。
    """
    connection = _dispatch(session, {"cmd": "optimize_conditions"})
    replies = connection.of_type("optimize_conditions")
    assert len(replies) == 1
    assert replies[0]["ready"] is False
    assert any("保持ソリッド" in problem for problem in replies[0]["problems"])
    assert not connection.of_type("error"), "点検の結果を赤い札で返している"


def test_resolution_command_carries_the_ladder(session: Session) -> None:
    four_conditions(session)
    connection = _dispatch(session, {"cmd": "optimize_resolution"})
    reply = connection.of_type("optimize_resolution")[0]
    limits = optimize_run.resolution_limits(session.document)
    assert reply["steps"][0]["element_size_mm"] == pytest.approx(limits["max_element_size_mm"])
    assert reply["feasible"] is True
    assert "error" not in reply


def test_resolution_without_condition_faces_is_not_an_error_toast(session: Session) -> None:
    """条件面がまだ1枚も無いのは**設定の途中**であって事故ではない。`type: "error"`
    (＝赤いトースト)で返すと、条件を置いている最中ずっと札が点滅する。
    """
    connection = _dispatch(session, {"cmd": "optimize_resolution"})
    reply = connection.of_type("optimize_resolution")[0]
    assert "拘束面" in reply["error"]
    assert not connection.of_type("error")


def test_resolution_rejects_the_old_rmin_argument_without_a_toast(session: Session) -> None:
    """**旧引数(`rmin_mm`)が届いたら、黙って無視せず分かるエラーにする**"""
    four_conditions(session)
    connection = _dispatch(session, {"cmd": "optimize_resolution", "rmin_mm": 3.0})
    reply = connection.of_type("optimize_resolution")[0]
    assert "rmin_mm" in reply["error"]
    assert "steps" not in reply
    assert not connection.of_type("error")


def test_run_is_refused_before_the_conditions_are_ready(session: Session) -> None:
    """**走らせる前に点検する。** 例外の文言1本ではなく、足りないものの一覧を返す。"""
    connection = _dispatch(session, {"cmd": "run_optimization"})
    reply = connection.of_type("optimize_error")[0]
    assert reply["message"] == "条件がそろっていない"
    assert any("保持ソリッド" in problem for problem in reply["problems"])
    assert not connection.of_type("optimize_status"), "走り出していないのに実行中を配っている"


def test_second_run_is_refused_while_one_is_running(session: Session) -> None:
    """**二重実行を断る。** 断らないと2本目が同じ文書を読みながら走り、
    どちらの結果が入ったのか分からなくなる(旗も1つしか無い)。
    """

    session.begin_optimization()  # 走っている最中の状態を作る(探索は回さない)
    connection = _dispatch(session, {"cmd": "run_optimization"})
    [error] = connection.of_type("optimize_error")
    assert "既に走っている" in error["message"] and error["already_running"] is True


def _run_to_completion(session: Session, message: dict) -> tuple:
    """`run_optimization` を1本走らせ、バックグラウンドタスクの完了まで待つ。"""

    async def scenario() -> tuple:
        server = Server(session)
        server.bind_loop(asyncio.get_running_loop())
        connection = FakeConnection()
        server.clients.add(connection)
        await server._dispatch(connection, json.dumps(message))
        await asyncio.gather(*server._pending_optimize_runs)
        return server, connection

    return asyncio.run(scenario())


@pytest.mark.parametrize(
    "kwarg", ["rmin_mm", "max_iter", "material_min_mm3", "material_max_mm3"]
)
def test_run_optimization_rejects_the_old_arguments_with_a_named_error(
    session: Session, kwarg: str
) -> None:
    """**旧引数(`rmin_mm`/`max_iter`/`material_min_mm3`/`material_max_mm3`)が届いたら、"""
    four_conditions(session)
    server, connection = _run_to_completion(
        session, {"cmd": "run_optimization", kwarg: 3.0}
    )
    reply = connection.of_type("optimize_error")[0]
    assert kwarg in reply["message"]
    assert reply.get("cancelled") is not True
    assert connection.of_type("optimize_result") == []
    assert session.steps_for_current() is None


def test_run_optimization_forwards_min_width_mm_so_a_below_floor_value_is_refused(session: Session) -> None:
    """`net/ws.py` が `min_width_mm` を落とすと、下限割れの入力が黙って通って走り出す。"""
    four_conditions(session)
    spacing = optimize_run.resolution_limits(session.document)["default_element_size_mm"]
    _, connection = _run_to_completion(session, {"cmd": "run_optimization", "min_width_mm": spacing})
    assert "下限" in connection.of_type("optimize_error")[0]["message"]
    assert connection.of_type("optimize_result") == []


def test_run_optimization_refuses_a_min_width_beyond_the_grid_at_the_ws_door(session: Session) -> None:
    """`net/ws.py` 経由でも上限(格子の最短の辺)を越える幅は導出の前に断る。落ちたら、WS直叩きの巨大な値が
    塊の導出・境界条件の確定の後にメモリを使い切る。"""
    four_conditions(session)
    _, connection = _run_to_completion(session, {"cmd": "run_optimization", "min_width_mm": 1e6})
    assert "上限" in connection.of_type("optimize_error")[0]["message"]
    assert connection.of_type("optimize_result") == []


def test_a_missing_min_width_mm_still_reaches_run_at_the_default_cells(session: Session, monkeypatch) -> None:
    """`min_width_mm` を渡さない実行が、`Session.run_optimization` → `optimize_run.run()` を経て
    既定の m(≥2)で探索へ届くこと(落ちたら、既定が入口検査だけに留まり、探索は制約なしのまま走る)。"""
    from duocad.topopt import binary_search

    four_conditions(session)
    seen: dict = {}

    def fake_find(design_space_mask, conditions, **kwargs):
        seen.update(kwargs)
        return binary_search.SearchResult(
            status="no_feasible_found", termination="evaluation_limit", best_feasible=None,
            history=[], evaluations=1, conditions_identifier="test",
        )

    monkeypatch.setattr(binary_search, "find_best_feasible", fake_find)
    # 既定(いちばん粗い 19.6mm)の段はこの題材では min_width と無関係に断られる(実測: ボクセル化で
    # 「レイのずらしを 6 通り試したがすべて縮退した」)。1段細かい段を明示する。
    spacing = optimize_run.resolution_limits(session.document)["steps"][1]["element_size_mm"]
    _run_to_completion(session, {"cmd": "run_optimization", "element_size_mm": spacing})
    assert seen.get("min_width_cells", 1) >= 2


def test_a_successful_run_is_saved_to_disk_even_with_no_screen_watching(
    session: Session, monkeypatch
) -> None:
    """2026-09-13実測: 数時間がかりの実行が、結果を見る前にサーバプロセスごと消えた。"""
    four_conditions(session)
    session.rename_document("hanger.json")
    (session.models_dir / "hanger.json").unlink()
    monkeypatch.setattr(session, "run_optimization", lambda **_: {"state": "done"})

    _run_to_completion(session, {"cmd": "run_optimization"})

    assert (session.models_dir / "hanger.json").exists()
    assert not session.warnings


def test_a_successful_run_on_an_untitled_document_is_saved_under_an_automatic_name(
    session: Session, monkeypatch
) -> None:
    """無題の文書(一度も名前を付けていない)でも結果は消えない —— 自動保存が
    `untitled-<n>.json` の名前を付けて書く(issue #5、2026-09-16)。警告は積まない。
    """
    four_conditions(session)  # 条件を置く verb が最初の変更 = ここで名前が付く
    assert session.current_name == "untitled-1.json"
    monkeypatch.setattr(session, "run_optimization", lambda **_: {"state": "done"})

    _, connection = _run_to_completion(session, {"cmd": "run_optimization"})

    assert connection.of_type("optimize_result")[0]["state"] == "done"
    assert (session.models_dir / "untitled-1.json").exists()
    assert connection.of_type("warning") == []


def test_cancel_does_nothing_when_nothing_is_running(session: Session) -> None:
    """**走っていないのに旗を立てない。** 立てても `run_optimization` の入口が畳むので
    実害は無いが、走っていないのに「中断中」と画面へ出ると、次の実行が中断表示から
    始まって見える。
    """
    connection = _dispatch(session, {"cmd": "cancel_optimization"})
    status = connection.of_type("optimize_status")[0]
    assert status["running"] is False
    assert "cancelling" not in status
    assert session.optimization_status() == {"running": False}


def test_cancel_sets_the_flag_while_running(session: Session) -> None:
    session.begin_optimization()
    connection = _dispatch(session, {"cmd": "cancel_optimization"})
    status = connection.of_type("optimize_status")[0]
    assert status["cancelling"] is True
    assert session.optimization_status() == {"running": True, "cancelling": True}


def test_a_cancel_sent_right_after_the_start_is_not_lost(session: Session, monkeypatch) -> None:
    """落ちたら、実行をワーカースレッドへ投げてから走り出すまでに届いた中断が、走り出した実行に消されて
    結果が足される(issue #35)。"""
    four_conditions(session)
    seen: dict = {}

    def fake_run(doc, options, *, should_cancel, **kwargs):
        seen["cancelled"] = should_cancel()
        raise optimize_run.OptimizeCancelled("中断した")

    monkeypatch.setattr(optimize_run, "run", fake_run)

    async def scenario() -> FakeConnection:
        server = Server(session)
        server.bind_loop(asyncio.get_running_loop())
        connection = FakeConnection()
        server.clients.add(connection)
        await server._dispatch(connection, json.dumps({"cmd": "run_optimization"}))
        await server._dispatch(connection, json.dumps({"cmd": "cancel_optimization"}))
        await asyncio.gather(*server._pending_optimize_runs)
        return connection

    connection = asyncio.run(scenario())
    assert seen["cancelled"] is True
    assert connection.of_type("optimize_status")[1]["cancelling"] is True
    assert connection.of_type("optimize_error")[0]["cancelled"] is True
    assert connection.of_type("optimize_status")[-1] == {"type": "optimize_status", "running": False, "available": True}


def test_a_run_that_added_nothing_does_not_overwrite_the_file(session: Session, monkeypatch) -> None:
    """落ちたら、形が見つからず結果を足さなかった実行でも、編集中のファイルを未保存の編集で上書きする(issue #36)。"""
    four_conditions(session)
    session.rename_document("hanger.json")
    (session.models_dir / "hanger.json").unlink()
    monkeypatch.setattr(session, "run_optimization", lambda **_: {"state": "no_feasible_found"})
    _, connection = _run_to_completion(session, {"cmd": "run_optimization"})
    assert connection.of_type("optimize_result")[0]["state"] == "no_feasible_found"
    assert not (session.models_dir / "hanger.json").exists()


def test_a_failed_autosave_still_delivers_the_result_and_the_end_of_the_run(session: Session, monkeypatch) -> None:
    """落ちたら、自動保存の `OSError`(Windows で別のプログラムがファイルを開いている等)が `_optimize_run` の外へ漏れ、
    結果も終了の `optimize_status` も配られず画面が実行中のまま固まる(issue #30)。"""
    four_conditions(session)
    session.rename_document("hanger.json")
    monkeypatch.setattr(session, "run_optimization", lambda **_: {"state": "done"})

    def locked(*args, **kwargs):
        raise PermissionError("別のプログラムが開いている")

    monkeypatch.setattr(session.store, "write_document_file", locked)
    _, connection = _run_to_completion(session, {"cmd": "run_optimization"})
    assert connection.of_type("optimize_result")[0]["state"] == "done"
    assert connection.of_type("optimize_status")[-1]["running"] is False
    assert "PermissionError" in connection.of_type("warning")[0]["message"]
    assert not session.optimizing


class _ClosingConnection(FakeConnection):
    """接続した直後に閉じる(`handle()` の最初の配信だけを見る)。"""

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


@pytest.mark.parametrize("running", [True, False])
def test_a_new_connection_is_told_whether_an_optimization_is_running(session: Session, running: bool) -> None:
    """落ちたら、再読み込み・再接続した画面が実行中だと知らず、中断できない/終わった後も実行中のまま固まる(issue #27)。"""

    async def scenario() -> _ClosingConnection:
        server = Server(session)
        server.bind_loop(asyncio.get_running_loop())
        if running:
            session.begin_optimization()
            session.cancel_optimization()
        connection = _ClosingConnection()
        await server.handle(connection)
        return connection

    connection = asyncio.run(scenario())
    [status] = connection.of_type("optimize_status")
    assert status["running"] is running and status.get("cancelling", False) is running


def test_a_run_keeps_going_on_another_document_and_shows_up_on_return(session: Session, monkeypatch) -> None:
    """落ちたら、別の文書の画面に進捗・結果が出る、または走らせた文書へ戻っても実行中/完了が見えない。"""
    four_conditions(session)
    session.rename_document("a.json")
    session.copy_document("a.json", "b.json")
    gates = [threading.Event(), threading.Event()]
    first_progress = asyncio.Event()
    second_progress = asyncio.Event()
    loop = None

    def fake_run(document, options, *, progress, **kwargs):
        progress({"phase": "search", "message": "1", "evaluation": 1})
        assert gates[0].wait(10)
        progress({"phase": "search", "message": "2", "evaluation": 2})
        loop.call_soon_threadsafe(second_progress.set)
        assert gates[1].wait(10)
        body_id = add_mesh_body(document, [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0], [0, 1, 2], name="結果")
        return {"state": "done", "body_id": body_id, "name": "結果", "triangle_count": 1}

    monkeypatch.setattr(optimize_run, "run", fake_run)

    class ProgressConnection(FakeConnection):
        async def send(self, raw):
            await super().send(raw)
            if self.payloads[-1].get("type") == "optimize_progress":
                first_progress.set()

    async def scenario() -> None:
        nonlocal loop
        loop = asyncio.get_running_loop()
        server = Server(session)
        server.bind_loop(asyncio.get_running_loop())
        connection = ProgressConnection()
        server.clients.add(connection)

        async def send(command: dict) -> None:
            await server._dispatch(connection, json.dumps(command))

        await send({"cmd": "run_optimization"})
        await asyncio.wait_for(first_progress.wait(), 10)
        await send({"cmd": "open_document", "name": "b.json"})
        assert connection.last("optimize_status")["running"] is False

        connection.payloads.clear()
        gates[0].set()
        await asyncio.wait_for(second_progress.wait(), 10)
        assert connection.of_type("optimize_progress") == [], "別の文書の画面に進捗を配った"

        await send({"cmd": "open_document", "name": "a.json"})
        assert connection.last("optimize_status")["running"] is True
        assert connection.last("optimize_progress")["evaluation"] == 2

        await send({"cmd": "open_document", "name": "b.json"})
        connection.payloads.clear()
        gates[1].set()
        await asyncio.gather(*server._pending_optimize_runs)
        assert connection.of_type("optimize_result") == [], "別の文書の画面に結果を配った"
        assert session.current_name == "b.json"

        await send({"cmd": "open_document", "name": "a.json"})
        assert connection.last("optimize_result")["state"] == "done"
        assert connection.last("optimize_status")["running"] is False
        assert [body.name for body in session.document.bodies][-1] == "結果"

        await send({"cmd": "open_document", "name": "b.json"})
        await send({"cmd": "open_document", "name": "a.json"})
        assert len(connection.of_type("optimize_result")) == 1

    asyncio.run(scenario())


def test_a_second_run_from_another_document_names_the_running_file(session: Session) -> None:
    """落ちたら、別の文書から2本目を押したときの断りが、どこで走っているかを言わない。"""
    session.save_document("b.json")
    session.new_document()
    four_conditions(session)
    session.rename_document("a.json")
    session.begin_optimization()
    session.open_document("b.json")
    connection = _dispatch(session, {"cmd": "run_optimization"})
    [error] = connection.of_type("optimize_error")
    assert "a.json で最適化が既に走っている" in error["message"] and error["already_running"] is True


def test_a_failure_while_on_another_document_is_shown_on_return(session: Session, monkeypatch) -> None:
    """落ちたら、別の文書を開いている間に失敗した実行の理由が、走らせた文書へ戻っても出ない(黙って消える)。"""
    four_conditions(session)
    session.rename_document("a.json")
    session.copy_document("a.json", "b.json")
    gate = threading.Event()

    def fake_run(document, options, **kwargs):
        gate.wait(10)
        raise optimize_run.OptimizeInputError("形が見つからない")

    monkeypatch.setattr(optimize_run, "run", fake_run)

    async def scenario() -> FakeConnection:
        server = Server(session)
        server.bind_loop(asyncio.get_running_loop())
        connection = FakeConnection()
        server.clients.add(connection)
        await server._dispatch(connection, json.dumps({"cmd": "run_optimization"}))
        await server._dispatch(connection, json.dumps({"cmd": "open_document", "name": "b.json"}))
        connection.payloads.clear()
        gate.set()
        await asyncio.gather(*server._pending_optimize_runs)
        assert connection.of_type("optimize_error") == [], "別の文書の画面に失敗を配った"
        await server._dispatch(connection, json.dumps({"cmd": "open_document", "name": "a.json"}))
        return connection

    connection = asyncio.run(scenario())
    assert "形が見つからない" in connection.last("optimize_error")["message"]
