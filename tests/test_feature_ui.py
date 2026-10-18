"""サーバ側の契約: ブラウザから**手でフィーチャをかける**ための口と、辺の配信。"""
from __future__ import annotations

import json
import math

import pytest

from duocad import kernel, query
from duocad.mcp_server import DocumentTools
from duocad.server import Session
from session_support import SIDE, box as _box, browser as _browser, select as _select

DEVIATION = 0.2

#: §7の実測値(20mm立方体の全辺)。**この2つが逆転したら §7 の記述が古い**。
RADIUS_THAT_FAILS = 10.0
RADIUS_THAT_WORKS = 3.0


def _edges(body, indices) -> list:
    return [{"body": body.id, "kind": "edge", "index": index} for index in indices]


def _fingerprint(session: Session) -> dict:
    """文書が「変わっていないこと」を1つの値にまとめる。"""
    return {
        "log": [
            (entry.seq, entry.verb, json.dumps(entry.args, sort_keys=True))
            for entry in session.document.log.entries
        ],
        "can_redo": session.document.log.can_redo,
        "applied_count": session._applied_count,
        "bodies": [
            (
                body.id,
                body.visible,
                id(body.shape),
                None if body.shape is None else round(query.volume(body.shape), 9),
            )
            for body in session.document.bodies
        ],
        "selection": session.get_selection(),
        "sketches": len(session.document.sketches),
        "parameters": dict(session.document.parameters),
    }


def test_every_edge_arrives_as_a_polyline_with_at_least_two_points(session: Session) -> None:
    """立方体の12辺すべてが折れ線で届く。**1本も欠けない**(欠けた辺は指せない辺になる)。"""
    body, _ = _box(session)
    described = kernel.tessellate_edges(body.shape, DEVIATION)

    assert [item["index"] for item in described] == list(range(1, 13))
    for item in described:
        assert len(item["points"]) >= 6, item  # 2点 = 6要素
        assert len(item["points"]) % 3 == 0
        assert all(isinstance(value, float) for value in item["points"])
        assert item["kind"] in kernel.EDGE_KINDS
        assert item["length"] == pytest.approx(SIDE, rel=1e-6)


def test_the_polyline_index_is_the_index_fillet_takes(session: Session) -> None:
    """折れ線の添字は `describe_edges` / `fillet(edges=[...])` と**同じ辺**を指す。"""
    body, _ = _box(session)
    polylines = {item["index"]: item for item in kernel.tessellate_edges(body.shape, DEVIATION)}

    for described in query.describe_edges(body.shape):
        polyline = polylines[described["index"]]
        points = polyline["points"]
        first = points[0:3]
        last = points[-3:]
        # 折れ線は `FirstParameter -> LastParameter` の順で、`describe_edges` の
        # `start`/`end` は辺の向きに従う。**どちらが先かは約束していない**ので端点の集合で比べる。
        ends = sorted([tuple(first), tuple(last)])
        expected = sorted([tuple(described["start"]), tuple(described["end"])])
        for got, want in zip(ends, expected):
            assert list(got) == pytest.approx(list(want), abs=1e-6), described["index"]
        assert polyline["kind"] == described["kind"]


