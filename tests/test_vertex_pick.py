"""頂点のピック/ハイライトの土台と、**人がverbを実行したら提案が確定する**規則。

ここが固定する契約は2つ:

1. **頂点の3D座標を引ける**(`kernel.vertex_positions` / `query.describe_vertices` /
   `Session.vertices` / WSの `get_vertices`)。添字は
   `kernel.count_subshapes(shape, "vertex")` の1始まりで、`selection.select_vertex` が
   範囲を検査する体系と同じ。**ずれると、AIが指した頂点と違う点が画面のマーカーに出る**
   (例外は出ない静かな事故)。
2. **対象集合を確定選択から取るverbがWebSocketへ届いたら、提案(`origin="proposed"`)は
   自動で確定する**(2026-08-23本人裁定「明示的な承認は不要」)。
   `SelectionPanel` の「承認する」ボタンを消した代わりであり、これが無いと
   **AIの提案に対して人が直接コマンドを実行した瞬間に `SelectionNotCommitted` で詰む**。
   宣言(`catalog.Command.uses_selection`)と実体(入口が確定選択を読むか)の一致も、ここが
   実際に呼んで見る —— verbを足した日に片方だけ直っても気づけるように。
"""
from __future__ import annotations

import inspect

import pytest

from duocad import catalog, kernel, selection
from duocad.document import Document
from duocad.entry import entries
from duocad.server import SELECTION_CONSUMING_COMMANDS, Session
from session_support import SIDE, box as _box, browser as _browser


# --------------------------------------------------------------------------------------
# 1. 頂点の座標(添字の体系)
# --------------------------------------------------------------------------------------
def test_vertex_positions_use_the_same_index_as_the_selection(session: Session) -> None:
    """`vertex_positions` の添字は `count_subshapes(shape, "vertex")` の体系と同じ。

    落ちたら、`selection.select_vertex` が通した添字で座標が引けない
    (= 選択に入った頂点を画面に出せない)。
    """
    body, _ = _box(session)
    described = kernel.vertex_positions(body.shape)

    assert len(described) == kernel.count_subshapes(body.shape, "vertex") == 8
    assert [item["index"] for item in described] == list(range(1, 9))
    for item in described:
        assert len(item["point"]) == 3
        # 20mm立方体なので、全頂点が 0 か 20(の丸め誤差内)に居る
        assert all(min(abs(value), abs(value - SIDE)) < 1e-6 for value in item["point"])


def test_an_empty_shape_has_no_vertex_map() -> None:
    """空の形状は ValueError(黙って0件を返すと「頂点が無いボディ」に見える)。"""
    with pytest.raises(ValueError):
        kernel.vertex_positions(None)


# --------------------------------------------------------------------------------------
# 2. 頂点を配る口(Session.vertices / get_vertices)
# --------------------------------------------------------------------------------------
def test_vertices_are_sent_only_to_the_connection_that_asked(session: Session) -> None:
    """`get_vertices` は要求した接続にだけ返る(`get_edges` と同じ扱い)。"""
    body, _ = _box(session)

    async def script(send):
        return await send({"cmd": "get_vertices", "body_id": body.id})

    (answer, clients) = _browser(session, script)

    assert answer["type"] == "vertices"
    assert answer["body_id"] == body.id
    assert len(answer["vertices"]) == 8
    assert clients[1].payloads == [], "頂点は全接続へ配らない(要求した接続だけ)"


def test_the_vertex_revision_follows_the_shape(session: Session) -> None:
    """形が変われば `revision` が上がる。**古い添字の座標を描き続けさせない**ための番号。"""
    body, _ = _box(session)
    before = session.vertices(body.id)
    session.clear_selection()
    session.modify_selection([{"body": body.id, "kind": "edge", "index": 1}], [])
    session.fillet(3.0)
    after = session.vertices(body.id)

    assert after["revision"] > before["revision"]
    assert len(after["vertices"]) != len(before["vertices"])


