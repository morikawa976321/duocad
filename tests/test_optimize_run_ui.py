"""形状最適化を**画面から走らせる**ための配線(`20260904-1705`)の契約。

`tests/test_optimize_backend.py` はコア(`duocad/optimize_run.py`)そのものを見る。
ここが見るのは**その上に開けた口**である:

1. **解像度の目盛り**(`resolution_limits()["steps"]`)—— **添字0が上限側(いちばん粗い=既定)**で、
   進むほど細かく・要素数が増え・予想時間が伸びる。**粗い側へ動く段を作らない**のが
   この機能の全部なので、順序そのものを固定する。
2. **WebSocketの4つの口** —— `optimize_conditions` / `optimize_resolution` /
   `run_optimization` / `cancel_optimization`。verbではない(`category="protocol"`)ので、
   応答は状態配信ではなく専用のイベントである。
3. **走らせられないときの断り方** —— 条件が足りない・二重実行。**どちらも `type: "error"`
   ではなく `optimize_error`** で返す(画面がパネルの中に出せる形)。
4. **見積もれないときは赤い札にしない** —— 条件面がまだ1枚も無いのは「設定の途中」であって
   事故ではないので、`optimize_resolution` は `error` キーに文を入れて返す。

**ここでSIMPは回さない。** 端から端まで実際に回すのは実ブラウザの
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


def test_steps_get_finer_costlier_and_slower(session: Session) -> None:
    """3つとも単調であること。**要素数だけを見ても足りない** —— 予想時間が単調でないと、
    「右へ動かすと時間を払う」という画面の説明が嘘になる。
    """
    _setup_four_conditions(session)
    steps = optimize_run.resolution_limits(session.document)["steps"]
    assert len(steps) >= 2
    for previous, current in zip(steps, steps[1:]):
        assert current["index"] == previous["index"] + 1
        assert current["element_size_mm"] < previous["element_size_mm"]
        assert current["element_count"] > previous["element_count"]
        assert current["seconds_typical"] > previous["seconds_typical"]
        assert current["element_count"] <= optimize_run.MAX_ELEMENTS


def test_min_member_width_moves_the_coarse_end(session: Session) -> None:
    """最小部材幅を打つと**上限側(既定)が動く**。`rmin_mm / 1.5` が条件面の要求より
    厳しければそちらが勝つ(`20260904-1704` の契約②の min)。
    """
    _setup_four_conditions(session)
    loose = optimize_run.resolution_limits(session.document)
    tight = optimize_run.resolution_limits(session.document, rmin_mm=3.0)
    assert tight["steps"][0]["element_size_mm"] == pytest.approx(3.0 / 1.5)
    assert tight["steps"][0]["element_size_mm"] < loose["steps"][0]["element_size_mm"]
    assert tight["steps"][0]["element_count"] > loose["steps"][0]["element_count"]


def test_steps_are_plain_values(session: Session) -> None:
    """**そのままWebSocketへ載せられる**こと(`20260904-1704` の契約①と同じ規律)。"""
    _setup_four_conditions(session)
    limits = optimize_run.resolution_limits(session.document)
    json.dumps(limits)  # 落ちなければよい


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
    """1コマンドを流して応答を集める。**SIMPは回らない**コマンドだけをここで使う。"""

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
    connection = _dispatch(session, {"cmd": "optimize_resolution", "rmin_mm": 3.0})
    reply = connection.of_type("optimize_resolution")[0]
    assert reply["steps"][0]["element_size_mm"] == pytest.approx(3.0 / 1.5)
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
        server._optimize_running = True  # 走っている最中の状態を作る(SIMPは回さない)
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


def test_a_successful_run_on_an_untitled_document_is_saved_under_an_automatic_name(
    session: Session, monkeypatch, tmp_path
) -> None:
    """無題の文書(一度も名前を付けていない)でも結果は消えない —— 自動保存が
    `untitled-<n>.json` の名前を付けて書く(issue #5、2026-09-16)。警告は積まない。
    """
    session.models_dir = tmp_path
    _setup_four_conditions(session)  # 条件を置く verb が最初の変更 = ここで名前が付く
    assert session._current_name == "untitled-1.json"
    monkeypatch.setattr(session, "run_optimization", lambda **_: {"state": "done"})

    _, connection = _run_to_completion(session, {"cmd": "run_optimization"})

    assert connection.of_type("optimize_result")[0]["state"] == "done"
    assert (tmp_path / "untitled-1.json").exists()
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
