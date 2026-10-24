"""形状最適化の各ステップ(探索の合格形)を見て、選んで仕上げる(`optimize_steps`・`optimize_run.finish_step`・`net/ws.py`)。"""
from __future__ import annotations

import asyncio
import gc
import json
import math
import weakref
from dataclasses import replace

import numpy as np
import pytest
from scipy import ndimage

from duocad import optimize_run
from duocad.optimize_steps import OptimizationSteps
from duocad.server import Server, Session
from session_support import FakeConnection, browser, four_conditions

pytestmark = pytest.mark.usefixtures("finished_shape_check_at_a_third_of_the_test_element")

OPTIONS = optimize_run.OptimizeOptions(element_size_mm=8.0)


@pytest.fixture(autouse=True)
def _coarse_grids_allowed(monkeypatch) -> None:
    """この題材を 8mm の粗い格子で回すため、要素サイズの上限の固定値と最小幅の上限を緩める(test_optimize_backend と同じ)。"""
    monkeypatch.setattr(optimize_run, "MAX_ELEMENT_SIZE_MM", math.inf)
    monkeypatch.setattr(optimize_run, "MIN_WIDTH_CEILING_RATIO", 1.0)


def test_a_finish_request_stops_the_search_and_finishes_the_chosen_step(session: Session) -> None:
    """落ちたら、「このステップで仕上げる」で探索が止まらないか、選んだのと別の形を仕上げるか、中断と同じく何も足さない。
    ステップの形は表面のセルだけで配る(`steps.voxels`)ことも、この実走で見る。"""
    four_conditions(session)
    steps = OptimizationSteps()
    events: list = []
    result = optimize_run.run(session.document, OPTIONS, steps=steps, progress=events.append,
                              finish_request=lambda final=False: 1 if len(steps) >= 3 else None)
    assert result["state"] == "done" and result["termination"] == "stopped_by_user", result.get("warning")
    assert result["finished_step"] == 1 and result["name"] == "optimized-step2"
    assert result["steps"] == len(steps) == 3
    meta = steps.meta()
    assert [row["index"] for row in meta] == [0, 1, 2]
    assert meta[0]["material_mm3"] > meta[1]["material_mm3"] > meta[2]["material_mm3"]
    if result["backtrack"] is None:
        assert result["search_safety_factor"] == meta[1]["safety_factor"]
    announced = [event["step"]["index"] for event in events if "step" in event]
    assert announced == [0, 1, 2]  # 合格形が増えるたびに進捗で画面へ知らせる
    # 画面へ配るのは表面のセルだけ(内側まで配って重くしていない・表面が欠けていない)
    payload = steps.voxels(0)
    mask = steps.shapes()[0].mask()
    surface = mask & ~ndimage.binary_erosion(mask, border_value=0)
    assert payload["index"] == 0 and payload["total"] == int(surface.sum()) < int(mask.sum())
    assert payload["stride"] == 1 and sorted(payload["elements"]) == np.flatnonzero(surface.ravel()).tolist()
    with pytest.raises(IndexError):
        steps.voxels(len(steps))


def test_after_the_run_another_step_is_finished_without_searching_again(session: Session, monkeypatch) -> None:
    """再仕上げが開始時の入力を使わない、探索や入力変換をやり直す、または終了した通知先を保持している。"""
    from duocad.topopt import binary_search

    four_conditions(session)
    steps = OptimizationSteps()
    options = replace(OPTIONS)

    class Reporter:
        def __call__(self, event):
            pass

    reporter = Reporter()
    report_ref = weakref.ref(reporter)
    first = optimize_run.run(session.document, options, steps=steps, progress=reporter)
    assert first["state"] == "done" and first["finished_step"] == len(steps) - 1 and first["name"] == "optimized"
    del reporter
    gc.collect()
    assert report_ref() is None
    options.target_safety_factor = 1e10
    options.element_size_mm = 0.1
    options.min_width_mm = 1000.0
    monkeypatch.setattr(optimize_run, "build_conditions", lambda *a: pytest.fail("条件を読み直した"))
    monkeypatch.setattr(optimize_run, "resolution_limits", lambda *a: pytest.fail("格子を作り直した"))
    monkeypatch.setattr(binary_search, "find_best_feasible", lambda *a, **k: pytest.fail("探索をやり直した"))
    bodies = len(session.document.bodies)
    again = optimize_run.finish_step(session.document, steps, 0)
    assert again["state"] == "done" and again["finished_step"] == 0 and again["termination"] == "finished_after_run"
    assert len(session.document.bodies) == bodies + 1 and again["name"] == "optimized-step1"
    with pytest.raises(optimize_run.OptimizeInputError):
        optimize_run.finish_step(session.document, steps, len(steps))


def test_a_cancel_wins_over_a_finish_request_and_nothing_is_finished_before_a_feasible_shape(session: Session) -> None:
    """落ちたら、中断と仕上げが同時に来たとき中断を無視して結果を足すか、合格形が無いのに何かを仕上げようとする。"""
    four_conditions(session)
    before = len(session.document.bodies)
    steps = OptimizationSteps()
    with pytest.raises(optimize_run.OptimizeCancelled, match="中断"):
        optimize_run.run(session.document, OPTIONS, steps=steps, should_cancel=lambda: len(steps) >= 2,
                         finish_request=lambda final=False: -1 if len(steps) >= 2 else None)
    with pytest.raises(optimize_run.OptimizeCancelled, match="合格した形がまだ無い"):
        optimize_run.run(session.document, OPTIONS, steps=OptimizationSteps(), finish_request=lambda final=False: -1)
    assert len(session.document.bodies) == before