def test_the_vertices_of_a_hidden_body_are_refused(session: Session) -> None:
    """隠したBodyの頂点は配らない(`edges()` / `geometry()` と同じ規律)。

    配ると、隠したはずの部品の頂点マーカーだけが宙に浮いて見え、しかも指せる。
    """
    body, _ = _box(session)
    session.set_visible(body.id, False)

    with pytest.raises(ValueError):
        session.vertices(body.id)


def test_asking_for_the_vertices_of_an_unknown_body_is_an_error(session: Session) -> None:
    _box(session)

    async def script(send):
        return await send({"cmd": "get_vertices", "body_id": "body-does-not-exist"})

    (answer, _clients) = _browser(session, script)
    assert answer["type"] == "error"
    assert answer["where"] == "get_vertices"


# --------------------------------------------------------------------------------------
# 3. 人がverbを実行したら提案は確定する(「承認する」ボタンの代替)
# --------------------------------------------------------------------------------------
def _propose_face(session: Session, body) -> None:
    selection.propose_selection(
        session.document, refs=[{"body": body.id, "kind": "face", "index": 1}]
    )


def _state_origins(client) -> list:
    """その接続が受け取った状態配信の origin を届いた順に返す。"""
    return [
        payload["state"]["selection"]["origin"]
        for payload in client.payloads
        if payload.get("type") == "state"
    ]


def test_the_automatic_commit_reaches_every_client(session: Session) -> None:
    """**自動確定は必ず配信する** —— サーバとブラウザで origin が食い違わないこと。

    落ちると、サーバの選択は `user` なのに画面は `proposed` のまま残る。
    `SelectionPanel` を廃した今、origin を人へ伝えるのは3Dの色だけであり、AI側が
    `get_selection` で読む値でもあるので、**人が承認していないのに承認済みに見える**
    状態そのものになる。

    とくに `preview` で落ちやすい —— あれは「状態を配らない」コマンドなので、確定の配信を
    後続のverbの `_push` に相乗りさせると preview だけが漏れる。
    プレビューして Esc で降りると、その食い違いが**そのまま残る**。
    """
    body, _ = _box(session)
    _propose_face(session, body)

    async def script(send):
        return await send(
            {"cmd": "preview", "verb": "extrude", "args": {"distance": 5.0, "mode": "join"}}
        )

    (answer, clients) = _browser(session, script)

    assert answer["type"] == "preview", answer
    assert session.get_selection()["origin"] == "user"
    # **要求した接続にも、見ているだけの接続にも届く**(2本つないでいる理由)
    for number, client in enumerate(clients):
        assert _state_origins(client) == ["user"], (
            f"client{number} に確定が届いていない: "
            f"{[payload.get('type') for payload in client.payloads]}"
        )


def test_the_automatic_commit_is_not_counted_as_a_human_selection(session: Session) -> None:
    """自動確定の配信に **`client_op` を載せない**。

    載せるとアプリ層がこれを「この画面が出した選択変更」と数え(`selfSelections` ——
    即適用の引き金)、さらに `pendingOp` をverbの完了前に解く。どちらも
    **人が指していないのに文書が変わる**方向の事故なので、載せないことを固定する。
    """
    body, _ = _box(session)
    _propose_face(session, body)

    async def script(send):
        return await send({"cmd": "extrude", "distance": 5.0, "mode": "join", "client_op": 7})

    (answer, clients) = _browser(session, script)

    selections = [
        payload["applied"]
        for payload in clients[0].payloads
        if payload.get("type") == "state"
        and (payload.get("applied") or {}).get("change") == "selection"
    ]
    assert len(selections) == 1, selections
    assert selections[0]["client_op"] is None
    # verbのほうは今までどおり自分の `client_op` を持って返る
    assert answer["applied"]["verb"] == "extrude"
    assert answer["applied"]["client_op"] == 7


def test_selection_editing_and_visibility_do_not_commit(session: Session) -> None:
    """**可視性の切替・問い合わせでは確定しない**(コマンド行を通るverbだけが承認になる)。

    ここが緩むと、AIの提案が人の意図と無関係な操作で黙って確定へ変わる。
    undoをここで見ないのは、undoが `resets_selection` で選択そのものを空にするから
    (空集合の origin は常に "user" —— 確定したのではなく消えている)。
    """
    body, _ = _box(session)

    for command in (
        {"cmd": "set_visible", "body_id": body.id, "visible": True},
        {"cmd": "get_state"},
        {"cmd": "get_edges", "body_id": body.id},
        {"cmd": "get_vertices", "body_id": body.id},
    ):
        _propose_face(session, body)

        async def script(send, command=command):
            return await send(command)

        _browser(session, script)
        assert session.get_selection()["origin"] == "proposed", command


