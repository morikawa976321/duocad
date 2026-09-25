"""ブラウザから**手でスケッチを描く**口(`SKETCH_COMMANDS`。`duocad/net/ws.py`)と、その配信。

落ちたら、UI のコマンド名と表の名前は合っていても(`tests/test_ui_command_wiring.py`)、WS から
図形を足しても `state.sketches` に届かない・引数が欠けたまま既定値で埋まる・領域が描ける形で
届かない、のどれかが起きている。コア側の図形・領域の契約は `tests/test_sketch.py` /
`tests/test_region.py` が見る。
"""
from __future__ import annotations

import math

from duocad.server import Session
from session_support import browser

RADIUS = 20.0


def _sketches(client) -> list:
    """その接続に最後に届いた状態配信のスケッチ一覧。"""
    return client.payloads[-1]["state"]["sketches"]


async def _new_sketch(send) -> str:
    created = await send({"cmd": "sketch_create", "base_plane": "XY", "client_op": 1})
    return created["applied"]["sketch_id"]


def _draw_four_kinds(sketch_id: str) -> list:
    """4種の図形を1つずつ足す(実際のブラウザと同じ順序)。"""
    return [
        {"cmd": "add_line", "sketch_id": sketch_id, "p0": [-30.0, 5.0], "p1": [30.0, 5.0], "client_op": 2},
        {"cmd": "add_circle", "sketch_id": sketch_id, "center": [0.0, 0.0], "radius": RADIUS, "client_op": 3},
        {"cmd": "add_rectangle", "sketch_id": sketch_id, "corner": [40.0, 0.0], "width": 10.0, "height": 4.0, "client_op": 4},
        {"cmd": "add_arc", "sketch_id": sketch_id, "center": [-40.0, 0.0], "radius": 8.0, "start_deg": 0.0, "end_deg": 90.0, "client_op": 5},
    ]


def test_the_browser_can_draw_all_four_kinds(session: Session) -> None:
    """線分・円・長方形・円弧の4つが WS から足せ、**解決済みの素の数値**で `state.sketches` に出る。"""

    async def script(send):
        sketch_id = await _new_sketch(send)
        answers = [await send(command) for command in _draw_four_kinds(sketch_id)]
        return answers

    answers, clients = browser(session, script)

    assert [answer["applied"]["verb"] for answer in answers] == ["add_line", "add_circle", "add_rectangle", "add_arc"]
    assert [answer["applied"]["client_op"] for answer in answers] == [2, 3, 4, 5]
    entities = _sketches(clients[0])[0]["entities"]
    assert [entity["kind"] for entity in entities] == ["line", "circle", "rectangle", "arc"]
    circle = entities[1]["params"]
    assert math.isclose(circle["radius"], RADIUS) and (circle["cx"], circle["cy"]) == (0.0, 0.0)
    arc = entities[3]["params"]
    assert (arc["start_deg"], arc["end_deg"]) == (0.0, 90.0)
    assert clients[1].payloads[-1]["state"]["sketches"] == _sketches(clients[0]), "2本目の接続にも同じ図形が届く"


def test_entities_carry_parameter_names_so_the_values_can_be_retyped(session: Session) -> None:
    """図形は**パラメータ名つき**で届き、その名前で WS の `set_parameter` から値を打ち直せる。"""

    async def script(send):
        sketch_id = await _new_sketch(send)
        added = await send(
            {"cmd": "add_circle", "sketch_id": sketch_id, "center": [0.0, 0.0], "radius": RADIUS, "client_op": 2}
        )
        name = added["state"]["sketches"][0]["entities"][0]["refs"]["radius"]
        retyped = await send({"cmd": "set_parameter", "name": name, "value": 7.5, "client_op": 3})
        return name, retyped

    (radius_name, retyped), clients = browser(session, script)

    assert isinstance(radius_name, str) and radius_name
    assert retyped["applied"]["verb"] == "set_parameter" and retyped["applied"]["client_op"] == 3
    assert math.isclose(_sketches(clients[0])[0]["entities"][0]["params"]["radius"], 7.5)


def test_undo_removes_one_entity_at_a_time(session: Session) -> None:
    """undo で図形が1つずつ消える(まとめて消えない・スケッチごと飛ばない)。"""

    async def script(send):
        sketch_id = await _new_sketch(send)
        for command in _draw_four_kinds(sketch_id)[:2]:
            await send(command)
        counts = [len(session.document.sketches[0].entities)]
        for _ in range(2):
            await send({"cmd": "undo"})
            counts.append(len(session.document.sketches[0].entities))
        return counts

    counts, _clients = browser(session, script)
    assert counts == [2, 1, 0]


def test_missing_arguments_are_refused(session: Session) -> None:
    """引数が欠けたコマンドは失敗させ、**既定値で埋めない**(0の半径が黙って入る)。"""

    async def script(send):
        sketch_id = await _new_sketch(send)
        return await send({"cmd": "add_circle", "sketch_id": sketch_id, "center": [0.0, 0.0], "client_op": 2})

    response, _clients = browser(session, script)
    assert response["type"] == "error"
    assert session.document.sketches[0].entities == []


def test_a_line_splits_the_circle_into_two_drawable_regions(session: Session) -> None:
    """円を線分で分けると領域が2つになり、**それぞれ画面に描ける三角形を持って**届く。"""

    async def script(send):
        sketch_id = await _new_sketch(send)
        for command in _draw_four_kinds(sketch_id)[:2]:
            await send(command)

    _, clients = browser(session, script)

    regions = _sketches(clients[0])[0]["regions"]
    assert [item["index"] for item in regions] == [1, 2]
    assert math.isclose(sum(item["area"] for item in regions), math.pi * RADIUS**2, rel_tol=1e-3)
    for item in regions:
        assert len(item["positions"]) >= 9 and len(item["indices"]) % 3 == 0 and item["indices"]
        assert len(item["centroid"]) == 3


def test_mirror_sketch_entities_is_reachable_from_the_socket(session: Session) -> None:
    """`mirror_sketch_entities` も表の口を通る: 確定選択の図形を軸の線分で映した複製が届く。"""

    async def script(send):
        sketch_id = await _new_sketch(send)
        await send({"cmd": "add_line", "sketch_id": sketch_id, "p0": [0.0, -30.0], "p1": [0.0, 30.0], "client_op": 2})
        await send({"cmd": "add_circle", "sketch_id": sketch_id, "center": [15.0, 0.0], "radius": 5.0, "client_op": 3})
        axis_id, circle_id = [entity.id for entity in session.document.sketches[0].entities]
        await send({"cmd": "modify_selection", "add": [{"sketch": sketch_id, "entity": circle_id}], "remove": []})
        await send({"cmd": "commit_selection"})
        return await send(
            {"cmd": "mirror_sketch_entities", "axis": {"sketch": sketch_id, "entity": axis_id}, "client_op": 4}
        )

    answer, clients = browser(session, script)

    assert answer["applied"]["verb"] == "mirror_sketch_entities" and answer["applied"]["client_op"] == 4
    circles = [item for item in _sketches(clients[0])[0]["entities"] if item["kind"] == "circle"]
    assert sorted(round(item["params"]["cx"], 9) for item in circles) == [-15.0, 15.0]
