"""D契約(サーバ側): ブラウザから**手でフィーチャをかける**ための口と、辺の配信。

UI(領域押し出し・フィレット・面取り・反復のコマンド)そのものは**`pytest` では
完了判定できない**(実機確認は `web/scripts/smoke.mjs`)。このファイルが固定するのは
コアとサーバ側の2つ——

1. **辺を折れ線で配る**(`kernel.tessellate_edges` + `Session.edges` + `get_edges`)。
   面がクリックで選べるのは面ごとの三角形を配っているからで、**辺も配らないと人は
   辺を指せない**。フィレット・面取りは「どの辺か」を選べないと使えないので、
   ここが無いとその2つのverbは人間側から永久に使えないままになる
2. **フィーチャのverbをWebSocketにも開ける**(`FEATURE_COMMANDS`)

——は機械で判定できる。ここが緑でないとUIの検証は意味を持たない(画面で辺を選べることの
実体は、コアと同じ数え方の辺がブラウザに届いていることだから)。

固定する契約:

- 辺は**1始まりの添字つきの折れ線**で届き、その添字は選択の参照(`{"kind":"edge","index":n}`)が
  持つ添字と同じ(ずれると「指した辺と違う辺が丸まる」という、例外も出ない静かな事故になる)
- **フィーチャのコマンドは対象集合を引数で受け取らない**(2026-08-20)。丸める辺も押し出す
  領域もコアの確定選択から取るので、WebSocketへ送るのは数値と `mode` だけである
- 辺は**要求した接続にだけ**返る(状態配信に混ぜない)。形が変われば `revision` が上がる
- 領域押し出し・フィレット・面取り・矩形反復・円形反復をWebSocketから踏める
- **`mode`(join/cut)は必須**。既定値で補わない(切り取るつもりで足すのを黙って通さない)
- **フィレットの失敗は文書に何も残さない**(§7: OCCTは例外を投げずに失敗し、半径に対して
  単調でもない)。**選択も保持される** —— 24本選び直しにならないことがUI側の要件の前提
- プレビューはフィレット・面取り・領域押し出しでも**何も残さない**
"""
from __future__ import annotations

import asyncio
import json
import math

import pytest

from duocad import kernel, query
from duocad.mcp_server import DocumentTools
from duocad.server import FEATURE_COMMANDS, PREVIEWABLE_VERBS, Server, Session

DEVIATION = 0.2
SIDE = 20.0
XY_PLANE = {"kind": "base", "name": "XY", "provenance": None}

#: §7の実測値(20mm立方体の全辺)。**この2つが逆転したら §7 の記述が古い**。
RADIUS_THAT_FAILS = 10.0
RADIUS_THAT_WORKS = 3.0


@pytest.fixture()
def session() -> Session:
    """**STEPを読まない空の文書**(Dは手で作る側の検証なので、参照部品は要らない)。"""
    return Session(None, DEVIATION)


def _select(session: Session, refs: list) -> None:
    """選択を `refs` で置き換える(人がクリックで選び直すのと同じ入口)。

    **verbの対象集合はここで決まる** —— コマンドの引数には入らない(2026-08-20)。
    """
    session.clear_selection()
    session.modify_selection(refs, [])


def _edges(body, indices) -> list:
    return [{"body": body.id, "kind": "edge", "index": index} for index in indices]


def _box(session: Session):
    """スケッチ→長方形→領域押し出しで 20mm の立方体を1つ作り、そのBodyを返す。

    **人が手でやる順序そのもの**(タスクDの検証1「手だけで板を作る」の最小形)。
    領域を選んでから押し出すのも人の順序どおり。
    """
    sketch_id = session.sketch_create(XY_PLANE)["sketch_id"]
    session.add_rectangle(sketch_id, [0.0, 0.0], SIDE, SIDE)
    _select(session, [{"sketch": sketch_id, "index": 1}])
    session.extrude_regions(SIDE, mode="join")
    session.clear_selection()
    return session.document.bodies[-1], sketch_id


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

    def kinds(self) -> list:
        return [payload.get("type") for payload in self.payloads]


