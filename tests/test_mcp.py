"""M4(最小の先取り)契約: MCPサーバと「コアが発火し、アプリ層が購読する」上り方向。

**このタスクの意義**: AIが「前面パネルを隠して」と言えるようにする。見えない面は選べない
以上、可視性は選択機構の一部であり(`tests/test_visibility.py`)、それをAIが触れて初めて
大規模モデルでの交互操作が成立する。

ここで固定する契約は4つ:

1. **プロトコル** — `initialize` / `tools/list` / `tools/call` が MCP(JSON-RPC 2.0)として
   正しく喋れること。**依存を増やさず素で実装した**(`duocad/mcp_server.py` のdocstring)
   ので、プロトコルの正しさはここでしか担保されない。
2. **公開するツールは3つだけ** — `describe_document` / `set_visible` / `describe_shape`。
   **【2026-08-11 F1で失効】** この上限は退役した。F が §0 の到達表のAI列を埋めるので
   ツールは28個になる(顔ぶれの契約は `tests/test_mcp_verbs.py` にひとつだけ置く)。
   ここに残るのは**M4の3つが名前も振る舞いも変わらず生き残っていること**の回帰ガードである。
3. **`set_visible` はコアの verb を経由する** — 操作ログに載り、undoで戻る。
   アプリ層(ブラウザ)を一切通らない。
4. **上り方向** — MCPが文書を変えると `Session` が変更イベントを発火し、購読している
   WebSocketサーバが**接続している全クライアントへ配信する**。これが動かないと交互操作は
   原理的に成立しない(実装計画書§2「外してはいけない3つ」の2番目)。

**実ブラウザでの確認は `web/scripts/smoke-mcp.mjs`** が行う(27部品のPCケースを読み、
MCP経由で手前の部品を隠し、画面から消えて奥の面が選べるようになることを本物のEdgeで見る)。
pytest側は軽いモデル(`shelf_attachment_GD.step`)だけを使う。
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from duocad import kernel, query
from duocad.mcp_server import DocumentTools, McpServer, serve_stdio
from duocad.server import Server, Session

MODELS = Path(r"C:\Users\tm976\mf\3d-models")
SHELF = MODELS / "shelf_attachment_GD.step"

REPO_ROOT = Path(__file__).resolve().parent.parent

#: M4(最小の先取り)で公開した3つ。**2026-08-11のF1で「これで全部」は退役した** ——
#: F が §0 の到達表のAI列を埋めるので、ツールは28個になる(正は
#: `tests/test_mcp_verbs.py` の `EXPECTED_TOOLS`)。ここに残すのは
#: **M4の3つが名前も振る舞いも変わらずに生き残っていること**の回帰ガードであり、
#: 顔ぶれの上限ではない(上限の契約を2ファイルに置くと必ず食い違う)。
M4_TOOLS = {"describe_document", "set_visible", "describe_shape"}

#: 対応を約束するプロトコル版。**実装からimportしない**(2026-08-11のQA指摘) — importすると
#: 実装側から版を削っても順序を壊してもテストが追従して緑になり、契約にならない。
#: 実クライアントが送ってくる版がここに無ければ、その版のクライアントは繋がらない。
EXPECTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
LATEST_PROTOCOL_VERSION = EXPECTED_PROTOCOL_VERSIONS[0]

# JSON-RPC 2.0 の標準エラーコード(duocad.mcp_server と同じ値をテスト側にも書く。
# 実装から import すると「実装が変わればテストも変わる」ので契約にならない)
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INVALID_REQUEST = -32600


# --------------------------------------------------------------------------------------
# 道具
# --------------------------------------------------------------------------------------
@pytest.fixture()
def session() -> Session:
    """`shelf_attachment_GD.step` を1つ読み込んだセッション(参照1 + 編集用1)。

    **テストごとに作り直す**(2026-08-11のQA指摘)。使い回して `body.visible` を直接
    書き戻す方式だと、文書の状態と操作ログがずれた土台の上でundo系のテストが走り、
    「偶然緑」になりうる。読み込みは軽いモデルなので作り直しのほうが安い。
    """
    if not SHELF.exists():
        pytest.skip(f"参照STEPが無い: {SHELF}")
    return Session(SHELF, 0.2)


@pytest.fixture()
def tools(session: Session) -> DocumentTools:
    return DocumentTools(session)


@pytest.fixture()
def server(tools: DocumentTools) -> McpServer:
    return McpServer(tools)


def request(server: McpServer, method: str, params: dict | None = None, request_id: int = 1) -> dict:
    message = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    response = server.handle(message)
    assert response is not None, f"{method} はレスポンスを返さなければならない"
    return response


def call_tool(server: McpServer, name: str, arguments: dict | None = None) -> dict:
    """`tools/call` を叩いて、返ってきたJSONテキストを辞書で返す(isErrorなら例外)。"""
    response = request(server, "tools/call", {"name": name, "arguments": arguments or {}})
    assert "error" not in response, response
    result = response["result"]
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


def call_tool_expecting_error(server: McpServer, name: str, arguments: dict) -> str:
    """実行時エラー(`isError: true`)を期待して、そのテキストを返す。"""
    response = request(server, "tools/call", {"name": name, "arguments": arguments})
    assert "error" not in response, response
    result = response["result"]
    assert result["isError"] is True, f"エラーになるはずが成功した: {result}"
    return result["content"][0]["text"]


def editable_body(session: Session):
    return next(body for body in session.document.bodies if body.kind == "solid")


# --------------------------------------------------------------------------------------
# 1. プロトコル
# --------------------------------------------------------------------------------------
def test_initialize_echoes_a_supported_protocol_version(server: McpServer):
    """クライアントが要求した版に対応していれば、その版をそのまま返す。"""
    for version in EXPECTED_PROTOCOL_VERSIONS:
        result = request(server, "initialize", {"protocolVersion": version, "capabilities": {}})["result"]
        assert result["protocolVersion"] == version


def test_initialize_falls_back_when_the_version_is_unknown(server: McpServer):
    """知らない版を要求されたら**自分が対応する最新版**を返す(黙って合わせない)。"""
    result = request(server, "initialize", {"protocolVersion": "1999-01-01"})["result"]
    assert result["protocolVersion"] == LATEST_PROTOCOL_VERSION


def test_initialize_declares_tools_capability_and_server_info(server: McpServer):
    result = request(server, "initialize", {"protocolVersion": LATEST_PROTOCOL_VERSION})["result"]
    assert "tools" in result["capabilities"]
    assert result["serverInfo"]["name"] == "duocad"
    assert result["serverInfo"]["version"]


def test_notifications_get_no_response(server: McpServer):
    """通知(idなし)に返事を書いてはいけない。書くとプロトコル違反になる。"""
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert server.initialized is True
    # 知らない通知も黙って無視する(エラーを返さない)
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/cancelled"}) is None


def test_ping_answers_empty_result(server: McpServer):
    assert request(server, "ping")["result"] == {}


def test_unknown_method_is_method_not_found(server: McpServer):
    response = request(server, "resources/list")
    assert response["error"]["code"] == METHOD_NOT_FOUND


def test_bad_jsonrpc_version_is_invalid_request(server: McpServer):
    response = server.handle({"jsonrpc": "1.0", "id": 1, "method": "ping"})
    assert response["error"]["code"] == INVALID_REQUEST


def test_batch_requests_are_rejected(server: McpServer):
    """JSON-RPCのバッチはMCP 2025-06-18で廃止。受け取っても解釈しない。"""
    response = server.handle([{"jsonrpc": "2.0", "id": 1, "method": "ping"}])
    assert response["error"]["code"] == INVALID_REQUEST


def test_protocol_layer_never_raises(server: McpServer):
    """壊れたメッセージでプロトコル層が例外を投げない(サーバが黙って死ぬのと同じになるため)。

    JSON-RPCとして解釈できないもの(オブジェクトですらない)は**エラーを返す**。
    一方 `method` が無いメッセージは仕様上「こちらが出したリクエストへの応答」なので、
    **返事を書かない**(このサーバはリクエストを出さないので、来ても無視するのが正しい)。
    """
    for broken in ("文字列", 42, None, ["a"]):
        response = server.handle(broken)
        assert response is not None and "error" in response, f"{broken!r} を素通しした"

    assert server.handle({"jsonrpc": "2.0", "id": 1, "result": {}}) is None


# --------------------------------------------------------------------------------------
# 2. ツールの顔ぶれ(最小構成であること)
# --------------------------------------------------------------------------------------
def test_the_m4_tools_survive(server: McpServer):
    """M4の3つが**名前を変えずに**公開され続けていること。

    **2026-08-11のF1で「3つだけ」という上限は退役した**(F が到達表のAI列を埋めるため)。
    顔ぶれの上限を決める契約は `tests/test_mcp_verbs.py` にひとつだけ置く ——
    上限を2ファイルに書くと、片方を更新し忘れた瞬間に F2 が両方緑にできなくなる。
    """
    tools = request(server, "tools/list")["result"]["tools"]
    assert M4_TOOLS <= {tool["name"] for tool in tools}


def test_every_tool_has_a_description_and_an_object_schema(server: McpServer):
    """説明文と入力スキーマはAIがツールを選ぶ唯一の手がかり。欠けたら使えない。"""
    for tool in request(server, "tools/list")["result"]["tools"]:
        assert tool["description"].strip()
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False


def test_set_visible_schema_requires_both_arguments(server: McpServer):
    tool = next(t for t in request(server, "tools/list")["result"]["tools"] if t["name"] == "set_visible")
    assert set(tool["inputSchema"]["required"]) == {"body_id", "visible"}


def test_unknown_tool_is_invalid_params(server: McpServer):
    """**実在しえない名前**で見る(2026-08-11のF1で差し替え)。

    元は `propose_selection` を例にしていたが、F がそれを本物のツールとして公開するので、
    「未知のツール」の例としては使えなくなった(そのままだと F2 が緑にできない)。
    """
    response = request(server, "tools/call", {"name": "no_such_tool_ever", "arguments": {}})
    assert response["error"]["code"] == INVALID_PARAMS


def test_unknown_argument_is_rejected(server: McpServer):
    response = request(
        server, "tools/call", {"name": "describe_document", "arguments": {"depth": 3}}
    )
    assert response["error"]["code"] == INVALID_PARAMS


def test_missing_required_argument_is_rejected(server: McpServer):
    response = request(server, "tools/call", {"name": "set_visible", "arguments": {"body_id": "x"}})
    assert response["error"]["code"] == INVALID_PARAMS


def test_visible_must_be_a_real_boolean(server: McpServer, session: Session):
    """`"false"` のような文字列を通すと Python では真になり、**黙って逆の操作をする**。"""
    body = editable_body(session)
    before = len(session.document.log.entries)
    response = request(
        server, "tools/call", {"name": "set_visible", "arguments": {"body_id": body.id, "visible": "false"}}
    )
    assert response["error"]["code"] == INVALID_PARAMS
    assert body.visible is True
    assert len(session.document.log.entries) == before, "拒否したのにログが動いた"


# --------------------------------------------------------------------------------------
# 3. describe_document / describe_shape
# --------------------------------------------------------------------------------------
def test_describe_document_hides_reference_bodies_by_default(server: McpServer, session: Session):
    """STEPを読むと「参照」と「編集用コピー」が対で出来る。**AIに見せるのは編集用だけ**。

    両方見せると同じ部品が2つ並び、AIが隠しても画面が変わらない方(既に隠れている参照)を
    掴む。ただし隠した件数は必ず知らせる(黙って落とさない)。
    """
    payload = call_tool(server, "describe_document")
    kinds = {body["kind"] for body in payload["bodies"]}
    assert kinds == {"solid"}
    assert payload["body_count"] == len(payload["bodies"])
    assert payload["reference_count"] == sum(
        1 for body in session.document.bodies if body.kind == "reference"
    )
    assert payload["reference_included"] is False


def test_describe_document_can_include_references(server: McpServer, session: Session):
    payload = call_tool(server, "describe_document", {"include_references": True})
    assert len(payload["bodies"]) == len(session.document.bodies)
    assert payload["reference_included"] is True


def test_describe_document_lists_what_an_ai_needs_to_choose_a_body(server: McpServer):
    """id・名前・種別・可視性・面数(タスクファイル指定)＋体積。どれか欠けたら選べない。"""
    payload = call_tool(server, "describe_document")
    for body in payload["bodies"]:
        assert set(body) >= {"body_id", "name", "kind", "visible", "face_count", "volume"}
        assert body["face_count"] > 0
    assert payload["units"] == "mm"
    assert "parameters" in payload


def test_describe_document_bbox_is_opt_in(server: McpServer):
    """境界箱は既定で出さない(部品数ぶん幾何を測るため)。要るときだけ true。"""
    assert "bbox" not in call_tool(server, "describe_document")["bodies"][0]
    with_bbox = call_tool(server, "describe_document", {"include_bbox": True})
    box = with_bbox["bodies"][0]["bbox"]
    assert all(size > 0 for size in box["size"])
    assert len(box["min"]) == len(box["max"]) == len(box["center"]) == 3


def test_describe_shape_agrees_with_the_query_layer(server: McpServer, session: Session):
    """MCPは問い合わせ層の値をそのまま渡す(**独自に数え直さない**)。"""
    body = editable_body(session)
    payload = call_tool(server, "describe_shape", {"body_id": body.id})
    expected = query.describe_shape(body.shape)
    assert payload["face_count"] == expected["face_count"]
    assert payload["edge_count"] == expected["edge_count"]
    assert payload["faces_by_type"] == expected["faces_by_type"]
    assert payload["volume"] == pytest.approx(query.volume(body.shape))
    assert payload["bbox"]["diagonal"] > 0


def test_unknown_body_is_an_execution_error_not_a_protocol_error(server: McpServer):
    """ツールの実行時エラーは `isError` で返す — JSON-RPCエラーにするとAIに理由が届かない。"""
    text = call_tool_expecting_error(server, "describe_shape", {"body_id": "no-such-body"})
    assert "no such body" in text


# --------------------------------------------------------------------------------------
# 4. set_visible — コアの verb を経由し、ログに載り、undoで戻る
# --------------------------------------------------------------------------------------
def test_set_visible_goes_through_the_core_verb(server: McpServer, session: Session):
    """アプリ層を通らずコアを直接叩く(=コアはUIが無くても機能的に完全)。"""
    body = editable_body(session)
    before = len(session.document.log.entries)

    payload = call_tool(server, "set_visible", {"body_id": body.id, "visible": False})

    assert body.visible is False
    assert payload["visible"] is False and payload["previous"] is True and payload["changed"] is True
    entries = session.document.log.entries
    assert len(entries) == before + 1
    assert entries[-1].verb == "set_visible"
    assert entries[-1].args == {"body_id": body.id, "visible": False, "previous": True}


def test_set_visible_is_undoable(server: McpServer, session: Session):
    """操作ログに載る以上、undoで戻る(可視性は文書の状態であってUIの設定ではない)。"""
    body = editable_body(session)
    call_tool(server, "set_visible", {"body_id": body.id, "visible": False})
    assert body.visible is False

    session.undo()
    assert body.visible is True


def test_set_visible_refuses_reference_bodies(server: McpServer, session: Session):
    """参照ボディ(読み込んだSTEPの原本)の可視性は触らせない(2026-08-11のQA指摘)。

    表示に戻すと**同じソリッドが二重に描かれる** — M3.5が「1ソリッドにつき同時に見えるのは
    1つ」として塞いだ状態が、AI経路だけで復活してしまう。`include_references=True` は
    見せるためのもので、触ってよいという意味ではない。
    """
    reference = next(body for body in session.document.bodies if body.kind == "reference")
    before = len(session.document.log.entries)
    text = call_tool_expecting_error(
        server, "set_visible", {"body_id": reference.id, "visible": True}
    )
    assert "reference" in text
    assert reference.visible is False
    assert len(session.document.log.entries) == before


def test_can_undo_agrees_with_the_session(server: McpServer, session: Session):
    """`describe_document` の `can_undo` は**実際にundoが通るか**を答える。

    文書の土台(部品の読み込み・編集用ボディ)は取り消せないので、生の `log.can_undo`
    (=1件でもログがあれば真)を返すと、AIが「戻せる」と信じて呼び、拒否される。
    """
    assert session.document.log.can_undo is True, "土台のログは積まれている(前提)"
    assert call_tool(server, "describe_document")["can_undo"] is False
    with pytest.raises(ValueError):
        session.undo()

    call_tool(server, "set_visible", {"body_id": editable_body(session).id, "visible": False})
    assert call_tool(server, "describe_document")["can_undo"] is True
    session.undo()  # 今度は通る


def test_set_visible_on_a_missing_body_changes_nothing(server: McpServer, session: Session):
    before = len(session.document.log.entries)
    text = call_tool_expecting_error(server, "set_visible", {"body_id": "ghost", "visible": False})
    assert "no such body" in text
    assert len(session.document.log.entries) == before, "失敗したのにログが動いた"


# --------------------------------------------------------------------------------------
# 5. 上り方向 — コアが発火し、アプリ層が購読して描き直す
# --------------------------------------------------------------------------------------
def test_core_emits_one_event_per_mutation(session: Session):
    """`Session` の変更は購読者へ**1操作1回**届く。"""
    seen: list = []
    listener = seen.append
    session.add_listener(listener)
    body = editable_body(session)
    try:
        session.set_visible(body.id, False)
        session.undo()
    finally:
        session.remove_listener(listener)
    assert [event["verb"] for event in seen] == ["set_visible", "undo"]


def test_removing_a_listener_stops_the_events(session: Session):
    """購読はやめられる(文書は接続より長生きするので、外せないと古い購読者が残る)。"""
    seen: list = []
    session.add_listener(seen.append)
    session.remove_listener(seen.append)
    session.set_visible(editable_body(session).id, False)
    assert seen == []
    with pytest.raises(ValueError):
        session.remove_listener(seen.append)  # 二重に外したら黙って空振りさせない


def test_a_broken_listener_does_not_break_the_operation(session: Session):
    """購読者が落ちても操作は成功のまま(通知の失敗で確定した変更は巻き戻せない)。"""
    def explode(_applied):
        raise RuntimeError("購読者が落ちた")

    body = editable_body(session)
    session.add_listener(explode)
    try:
        session.set_visible(body.id, False)
        assert body.visible is False
        assert any("変更イベントの配信に失敗" in warning for warning in session.warnings)
    finally:
        session.remove_listener(explode)


def test_mcp_change_reaches_every_connected_websocket_client(session: Session):
    """**このマイルストーンの核心**: AIが可視性を変えると、開いているブラウザへ届く。

    WebSocketの接続を模した器を `Server` に登録し、**MCPのツール経由で**可視性を変えて、
    その接続に `state` が届き、対象Bodyが不可視になっていることを見る。
    ここが落ちたら「AIが操作しても人間の画面が変わらない」= 交互操作は成立しない。
    """
    body = editable_body(session)

    class FakeConnection:
        """`Server` が使うのは `send` だけ(配信の出口)。"""

        def __init__(self) -> None:
            self.payloads: list = []

        async def send(self, raw: str) -> None:
            self.payloads.append(json.loads(raw))

    async def scenario() -> tuple:
        server = Server(session)
        server.bind_loop(asyncio.get_running_loop())
        # **2本つなぐ**(2026-08-11のQA指摘: 1本だけだと「2本目に届かない」実装でも緑になる)
        browsers = [FakeConnection(), FakeConnection()]
        for browser in browsers:
            server.clients.add(browser)
            await server._push(browser, applied=None, only=browser)  # 接続直後の絵
        first = [len(browser.payloads) for browser in browsers]

        # ここからがAIの操作。**MCPのツールを通す**(Sessionを直接触らない)。
        mcp = McpServer(DocumentTools(session))
        request(mcp, "tools/call", {"name": "set_visible", "arguments": {"body_id": body.id, "visible": False}})
        # 配信はイベントループのタスクとして積まれるので、1周回して届かせる
        for _ in range(20):
            await asyncio.sleep(0)
            if all(len(b.payloads) > f for b, f in zip(browsers, first)):
                break
        return [browser.payloads for browser in browsers], first

    payloads_per_client, first = asyncio.run(scenario())

    for payloads, before in zip(payloads_per_client, first):
        assert len(payloads) > before, "MCPで文書を変えたのにWebSocketへ配信されていない"
        pushed = payloads[-1]
        assert pushed["type"] == "state"
        states = {b["body_id"]: b for b in pushed["state"]["bodies"]}
        assert states[body.id]["visible"] is False
        # 出し手のいない変更に `applied`(操作元への確認応答)を付けない
        assert "applied" not in pushed
        # 版が変わったBodyだけが `delta` に載る(全部送り直さない。M3.5の契約を壊さない)
        assert [b["body_id"] for b in pushed["delta"]["bodies"]] == [body.id]


def test_websocket_commands_are_not_broadcast_twice(session: Session):
    """WebSocket経由の操作は `_dispatch` が配信する。購読者側が二重に配ってはいけない。"""
    body = editable_body(session)

    class FakeConnection:
        def __init__(self) -> None:
            self.payloads: list = []

        async def send(self, raw: str) -> None:
            self.payloads.append(json.loads(raw))

    async def scenario() -> list:
        server = Server(session)
        server.bind_loop(asyncio.get_running_loop())
        browser = FakeConnection()
        server.clients.add(browser)
        await server._dispatch(
            browser, json.dumps({"cmd": "set_visible", "body_id": body.id, "visible": False})
        )
        for _ in range(10):
            await asyncio.sleep(0)
        return browser.payloads

    payloads = asyncio.run(scenario())

    states = [payload for payload in payloads if payload["type"] == "state"]
    assert len(states) == 1, f"1回の操作で {len(states)} 回配信された(二重配信)"
    assert states[0]["applied"]["verb"] == "set_visible"


# --------------------------------------------------------------------------------------
# 6. stdio トランスポート(実際にプロセスを起動して喋る)
# --------------------------------------------------------------------------------------
def test_stdio_transport_speaks_line_delimited_json_rpc():
    """`serve_stdio` が1行1メッセージで読み書きし、EOFで戻ること(プロセスは起こさない)。"""
    import io

    class Pipe(io.BytesIO):
        pass

    incoming = Pipe(
        b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}\n'
        b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
        b"\n"  # 空行は読み飛ばす
        b"{ not json\n"
        b'{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n'
    )
    outgoing = Pipe()

    class Tools(DocumentTools):
        def __init__(self) -> None:  # 文書を持たないダミー(tools/listしか叩かない)
            super().__init__(session=None)  # type: ignore[arg-type]

    asyncio.run(serve_stdio(McpServer(Tools()), reader=incoming, writer=outgoing))

    lines = [json.loads(line) for line in outgoing.getvalue().decode("utf-8").splitlines()]
    assert [line.get("id") for line in lines] == [1, None, 2], "通知に返事を書いた/壊れた行を飛ばした"
    assert lines[0]["result"]["serverInfo"]["name"] == "duocad"
    assert lines[1]["error"]["code"] == -32700  # 壊れたJSONはパースエラー
    assert M4_TOOLS <= {tool["name"] for tool in lines[2]["result"]["tools"]}


def test_the_process_starts_and_answers_over_real_stdio():
    """`python -m duocad.mcp_server` を**実プロセスとして起動**して喋れること。

    MCPクライアント(Claude Code / Claude Desktop)はこの経路でしか繋がらない。
    あわせて **stdoutにJSON-RPC以外の行が出ていないこと**を確かめる — 起動メッセージを
    1行stdoutへ出すだけで接続は失敗する(だからログはstderrへ出す設計にしてある)。
    """
    handshake = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "duocad.mcp_server",
            "--no-step",
            # 既定のポート(8765/8766)は実機のサーバが使っている可能性があるので避ける
            "--port",
            "18765",
            "--http-port",
            "18766",
            "--web-dist",
            str(REPO_ROOT / "web" / "does-not-exist"),
        ],
        input="\n".join(json.dumps(message) for message in handshake) + "\n",
        capture_output=True,
        text=True,
        # **両側でUTF-8に揃える**。既定だとWindowsのパイプはcp932で読み書きされ、
        # 日本語のログを1行stderrへ出しただけで親側が UnicodeDecodeError で落ちる
        # (実測: このテストを書いた2026-08-11に踏んだ)。MCPの中身がUTF-8なので、
        # 子プロセスの側も PYTHONIOENCODING で明示する。
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        cwd=str(REPO_ROOT),
        timeout=180,
    )
    assert completed.returncode == 0, completed.stderr

    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    responses = []
    for line in lines:
        try:
            responses.append(json.loads(line))
        except ValueError:  # noqa: PERF203
            pytest.fail(f"stdoutにJSON-RPC以外の行が出ている(接続が壊れる): {line!r}")
    assert [response["id"] for response in responses] == [1, 2]
    assert responses[0]["result"]["protocolVersion"] == "2025-06-18"
    assert M4_TOOLS <= {tool["name"] for tool in responses[1]["result"]["tools"]}
    # 静的配信が立てられなくてもMCPは動く(理由はstderrに残る)
    assert "warning" in completed.stderr


# --------------------------------------------------------------------------------------
# 7. 境界(既存の規律を壊していないこと)
# --------------------------------------------------------------------------------------
def test_mcp_server_does_not_touch_ocp_directly():
    """MCPサーバはOCPを直接importしない(境界は kernel.py だけ)。

    `tests/test_boundaries.py` が `duocad/` 全体を走査するので二重の検査だが、
    **新しく増えた層こそ境界を破りやすい**ので、ここでも名指しで固定する。
    """
    from test_boundaries import DUOCAD_DIR, _imported_module_roots

    roots = _imported_module_roots(DUOCAD_DIR / "mcp_server.py")
    assert "OCP" not in roots
    assert "build123d" not in roots


def test_bounding_box_does_not_depend_on_tessellation():
    """`kernel.bounding_box` は三角形分割の粗さで値が変わらない(幾何から求める)。

    `useTriangulation=True`(OCCTの既定)だと、`tessellate` を呼んだ後だけ違う値が返る
    という再現性の無い挙動になる。
    """
    if not SHELF.exists():
        pytest.skip(f"参照STEPが無い: {SHELF}")
    shape = kernel.read_step(str(SHELF))
    before = kernel.bounding_box(shape)
    kernel.tessellate(shape, 5.0)  # わざと粗く測る
    after = kernel.bounding_box(shape)
    assert before == after
