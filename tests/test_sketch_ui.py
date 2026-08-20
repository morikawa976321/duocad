"""C契約(サーバ側): ブラウザから**手でスケッチを描く**ための口と、その配信。

UI(スケッチモード・図形・領域の表示)そのものは**`pytest` では完了判定できない**
(実機確認は `web/scripts/smoke.mjs`)。このファイルが固定するのはサーバ側の2つ——

1. **スケッチのverbをWebSocketにも開ける**(`sketch_create` / `add_line` / `add_circle` /
   `add_rectangle` / `add_arc` ＋ 値の打ち直しの `set_parameter`)
2. **スケッチを描ける形で配信する**(`state.sketches`: 平面の3D枠・図形・領域の三角形)

——は機械で判定できる。ここが緑でないとUIの検証は意味を持たない(画面に線が出ることの
実体は、平面の枠と図形がコアから届いていることだから)。

**中心命題との関係**: スケッチのverbはコアとMCPにあり、AIは描ける。**人が描くための
口**が `SKETCH_COMMANDS` であり、これが無いと「手動操作が一級の操作モード」
(実装計画書§8b C)がスケッチで成立しない。**コアには何も足していない**
(選択の3コマンドをブラウザに開けているのと同じ形)。

固定する契約:

- 基準平面3面と**既存ソリッドの平面状の面**の両方にスケッチを作れる。平面でない面は
  黙って通さない
- 4種の図形(線分・円・長方形・円弧)をWebSocketから足せ、`state.sketches` に出る
- 図形は**パラメータ名を持って届く**(クリックで置いた後に値を打ち直せる=§3.4)
- 円を線分で分けると**領域が2つ**になり、各領域は**画面に描ける三角形**を持つ
- 領域は共有選択集合に入れられる(Dの押し出しがこの選択を引数に取る)
- undoは図形を1つずつ戻す
- 引数が欠けたコマンドは**黙って既定値で埋めない**
"""
from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path

import pytest

from test_visibility import first_extrudable_face

from duocad import query
from duocad.server import SKETCH_COMMANDS, Server, Session

from models import MODELS  # テスト共通のSTEPの置き場(tests/models.py)
from models import SHELF  # noqa: F401

DEVIATION = 0.2
RADIUS = 20.0
CIRCLE_AREA = math.pi * RADIUS * RADIUS
AREA_REL_TOL = 1.0e-3
XY_PLANE = {"kind": "base", "name": "XY", "provenance": None}


@pytest.fixture()
def session() -> Session:
    if not SHELF.exists():
        pytest.skip(f"参照STEPが無い: {SHELF}")
    return Session(SHELF, DEVIATION)


class FakeConnection:
    """`Server` が使うのは `send` だけ(配信の出口)。test_preview.py と同じ形。"""

    def __init__(self) -> None:
        self.payloads: list = []

    async def send(self, raw: str) -> None:
        self.payloads.append(json.loads(raw))

    def last(self, kind: str) -> dict:
        for payload in reversed(self.payloads):
            if payload.get("type") == kind:
                return payload
        raise AssertionError(f"{kind!r} の応答が無い: {[p.get('type') for p in self.payloads]}")


def _browser(session: Session, script):
    """`script(send)` を**1つのイベントループの中で**走らせる。

    `send(command)` はコマンドを1つ流して、その接続が受け取った最後の応答を返す。
    **1コマンドずつ流せる**ことがここの要点で、`sketch_create` が返したIDを次の
    `add_circle` に渡す、という実際のブラウザと同じ順序を書ける。

    **2本つなぐ**(1本だけだと「2本目に届かない」実装でも緑になる。test_preview.py と同じ)。
    ループを1つに閉じているのは、接続ごとの送信ロックが最初に待った ループに縛られるため
    (`asyncio.run` を複数回呼ぶと別ループになり RuntimeError)。
    """

    async def scenario():
        server = Server(session)
        server.bind_loop(asyncio.get_running_loop())
        clients = [FakeConnection(), FakeConnection()]
        for client in clients:
            server.clients.add(client)
            await server._push(client, applied=None, only=client)

        async def send(command: dict) -> dict:
            await server._dispatch(clients[0], json.dumps(command))
            return clients[0].payloads[-1]

        return await script(send), clients

    return asyncio.run(scenario())