def test_a_sketch_on_a_named_plane_does_not_commit(session: Session) -> None:
    """`sketch_create` に `base_plane` が付いていたら確定しない。

    そのときは貼る先が引数で来ており、選択は使われない —— 使われない集合を承認扱いに
    すると、「基準平面を指しただけなのにAIの提案が確定していた」という静かな変化になる。
    """
    body, _ = _box(session)
    _propose_face(session, body)

    async def script(send):
        return await send({"cmd": "sketch_create", "base_plane": "XZ"})

    (answer, _clients) = _browser(session, script)

    assert answer["type"] == "state", answer
    assert session.get_selection()["origin"] == "proposed"


def test_an_offset_sketch_on_a_named_plane_does_not_commit(session: Session) -> None:
    """`sketch_create_offset` も同じ —— `base_plane` 付きなら選択は使わない(`sketch_create`
    と同じ理由。`Server._dispatch` のガードが2つのverb名を見ているかを固定する)。
    """
    body, _ = _box(session)
    _propose_face(session, body)

    async def script(send):
        return await send(
            {"cmd": "sketch_create_offset", "base_plane": "XZ", "distance": 5.0}
        )

    (answer, _clients) = _browser(session, script)

    assert answer["type"] == "state", answer
    assert session.get_selection()["origin"] == "proposed"


def test_an_offset_sketch_without_a_named_plane_commits_the_selected_face(
    session: Session,
) -> None:
    """`base_plane` を省いたら、`sketch_create` と同じく確定選択の面を読み、その提案は
    実行そのもので確定する。
    """
    body, _ = _box(session)
    _propose_face(session, body)

    async def script(send):
        return await send({"cmd": "sketch_create_offset", "distance": 5.0})

    (answer, _clients) = _browser(session, script)

    assert answer["type"] == "state", answer
    assert session.get_selection()["origin"] == "user"


def test_the_declaration_matches_which_verbs_read_the_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    """`catalog.Command.uses_selection` と、入口が実際に確定選択を読むverbが一致する。

    落ちたら、**新しく足したverbが自動確定の対象から漏れている**(AIの提案に対して
    人がそのverbを実行すると詰む)か、逆に選択を読まないverbが余計に確定させている。
    """
    reads: list = []
    original = selection.confirmed_items

    def recording(doc, kind=None):
        reads.append(kind)
        return original(doc, kind=kind)

    monkeypatch.setattr(selection, "confirmed_items", recording)

    def reads_selection(call) -> bool:
        reads.clear()
        try:
            call()
        except Exception:  # noqa: BLE001 — 見るのは「読んだか」だけ(空の文書なのでどれも途中で断られる)
            pass
        return bool(reads)

    def without_arguments(function):
        """必須の引数を全部 `None` で埋めて呼ぶ(選択を読む入口は、引数を見る前に読む)。"""
        required = [
            name
            for name, parameter in list(inspect.signature(function).parameters.items())[1:]
            if parameter.default is inspect.Parameter.empty
        ]
        return lambda: function(Document(), **dict.fromkeys(required))

    actual = {verb for verb, function in entries().items() if reads_selection(without_arguments(function))}
    assert actual == set(SELECTION_CONSUMING_COMMANDS), (
        f"宣言 {sorted(SELECTION_CONSUMING_COMMANDS)} と実体 {sorted(actual)} がずれている"
    )

    # 確定前の姿だけを計算する口(`preview_session`)も、確定と同じ選択を読む。
    session = Session(None, 0.2)
    for name in SELECTION_CONSUMING_COMMANDS:
        preview = catalog.BY_NAME[name].preview_session
        if preview is not None:
            assert reads_selection(getattr(session, preview)), f"{preview} が確定選択を読んでいない"
