"""形状最適化を**画面から走らせる**ための配線(`20260904-1705`)の契約。

`tests/test_optimize_backend.py` はコア(`duocad/optimize_run.py`)そのものを見る。
ここが見るのは**その上に開けた口**である:

1. **解像度の目盛り**(`resolution_limits()["steps"]`)—— **添字0が上限側(いちばん粗い=既定)**で、
   進むほど細かく・要素数が増える。**粗い側へ動く段を作らない**のが
   この機能の全部なので、順序そのものを固定する。
2. **WebSocketの4つの口** —— `optimize_conditions` / `optimize_resolution` /
   `run_optimization` / `cancel_optimization`。verbではない(`category="protocol"`)ので、
   応答は状態配信ではなく専用のイベントである。
3. **走らせられないときの断り方** —— 条件が足りない・二重実行。**どちらも `type: "error"`
   ではなく `optimize_error`** で返す(画面がパネルの中に出せる形)。
4. **見積もれないときは赤い札にしない** —— 条件面がまだ1枚も無いのは「設定の途中」であって
   事故ではないので、`optimize_resolution` は `error` キーに文を入れて返す。

**ここで最適化の探索は回さない。** 端から端まで実際に回すのは実ブラウザの
`web/scripts/smoke-optimize.mjs`(約1〜2分)である。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from duocad import optimize_run
from duocad.server import Server, Session

DEVIATION = 0.2


# --------------------------------------------------------------------------------------
# 題材(`tests/test_optimize_backend.py` と同じ作り方。OCCT を余計に回さない)
# --------------------------------------------------------------------------------------
@pytest.fixture()
def session() -> Session:
    return Session(None, DEVIATION)


def _select(session: Session, refs: list) -> None:
    session.clear_selection()
    session.modify_selection(refs, [])


def _box(session: Session, width: float, depth: float, height: float, corner=(0.0, 0.0)):
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_rectangle(sketch_id, list(corner), width, depth)
    regions = len(session.state_meta()["sketches"][-1]["regions"])
    _select(session, [{"sketch": sketch_id, "index": regions}])
    session.extrude(height, mode="new")
    session.clear_selection()
    return session.document.bodies[-1]


def _face_with_normal(body, nx: float, ny: float, nz: float) -> int:
    from duocad import query

    for face in query.describe_faces(body.shape):
        normal = face["normal"]
        if normal[0] * nx + normal[1] * ny + normal[2] * nz > 0.9:
            return int(face["index"])
    raise AssertionError("そのような法線の面が無い")


def _setup_four_conditions(session: Session):
    left = _box(session, 10.0, 40.0, 40.0)
    right = _box(session, 10.0, 40.0, 10.0, corner=(60.0, 0.0))
    _select(session, [{"body": left.id, "kind": "body"}, {"body": right.id, "kind": "body"}])
    session.set_preserve_bodies()
    _select(session, [{"body": left.id, "kind": "face", "index": _face_with_normal(left, -1.0, 0.0, 0.0)}])
    session.set_fixtures()
    _select(session, [{"body": right.id, "kind": "face", "index": _face_with_normal(right, 0.0, 0.0, 1.0)}])
    session.set_loads(500.0, [0.0, 0.0, -1.0])
    session.clear_selection()
    return left, right


# --------------------------------------------------------------------------------------
# 1. 解像度の目盛り —— **細かくする方向にだけ動く**
# --------------------------------------------------------------------------------------
def test_steps_start_at_the_coarse_end(session: Session) -> None:
    """**添字0が上限側**(＝既定＝最も安い設定)。ここが逆を向くと、画面のスライダーは
    「既定より粗い設定」を用意することになり、条件が黙って消える側へ人を送り出す。
    """
    _setup_four_conditions(session)
    limits = optimize_run.resolution_limits(session.document)
    steps = limits["steps"]
    assert steps, "目盛りが1段も無い"
    assert steps[0]["element_size_mm"] == pytest.approx(limits["max_element_size_mm"])
    assert steps[0]["element_size_mm"] == pytest.approx(limits["default_element_size_mm"])
    assert steps[-1]["element_size_mm"] == pytest.approx(limits["min_element_size_mm"])


def test_steps_get_finer_and_costlier(session: Session) -> None:
    """落ちたら、目盛りを進めても要素サイズが細かく・要素数が多くならない段がある。"""
    _setup_four_conditions(session)
    steps = optimize_run.resolution_limits(session.document)["steps"]
    assert len(steps) >= 2
    for previous, current in zip(steps, steps[1:]):
        assert current["index"] == previous["index"] + 1
        assert current["element_size_mm"] < previous["element_size_mm"]
        assert current["element_count"] > previous["element_count"]
        assert current["element_count"] <= optimize_run.MAX_ELEMENTS


# --------------------------------------------------------------------------------------
# 2. WebSocketの4つの口
# --------------------------------------------------------------------------------------
class FakeConnection:
    """`Server` が使うのは `send` だけ(`tests/test_agent_ws.py` と同じ形)。"""

    def __init__(self) -> None:
        self.payloads: list = []

    async def send(self, raw: str) -> None:
        self.payloads.append(json.loads(raw))

    def of_type(self, type_: str) -> list:
        return [p for p in self.payloads if p.get("type") == type_]


def _dispatch(session: Session, message: dict) -> FakeConnection:
    """1コマンドを流して応答を集める。**探索が回らない**コマンドだけをここで使う。"""

    async def scenario() -> FakeConnection:
        server = Server(session)
        server.bind_loop(asyncio.get_running_loop())
        connection = FakeConnection()
        server.clients.add(connection)
        await server._dispatch(connection, json.dumps(message))
        return connection

    return asyncio.run(scenario())


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
    _setup_four_conditions(session)
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
    """**旧引数(`rmin_mm`)が届いたら、黙って無視せず分かるエラーにする**

    (計画書§6.2)。この口は元々「まだ設定の途中」を非トーストで運ぶ経路
    (`ValueError` → `{"error": ...}`)を持っているので、そこへ乗せる。
    """
    _setup_four_conditions(session)
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

    async def scenario() -> FakeConnection:
        server = Server(session)
        server.bind_loop(asyncio.get_running_loop())
        connection = FakeConnection()
        server.clients.add(connection)
        server._optimize_running = True  # 走っている最中の状態を作る(探索は回さない)
        await server._dispatch(connection, json.dumps({"cmd": "run_optimization"}))
        return connection

    connection = asyncio.run(scenario())
    assert "既に走っている" in connection.of_type("optimize_error")[0]["message"]


def _run_to_completion(session: Session, message: dict) -> tuple:
    """`run_optimization` を1本走らせ、バックグラウンドタスクの完了まで待つ。

    `_optimize_start` は待たずに返る(`FakeConnection` 経由の応答だけでは
    完了後の副作用(自動保存)が終わっているとは限らない)ので、
    `_pending_optimize_runs` を明示的に `gather` してから戻る。
    """

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
    """**旧引数(`rmin_mm`/`max_iter`/`material_min_mm3`/`material_max_mm3`)が届いたら、

    黙って無視せず `optimize_error` として断る**(計画書§6.2)。`net/ws.py` がこれらを
    転送し(`_optimize_start` のkwargs構築)、`session.run_optimization` が
    `OptimizeInputError` で断る配線を4つとも確かめる。
    """
    _setup_four_conditions(session)
    server, connection = _run_to_completion(
        session, {"cmd": "run_optimization", kwarg: 3.0}
    )
    reply = connection.of_type("optimize_error")[0]
    assert kwarg in reply["message"]
    assert reply.get("cancelled") is not True
    assert connection.of_type("optimize_result") == []


def test_run_optimization_forwards_min_width_mm_so_a_below_floor_value_is_refused(session: Session) -> None:
    """`net/ws.py` が `min_width_mm` を落とすと、下限割れの入力が黙って通って走り出す。"""
    _setup_four_conditions(session)
    spacing = optimize_run.resolution_limits(session.document)["default_element_size_mm"]
    _, connection = _run_to_completion(session, {"cmd": "run_optimization", "min_width_mm": spacing})
    assert "下限" in connection.of_type("optimize_error")[0]["message"]
    assert connection.of_type("optimize_result") == []


def test_run_optimization_refuses_a_min_width_beyond_the_grid_at_the_ws_door(session: Session) -> None:
    """`net/ws.py` 経由でも上限(格子の最短の辺)を越える幅は導出の前に断る。落ちたら、WS直叩きの巨大な値が
    塊の導出・境界条件の確定の後にメモリを使い切る。"""
    _setup_four_conditions(session)
    _, connection = _run_to_completion(session, {"cmd": "run_optimization", "min_width_mm": 1e6})
    assert "上限" in connection.of_type("optimize_error")[0]["message"]
    assert connection.of_type("optimize_result") == []


def test_a_missing_min_width_mm_passes_none_through_to_the_session(session: Session, monkeypatch) -> None:
    """`min_width_mm` を省いたメッセージは、`net/ws.py` が捨てずに `None` のまま
    `Session.run_optimization` へ届ける(キー自体を渡さないのと同じ)。既定 `max(2 × spacing, 1.0mm)`
    への解決は `optimize_run._resolve_min_width` 自身の仕事(`test_optimize_backend.py` で固定)。"""
    _setup_four_conditions(session)
    seen: dict = {}
    monkeypatch.setattr(session, "run_optimization", lambda **kwargs: seen.update(kwargs) or {"state": "done"})
    _run_to_completion(session, {"cmd": "run_optimization"})
    assert seen.get("min_width_mm") is None


def test_a_missing_min_width_mm_still_reaches_run_at_the_default_cells(session: Session, monkeypatch) -> None:
    """`min_width_mm` を渡さない実行が、`Session.run_optimization` → `optimize_run.run()` を経て
    既定の m(≥2)で探索へ届くこと(落ちたら、既定が入口検査だけに留まり、探索は制約なしのまま走る)。"""
    from duocad.topopt import binary_search

    _setup_four_conditions(session)
    seen: dict = {}

    def fake_find(design_space_mask, conditions, **kwargs):
        seen.update(kwargs)
        return binary_search.SearchResult(
            status="no_feasible_found", termination="evaluation_limit", best_feasible=None,
            history=[], evaluations=1, conditions_identifier="test",
        )

    monkeypatch.setattr(binary_search, "find_best_feasible", fake_find)
    # 既定(いちばん粗い)解像度はこの題材では面の対応が縮んで別の理由で断られる
    # (`_verify_face_mapping`。min_width とは無関係)。他の ws テストと同じ段を明示する。
    spacing = optimize_run.resolution_limits(session.document)["steps"][1]["element_size_mm"]
    _run_to_completion(session, {"cmd": "run_optimization", "element_size_mm": spacing})
    assert seen.get("min_width_cells", 1) >= 2


def test_a_successful_run_is_saved_to_disk_even_with_no_screen_watching(
    session: Session, monkeypatch, tmp_path
) -> None:
    """2026-09-13実測: 数時間がかりの実行が、結果を見る前にサーバプロセスごと消えた。

    結果は文書のRAMにしか無いので、**成功した瞬間にディスクへ書く**——broadcastを
    待つ相手(タブ)がいなくても、そのあとサーバが落ちても消えない形にする。
    """
    _setup_four_conditions(session)
    session.models_dir = tmp_path
    session._current_name = "hanger.json"
    monkeypatch.setattr(session, "run_optimization", lambda **_: {"state": "done"})

    _run_to_completion(session, {"cmd": "run_optimization"})

    assert (tmp_path / "hanger.json").exists()
    assert not session.warnings


def test_a_successful_run_on_an_untitled_document_only_warns(
    session: Session, monkeypatch, tmp_path
) -> None:
    """無題の文書(一度も保存していない)は保存先の名前が無い。

    黙って名前を作って書くと「知らないファイルが増えている」が起きるので、
    警告を積んで諦める——**新規ボディの成功そのものは変えない**(`optimize_result` は届く)。
    """
    _setup_four_conditions(session)
    session.models_dir = tmp_path
    assert session._current_name is None
    monkeypatch.setattr(session, "run_optimization", lambda **_: {"state": "done"})

    _, connection = _run_to_completion(session, {"cmd": "run_optimization"})

    assert connection.of_type("optimize_result")[0]["state"] == "done"
    assert list(tmp_path.iterdir()) == []
    # `session.warnings` は `_push` の中の `take_warnings()` で配信済みに drain されるので、
    # 配信された側(`connection`)で確かめる。
    assert "自動保存できなかった" in connection.of_type("warning")[0]["message"]


def test_cancel_does_nothing_when_nothing_is_running(session: Session) -> None:
    """**走っていないのに旗を立てない。** 立てても `run_optimization` の入口が畳むので
    実害は無いが、走っていないのに「中断中」と画面へ出ると、次の実行が中断表示から
    始まって見える。
    """
    connection = _dispatch(session, {"cmd": "cancel_optimization"})
    status = connection.of_type("optimize_status")[0]
    assert status["running"] is False
    assert "cancelling" not in status
    assert session._optimize_cancel.is_set() is False


def test_cancel_sets_the_flag_while_running(session: Session) -> None:
    async def scenario() -> tuple:
        server = Server(session)
        server.bind_loop(asyncio.get_running_loop())
        connection = FakeConnection()
        server.clients.add(connection)
        server._optimize_running = True
        await server._dispatch(connection, json.dumps({"cmd": "cancel_optimization"}))
        return connection

    connection = asyncio.run(scenario())
    status = connection.of_type("optimize_status")[0]
    assert status["cancelling"] is True
    assert session._optimize_cancel.is_set() is True