def _sketches(client: FakeConnection) -> list:
    return client.last("state")["state"]["sketches"]


def _planar_face(session: Session):
    """編集用Bodyと、その中の平面状の面の添字(スケッチを貼れる面)。"""
    body = next(b for b in session.document.bodies if b.kind == "solid" and b.visible)
    return body, first_extrudable_face(body.shape)


def _curved_face(session: Session):
    """編集用Bodyと、その中の**平面でない**面の添字(貼れない面)。"""
    body = next(b for b in session.document.bodies if b.kind == "solid" and b.visible)
    for face in query.describe_faces(body.shape):
        if face["kind"] != "plane":
            return body, face["index"]
    pytest.skip("この部品には平面でない面が無い")


# --------------------------------------------------------------------------------------
# スケッチを作る
# --------------------------------------------------------------------------------------
def test_the_browser_can_create_a_sketch_on_a_base_plane(session: Session) -> None:
    """基準平面にスケッチを作れ、**全接続へ**届く。平面の枠(u,v の軸)も一緒に来る。

    枠が来ないとアプリ層は画面のクリックを (u, v) へ落とせない(＝手で描けない)。
    """

    async def script(send):
        return await send({"cmd": "sketch_create", "plane": XY_PLANE, "client_op": 1})

    applied, clients = _browser(session, script)

    sketch_id = applied["applied"]["sketch_id"]
    assert applied["applied"]["verb"] == "sketch_create"
    assert applied["applied"]["client_op"] == 1
    for client in clients:  # 操作していない接続にも届いている
        sketches = _sketches(client)
        assert [item["sketch_id"] for item in sketches] == [sketch_id]
        frame = sketches[0]["frame"]
        assert frame["origin"] == [0.0, 0.0, 0.0]
        assert frame["u"] == [1.0, 0.0, 0.0]
        assert frame["v"] == [0.0, 1.0, 0.0]
        assert frame["normal"] == [0.0, 0.0, 1.0]


def test_a_sketch_can_be_glued_to_a_planar_face_of_a_solid(session: Session) -> None:
    """既存ソリッドの平面状の面にも貼れる。枠は**その面**のもの(基準平面ではない)。"""
    body, face_index = _planar_face(session)

    async def script(send):
        return await send(
            {
                "cmd": "sketch_create",
                "plane": {"kind": "face", "body": body.id, "index": face_index, "provenance": None},
                "client_op": 1,
            }
        )

    _applied, clients = _browser(session, script)

    frame = _sketches(clients[0])[0]["frame"]
    assert len(frame["origin"]) == 3
    # 法線は単位ベクトル(面から取れている証拠。基準平面の決め打ちではない)
    assert math.isclose(math.hypot(*frame["normal"]), 1.0, rel_tol=1e-9)
    assert _sketches(clients[0])[0]["plane"]["kind"] == "face"


def test_a_curved_face_is_refused_loudly(session: Session) -> None:
    """平面でない面にスケッチを貼ろうとしたら**失敗を明示**する(黙って入らない)。"""
    body, face_index = _curved_face(session)

    async def script(send):
        return await send(
            {
                "cmd": "sketch_create",
                "plane": {"kind": "face", "body": body.id, "index": face_index, "provenance": None},
                "client_op": 1,
            }
        )

    response, _clients = _browser(session, script)

    assert response["type"] == "error"
    assert "平面" in response["message"] and "ValueError" in response["message"]
    assert session.document.sketches == []


# --------------------------------------------------------------------------------------
# 図形を描く
# --------------------------------------------------------------------------------------
def _draw_four_kinds(send, sketch_id: str):
    """4種の図形を1つずつ足す(実際のブラウザと同じ順序)。"""
    return [
        {"cmd": "add_line", "sketch_id": sketch_id, "p0": [-30.0, 5.0], "p1": [30.0, 5.0], "client_op": 2},
        {"cmd": "add_circle", "sketch_id": sketch_id, "center": [0.0, 0.0], "radius": RADIUS, "client_op": 3},
        {
            "cmd": "add_rectangle",
            "sketch_id": sketch_id,
            "corner": [40.0, 0.0],
            "width": 10.0,
            "height": 4.0,
            "client_op": 4,
        },
        {
            "cmd": "add_arc",
            "sketch_id": sketch_id,
            "center": [-40.0, 0.0],
            "radius": 8.0,
            "start_deg": 0.0,
            "end_deg": 90.0,
            "client_op": 5,
        },
    ]