def test_the_screen_lists_the_steps_shows_one_and_finishes_another_after_the_run(session: Session) -> None:
    """落ちたら、画面がステップの一覧・形を取れないか、実行の後に選んだステップの仕上げが結果として届かない。進捗は実行の識別子を持つ。"""
    four_conditions(session)

    async def scenario() -> FakeConnection:
        server = Server(session)
        server.bind_loop(asyncio.get_running_loop())
        connection = FakeConnection()
        server.clients.add(connection)
        await server._dispatch(connection, json.dumps({"cmd": "run_optimization", "element_size_mm": 8.0}))
        await asyncio.gather(*server._pending_optimize_runs)
        await server._dispatch(connection, json.dumps({"cmd": "optimize_steps"}))
        await server._dispatch(connection, json.dumps({"cmd": "optimize_step_voxels", "index": 0}))
        await server._dispatch(connection, json.dumps({"cmd": "finish_optimization", "step": 0}))
        await asyncio.gather(*server._pending_optimize_runs)
        return connection

    connection = asyncio.run(scenario())
    [listing] = connection.of_type("optimize_steps")
    assert listing["finishable"] is True and [row["index"] for row in listing["steps"]] == list(range(len(listing["steps"])))
    [voxels] = connection.of_type("optimize_step_voxels")
    assert voxels["index"] == 0 and voxels["elements"] and len(voxels["grid"]["shape"]) == 3
    results = connection.of_type("optimize_result")
    assert [row["finished_step"] for row in results] == [len(listing["steps"]) - 1, 0]
    assert results[1]["name"] == "optimized-step1"
    # 後から届いた前の実行の進捗を画面が見分けられるよう、進捗はどれも実行の識別子を持つ
    progress = connection.of_type("optimize_progress")
    assert progress and {event["run"] for event in progress} == {session.steps_for_current().run}
    session.new_document()
    assert session.steps_for_current() is None
    assert not session.steps_finishable()


def test_finish_while_running_sets_the_request_and_tells_the_screen(session: Session) -> None:
    """落ちたら、走っている間の「このステップで仕上げる」が実行に届かないか、画面が仕上げ待ちを表示できない。"""
    session.begin_optimization()
    connection = browser(session, lambda send: send({"cmd": "finish_optimization", "step": 2}), connections=1)[1][0]
    [status] = connection.of_type("optimize_status")
    assert status["finishing"] is True
    session.end_optimization()
    session.begin_optimization()
    assert "finishing" not in session.optimization_status()


def test_finish_without_steps_is_refused_with_a_reason(session: Session) -> None:
    """落ちたら、探索をしていない文書で仕上げを押したとき黙って何もしないか、別の文書の実行のステップを仕上げる。"""
    connection = browser(session, lambda send: send({"cmd": "finish_optimization"}), connections=1)[1][0]
    [error] = connection.of_type("optimize_error")
    assert "仕上げられるステップが無い" in error["message"]


def test_a_finish_request_is_honoured_even_when_the_search_ends_on_its_own(session: Session, monkeypatch) -> None:
    """落ちたら、探索が予算・枯渇で終わる直前に届いた「このステップで仕上げる」を黙って捨て、最後の形を仕上げている。"""
    from duocad.topopt import binary_search

    four_conditions(session)
    real = binary_search.find_best_feasible

    def deaf(*args, **kwargs):  # 要求を探索の途中で見ない(終わる直前に届いたのと同じ)
        kwargs["is_cancelled"] = None
        return real(*args, **kwargs)

    monkeypatch.setattr(binary_search, "find_best_feasible", deaf)
    steps = OptimizationSteps()
    result = optimize_run.run(session.document, OPTIONS, steps=steps, finish_request=lambda final=False: 1)
    assert result["state"] == "done" and result["finished_step"] == 1 and result["name"] == "optimized-step2"
    assert result["termination"] != "stopped_by_user" and len(steps) > 2
    assert {row["run"] for row in steps.meta()} == {steps.run}


def test_a_step_is_not_finished_into_a_document_that_changed_after_the_run(session: Session) -> None:
    """落ちたら、実行の後に条件や形を変えた文書へ、古い条件・古い配置で作った結果が黙って足される。
    実行が足した結果を取り消す・足すだけでは「変わった」と数えない。"""
    left, _right, _fixed, _load = four_conditions(session)
    first = session.run_optimization(element_size_mm=8.0)
    assert first["state"] == "done", first.get("warning")

    again = session.finish_optimization_step(0)
    assert again["state"] == "done" and again["name"] == "optimized-step1"
    session.undo()
    session.undo()

    session.modify_selection([{"body": left.id, "kind": "body"}], [])
    session.move_body(0.0, 0.0, 100.0)
    session.clear_selection()
    entries = len(session.document.log.entries)
    with pytest.raises(optimize_run.OptimizeInputError, match="変わった"):
        session.finish_optimization_step(0)
    assert len(session.document.log.entries) == entries and not session.optimizing


def test_the_finish_button_is_offered_only_while_the_document_is_as_it_was_at_the_run(session: Session) -> None:
    """落ちたら、実行の後に文書を変えても画面の「このステップで仕上げる」が出たままで、押すと optimize_error になる。"""
    left, _right, _fixed, _load = four_conditions(session)
    assert session.steps_finishable() is False  # 実行する前はステップが無い
    first = session.run_optimization(element_size_mm=8.0)
    assert first["state"] == "done", first.get("warning")
    assert session.steps_finishable() is True

    session.undo()  # 結果を取り消すだけでは「変わった」と数えない
    assert session.steps_finishable() is True

    session.modify_selection([{"body": left.id, "kind": "body"}], [])
    session.move_body(0.0, 0.0, 100.0)
    session.clear_selection()
    assert session.steps_finishable() is False