def _browser(session: Session, script):
    """`script(send)` を1つのイベントループで走らせる(test_sketch_ui.py と同じ harness)。

    **2本つなぐ** —— 1本だけだと「2本目に届かない」実装でも緑になる。`get_edges` が
    要求した接続にだけ返ることは、2本目に何も来ないことでしか確かめられない。
    """

    async def scenario():
        server = Server(session)
        server.bind_loop(asyncio.get_running_loop())
        clients = [FakeConnection(), FakeConnection()]
        for client in clients:
            server.clients.add(client)
            await server._push(client, applied=None, only=client)
        for client in clients:
            client.payloads.clear()  # 初期配信は数えない(以後に届いたものだけ見る)

        async def send(command: dict) -> dict:
            await server._dispatch(clients[0], json.dumps(command))
            return clients[0].payloads[-1]

        return await script(send), clients

    return asyncio.run(scenario())


def _fingerprint(session: Session) -> dict:
    """文書が「変わっていないこと」を1つの値にまとめる(test_preview.py と同じ道具)。"""
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


def _drawn(session: Session, sketch_id: str) -> list:
    sketch = next(
        item for item in session.state_meta()["sketches"] if item["sketch_id"] == sketch_id
    )
    return sketch["drawn"]


# --------------------------------------------------------------------------------------
# 辺を折れ線で配る(kernel)
# --------------------------------------------------------------------------------------
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
    """折れ線の添字は `describe_edges` / `fillet(edges=[...])` と**同じ辺**を指す。

    ここがずれると「指した辺と違う辺が丸まる」という、例外も出ない静かな事故になる。
    `describe_edges` は端点を build123d で測る別経路なので、**両端が一致することが
    2経路の突き合わせ**になっている(`describe_faces` の法線が tessellate と突き合わせ
    られているのと同じ規律)。
    """
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
    """円い辺は**折れ線として曲がって**届き、閉じた辺には `closed` が立つ。

    直線と同じ2点で返すと、画面上は円筒の丸い稜線が弦1本になり、**狙ってクリックできない**
    (面取りで円錐を作るには円い辺を指す必要がある。タスクDの検証3 = `PC_case_feet` の構成)。
    """
    sketch_id = session.sketch_create(XY_PLANE)["sketch_id"]
    session.add_circle(sketch_id, [0.0, 0.0], 10.0)
    _select(session, [{"sketch": sketch_id, "index": 1}])
    session.extrude_regions(SIDE, mode="join")
    cylinder = session.document.bodies[-1]

    described = kernel.tessellate_edges(cylinder.shape, DEVIATION)
    curved = [item for item in described if item["kind"] == "circle"]
    assert curved, "円筒に円い辺が1本も無いのはおかしい"
    for item in curved:
        assert len(item["points"]) > 6  # 弦1本(2点)ではない
        assert item["closed"] is True
        assert item["length"] == pytest.approx(2.0 * math.pi * 10.0, rel=1e-3)


def test_tessellate_edges_rejects_an_empty_shape_and_a_bad_deviation(session: Session) -> None:
    """黙って空を返さない(§6予防6)。"""
    body, _ = _box(session)
    with pytest.raises(ValueError):
        kernel.tessellate_edges(None, DEVIATION)
    with pytest.raises(ValueError):
        kernel.tessellate_edges(body.shape, 0.0)


# --------------------------------------------------------------------------------------
# 辺を配る口(Session.edges / get_edges)
# --------------------------------------------------------------------------------------
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
    """隠したBodyの辺は配らない(`geometry()` と同じ規律)。

    配ると、隠したはずの部品の辺だけが宙に浮いて見え、しかも指せる —— 辺のピッキングは
    「見えているものと指せるものを一致させる」ことで成り立っているので、ここが破れると
    見えない辺を丸める経路ができる。
    """
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


# --------------------------------------------------------------------------------------
# フィーチャのverbをブラウザから踏む
# --------------------------------------------------------------------------------------
def test_the_browser_can_extrude_a_region_and_cut_a_hole(session: Session) -> None:
    """**手だけで板に穴を開けられる**(タスクDの検証1)。cut は体積を減らす。"""
    body, _ = _box(session)
    volume_before = query.volume(body.shape)
    hole_sketch = session.sketch_create(XY_PLANE)["sketch_id"]
    session.add_circle(hole_sketch, [10.0, 10.0], 4.0)
    _select(session, [{"sketch": hole_sketch, "index": 1}])

    async def script(send):
        return await send(
            {
                "cmd": "extrude",
                "distance": SIDE,
                "mode": "cut",
                "client_op": 7,
            }
        )

    (answer, clients) = _browser(session, script)

    assert answer["type"] == "state"
    assert answer["applied"]["verb"] == "extrude"
    assert answer["applied"]["client_op"] == 7
    assert query.volume(body.shape) < volume_before
    assert clients[1].payloads, "他の接続にも状態が届く(文書が変わったのだから)"