def test_the_browser_can_draw_all_four_kinds(session: Session) -> None:
    """線分・円・長方形・円弧の4つがWebSocketから足せ、`state.sketches` に出る。"""

    async def script(send):
        created = await send({"cmd": "sketch_create", "plane": XY_PLANE, "client_op": 1})
        sketch_id = created["applied"]["sketch_id"]
        for command in _draw_four_kinds(send, sketch_id):
            await send(command)
        return sketch_id

    _sketch_id, clients = _browser(session, script)

    entities = _sketches(clients[0])[0]["entities"]
    assert [entity["kind"] for entity in entities] == ["line", "circle", "rectangle", "arc"]
    # 値は**解決済みの素の数値**で届く(画面はパラメータ名を解決しない)
    circle = entities[1]["params"]
    assert math.isclose(circle["radius"], RADIUS)
    assert (circle["cx"], circle["cy"]) == (0.0, 0.0)
    arc = entities[3]["params"]
    assert (arc["start_deg"], arc["end_deg"]) == (0.0, 90.0)


def test_entities_carry_parameter_names_so_the_values_can_be_retyped(session: Session) -> None:
    """図形は**パラメータ名つき**で届き、その名前で値を打ち直せる。

    「クリックだけで置いた後も値を打ち直せる」(§3.4 パラメトリック・プリミティブ)の実体。
    名前が届かないと、画面は数値を出せても直す先が無い。
    """

    async def script(send):
        created = await send({"cmd": "sketch_create", "plane": XY_PLANE, "client_op": 1})
        sketch_id = created["applied"]["sketch_id"]
        added = await send(
            {"cmd": "add_circle", "sketch_id": sketch_id, "center": [0.0, 0.0], "radius": RADIUS, "client_op": 2}
        )
        name = added["state"]["sketches"][0]["entities"][0]["refs"]["radius"]
        # その名前で打ち直す(画面の「打ち直し」の経路そのもの)
        retyped = await send({"cmd": "set_parameter", "name": name, "value": 7.5, "client_op": 3})
        return name, retyped

    (radius_name, retyped), clients = _browser(session, script)

    assert isinstance(radius_name, str) and radius_name
    assert retyped["applied"]["verb"] == "set_parameter"
    assert retyped["applied"]["client_op"] == 3
    assert math.isclose(_sketches(clients[0])[0]["entities"][0]["params"]["radius"], 7.5)


def test_undo_removes_one_entity_at_a_time(session: Session) -> None:
    """undoで図形が1つずつ消える(まとめて消えない・スケッチごと飛ばない)。"""

    async def script(send):
        created = await send({"cmd": "sketch_create", "plane": XY_PLANE, "client_op": 1})
        sketch_id = created["applied"]["sketch_id"]
        await send(
            {"cmd": "add_circle", "sketch_id": sketch_id, "center": [0.0, 0.0], "radius": RADIUS, "client_op": 2}
        )
        await send(
            {"cmd": "add_line", "sketch_id": sketch_id, "p0": [-30.0, 5.0], "p1": [30.0, 5.0], "client_op": 3}
        )
        counts = [len(session.document.sketches[0].entities)]
        await send({"cmd": "undo"})
        counts.append(len(session.document.sketches[0].entities))
        await send({"cmd": "undo"})
        counts.append(len(session.document.sketches[0].entities))
        return counts

    counts, _clients = _browser(session, script)
    assert counts == [2, 1, 0]


def test_missing_arguments_are_refused(session: Session) -> None:
    """引数が欠けたコマンドは失敗させる。**既定値で埋めない**(0の半径が黙って入る)。"""

    async def script(send):
        created = await send({"cmd": "sketch_create", "plane": XY_PLANE, "client_op": 1})
        sketch_id = created["applied"]["sketch_id"]
        return await send({"cmd": "add_circle", "sketch_id": sketch_id, "center": [0.0, 0.0]})

    response, _clients = _browser(session, script)
    assert response["type"] == "error"
    assert session.document.sketches[0].entities == []