def test_a_round_edge_comes_back_curved_and_closed(session: Session) -> None:
    """円い辺は**折れ線として曲がって**届き、閉じた辺には `closed` が立つ。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_circle(sketch_id, [0.0, 0.0], 20.0)
    _select(session, [{"sketch": sketch_id, "index": 1}])
    session.extrude(SIDE, mode="join")
    cylinder = session.document.bodies[-1]

    described = kernel.tessellate_edges(cylinder.shape, DEVIATION)
    curved = [item for item in described if item["kind"] == "circle"]
    assert curved, "円筒に円い辺が1本も無いのはおかしい"
    for item in curved:
        assert len(item["points"]) > 6  # 弦1本(2点)ではない
        assert item["closed"] is True
        assert item["length"] == pytest.approx(2.0 * math.pi * 10.0, rel=1e-3)


def test_small_radius_circle_still_gets_enough_points_from_angular_deflection(
    session: Session,
) -> None:
    """小半径の円弧は**線形偏差だけでは粗くなる**——角度偏差が効いて初めて滑らかになる。

    小半径ではsagitta(弦と弧の最大乖離)がすぐ`deviation`(既定0.2mm)を下回るため、
    線形偏差だけ(`GCPnts_QuasiUniformDeflection`)では分割点がどんどん減っていく——
    実測で半径1mmの円は6点(六角形)まで落ち込む。現在の実装
    `GCPnts_TangentialDeflection`(線形+角度)は角度偏差
    `kernel.DEFAULT_ANGULAR_DEFLECTION`(ラジアン)1本で必要点数の下限が決まり、
    半径に依存しない。
    """
    minimum_points = math.ceil(2.0 * math.pi / kernel.DEFAULT_ANGULAR_DEFLECTION) + 1

    counts_by_radius: dict[float, int] = {}
    for radius in (1.0, 10.0):
        sketch_id = session.sketch_create("XY")["sketch_id"]
        session.add_circle(sketch_id, [0.0, 0.0], 2 * radius)
        _select(session, [{"sketch": sketch_id, "index": 1}])
        session.extrude(SIDE, mode="new")
        cylinder = session.document.bodies[-1]

        described = kernel.tessellate_edges(cylinder.shape, DEVIATION)
        curved = [item for item in described if item["kind"] == "circle"]
        assert curved, "円筒に円い辺が1本も無いのはおかしい"
        counts = {len(item["points"]) // 3 for item in curved}
        assert len(counts) == 1, f"半径{radius}mmの上下2つの円の点数が揃わない: {counts}"
        (count,) = counts
        assert count >= minimum_points, (
            f"半径{radius}mmの円が角度偏差の下限{minimum_points}点を下回った"
            f"(実際{count}点)。線形偏差だけの分割に戻っていないか"
        )
        counts_by_radius[radius] = count

    # 角度偏差が主導する分割数は**半径に依存しない**(線形偏差だけだと半径が小さいほど
    # 点数が減る)。
    assert counts_by_radius[1.0] == counts_by_radius[10.0], (
        "半径によって分割点数が変わっている: 角度偏差ではなく線形偏差が支配的になっている"
        f"疑いがある({counts_by_radius})"
    )


def test_tessellate_edges_rejects_an_empty_shape_and_a_bad_deviation(session: Session) -> None:
    """黙って空を返さない(§6予防6)。"""
    body, _ = _box(session)
    with pytest.raises(ValueError):
        kernel.tessellate_edges(None, DEVIATION)
    with pytest.raises(ValueError):
        kernel.tessellate_edges(body.shape, 0.0)


def test_edges_are_sent_only_to_the_connection_that_asked(session: Session) -> None:
    """`get_edges` は要求した接続にだけ返る(状態配信に混ぜない。`preview` と同じ扱い)。"""
    body, _ = _box(session)

    async def script(send):
        return await send({"cmd": "get_edges", "body_id": body.id})

    (answer, clients) = _browser(session, script)

    assert answer["type"] == "edges"
    assert answer["body_id"] == body.id
    assert len(answer["edges"]) == 12
    assert clients[1].payloads == [], "辺は全接続へ配らない(要求した接続だけ)"


def test_edge_chains_are_sent_only_to_the_connection_that_asked(session: Session) -> None:
    """`get_edge_chains` は要求した接続にだけ、頼んだ辺の連なりを返す(連なりは辺の折れ線に入れない)。"""
    body, _ = _box(session)
    _select(session, _edges(body, [1]))
    session.fillet(RADIUS_THAT_WORKS)
    arc = next(item["index"] for item in session.edges(body.id)["edges"] if item["kind"] == "circle")

    async def script(send):
        return await send({"cmd": "get_edge_chains", "body_id": body.id, "chain_of": [arc, arc]})

    (answer, clients) = _browser(session, script)

    assert answer["type"] == "edge_chains"
    assert answer["revision"] == session.edges(body.id)["revision"]
    assert len(answer["chains"]) == 1, "同じ連なりは1つにまとめる"
    assert arc in answer["chains"][0] and len(answer["chains"][0]) == 3, "直線→円弧→直線が1つの連なり"
    assert "chain" not in session.edges(body.id)["edges"][0]
    assert clients[1].payloads == [], "連なりは全接続へ配らない(要求した接続だけ)"


def test_edge_chains_survive_a_zoom(session: Session, monkeypatch) -> None:
    """落ちたら、ズームで粗さが変わるたびに連なりを求め直す(連なりは形にだけ依存する。大きな部品で辺1本 10ms)。"""
    body, _ = _box(session)
    made = []
    real = kernel.edge_chain_finder
    monkeypatch.setattr(kernel, "edge_chain_finder", lambda shape: made.append(shape) or real(shape))
    session.edge_chains(body.id, [1])
    session.set_view_resolution(0.01, 40.0)
    session.edge_chains(body.id, [2])
    assert len(made) == 1
    _select(session, _edges(body, [1]))
    session.fillet(RADIUS_THAT_WORKS)
    session.edge_chains(body.id, [1])
    assert len(made) == 2, "形が変われば求め直す"


def test_the_edge_revision_follows_the_shape(session: Session) -> None:
    """形が変われば `revision` が上がる。**古い辺の添字を持ち越させない**ための番号。"""
    body, _ = _box(session)
    before = session.edges(body.id)
    _select(session, _edges(body, [1]))
    session.fillet(RADIUS_THAT_WORKS)
    after = session.edges(body.id)

    assert after["revision"] > before["revision"]
    assert len(after["edges"]) != len(before["edges"])


def test_the_edges_of_a_hidden_body_are_refused(session: Session) -> None:
    """隠したBodyの辺は配らない(`geometry()` と同じ規律)。"""
    body, _ = _box(session)
    session.set_visible(body.id, False)

    with pytest.raises(ValueError):
        session.edges(body.id)


def test_asking_for_the_edges_of_an_unknown_body_is_an_error(session: Session) -> None:
    _box(session)

    async def script(send):
        return await send({"cmd": "get_edges", "body_id": "body-does-not-exist"})

    (answer, _clients) = _browser(session, script)
    assert answer["type"] == "error"
    assert answer["where"] == "get_edges"


def test_extrude_without_a_mode_is_refused(session: Session) -> None:
    """落ちたら、WSの表が欠けた `mode` を既定(join)で埋めている(切り取るつもりの押し出しが黙って足す)。"""
    _box(session)
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_circle(sketch_id, [40.0, 40.0], 8.0)
    _select(session, [{"sketch": sketch_id, "index": 1}])
    before = _fingerprint(session)

    async def script(send):
        return await send({"cmd": "extrude", "distance": SIDE})

    (answer, _clients) = _browser(session, script)

    assert answer["type"] == "error"
    assert _fingerprint(session) == before


def test_the_browser_can_make_a_new_body(session: Session) -> None:
    """`mode="new"` をブラウザから打つと、**既存の立方体に融合せず**2つ目のボディが増える。"""
    box, _ = _box(session)
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_rectangle(sketch_id, [5.0, 5.0], 10.0, 10.0)
    _select(session, [{"sketch": sketch_id, "index": 1}])

    async def script(send):
        return await send({"cmd": "extrude", "distance": SIDE, "mode": "new", "client_op": 3})

    (answer, _clients) = _browser(session, script)

    assert answer["type"] == "state"
    assert len(session.document.bodies) == 2, "new が既存ボディへ融合している"
    assert math.isclose(query.volume(box.shape), SIDE**3, rel_tol=1e-9), "元の立方体が変わっている"
    created = session.document.bodies[-1]
    assert created.id != box.id
    assert math.isclose(query.volume(created.shape), 10.0 * 10.0 * SIDE, rel_tol=1e-9)


def test_the_browser_can_fillet_the_edges_it_picked(session: Session) -> None:
    """辺を複数選んでフィレットがかけられる(タスクDの検証2)。"""
    body, _ = _box(session)
    faces_before = kernel.face_count(body.shape)
    _select(session, _edges(body, [1, 2]))

    async def script(send):
        return await send(
            {"cmd": "fillet", "radius": RADIUS_THAT_WORKS, "client_op": 3}
        )

    (answer, _clients) = _browser(session, script)

    assert answer["applied"]["verb"] == "fillet"
    assert answer["applied"]["client_op"] == 3
    assert kernel.face_count(body.shape) > faces_before


def test_the_ui_and_the_ai_get_the_same_shape_from_the_same_verb() -> None:
    """**同じ操作をUIからとMCPからで行い、結果が一致する**(タスクDの検証6)。"""
    from_ui = Session(DEVIATION)
    from_ai = Session(DEVIATION)
    ui_body, _ = _box(from_ui)
    ai_body, _ = _box(from_ai)

    _select(from_ui, _edges(ui_body, (1, 3)))

    async def script(send):
        return await send({"cmd": "fillet", "radius": RADIUS_THAT_WORKS, "client_op": 1})

    _browser(from_ui, script)

    tools = DocumentTools(from_ai)
    _select(from_ai, _edges(ai_body, (1, 3)))
    tools.call("fillet", {"radius": RADIUS_THAT_WORKS})

    assert kernel.face_count(ui_body.shape) == kernel.face_count(ai_body.shape)
    assert query.volume(ui_body.shape) == pytest.approx(query.volume(ai_body.shape), rel=1e-12)
    assert [entry.verb for entry in from_ui.document.log.entries] == [
        entry.verb for entry in from_ai.document.log.entries
    ]


def test_previewing_a_fillet_shows_the_shape_and_leaves_nothing_behind(
    session: Session,
) -> None:
    """プレビューは形を返し、**文書に何も残さない**(Escで文書が変わらないことの実体)。"""
    body, _ = _box(session)
    _select(session, _edges(body, [1, 2]))
    before = _fingerprint(session)

    result = session.preview("fillet", {"radius": RADIUS_THAT_WORKS})

    assert result["verb"] == "fillet"
    assert [item["body_id"] for item in result["bodies"]] == [body.id]
    assert result["bodies"][0]["face_count"] > kernel.face_count(body.shape)
    assert _fingerprint(session) == before


def test_a_failing_preview_leaves_nothing_behind(session: Session) -> None:
    """**失敗したプレビューも何も残さない**(§7の非単調な失敗は打鍵中に普通に起きる)。"""
    body, _ = _box(session)
    _select(session, _edges(body, range(1, 13)))
    before = _fingerprint(session)

    with pytest.raises(ValueError):
        session.preview("fillet", {"radius": RADIUS_THAT_FAILS})

    assert _fingerprint(session) == before


def test_previewing_a_region_extrude_does_not_add_a_body(session: Session) -> None:
    """新しいソリッドを作る押し出し(join・対象なし)でも、プレビューは1つも残さない。"""
    sketch_id = session.sketch_create("XY")["sketch_id"]
    session.add_rectangle(sketch_id, [0.0, 0.0], SIDE, SIDE)
    _select(session, [{"sketch": sketch_id, "index": 1}])
    before = _fingerprint(session)

    result = session.preview("extrude", {"distance": SIDE, "mode": "join"})

    assert len(result["bodies"]) == 1
    assert result["bodies"][0]["face_count"] == 6
    assert _fingerprint(session) == before
    assert session.document.bodies == []


def test_preview_still_refuses_unknown_verbs_and_arguments(session: Session) -> None:
    body, _ = _box(session)
    _select(session, _edges(body, [1]))
    with pytest.raises(ValueError):
        session.preview("pattern_circular", {})
    with pytest.raises(ValueError):
        session.preview("fillet", {"radius": 1.0, "sharpness": 2})
    # **対象集合はもう受け取らない** —— 古い形の引数はここで弾かれる(黙って無視しない)
    with pytest.raises(ValueError):
        session.preview("fillet", {"edges": [1], "radius": 1.0})