def test_extrude_without_a_mode_is_refused(session: Session) -> None:
    """**`mode` を既定で埋めない**。「切り取るつもりで足した」が黙って通らないこと。"""
    _box(session)
    sketch_id = session.sketch_create(XY_PLANE)["sketch_id"]
    session.add_circle(sketch_id, [10.0, 10.0], 4.0)
    _select(session, [{"sketch": sketch_id, "index": 1}])
    before = _fingerprint(session)

    async def script(send):
        return await send({"cmd": "extrude", "distance": SIDE})

    (answer, _clients) = _browser(session, script)

    assert answer["type"] == "error"
    assert _fingerprint(session) == before


def test_the_browser_can_fillet_the_edges_it_picked(session: Session) -> None:
    """辺を複数選んでフィレットがかけられる(タスクDの検証2)。

    **選んだ辺はコマンドに載らない** —— 画面のクリックが `modify_selection` で
    コアの選択集合を編集し、`fillet` はそこから辺を取る(MCPと同じ入口)。
    """
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


def test_a_fillet_that_fails_leaves_the_document_and_the_selection_untouched(
    session: Session,
) -> None:
    """**§7の失敗が握りつぶされない**。理由が画面へ返り、文書も選択もそのまま。

    OCCTのフィレットは例外を投げずに `IsDone()=False` で失敗し、しかも半径に対して
    単調でない(r=10.0 失敗 / r=10.5 成功)。**選択が残ることがUI側の要件の前提**で、
    ここが消えると24本選び直しになる(タスクDの「失敗の扱い」)。
    """
    body, _ = _box(session)
    _select(session, _edges(body, range(1, 13)))
    before = _fingerprint(session)

    async def script(send):
        return await send(
            {"cmd": "fillet", "radius": RADIUS_THAT_FAILS, "client_op": 4}
        )

    (answer, _clients) = _browser(session, script)

    assert answer["type"] == "error"
    assert answer["where"] == "fillet"
    assert "fillet" in answer["message"]
    assert _fingerprint(session) == before
    assert session.get_selection()["count"] == 12


def test_the_browser_can_chamfer(session: Session) -> None:
    body, _ = _box(session)
    faces_before = kernel.face_count(body.shape)
    _select(session, _edges(body, [1]))

    async def script(send):
        return await send({"cmd": "chamfer", "distance": 2.0, "client_op": 5})

    (answer, _clients) = _browser(session, script)

    assert answer["applied"]["verb"] == "chamfer"
    assert kernel.face_count(body.shape) == faces_before + 1


def test_the_browser_can_repeat_shapes_in_a_grid(session: Session) -> None:
    """矩形反復。**元を含めた個数**(nx×ny)がそのまま描画対象の数になる。"""
    sketch_id = session.sketch_create(XY_PLANE)["sketch_id"]
    entity_id = session.add_circle(sketch_id, [5.0, 5.0], 2.0)["entity_id"]

    async def script(send):
        return await send(
            {
                "cmd": "pattern_rectangular",
                "sketch_id": sketch_id,
                "entities": [entity_id],
                "dx": 10.0,
                "dy": 10.0,
                "nx": 3,
                "ny": 2,
                "client_op": 6,
            }
        )

    (answer, _clients) = _browser(session, script)

    assert answer["applied"]["verb"] == "pattern_rectangular"
    assert len(_drawn(session, sketch_id)) == 6