def test_only_the_registered_commands_are_reachable() -> None:
    """ソケットから呼べるのは登録した6つだけ(文字列で任意のメソッドを呼ばせない)。"""
    assert set(SKETCH_COMMANDS) == {
        "sketch_create",
        "add_line",
        "add_circle",
        "add_rectangle",
        "add_arc",
        "set_parameter",
    }


# --------------------------------------------------------------------------------------
# 領域 — 画面に出す・選ぶ
# --------------------------------------------------------------------------------------
def test_a_line_splits_the_circle_into_two_drawable_regions(session: Session) -> None:
    """円を線分で分けると領域が2つになり、**それぞれ画面に描ける三角形を持つ**。

    本人がFusionで日常的にやっていた操作(タスクCの検証3)。コア側の領域検出は
    `test_region.py` が縛っているので、ここが足すのは「**アプリ層が描ける形で届くか**」。
    """

    async def script(send):
        created = await send({"cmd": "sketch_create", "plane": XY_PLANE, "client_op": 1})
        sketch_id = created["applied"]["sketch_id"]
        await send(
            {"cmd": "add_circle", "sketch_id": sketch_id, "center": [0.0, 0.0], "radius": RADIUS, "client_op": 2}
        )
        await send(
            {"cmd": "add_line", "sketch_id": sketch_id, "p0": [-30.0, 5.0], "p1": [30.0, 5.0], "client_op": 3}
        )
        return sketch_id

    _sketch_id, clients = _browser(session, script)

    regions = _sketches(clients[0])[0]["regions"]
    assert [item["index"] for item in regions] == [1, 2]
    assert math.isclose(sum(item["area"] for item in regions), CIRCLE_AREA, rel_tol=AREA_REL_TOL)
    for item in regions:
        assert len(item["positions"]) >= 9      # 三角形1枚ぶん以上
        assert len(item["indices"]) % 3 == 0 and item["indices"]
        assert len(item["centroid"]) == 3


def test_a_region_can_be_selected_through_the_shared_selection(session: Session) -> None:
    """領域は共有選択集合に入れられる(Dの押し出しがこの選択を引数に取る)。"""

    async def script(send):
        created = await send({"cmd": "sketch_create", "plane": XY_PLANE, "client_op": 1})
        sketch_id = created["applied"]["sketch_id"]
        await send(
            {"cmd": "add_circle", "sketch_id": sketch_id, "center": [0.0, 0.0], "radius": RADIUS, "client_op": 2}
        )
        await send(
            {
                "cmd": "modify_selection",
                "add": [{"sketch": sketch_id, "index": 1, "provenance": None}],
                "remove": [],
                "client_op": 3,
            }
        )
        return sketch_id

    sketch_id, clients = _browser(session, script)

    shared = clients[0].last("state")["state"]["selection"]
    assert shared["count"] == 1
    assert shared["origin"] == "user"
    assert shared["items"][0]["sketch"] == sketch_id
    assert shared["items"][0]["index"] == 1


def test_the_region_payload_is_cached_until_the_sketch_changes(session: Session) -> None:
    """領域は重い(`BRepAlgoAPI_Splitter`+テセレーション)ので、内容が同じなら作り直さない。

    `state` は変更のたびに全接続へ配るため、ここを毎回計算すると図形を1本足すだけで
    全領域の再計算が走る。**キャッシュは内容の署名で引く**ので、`set_parameter` で
    値が変われば必ず作り直される(古い形が画面に残らない)。
    """

    async def script(send):
        created = await send({"cmd": "sketch_create", "plane": XY_PLANE, "client_op": 1})
        sketch_id = created["applied"]["sketch_id"]
        await send(
            {"cmd": "add_circle", "sketch_id": sketch_id, "center": [0.0, 0.0], "radius": RADIUS, "client_op": 2}
        )
        return sketch_id

    sketch_id, _clients = _browser(session, script)

    first = session.sketches_state()[0]["regions"]
    second = session.sketches_state()[0]["regions"]
    assert second[0]["positions"] is first[0]["positions"]  # 作り直していない(同一オブジェクト)

    radius_name = session.sketches_state()[0]["entities"][0]["refs"]["radius"]
    session.set_parameter(radius_name, 5.0)
    after = session.sketches_state()[0]["regions"]
    assert after[0]["positions"] is not first[0]["positions"]
    assert math.isclose(after[0]["area"], math.pi * 25.0, rel_tol=AREA_REL_TOL)
