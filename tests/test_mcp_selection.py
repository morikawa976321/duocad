"""F1契約: **選択と述語をMCPへ出す**(2026-08-11のタスク・実装は F2)。

`tests/test_mcp_verbs.py` が操作(verb)の側を持ち、こちらが**交互操作の中心**——
AIが選択を提案し、人が過不足を直し、AIが読み直して適用する——の側を持つ。
契約の散文は `tests/README.md` の「F」節が正。

A1/A2 で `duocad/selection.py` と `duocad/predicates.py` に入ったコアAPIを、そのまま
MCPツールとして出す。**ここに新しい選択のロジックを書かない** —— MCPは
コアのもう一人のクライアントであり、ブラウザと同じ `Document.selection` を触る。

## このファイルが固定する5つ

1. **選択5ツールが届く** — `get_selection` / `propose_selection`(`refs=` と `predicate=` の
   両方)/ `modify_selection` / `commit_selection` / `clear_selection`。
   **戻り値の形はMCP層で `{"selection": {...}}` に統一する**(下記)。
2. **述語の語彙をAIが自己記述で知れる** — `describe_predicates` が
   `predicates.describe_ops()` を**そのまま**返す。AIが使える `op` を推測しなくてよい状態にする。
3. **0件と不正は別物**(A1の決定6) — 0件は成功して `count: 0`、不正な述語は
   `invalid_predicate` で**有効なopの一覧つき**。AIが自力で直せることが条件。
4. **提案のままでは verb に渡せない**(A1の決定4) — MCP経由でも同じ。
   ただし `commit_selection` を明示すれば通る(禁止ではなく「黙って通さない」)。
5. **AI起点の選択変更が画面へ届く** — イベントとして発火し、かつ `state_meta()["selection"]`
   に載る(A2で確認済みの経路にMCPも乗ること)。

## なぜMCP層で戻り値の形を統一するのか

`Session` 側は**変更する4メソッドが発火したイベントそのもの**
(`{"change","selection","client_op"}`)を返し、`get_selection` だけが
`Selection.to_dict()`(`{"items","origin","count"}`)を返す(A1の決定8)。
この差はコアの都合(イベントの形を1箇所で作る)であって、**AIに見せる理由が無い**。
MCPは5つとも `{"selection": {"items","origin","count"}, ...}` に揃える ——
揃えないと、AIは `["selection"]` を挟むかどうかをツールごとに覚えることになる。

**このタスク(F1)は契約だけを書き、実装は F2 が入れる。赤で終わるのが正常。**
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

from duocad import kernel, predicates, query
from duocad.document import Body
from duocad.mcp_server import DocumentTools, McpServer
from duocad.server import Server, Session

BOX_X, BOX_Y, BOX_Z = 30.0, 30.0, 40.0

#: A1と同じ述語(§5の例 `edges where parallel_to Z and length > 20`)。
#: 30×30×40 の直方体では**Zに平行で長さ40の辺がちょうど4本**一致する。
VERTICAL_EDGES = {
    "kind": "edge",
    "where": [{"op": "parallel_to", "axis": "Z"}, {"op": "length_gt", "value": 20}],
}

#: 一致0件になる述語(**エラーではない**)。この直方体に長さ1000mmの辺は無い。
NO_MATCH = {"kind": "edge", "where": [{"op": "length_gt", "value": 1000.0}]}


# --------------------------------------------------------------------------------------
# 道具
# --------------------------------------------------------------------------------------
@pytest.fixture()
def session(tmp_path: Path) -> Session:
    """30×30×40 の直方体を1つ持つセッション(面6・辺12・頂点8)。"""
    made = Session(None, 0.2)
    made.document.add_body(
        Body(id="box", name="box", kind="solid", shape=BRepPrimAPI_MakeBox(BOX_X, BOX_Y, BOX_Z).Shape())
    )
    return made


@pytest.fixture()
def server(session: Session) -> McpServer:
    return McpServer(DocumentTools(session))


def request(server: McpServer, method: str, params: dict | None = None) -> dict:
    message: dict = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        message["params"] = params
    response = server.handle(message)
    assert response is not None
    return response


def call(server: McpServer, name: str, arguments: dict | None = None) -> dict:
    response = request(server, "tools/call", {"name": name, "arguments": arguments or {}})
    assert "error" not in response, response
    result = response["result"]
    assert result["isError"] is False, result["content"][0]["text"]
    return json.loads(result["content"][0]["text"])


def call_error(server: McpServer, name: str, arguments: dict | None = None) -> dict:
    response = request(server, "tools/call", {"name": name, "arguments": arguments or {}})
    assert "error" not in response, f"実行時エラーをプロトコルエラーにしている: {response}"
    result = response["result"]
    assert result["isError"] is True, f"エラーになるはずが成功した: {result}"
    text = result["content"][0]["text"]
    try:
        payload = json.loads(text)
    except ValueError:
        pytest.fail(f"失敗が機械可読でない(例外のテキストを素で返している): {text!r}")
    return payload["error"]


def selection_of(server: McpServer) -> dict:
    return call(server, "get_selection")["selection"]


def box_of(session: Session) -> Body:
    return session.document.bodies[0]


def edge_ref(body_id: str, index: int) -> dict:
    return {"body": body_id, "kind": "edge", "index": index}


def top_face_index(shape) -> int:
    for face in query.describe_faces(shape):
        if face["normal"][2] > 0.9:
            return face["index"]
    raise AssertionError("上向きの面が無い(テストの前提が崩れている)")


# --------------------------------------------------------------------------------------
# 1. 選択5ツールが届き、戻り値の形が揃っている
# --------------------------------------------------------------------------------------
def _assert_selection_payload(payload: dict) -> dict:
    """選択ツールの戻り値の形(モジュールdocstring「なぜ統一するのか」)。"""
    selection = payload["selection"]
    assert set(selection) == {"items", "origin", "count"}
    assert selection["count"] == len(selection["items"])
    assert selection["origin"] in {"user", "proposed"}
    return selection


def test_all_five_selection_tools_return_the_same_shape(server: McpServer, session: Session):
    """5つとも `{"selection": {...}}` を返す(`Session` 側の2つの形を外へ漏らさない)。"""
    box = box_of(session)
    _assert_selection_payload(call(server, "get_selection"))
    _assert_selection_payload(call(server, "propose_selection", {"refs": [edge_ref(box.id, 1)]}))
    _assert_selection_payload(call(server, "modify_selection", {"add": [edge_ref(box.id, 2)]}))
    _assert_selection_payload(call(server, "commit_selection"))
    _assert_selection_payload(call(server, "clear_selection"))


def test_propose_selection_accepts_explicit_references(server: McpServer, session: Session):
    """`refs=` の経路(§0の「明示的な参照」)。**提案**として入る。"""
    box = box_of(session)
    selection = _assert_selection_payload(
        call(server, "propose_selection", {"refs": [edge_ref(box.id, 1), edge_ref(box.id, 2)]})
    )
    assert selection["count"] == 2
    assert selection["origin"] == "proposed"
    assert [item["index"] for item in selection["items"]] == [1, 2]
    assert all(item["kind"] == "edge" for item in selection["items"])


def test_propose_selection_accepts_a_predicate(server: McpServer, session: Session):
    """`predicate=` の経路(§0の「述語」)。タスクファイル項番3の片割れ。"""
    selection = _assert_selection_payload(
        call(server, "propose_selection", {"predicate": VERTICAL_EDGES})
    )
    assert selection["count"] == 4, "Zに平行で長さ40の辺は4本(直方体)"
    assert selection["origin"] == "proposed"


def test_propose_selection_needs_exactly_one_of_them(server: McpServer, session: Session):
    """`refs` と `predicate` の**両方**も**どちらも無し**も拒む(コアの契約と同じ)。

    両方通すと「どちらが効いたのか」がAIから見て分からない集合ができる。
    """
    box = box_of(session)
    both = call_error(
        server, "propose_selection", {"refs": [edge_ref(box.id, 1)], "predicate": VERTICAL_EDGES}
    )
    assert both["code"] == "invalid_argument"
    neither = call_error(server, "propose_selection", {})
    assert neither["code"] == "invalid_argument"


def test_modify_selection_edits_the_difference(server: McpServer, session: Session):
    """差分編集(作り直しではない)。**人が触ること自体が承認**なので `origin` は user になる。"""
    box = box_of(session)
    call(server, "propose_selection", {"refs": [edge_ref(box.id, 1), edge_ref(box.id, 2)]})
    selection = _assert_selection_payload(
        call(
            server,
            "modify_selection",
            {"add": [edge_ref(box.id, 3)], "remove": [edge_ref(box.id, 1)]},
        )
    )
    assert [item["index"] for item in selection["items"]] == [2, 3]
    assert selection["origin"] == "user"


def test_clear_selection_empties_it(server: McpServer, session: Session):
    box = box_of(session)
    call(server, "propose_selection", {"refs": [edge_ref(box.id, 1)]})
    selection = _assert_selection_payload(call(server, "clear_selection"))
    assert selection["count"] == 0 and selection["origin"] == "user"


def test_changing_the_selection_through_mcp_does_not_touch_the_log(server: McpServer, session: Session):
    """選択の変更は**操作ログに積まない**(A1の決定7)。MCP経由でも同じ。

    積むと、選択し直すたびに undo を1手消費する編集になる(使い物にならない)。
    """
    box = box_of(session)
    before = len(session.document.log.entries)
    call(server, "propose_selection", {"predicate": VERTICAL_EDGES})
    call(server, "modify_selection", {"remove": [edge_ref(box.id, 1)]})
    call(server, "commit_selection")
    call(server, "clear_selection")
    assert len(session.document.log.entries) == before


# --------------------------------------------------------------------------------------
# 2. 述語の語彙をAIが自己記述で知れる(タスクファイルの「述語の語彙を〜推測しなくてよい状態に」)
# --------------------------------------------------------------------------------------
def test_describe_predicates_is_the_core_vocabulary_itself(server: McpServer):
    """`predicates.describe_ops()` を**そのまま**出す(二重管理にしない)。

    `describe_ops` の docstring が「F1で書き直さずに済むように、opの定義はここ1箇所を
    正にする」と宣言している。MCP側で書き直すと、**片方だけ増えたopが黙って通る**
    (スキーマにはあるがバリデータには無い、またはその逆)。
    """
    payload = call(server, "describe_predicates")
    assert payload["ops"] == predicates.describe_ops()


def test_describe_predicates_tells_the_ai_how_to_write_one(server: McpServer):
    """opの一覧だけでは述語を組み立てられない —— 形・種別・ANDの規則まで出す。

    `where` がANDであること・**ORが無い**ことをAIが知らないと、無いものを試して失敗する
    (A1の決定5: ORが要るときは2回評価して `modify_selection(add=...)` で足す)。
    """
    payload = call(server, "describe_predicates")
    assert set(payload["kinds"]) == set(predicates.PREDICATE_KINDS)
    assert payload["where_is"] == "and"
    assert isinstance(payload["example"], dict)
    # 出した例がそのまま通ること(**例が古くなって嘘になるのを防ぐ**)
    assert predicates.validate(payload["example"])


def test_describe_predicates_changes_nothing(server: McpServer, session: Session):
    """自己記述は読み取り(選択もログも動かさない)。"""
    before = len(session.document.log.entries)
    call(server, "describe_predicates")
    assert selection_of(server)["count"] == 0
    assert len(session.document.log.entries) == before


# --------------------------------------------------------------------------------------
# 3. 0件と不正は別物(A1の決定6・タスクファイル項番6/7)
# --------------------------------------------------------------------------------------
def test_zero_matches_is_a_success_that_says_zero(server: McpServer):
    """**一致0件はエラーではない**。0件だと分かる形で返る(項番7)。

    ここをエラーにすると、AIは「述語が壊れている」と読んで書き方をいじり始める。
    実際に直すべきは条件の値である。逆に黙って古い選択を残すと、人が承認したつもりで
    **別の集合に verb がかかる**。
    """
    selection = _assert_selection_payload(call(server, "propose_selection", {"predicate": NO_MATCH}))
    assert selection["count"] == 0
    assert selection["items"] == []


def test_zero_matches_still_replaces_the_previous_selection(server: McpServer, session: Session):
    """0件でも**置き換えは起きる**(A1の `propose_selection` の契約)。"""
    box = box_of(session)
    call(server, "propose_selection", {"refs": [edge_ref(box.id, 1)]})
    call(server, "propose_selection", {"predicate": NO_MATCH})
    assert selection_of(server)["count"] == 0


def test_an_unknown_op_comes_back_with_the_valid_ones(server: McpServer):
    """**AIが自力で直せるエラー**(項番6)。未知の `op` に有効なopの一覧を添える。

    一覧を添えないと、AIは `describe_predicates` を知らないかぎり総当たりに入る。
    添える一覧は `describe_ops()` そのもの —— ここで書き下すと二重管理になる。
    """
    error = call_error(
        server,
        "propose_selection",
        {"predicate": {"kind": "edge", "where": [{"op": "longer_than", "value": 20}]}},
    )
    assert error["code"] == "invalid_predicate"
    assert error["valid_ops"] == predicates.describe_ops()
    assert set(error["valid_kinds"]) == set(predicates.PREDICATE_KINDS)


def test_a_type_error_in_a_predicate_is_also_self_repairable(server: McpServer):
    """型違い(`value` に文字列)も同じ扱い。`op` の綴りだけが対象ではない。"""
    error = call_error(
        server,
        "propose_selection",
        {"predicate": {"kind": "edge", "where": [{"op": "length_gt", "value": "20"}]}},
    )
    assert error["code"] == "invalid_predicate"
    assert error["valid_ops"] == predicates.describe_ops()


def test_an_unknown_predicate_kind_is_reported_the_same_way(server: McpServer):
    """`kind` の間違い(領域を述語で選ぼうとする等)も同じ経路で直せる。"""
    error = call_error(
        server, "propose_selection", {"predicate": {"kind": "region", "where": []}}
    )
    assert error["code"] == "invalid_predicate"
    assert set(error["valid_kinds"]) == set(predicates.PREDICATE_KINDS)


def test_an_invalid_predicate_leaves_the_selection_untouched(server: McpServer, session: Session):
    """不正な述語で**選択を変えない**(A1の決定6)。MCP経由でも同じ。

    黙って空集合にすると「0件だった」と区別が付かなくなる。
    """
    box = box_of(session)
    call(server, "propose_selection", {"refs": [edge_ref(box.id, 1)]})
    call(server, "commit_selection")
    before = selection_of(server)

    call_error(server, "propose_selection", {"predicate": {"kind": "edge", "where": [{"op": "nope"}]}})

    assert selection_of(server) == before


def test_a_malformed_reference_is_refused_without_touching_the_selection(
    server: McpServer, session: Session
):
    """**参照**が不正な場合は述語とは別のコード(直し方が違う)。

    例外の種類はコアの `select_*` と揃っている —— 形が不正なら ValueError、
    存在しないBodyなら KeyError。MCPはそれを `invalid_argument` / `not_found` に写す。
    """
    box = box_of(session)
    call(server, "propose_selection", {"refs": [edge_ref(box.id, 1)]})
    before = selection_of(server)

    shapeless = call_error(server, "propose_selection", {"refs": [{"kind": "edge", "index": 1}]})
    assert shapeless["code"] == "invalid_argument"
    ghost = call_error(server, "propose_selection", {"refs": [edge_ref("ghost", 1)]})
    assert ghost["code"] == "not_found"
    out_of_range = call_error(server, "modify_selection", {"add": [edge_ref(box.id, 999)]})
    assert out_of_range["code"] == "invalid_argument"

    assert selection_of(server) == before


def test_a_hidden_body_is_not_scanned_and_saying_so_is_an_error(server: McpServer, session: Session):
    """不可視Bodyは走査しない(A1の項番18)。**明示して指したら**エラーになる。

    「指定したのに0件」と「見えないので対象外」はAIから区別が付かないため。
    """
    box = box_of(session)
    session.set_visible(box.id, False)
    assert call(server, "propose_selection", {"predicate": VERTICAL_EDGES})["selection"]["count"] == 0
    error = call_error(
        server, "propose_selection", {"predicate": {**VERTICAL_EDGES, "body": box.id}}
    )
    assert error["code"] == "invalid_argument"


# --------------------------------------------------------------------------------------
# 4. 提案のままでは verb に渡せない(A1の決定4・タスクファイル項番8)
# --------------------------------------------------------------------------------------
def test_a_proposal_cannot_be_fed_to_a_verb_through_mcp(server: McpServer, session: Session):
    """**黙って通さない**。MCP経由でも同じ挙動であること(項番8)。

    このコードが `invalid_argument` に潰れていると、AIは半径を変えて再試行しはじめる
    (直すべきは半径ではなく「確定していないこと」である)。`hint` に次の一手を書く。
    """
    box = box_of(session)
    call(server, "propose_selection", {"predicate": VERTICAL_EDGES})
    shape_before = box.shape

    error = call_error(server, "fillet", {"radius": 2.0})

    assert error["code"] == "selection_not_committed"
    assert "commit_selection" in error["hint"]
    assert error["selection"]["origin"] == "proposed"
    assert box.shape is shape_before, "拒否したのに形が変わった"
    assert session.document.log.entries == []


#: **形を選択から取る全ツール**と、それぞれが提案のまま呼ばれたときの引数。
#: 1つでも漏れると、そのツールだけ提案と確定の区別が素通りになる(F の決定1は
#: 「全verbへ自動的にかかる」と主張しているので、**全部を名指しで検査する**)。
SELECTION_CONSUMING_TOOLS = [
    ("fillet", {"radius": 2.0}, "edge"),
    ("chamfer", {"distance": 2.0}, "edge"),
    ("extrude_face", {"distance": 5.0}, "face"),
    ("sketch_create", {}, "face"),
]


@pytest.mark.parametrize("tool,arguments,kind", SELECTION_CONSUMING_TOOLS)
def test_no_selection_consuming_tool_accepts_a_proposal(
    server: McpServer, session: Session, tool: str, arguments: dict, kind: str
):
    """**`fillet` だけの話ではない。** 形を選択から取るツールは全部この門を通る。

    1つでも `get_selection()["items"]` を素で読む実装があると、そのツールだけ
    A1の決定4(提案のままでは verb に渡せない)が効かなくなる —— しかも
    `fillet` のテストは緑のままなので気づけない。
    """
    box = box_of(session)
    predicate = VERTICAL_EDGES if kind == "edge" else {
        "kind": "face",
        "where": [{"op": "normal_dot_gt", "axis": "Z", "value": 0.9}],
    }
    call(server, "propose_selection", {"predicate": predicate})
    shape_before = box.shape

    error = call_error(server, tool, arguments)

    assert error["code"] == "selection_not_committed", f"{tool} が提案を素通しした"
    assert "commit_selection" in error["hint"]
    assert box.shape is shape_before
    assert session.document.log.entries == []


def test_extrude_also_refuses_a_proposed_region(server: McpServer, tmp_path: Path):
    """領域を食う `extrude` にも同じ門がかかる(形が `ShapeRef` ではない側の対照)。"""
    from duocad.sketch import add_rectangle, sketch_create

    empty = Session(None, 0.2)
    sketch_id = sketch_create(empty.document, "XY")
    add_rectangle(empty.document, sketch_id, (0.0, 0.0), 30.0, 30.0)
    mcp = McpServer(DocumentTools(empty))
    call(mcp, "propose_selection", {"refs": [{"sketch": sketch_id, "index": 1}]})

    error = call_error(mcp, "extrude", {"distance": 10.0})

    assert error["code"] == "selection_not_committed"
    assert "commit_selection" in error["hint"]
    assert empty.document.bodies == [], "拒否したのにボディができた"


def _open_the_core_gate(monkeypatch) -> None:
    """`selection.confirmed_items` を `origin` を見ない版へ差し替える(絞り込みは同じ)。

    これで通るかどうかが「門がコアに1つだけか」の判定になる(下の2本が使う)。
    """
    from duocad import selection as selection_module

    def permissive(doc, kind=None):
        items = list(selection_module.get_selection(doc).items)
        if kind is None:
            return items
        return [item for item in items if selection_module.ref_kind(item) == kind]

    monkeypatch.setattr(selection_module, "confirmed_items", permissive)


@pytest.mark.parametrize("tool,arguments,kind", SELECTION_CONSUMING_TOOLS)
def test_the_gate_lives_in_the_core_not_in_the_mcp_layer(
    server: McpServer, session: Session, monkeypatch, tool: str, arguments: dict, kind: str
):
    """**MCPは自分では弾かない**(Fの決定3)。拒否するのはコアの `confirmed_items` だけ。

    コア側の門を一時的に外すと、MCPは**そのまま通す**はずである ——
    通らなければ、MCP層にもう1つ門があるということで、同じ規則が2箇所に生まれている。
    片方だけ緩んだときに誰も気づかない状態を、ここで作らせない。

    `test_no_selection_consuming_tool_accepts_a_proposal` だけでは**この向きは検査できない**
    (MCPが先に弾いても観察結果は同じ)ので、対で必要になる。**対の両方を同じ
    `SELECTION_CONSUMING_TOOLS` で回す** —— 片方だけ全ツール、片方は `fillet` だけ、という
    非対称にすると、残りのツールは「MCPが先読みして弾き、確定後は `get_selection()` を
    素読み」でも全部緑になる(QA2回目の指摘)。
    """
    _open_the_core_gate(monkeypatch)

    box = box_of(session)
    predicate = VERTICAL_EDGES if kind == "edge" else {
        "kind": "face",
        "where": [{"op": "normal_dot_gt", "axis": "Z", "value": 0.9}],
    }
    log_before = len(session.document.log.entries)
    call(server, "propose_selection", {"predicate": predicate})  # commit しない

    payload = call(server, tool, arguments)

    assert payload["verb"] == tool, f"{tool} がMCP層で弾いている(門が2箇所ある)"
    assert len(session.document.log.entries) == log_before + 1
    assert box is box_of(session)


def test_the_core_gate_is_the_only_one_for_regions_too(tmp_path: Path, monkeypatch):
    """領域を食う `extrude` にも同じ向きの検査をかける(`ShapeRef` ではない側の対照)。"""
    from duocad.sketch import add_rectangle, sketch_create

    _open_the_core_gate(monkeypatch)

    empty = Session(None, 0.2)
    sketch_id = sketch_create(empty.document, "XY")
    add_rectangle(empty.document, sketch_id, (0.0, 0.0), 30.0, 30.0)
    mcp = McpServer(DocumentTools(empty))
    call(mcp, "propose_selection", {"refs": [{"sketch": sketch_id, "index": 1}]})

    payload = call(mcp, "extrude", {"distance": 10.0})

    assert payload["verb"] == "extrude", "extrude がMCP層で弾いている(門が2箇所ある)"
    assert len(empty.document.bodies) == 1


@pytest.mark.parametrize("tool,arguments", [("fillet", {"radius": 2.0}), ("chamfer", {"distance": 2.0})])
def test_a_verb_targets_the_body_the_selection_names(tmp_path: Path, tool: str, arguments: dict):
    """**選択が指しているBodyに当たること**(2026-08-12のF2のQAで見つかった穴)。

    辺の参照は `body` と `index` の両方を持つ。MCPが `index` だけを取り出して
    `body_id` を渡さないと、コアのverbは**アクティブなBody**を対象にする ——
    27部品のPCケースでは「同じ添字の別部品の辺」を黙って丸める。例外も出ない。

    F1の契約は1Bodyの直方体でしか検査しておらず、**この穴を素通しした**
    (`test_the_whole_interleaved_lap_runs_through_mcp_alone` は緑のままだった)。
    `extrude_face` は面の参照から `body` を渡していて、辺の側だけが抜けていた。
    """
    session = Session(None, 0.2)
    # **選択する側を先に足す** —— `add_body` は足したBodyをアクティブにするので、
    # 後から足した `other` がアクティブになる。アクティブでないほうを狙うのが試験の要点。
    session.document.add_body(
        Body(id="target", name="target", kind="solid", shape=BRepPrimAPI_MakeBox(BOX_X, BOX_Y, BOX_Z).Shape())
    )
    session.document.add_body(
        Body(id="other", name="other", kind="solid", shape=BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape())
    )
    server = McpServer(DocumentTools(session))
    target, other = session.document.bodies
    assert session.document.active_body().id == "other", "前提: 狙う側はアクティブではない"
    target_faces = kernel.face_count(target.shape)
    other_faces = kernel.face_count(other.shape)

    call(server, "propose_selection", {"predicate": {**VERTICAL_EDGES, "body": "target"}})
    call(server, "commit_selection")
    payload = call(server, tool, arguments)

    assert payload["changed_bodies"] == ["target"], "選択と違うBodyに当たっている"
    assert kernel.face_count(target.shape) > target_faces
    assert kernel.face_count(other.shape) == other_faces, "無関係なBodyが変わった"


def test_the_ai_can_execute_its_own_proposal_after_committing(server: McpServer, session: Session):
    """**禁止ではない** —— `commit_selection` を明示すれば自分の提案を実行できる。

    人が居ない場面(AI単独でモデルを作る)で提案→実行が塞がると、§0の到達表が埋まらない。
    明示の1手を踏ませることで、「提案のまま適用された操作」が後から区別できなくなる
    事故だけを防いでいる。
    """
    box = box_of(session)
    faces_before = kernel.face_count(box.shape)
    call(server, "propose_selection", {"predicate": VERTICAL_EDGES})
    call(server, "commit_selection")

    payload = call(server, "fillet", {"radius": 2.0})

    assert payload["verb"] == "fillet"
    assert kernel.face_count(box.shape) > faces_before


def test_measure_works_on_a_proposal(server: McpServer):
    """**測るのは verb ではない**ので、提案のままでも通る。

    AIが自分の提案を確かめてから確定する——という往復がここで閉じる。
    ここを `confirmed_items` 経由にすると、AIは中身を見ずに commit する動機ができる
    (提案と確定の区別を実際に弱める)。
    """
    call(server, "propose_selection", {"predicate": VERTICAL_EDGES})
    payload = call(server, "measure")
    assert payload["origin"] == "proposed"
    assert payload["count"] == 4


# --------------------------------------------------------------------------------------
# 5. measure(selection) — §5の問い合わせ
# --------------------------------------------------------------------------------------
def test_measure_reports_lengths_for_edges(server: McpServer, session: Session):
    """辺は長さと端点(§5「面積・長さ・法線・中心」の長さ側)。

    値は**問い合わせ層のもの**をそのまま渡す(MCPが測り直さない)。測り直すと、
    述語が使う `describe_edges` と別の値になりうる。
    """
    box = box_of(session)
    call(server, "propose_selection", {"predicate": VERTICAL_EDGES})
    payload = call(server, "measure")

    lengths = {edge["index"]: edge["length"] for edge in query.describe_edges(box.shape)}
    for item in payload["items"]:
        assert item["kind"] == "edge"
        assert item["length"] == lengths[item["index"]]
        assert len(item["start"]) == len(item["end"]) == 3
    assert payload["total_length"] == pytest.approx(BOX_Z * 4)


def test_measure_reports_area_normal_and_center_for_faces(server: McpServer, session: Session):
    """面は面積・法線・中心(§5)。"""
    box = box_of(session)
    index = top_face_index(box.shape)
    call(server, "propose_selection", {"refs": [{"body": box.id, "kind": "face", "index": index}]})

    item = call(server, "measure")["items"][0]

    assert item["kind"] == "face"
    assert item["area"] == pytest.approx(BOX_X * BOX_Y)
    assert item["normal"][2] > 0.9
    assert len(item["center"]) == 3


def test_measure_of_an_empty_selection_is_not_an_error(server: McpServer):
    """0件は測れないが**失敗ではない**(述語の0件と同じ扱い)。"""
    payload = call(server, "measure")
    assert payload["count"] == 0 and payload["items"] == []
    assert payload["total_length"] == 0 and payload["total_area"] == 0


def test_measure_reports_the_area_of_a_region(tmp_path: Path):
    """領域も測れる(§5の面積側。`region.regions` が既に面積を持っている)。

    領域を黙って落とす実装だと、AIは押し出す前に「この領域で合っているか」を確かめられない
    —— 領域は**描くものではなく計算されるもの**(§3.3)なので、確かめる手段が要る。
    """
    from duocad.sketch import add_circle, sketch_create

    empty = Session(None, 0.2)
    sketch_id = sketch_create(empty.document, "XY")
    add_circle(empty.document, sketch_id, (0.0, 0.0), 10.0)
    mcp = McpServer(DocumentTools(empty))
    call(mcp, "propose_selection", {"refs": [{"sketch": sketch_id, "index": 1}]})

    item = call(mcp, "measure")["items"][0]

    assert item["kind"] == "region"
    assert item["sketch"] == sketch_id
    assert item["area"] == pytest.approx(math.pi * 100.0, rel=1e-3)


def test_measure_admits_what_it_cannot_measure(server: McpServer, session: Session):
    """頂点は**位置を返す問い合わせ層の関数がまだ無い**ので、測れないと明示して返す。

    黙って落とすと、AIは「3つ選んだのに2つしか返らない」を自分の誤りと解釈する。
    測れないことを言うのが正しい(§6予防6の「黙って通さない」の読み取り側)。
    """
    box = box_of(session)
    call(server, "propose_selection", {"refs": [{"body": box.id, "kind": "vertex", "index": 1}]})
    item = call(server, "measure")["items"][0]
    assert item["kind"] == "vertex"
    assert item["measured"] is False
    assert item["reason"].strip()


# --------------------------------------------------------------------------------------
# 6. 交互操作の一巡 — **MCP経由だけで**(タスクファイル項番9・F の核心)
# --------------------------------------------------------------------------------------
def test_the_whole_interleaved_lap_runs_through_mcp_alone(server: McpServer, session: Session):
    """**このファイルで最も重要な1本**。

    述語で辺を提案 → `get_selection` で読む → `modify_selection` で1本足して1本引く →
    `commit_selection` → `fillet` を適用 → 体積と面数が変わる。
    A2 が素のPythonで通した一巡(`tests/README.md` の「Aの実装で分かったこと」)を、
    **MCPのツールだけで**もう一度通す。ここが通れば §0 の交互操作の条件はAI側から満たせている。
    """
    box = box_of(session)
    faces_before = kernel.face_count(box.shape)
    volume_before = query.volume(box.shape)

    # 1. AIが述語で提案する
    proposed = call(server, "propose_selection", {"predicate": VERTICAL_EDGES})["selection"]
    assert proposed["count"] == 4 and proposed["origin"] == "proposed"

    # 2. 人(と、それを代行するAI)が読み直す
    read_back = selection_of(server)
    assert read_back["items"] == proposed["items"], "書いた直後に読んだら別物だった"

    # 3. 1本足して1本引く —— 足すのは上面の外周のうち、まだ選ばれていない辺
    vertical = [item["index"] for item in read_back["items"]]
    boundary = query.edges_of_face(box.shape, top_face_index(box.shape))
    added = next(index for index in boundary if index not in vertical)
    modified = call(
        server,
        "modify_selection",
        {"add": [edge_ref(box.id, added)], "remove": [edge_ref(box.id, vertical[0])]},
    )["selection"]

    assert [item["index"] for item in modified["items"]] == vertical[1:] + [added], (
        "順序は選択順(追加順)であること —— fillet は edges と radius を位置で対応させるので、"
        "並べ替えると半径の割り当てが黙ってずれる"
    )
    assert modified["origin"] == "user", "人が触ること自体が承認(A1の決定3)"

    # 4. 確定して適用する
    call(server, "commit_selection")
    payload = call(server, "fillet", {"radius": 2.0})

    # 5. 体積と面数が変わる
    assert payload["verb"] == "fillet" and payload["changed_bodies"] == [box.id]
    assert kernel.face_count(box.shape) > faces_before
    assert query.volume(box.shape) < volume_before
    assert session.document.log.entries[-1].args["edges"] == vertical[1:] + [added]

    # 6. **選択は操作をまたいで残る**(A1の項番5)。次の一手をAIがそのまま打てる
    assert selection_of(server)["count"] == 4


# --------------------------------------------------------------------------------------
# 7. AI起点の選択変更が画面へ届く(タスクファイル項番10)
# --------------------------------------------------------------------------------------
def test_a_selection_change_from_mcp_fires_exactly_one_event(server: McpServer, session: Session):
    """1ツール1イベント。イベントは**選択を丸ごと**運ぶ(UIが描き直すのに足る・項番13)。"""
    seen: list = []
    session.add_listener(seen.append)
    try:
        call(server, "propose_selection", {"predicate": VERTICAL_EDGES})
    finally:
        session.remove_listener(seen.append)

    assert len(seen) == 1
    event = seen[0]
    assert event["change"] == "selection", "verbの applied とはキーで区別する(選択はverbではない)"
    assert event["selection"]["count"] == 4
    assert event["selection"]["origin"] == "proposed"
    assert "verb" not in event


def test_a_selection_change_from_mcp_reaches_the_browser(session: Session):
    """**AI→人の方向**が生きていること(A1の決定8が名指しで警告している経路)。

    `Server._on_core_change` はMCP起点の変更から `applied` を落として配るので、
    **`state_meta()["selection"]` に載っていないとAIの提案は画面へ届かない**。
    イベントだけ実装して状態に載せないと、人→AIは動くのでテストが無ければ気づけない。
    """
    import asyncio

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
        await server._push(browser, applied=None, only=browser)
        before = len(browser.payloads)

        mcp = McpServer(DocumentTools(session))
        call(mcp, "propose_selection", {"predicate": VERTICAL_EDGES})
        for _ in range(50):
            await asyncio.sleep(0)
            if len(browser.payloads) > before:
                break
        return browser.payloads

    payloads = asyncio.run(scenario())
    assert len(payloads) > 1, "AIが選択を書き換えたのにブラウザへ配信されていない"
    pushed = payloads[-1]
    assert pushed["type"] == "state"
    assert pushed["state"]["selection"]["origin"] == "proposed"
    assert pushed["state"]["selection"]["count"] == 4
    assert "applied" not in pushed


def test_the_selection_is_not_saved(server: McpServer, session: Session):
    """選択はセッション状態であって文書の保存対象ではない(A1の項番7)。

    MCPから提案した選択が保存されると、次に開いたときAIの提案が人の確定として復活する。
    """
    call(server, "propose_selection", {"predicate": VERTICAL_EDGES})
    saved = session.document.log.to_json()  # 保存の経路はこれだけ(選択はログに載らない)
    assert "proposed" not in saved and "selection" not in json.loads(saved)