def test_a_full_circle_pattern_of_eight_steps_by_45_degrees(session: Session) -> None:
    """円形反復。**全周8個なら隣り合う角度は45度**(`360/8` であって `360/7` ではない)。

    タスクDの検証4がこの目視確認を要求している —— 全周を `count-1` で割る実装だと
    最後の複製が最初と重なり、8個のつもりが7個に見える。
    """
    sketch_id = session.sketch_create(XY_PLANE)["sketch_id"]
    entity_id = session.add_circle(sketch_id, [20.0, 0.0], 2.0)["entity_id"]

    async def script(send):
        return await send(
            {
                "cmd": "pattern_circular",
                "sketch_id": sketch_id,
                "entities": [entity_id],
                "center": [0.0, 0.0],
                "count": 8,
                "total_angle": 360.0,
                "client_op": 8,
            }
        )

    (answer, _clients) = _browser(session, script)

    assert answer["applied"]["verb"] == "pattern_circular"
    drawn = _drawn(session, sketch_id)
    assert len(drawn) == 8
    angles = sorted(
        math.degrees(math.atan2(item["params"]["cy"], item["params"]["cx"])) % 360.0
        for item in drawn
    )
    steps = [round(b - a, 6) for a, b in zip(angles, angles[1:])]
    assert steps == [pytest.approx(45.0)] * 7


def test_a_feature_command_missing_an_argument_is_refused(session: Session) -> None:
    """引数が欠けたコマンドは**黙って既定値で埋めない**(`SKETCH_COMMANDS` と同じ規律)。"""
    body, _ = _box(session)
    _select(session, _edges(body, [1]))
    before = _fingerprint(session)

    async def script(send):
        return await send({"cmd": "fillet"})  # 半径が無い

    (answer, _clients) = _browser(session, script)

    assert answer["type"] == "error"
    assert _fingerprint(session) == before


def test_a_feature_verb_can_be_undone_from_the_browser(session: Session) -> None:
    body, _ = _box(session)
    faces_before = kernel.face_count(body.shape)
    _select(session, _edges(body, [1]))

    async def script(send):
        await send({"cmd": "fillet", "radius": RADIUS_THAT_WORKS, "client_op": 1})
        return await send({"cmd": "undo", "client_op": 2})

    (answer, _clients) = _browser(session, script)

    assert answer["applied"]["undone"] == "fillet"
    assert kernel.face_count(body.shape) == faces_before


def test_the_ui_and_the_ai_get_the_same_shape_from_the_same_verb() -> None:
    """**同じ操作をUIからとMCPからで行い、結果が一致する**(タスクDの検証6)。

    UIが叩くのは `FEATURE_COMMANDS` 経由の `Session.fillet`、AIが叩くのは
    `DocumentTools.fillet`。**同じ `Session` のメソッドに集まる**というのがこの企画の
    設計(§2「コアはUIが無くても完全」)であり、ここが分岐した瞬間に
    「画面では丸まったのにAIから見ると丸まっていない」が起きる。

    **両側とも確定選択の辺を取る**(2026-08-20)。どちらも添字を引数で渡す口を持たず、
    違いは選択の入れ方だけ —— 人はクリック(`modify_selection`)、AIは述語や参照である。
    """
    from_ui = Session(None, DEVIATION)
    from_ai = Session(None, DEVIATION)
    ui_body, _ = _box(from_ui)
    ai_body, _ = _box(from_ai)

    _select(from_ui, _edges(ui_body, (1, 3)))

    async def script(send):
        return await send({"cmd": "fillet", "radius": RADIUS_THAT_WORKS, "client_op": 1})

    _browser(from_ui, script)

    tools = DocumentTools(from_ai)
    _select(from_ai, _edges(ai_body, (1, 3)))
    tools.fillet(RADIUS_THAT_WORKS)

    assert kernel.face_count(ui_body.shape) == kernel.face_count(ai_body.shape)
    assert query.volume(ui_body.shape) == pytest.approx(query.volume(ai_body.shape), rel=1e-12)
    assert [entry.verb for entry in from_ui.document.log.entries] == [
        entry.verb for entry in from_ai.document.log.entries
    ]


def test_every_feature_command_names_a_method_that_exists(session: Session) -> None:
    """表とメソッドの食い違いを起動時ではなくここで捕まえる(`getattr` は実行時にしか落ちない)。"""
    for command, (method, keys) in FEATURE_COMMANDS.items():
        assert callable(getattr(session, method)), command
        assert keys, command


# --------------------------------------------------------------------------------------
# プレビュー(確定前の姿)
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize("verb", ["fillet", "chamfer", "extrude"])
def test_the_new_verbs_are_previewable(verb: str) -> None:
    """D で足した3つは投機的実行の対象(`pattern_*` は形を変えないので載せていない)。"""
    assert verb in PREVIEWABLE_VERBS


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
    sketch_id = session.sketch_create(XY_PLANE)["sketch_id"]
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
