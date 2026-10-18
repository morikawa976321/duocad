"""全verbをMCPツールとして公開する契約。ツールの顔ぶれは `tests/test_doc_code_agreement.py` が `MCP.md` の表と、
選択・述語は `tests/test_mcp_selection.py` が見る。ここが固定するのは:
1. 到達性: 各ツールが引数スキーマを持ち、配った引数を `DocumentTools` のメソッドがそのまま受け取れる。
2. 形は確定した選択から取り、数値・名前・パスだけを引数で渡す(例外はBodyの `body_id`)。
3. verbの結果は `verb`/`seq`/`changed_bodies`/`bodies`/`can_undo`/`can_redo` の構造で返す。`changed_bodies` は
`Session.body_revision` の前後差分で導く(verbごとの数え上げは新verbで数え漏れる)。
4. 失敗は `{"error": {"code","message","tool","exception"}}` のJSONで返す。コードは `tests/mcp_support.py` の
`ERROR_CODES` の語彙に限る(`MCP.md`「実装のメモ」が正、一致は `tests/test_doc_code_agreement.py`)。
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

from duocad import kernel, kernel_sketch, query
from duocad.catalog import COMMANDS
from duocad.document import Body
from duocad.entry import entry_for
from duocad.mcp_server import DocumentTools, McpServer
from duocad.selection import set_visible
from duocad.server import Session
from document_support import BOX_X, BOX_Y, BOX_Z
from mcp_support import call, call_error, request
from session_support import pushed_by_mcp

# # JSON-RPC 2.0 の標準エラーコード。実装から import しない(実装に追従して契約でなくなる)。
INVALID_PARAMS = -32602


from duocad.paths import REPO_ROOT

#: 形を変えるverbのツール(問い合わせ・選択・履歴を除く)。**catalog の宣言から引く**。
SHAPE_VERB_TOOLS = {
    command.name
    for command in COMMANDS
    if command.mcp and command.category in ("sketch", "feature", "file")
}


@pytest.fixture()
def empty_session(tmp_path: Path) -> Session:
    return Session(0.2)


@pytest.fixture()
def server(box_session: Session) -> McpServer:
    return McpServer(DocumentTools(box_session))


def tool_list(server: McpServer) -> list:
    return request(server, "tools/list")["result"]["tools"]


def select_refs(server: McpServer, refs: list) -> None:
    call(server, "set_selection", {"refs": refs})


def face_ref(body_id: str, index: int) -> dict:
    return {"body": body_id, "kind": "face", "index": index}


def edge_ref(body_id: str, index: int) -> dict:
    return {"body": body_id, "kind": "edge", "index": index}


def top_face_index(shape) -> int:
    for face in query.describe_faces(shape):
        if face["normal"][2] > 0.9:
            return face["index"]
    raise AssertionError("上向きの面が無い(テストの前提が崩れている)")


def vertical_edge_indices(shape) -> list:
    found = []
    for edge in query.describe_edges(shape):
        start, end = edge["start"], edge["end"]
        if abs(end[2] - start[2]) > 1.0 and abs(end[0] - start[0]) < 1e-6 and abs(end[1] - start[1]) < 1e-6:
            found.append(edge["index"])
    return found


def test_tool_names_match_the_verb_names_in_the_operation_log():
    """落ちたら: ログのverb名と同じ綴りのツールが無いか、catalog が「ログに載る」と宣言した形のverbがログに登録されていない。"""
    from duocad.oplog import registered_verbs

    # `extrude_face` / `set_parameter` は保存済みログの再生のためだけに登録が残る(公開ツールではない)
    registered = set(registered_verbs()) - {"extrude_face", "set_parameter"}
    expected = {
        command.name
        for command in COMMANDS
        if command.category in ("sketch", "feature")
        or (command.category == "file" and command.changes_document and command.mcp)
        # 公開面に無いが保存済みログの再生のために verb として残るもの(`add_solid_body`)。文書を変えない読み取り
        # (作業空間の `get_command`)は含めない。
        or (not command.mcp and command.ws == "none" and command.changes_document)
    }
    assert registered == expected, {
        "catalog に同じ綴りの宣言が無いverb": sorted(registered - expected),
        "verbとして登録されていない宣言": sorted(expected - registered),
    }


def test_every_tool_has_a_description_and_an_object_schema(server: McpServer):
    """説明文と入力スキーマはAIがツールを選ぶ唯一の手がかり。"""
    for tool in tool_list(server):
        assert tool["description"].strip(), f"{tool['name']} に説明が無い"
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False, f"{tool['name']} が未知の引数を許している"


def test_every_argument_is_typed_and_documented(server: McpServer):
    """引数は型と説明を持つ(型が無いと `_check_arguments` が検査できない)。型は文字列か文字列のリスト(`fillet.radius` は数値と配列の両方を取る)。"""
    known = {"object", "array", "string", "number", "integer", "boolean"}
    for tool in tool_list(server):
        for name, spec in (tool["inputSchema"].get("properties") or {}).items():
            where = f"{tool['name']}.{name}"
            declared = spec.get("type")
            assert declared, f"{where} に型が無い"
            names = [declared] if isinstance(declared, str) else list(declared)
            assert names and set(names) <= known, f"{where} の型が不明: {declared!r}"
            assert (spec.get("description") or "").strip(), f"{where} に説明が無い"


def test_the_published_arguments_are_exactly_what_the_implementation_accepts(server: McpServer):
    """配る引数(名前・必須)が実体(`DocumentTools` の同名メソッド、無ければ `duocad/entry.py` のverb入口)の引数と一致する。
    落ちたら: AIに見せた引数を実体が受け取れない(`TypeError` → `internal`)か、実体の引数を見せていない。
    """
    mismatched = {}
    for tool in tool_list(server):
        schema = tool["inputSchema"]
        required = set(schema.get("required") or [])
        published = {name: name in required for name in (schema.get("properties") or {})}
        own = getattr(DocumentTools, tool["name"], None)
        parameters = list(inspect.signature(own or entry_for(tool["name"])).parameters.items())[1:]
        accepted = {
            name: parameter.default is inspect.Parameter.empty for name, parameter in parameters
        }
        if published != accepted:
            mismatched[tool["name"]] = {"配る(必須か)": published, "実体(必須か)": accepted}
    assert not mismatched, mismatched


def test_no_verb_tool_takes_a_shape_argument(server: McpServer):
    """形の引数(面・辺・領域)を持つverbツールは無い。`face_index=` や `edges=` の裏口は「提案のままではverbに渡せない」を素通りさせる。"""
    forbidden = {"face_index", "face", "edges", "edge_indices", "regions", "refs", "selection"}
    for tool in tool_list(server):
        if tool["name"] not in SHAPE_VERB_TOOLS:
            continue
        leaked = forbidden & set(tool["inputSchema"].get("properties") or {})
        assert not leaked, f"{tool['name']} が形を直接受けている: {sorted(leaked)}"


def _assert_rejects_argument(server: McpServer, name: str, arguments: dict, argument: str) -> None:
    """スキーマ違反は INVALID_PARAMS で、どの引数が悪いかを名指しする(名指しが無いと「そんなツールは無い」と区別できず、未実装のまま緑になる)。"""
    response = request(server, "tools/call", {"name": name, "arguments": arguments})
    assert "error" in response, f"{name}.{argument} を素通しした: {response}"
    assert response["error"]["code"] == INVALID_PARAMS
    assert argument in response["error"]["message"], response["error"]["message"]


def test_numbers_are_not_accepted_as_strings(server: McpServer):
    """`radius: "3"` を通すとコアの `float("3")` が成功して黙って動く。プロトコル層で INVALID_PARAMS にする。"""
    _assert_rejects_argument(server, "fillet", {"radius": "3"}, "radius")


def test_booleans_are_not_accepted_as_numbers(server: McpServer):
    """Pythonでは `True` が `int` の一種なので、`radius: true` は素通りすると 1.0 になる。"""
    _assert_rejects_argument(server, "fillet", {"radius": True}, "radius")


def test_arrays_are_checked(server: McpServer):
    """`p0` のような座標は配列。数値を渡されたら弾く(添字アクセスで別の例外になる前に)。"""
    _assert_rejects_argument(
        server, "add_line", {"sketch_id": "s1", "p0": 3, "p1": [1, 1]}, "p0"
    )


def test_integer_arguments_reject_fractions(server: McpServer):
    """反復の個数は整数。`nx: 2.5` を黙って `int()` すると 2 個になる。"""
    _assert_rejects_argument(
        server,
        "pattern_rectangular",
        {"sketch_id": "s1", "entities": ["e1"], "dx": 10, "dy": 0, "nx": 2.5, "ny": 1},
        "nx",
    )


# #: `bodies[]` の1要素のキー。過不足なく固定する(落とした実装が通ると、AIが `describe_document` をもう1往復する)。
BODY_KEYS = {"body_id", "name", "kind", "visible", "face_count", "volume"}


def _assert_verb_envelope(payload: dict, verb: str) -> None:
    assert payload["verb"] == verb
    assert isinstance(payload["seq"], int) and payload["seq"] >= 1
    assert isinstance(payload["changed_bodies"], list)
    assert isinstance(payload["bodies"], list)
    assert isinstance(payload["can_undo"], bool)
    assert isinstance(payload["can_redo"], bool)
    assert [body["body_id"] for body in payload["bodies"]] == payload["changed_bodies"], (
        "`bodies` は `changed_bodies` の変更後の姿(順序も同じ)"
    )
    for body in payload["bodies"]:
        assert set(body) == BODY_KEYS, sorted(set(body) ^ BODY_KEYS)


def test_a_verb_reports_what_changed_not_a_sentence(server: McpServer, box_session: Session):
    """成功したら変わったBody・新しい面数・操作のseqが構造で返る(自然文だけだとAIが次の一手を推測する)。"""
    box = box_session.document.bodies[0]
    faces_before = kernel.face_count(box.shape)
    volume_before = query.volume(box.shape)

    select_refs(server, [edge_ref(box.id, index) for index in vertical_edge_indices(box.shape)])
    payload = call(server, "fillet", {"radius": 2.0})

    _assert_verb_envelope(payload, "fillet")
    assert payload["changed_bodies"] == [box.id]
    after = {body["body_id"]: body for body in payload["bodies"]}[box.id]
    assert after["face_count"] > faces_before, "新しい面数が返っていない/丸まっていない"
    assert after["volume"] < volume_before
    assert after["face_count"] == kernel.face_count(box.shape), "返した面数が実物と違う"


def test_changed_bodies_is_empty_when_only_a_sketch_changed(
    empty_session: Session,
):
    """スケッチだけを触った操作は `changed_bodies` が空。verbごとに数え上げる実装だとここが静かに間違う。"""
    server = McpServer(DocumentTools(empty_session))
    payload = call(server, "sketch_create", {"base_plane": "XY"})
    _assert_verb_envelope(payload, "sketch_create")
    assert payload["changed_bodies"] == []
    assert payload["sketch_id"]


def test_set_visible_reports_the_change_but_not_a_verb_envelope(
    server: McpServer, box_session: Session
):
    """`set_visible` の戻り値(`previous` / `changed`)は壊さず、`verb` / `seq` は載せない(可視性は操作ログに載らず、
    封筒を配ると「undoで戻る」という嘘になる)。見分けは選択と同じ `change` キー。
    """
    box = box_session.document.bodies[0]
    payload = call(server, "set_visible", {"body_id": box.id, "visible": False})
    assert payload["change"] == "visibility"
    assert "verb" not in payload and "seq" not in payload
    assert payload["previous"] is True and payload["visible"] is False and payload["changed"] is True
    assert payload["changed_bodies"] == [box.id], "版の前後差では拾えている(可視性は版に入る)"
    assert box_session.document.log.entries == [], "可視性が操作ログに積まれている"


def test_every_sketch_verb_is_reachable_and_returns_its_id(empty_session: Session):
    """スケッチの5verbを実際に1回ずつ成功させる: コアへ届くこと(ログのverbと図形の増加)と作ったもののIDが返ること
    (`tools/list` の顔ぶれだけでは偽の封筒でも緑になる)。
    """
    server = McpServer(DocumentTools(empty_session))
    sketch_id = call(server, "sketch_create", {"base_plane": "XY"})["sketch_id"]

    created = {
        "add_line": call(
            server, "add_line", {"sketch_id": sketch_id, "p0": [0, 0], "p1": [30, 0]}
        ),
        "add_circle": call(
            server, "add_circle", {"sketch_id": sketch_id, "center": [60, 0], "diameter": 20}
        ),
        "add_rectangle": call(
            server,
            "add_rectangle",
            {"sketch_id": sketch_id, "corner": [0, 40], "width": 30, "height": 30},
        ),
        "add_arc": call(
            server,
            "add_arc",
            {
                "sketch_id": sketch_id,
                "center": [100, 0],
                "radius": 10,
                "start_deg": 0,
                "end_deg": 90,
            },
        ),
    }
    for verb, payload in created.items():
        _assert_verb_envelope(payload, verb)
        assert isinstance(payload["entity_id"], str) and payload["entity_id"]
        assert payload["changed_bodies"] == [], f"{verb} は形状を変えない"

    sketch = empty_session.document.sketches[0]
    assert [entity.id for entity in sketch.entities] == [
        payload["entity_id"] for payload in created.values()
    ], "MCPが返したIDと、コアに実際に積まれた図形が一致しない"
    assert [entry.verb for entry in empty_session.document.log.entries] == [
        "sketch_create", "add_line", "add_circle", "add_rectangle", "add_arc"
    ]


def test_both_patterns_are_reachable_and_return_the_created_entities(empty_session: Session):
    """反復2verb(矩形・円形)も1回ずつ成功し、作った複製の図形IDの並び `created` が返る。"""
    server = McpServer(DocumentTools(empty_session))
    sketch_id = call(server, "sketch_create", {"base_plane": "XY"})["sketch_id"]
    first = call(server, "add_circle", {"sketch_id": sketch_id, "center": [10, 0], "diameter": 4})[
        "entity_id"
    ]
    second = call(server, "add_circle", {"sketch_id": sketch_id, "center": [0, 10], "diameter": 4})[
        "entity_id"
    ]

    rectangular = call(
        server,
        "pattern_rectangular",
        {"sketch_id": sketch_id, "entities": [first], "dx": 20, "dy": 0, "nx": 2, "ny": 1},
    )
    circular = call(
        server,
        "pattern_circular",
        {"sketch_id": sketch_id, "entities": [second], "center": [0, 0], "count": 4},
    )

    _assert_verb_envelope(rectangular, "pattern_rectangular")
    _assert_verb_envelope(circular, "pattern_circular")
    assert len(rectangular["created"]) == 1
    assert len(circular["created"]) == 3
    assert [entity.id for entity in empty_session.document.sketches[0].entities] == [
        first, second, *rectangular["created"], *circular["created"]
    ]


def test_a_missing_body_is_reported_as_not_found(server: McpServer, box_session: Session):
    """`KeyError` は `not_found` で、**その場で有効なボディIDを添える**(行き止まりのエラーにしない)。"""
    error = call_error(server, "describe_shape", {"body_id": "ghost"})
    assert error["code"] == "not_found"
    assert error["exception"] == "KeyError"
    assert box_session.document.bodies[0].id in error["message"], "有効なボディIDが添えられていない"


def test_an_unknown_entity_id_shows_the_real_ones(empty_session: Session):
    """存在しない図形IDは弾くだけでなく有効なIDを見せる。実測: ローカルLLMが「ID形式を推測して試します」と述べて
    存在しない `c1` を渡した。IDが分からないとAIは尋ねず推測するので、正解の集合が見えれば次の一手で復帰できる。
    """
    server = McpServer(DocumentTools(empty_session))
    sketch_id = call(server, "sketch_create", {"base_plane": "XY"})["sketch_id"]
    real = call(
        server, "add_circle", {"sketch_id": sketch_id, "center": [0, 0], "diameter": 4}
    )["entity_id"]

    error = call_error(
        server,
        "pattern_rectangular",
        {"sketch_id": sketch_id, "entities": ["c1"], "dx": 20, "dy": 0, "nx": 2, "ny": 1},
    )

    assert error["code"] == "not_found"
    assert "c1" in error["message"]
    assert real in error["message"], "有効な図形IDが添えられていない(行き止まりのエラー)"


def test_an_unknown_sketch_id_shows_the_real_ones(empty_session: Session):
    """スケッチIDでも同じ —— 有効IDを添える作法は引くIDの種類で変わらない。"""
    server = McpServer(DocumentTools(empty_session))
    real = call(server, "sketch_create", {"base_plane": "XY"})["sketch_id"]

    error = call_error(
        server, "add_circle", {"sketch_id": "sk_ghost", "center": [0, 0], "diameter": 4}
    )

    assert error["code"] == "not_found"
    assert real in error["message"], "有効なスケッチIDが添えられていない"


def test_a_bad_value_is_reported_as_invalid_argument(server: McpServer, box_session: Session):
    """`ValueError` は `invalid_argument`(半径0は形を変えないので verb が拒む)。"""
    box = box_session.document.bodies[0]
    select_refs(server, [edge_ref(box.id, index) for index in vertical_edge_indices(box.shape)])
    error = call_error(server, "fillet", {"radius": 0.0})
    assert error["code"] == "invalid_argument"


def test_applying_a_verb_with_nothing_selected_says_so(server: McpServer):
    """選択が空のまま verb を呼んだら `selection_empty`(`invalid_argument` に潰すとAIが数値をいじり始める)。
    `required_kind` と `selection`(今の中身の要約)を必ず載せる。
    """
    error = call_error(server, "fillet", {"radius": 2.0})
    assert error["code"] == "selection_empty"
    assert error["required_kind"] == "edge"
    assert error["selection"] == {"count": 0}
    assert "present_kinds" not in error, "空の選択を種別違いと名乗っている"


def test_selecting_the_wrong_kind_has_its_own_code(server: McpServer, box_session: Session):
    """面を選んで `fillet` を呼ぶと `selection_kind_mismatch`。`selection_empty` と分けるのは、面が入っている選択に
    「空」と名乗ると嘘になるため。`present_kinds` に確定している種別と件数が入る。
    """
    box = box_session.document.bodies[0]
    select_refs(server, [face_ref(box.id, 1)])
    error = call_error(server, "fillet", {"radius": 2.0})
    assert error["code"] == "selection_kind_mismatch"
    assert error["required_kind"] == "edge"
    assert error["present_kinds"] == {"face": 1}
    assert error["selection"]["count"] == 1
    assert "edge" in error["hint"]


def test_a_solid_can_be_built_from_scratch_through_mcp(empty_session: Session):
    """空の文書からMCPだけで箱が作れる(スケッチ4verb + `extrude`)。領域は `set_selection(refs=[{"sketch":.., "index":..}])` で選んでから押し出す。"""
    server = McpServer(DocumentTools(empty_session))
    sketch_id = call(server, "sketch_create", {"base_plane": "XY"})["sketch_id"]
    call(server, "add_rectangle", {"sketch_id": sketch_id, "corner": [0, 0], "width": BOX_X, "height": BOX_Y})

    select_refs(server, [{"sketch": sketch_id, "index": 1}])
    payload = call(server, "extrude", {"distance": BOX_Z, "mode": "join"})

    _assert_verb_envelope(payload, "extrude")
    assert len(payload["changed_bodies"]) == 1
    body = empty_session.document.bodies[0]
    assert kernel.face_count(body.shape) == 6
    assert query.volume(body.shape) == pytest.approx(BOX_X * BOX_Y * BOX_Z, rel=1e-9)


def test_extrude_can_cut(empty_session: Session):
    """`mode="cut"` が実際にコアへ届く。`join` しか通っていないと、`mode` を読み飛ばして切り取りが足し算になる実装でも緑になる。"""
    server = McpServer(DocumentTools(empty_session))
    plate = call(server, "sketch_create", {"base_plane": "XY"})["sketch_id"]
    call(server, "add_rectangle", {"sketch_id": plate, "corner": [0, 0], "width": BOX_X, "height": BOX_Y})
    select_refs(server, [{"sketch": plate, "index": 1}])
    call(server, "extrude", {"distance": BOX_Z, "mode": "join"})

    hole = call(server, "sketch_create", {"base_plane": "XY"})["sketch_id"]
    call(server, "add_circle", {"sketch_id": hole, "center": [15, 15], "diameter": 10})
    select_refs(server, [{"sketch": hole, "index": 1}])
    payload = call(server, "extrude", {"distance": BOX_Z, "mode": "cut"})

    _assert_verb_envelope(payload, "extrude")
    body = empty_session.document.bodies[0]
    import math

    assert query.volume(body.shape) == pytest.approx(
        BOX_X * BOX_Y * BOX_Z - math.pi * 25.0 * BOX_Z, rel=1e-3
    ), "cut が足し算になっている(または穴が開いていない)"
    assert payload["changed_bodies"] == [body.id]


def test_extrude_can_make_a_new_body(empty_session: Session):
    """`mode="new"` が実際にコアへ届く(既存ボディがあっても融合しない)。差はボディの本数にしか出ず、体積だけ見ていると緑になる。"""
    server = McpServer(DocumentTools(empty_session))
    plate = call(server, "sketch_create", {"base_plane": "XY"})["sketch_id"]
    call(server, "add_rectangle", {"sketch_id": plate, "corner": [0, 0], "width": BOX_X, "height": BOX_Y})
    select_refs(server, [{"sketch": plate, "index": 1}])
    call(server, "extrude", {"distance": BOX_Z, "mode": "join"})
    first = empty_session.document.bodies[0]

    second = call(server, "sketch_create", {"base_plane": "XY"})["sketch_id"]
    call(server, "add_rectangle", {"sketch_id": second, "corner": [5, 5], "width": 10, "height": 10})
    select_refs(server, [{"sketch": second, "index": 1}])
    payload = call(server, "extrude", {"distance": BOX_Z, "mode": "new"})

    _assert_verb_envelope(payload, "extrude")
    assert len(empty_session.document.bodies) == 2, "new が既存ボディへ融合している"
    created = empty_session.document.bodies[1]
    assert payload["changed_bodies"] == [created.id]
    assert query.volume(first.shape) == pytest.approx(BOX_X * BOX_Y * BOX_Z, rel=1e-9), (
        "new が元のボディの形を変えている"
    )
    assert query.volume(created.shape) == pytest.approx(10.0 * 10.0 * BOX_Z, rel=1e-9)


def test_extrude_takes_its_face_from_the_selection(server: McpServer, box_session: Session):
    """`extrude` は**選択された面**を押す(添字を引数で受けない)。"""
    box = box_session.document.bodies[0]
    volume_before = query.volume(box.shape)
    select_refs(server, [face_ref(box.id, top_face_index(box.shape))])

    payload = call(server, "extrude", {"distance": 5.0, "mode": "join"})

    _assert_verb_envelope(payload, "extrude")
    assert payload["changed_bodies"] == [box.id]
    assert query.volume(box.shape) == pytest.approx(volume_before + BOX_X * BOX_Y * 5.0, rel=1e-6)


def test_extrude_refuses_faces_that_are_not_coplanar(server: McpServer, box_session: Session):
    """同一平面でない面が混ざる選択は**どれを押すか黙って決めず**、操作を積まずに断る。"""
    box = box_session.document.bodies[0]
    select_refs(server, [face_ref(box.id, 1), face_ref(box.id, 2)])
    error = call_error(server, "extrude", {"distance": 5.0, "mode": "join"})
    assert error["code"] == "invalid_argument"
    assert box_session.document.log.entries == [], "拒否したのに操作が積まれた"


def test_sketch_create_can_sit_on_a_selected_face(server: McpServer, box_session: Session):
    """`base_plane` を省いたら、**選択の面**にスケッチを貼る(§1 `sketch_create` の2つ目の引数形)。"""
    box = box_session.document.bodies[0]
    select_refs(server, [face_ref(box.id, top_face_index(box.shape))])
    payload = call(server, "sketch_create")
    _assert_verb_envelope(payload, "sketch_create")
    sketch = box_session.document.sketches[-1]
    assert sketch.plane["kind"] == "face"
    assert sketch.plane["body"] == box.id


def _plate_with_face_selected(server: McpServer, axis: str) -> str:
    plate = call(server, "sketch_create", {"base_plane": "XY"})["sketch_id"]
    call(server, "add_rectangle", {"sketch_id": plate, "corner": [0, 0], "width": 100, "height": 60})
    select_refs(server, [{"sketch": plate, "index": 1}])
    body_id = call(server, "extrude", {"distance": 10, "mode": "join"})["changed_bodies"][0]
    where = [{"op": "normal_dot_gt", "axis": axis, "value": 0.9}]
    call(server, "set_selection", {"predicate": {"kind": "face", "body": body_id, "where": where}})
    return body_id


def _to_uv(frame: dict, point: list) -> list:
    offset = [p - o for p, o in zip(point, frame["origin"])]
    return [sum(d * a for d, a in zip(offset, frame[axis])) for axis in ("u", "v")]


@pytest.mark.parametrize("axis", ["Z", "X"])
def test_a_hole_drawn_in_the_returned_frame_lands_where_intended(empty_session: Session, axis: str):
    """落ちたら、AIが戻り値の frame だけでは面の上の狙った位置に描けない(Issue #125。
    側面の枠の原点は面の中心でなく角にあり、v は世界の -Z を向く)。"""
    server = McpServer(DocumentTools(empty_session))
    assert call(server, "sketch_create", {"base_plane": "XY"})["frame"]["origin"] == [0.0, 0.0, 0.0]
    body = empty_session.document.get_body(_plate_with_face_selected(server, axis))
    face = next(f for f in query.describe_faces(body.shape) if f["normal"]["XYZ".index(axis)] > 0.9)
    target = query.face_centroid(body.shape, face["index"])

    payload = call(server, "sketch_create")
    frame = payload["frame"]
    described = call(server, "describe_document")["sketches"]
    assert next(s for s in described if s["sketch_id"] == payload["sketch_id"])["plane"]["frame"] == frame
    call(server, "add_circle", {"sketch_id": payload["sketch_id"], "center": _to_uv(frame, target), "diameter": 4})
    select_refs(server, [{"sketch": payload["sketch_id"], "index": 1}])
    call(server, "extrude", {"distance": -5, "mode": "cut"})

    wall = next(f["index"] for f in query.describe_faces(body.shape) if f["kind"].startswith("cylinder"))
    expected = [t - 2.5 * n for t, n in zip(target, frame["normal"])]
    assert query.face_centroid(body.shape, wall) == pytest.approx(expected, abs=1e-6)


def test_sketch_create_offset_on_a_face_returns_the_face_frame_moved_along_its_normal(empty_session: Session):
    """落ちたら、面を基準にしたオフセットのスケッチの座標系が戻り値から読めない。"""
    server = McpServer(DocumentTools(empty_session))
    _plate_with_face_selected(server, "X")
    face = call(server, "sketch_create")["frame"]
    offset = call(server, "sketch_create_offset", {"distance": 5})["frame"]
    assert offset["origin"] == pytest.approx([o + 5 * n for o, n in zip(face["origin"], face["normal"])])
    assert [offset[k] for k in ("u", "v", "normal")] == [face[k] for k in ("u", "v", "normal")]


def test_sketch_create_without_a_plane_or_a_selection_is_an_error(server: McpServer):
    """平面も選択も無ければ**推測しない**(既定でXYに貼ると静かに別の場所に図形が出る)。"""
    error = call_error(server, "sketch_create")
    assert error["code"] in {"selection_empty", "invalid_argument"}


def test_sketch_create_offset_with_a_named_base_plane(
    server: McpServer, box_session: Session
):
    """`base_plane` + `distance` だけで、選択が無くても作れる(issue #3)。"""
    payload = call(server, "sketch_create_offset", {"base_plane": "XY", "distance": 7.5})
    _assert_verb_envelope(payload, "sketch_create_offset")
    sketch = box_session.document.sketches[-1]
    assert sketch.plane["kind"] == "offset"
    assert sketch.plane["base"] == {"kind": "base", "name": "XY", "provenance": None}
    assert sketch.plane["distance"] == 7.5
    assert sketch.plane["frame"]["origin"] == pytest.approx([0.0, 0.0, 7.5])


def test_sketch_create_offset_can_sit_on_a_selected_face(
    server: McpServer, box_session: Session
):
    """`base_plane` を省いたら、`sketch_create` と同じく**選択の面**を基準にする。"""
    box = box_session.document.bodies[0]
    face_index = top_face_index(box.shape)
    select_refs(server, [face_ref(box.id, face_index)])
    payload = call(server, "sketch_create_offset", {"distance": 3.0})
    _assert_verb_envelope(payload, "sketch_create_offset")

    sketch = box_session.document.sketches[-1]
    assert sketch.plane["kind"] == "offset"
    assert sketch.plane["distance"] == 3.0
    assert sketch.plane["base"] == {
        "kind": "face",
        "body": box.id,
        "index": face_index,
        "provenance": None,
    }
    face_frame = kernel_sketch.face_frame(box.shape, face_index)
    assert sketch.plane["frame"]["origin"] == pytest.approx(
        [face_frame["origin"][i] + face_frame["normal"][i] * 3.0 for i in range(3)]
    )


def test_sketch_create_offset_without_a_plane_or_a_selection_is_an_error(
    server: McpServer,
):
    error = call_error(server, "sketch_create_offset", {"distance": 3.0})
    assert error["code"] in {"selection_empty", "invalid_argument"}


def test_move_body_can_rotate_through_mcp(server: McpServer, box_session: Session):
    """`mode="rotate"` がMCPの口からコアまで届く。`DocumentTools` が引数を `float()` で潰す/`mode` を読み飛ばして
    並進になる層の取りこぼしを見る(AIが回転を使う経路はこの1本だけ)。
    """
    box = box_session.document.bodies[0]
    select_refs(server, [face_ref(box.id, 1)])
    payload = call(
        server,
        "move_body",
        {"mode": "rotate", "axis_point": [0, 0, 0], "axis_dir": [0, 0, 1], "angle": 90},
    )

    _assert_verb_envelope(payload, "move_body")
    assert payload["changed_bodies"] == [box.id]
    box_after = kernel.bounding_box(box.shape)
    # (0..30, 0..30) がZまわりに+90度 -> (-30..0, 0..30)
    assert box_after["min"][:2] == pytest.approx([-BOX_X, 0.0], abs=1e-6)
    assert box_after["max"][:2] == pytest.approx([0.0, BOX_Y], abs=1e-6)
    assert query.volume(box.shape) == pytest.approx(BOX_X * BOX_Y * BOX_Z, rel=1e-9)


def test_move_body_refuses_a_mixed_mode_through_mcp(server: McpServer, box_session: Session):
    """並進と回転を混ぜたら `invalid_argument`(黙ってどちらかが効くことは無い)。"""
    box = box_session.document.bodies[0]
    select_refs(server, [face_ref(box.id, 1)])
    error = call_error(
        server,
        "move_body",
        {"mode": "rotate", "axis_dir": [0, 0, 1], "angle": 90, "dx": 5},
    )
    assert error["code"] == "invalid_argument"
    assert box_session.document.log.entries == [], "拒否したのに操作が積まれた"


def test_move_sketch_entities_can_rotate_through_mcp(empty_session: Session):
    """スケッチ側の `mode="rotate"` もMCPの口から届く(対象は選択)。"""
    server = McpServer(DocumentTools(empty_session))
    sketch_id = call(server, "sketch_create", {"base_plane": "XY"})["sketch_id"]
    entity_id = call(
        server, "add_line", {"sketch_id": sketch_id, "p0": [0, 0], "p1": [10, 0]}
    )["entity_id"]
    select_refs(server, [{"sketch": sketch_id, "entity": entity_id, "point": None}])
    payload = call(
        server, "move_sketch_entities", {"mode": "rotate", "angle": 90, "center": [0, 0]}
    )

    _assert_verb_envelope(payload, "move_sketch_entities")
    from duocad import sketch as sketch_module

    moved = sketch_module.entity_params(empty_session.document, sketch_id, entity_id)
    assert (moved["x1"], moved["y1"]) == pytest.approx((0.0, 10.0), abs=1e-9)


def test_chamfer_reaches_the_core_verb(server: McpServer, box_session: Session):
    box = box_session.document.bodies[0]
    faces_before = kernel.face_count(box.shape)
    select_refs(server, [edge_ref(box.id, index) for index in vertical_edge_indices(box.shape)])

    payload = call(server, "chamfer", {"distance": 2.0})

    _assert_verb_envelope(payload, "chamfer")
    assert kernel.face_count(box.shape) > faces_before
    assert box_session.document.log.entries[-1].verb == "chamfer"


def test_import_step_reports_a_missing_file_as_not_found(empty_session: Session):
    """無いファイルは `FileNotFoundError` で来て `internal` に落とさない(AIが直せる誤りを内部エラーと名付けると諦める)。"""
    server = McpServer(DocumentTools(empty_session))
    error = call_error(server, "import_step", {"path": str(REPO_ROOT / "no-such-file.step")})
    assert error["code"] == "not_found"
    assert error["exception"] == "FileNotFoundError"


def test_export_writes_a_file_and_reports_where(server: McpServer, tmp_path: Path):
    """書き出しはログに載らず(verbではない)、どこへ書いたかを返す(AIが自分で `ls` できないので戻り値が唯一の確認手段)。"""
    step_path = tmp_path / "out.step"
    payload = call(server, "export_step", {"path": str(step_path)})
    assert Path(payload["path"]) == step_path
    assert step_path.stat().st_size > 0

    stl_path = tmp_path / "out.stl"
    payload = call(server, "export_stl", {"path": str(stl_path)})
    assert Path(payload["path"]) == stl_path
    assert stl_path.stat().st_size > 0


def test_export_reports_skipped_hidden_bodies_and_no_body_ids_argument(
    server: McpServer, box_session: Session, tmp_path: Path
):
    """`body_ids` 引数は存在せず(渡しても未知の引数エラーになる)、応答の `body_ids` /
    `skipped_hidden` は実際に書き出した/除外したBodyと一致する。
    """
    hidden = Body(
        id="hidden-box",
        name="hidden-box",
        kind="solid",
        shape=BRepPrimAPI_MakeBox(5.0, 5.0, 5.0).Shape(),
    )
    box_session.document.add_body(hidden)
    set_visible(box_session.document, "hidden-box", False)

    step_path = tmp_path / "visible_only.step"
    payload = call(server, "export_step", {"path": str(step_path)})
    assert payload["body_ids"] == ["box"]
    assert payload["skipped_hidden"] == ["hidden-box"]

    stl_path = tmp_path / "visible_only.stl"
    payload = call(server, "export_stl", {"path": str(stl_path)})
    assert payload["body_ids"] == ["box"]
    assert payload["skipped_hidden"] == ["hidden-box"]

    # `body_ids` はスキーマから消えている(未知の引数として弾かれる=プロトコルエラー)。
    response = request(
        server, "tools/call", {"name": "export_step", "arguments": {"path": str(step_path), "body_ids": ["box"]}}
    )
    assert response["error"]["code"] == INVALID_PARAMS


def test_export_does_not_touch_the_operation_log(server: McpServer, box_session: Session, tmp_path: Path):
    before = len(box_session.document.log.entries)
    call(server, "export_stl", {"path": str(tmp_path / "x.stl")})
    assert len(box_session.document.log.entries) == before


def test_undo_through_mcp_undoes_the_ui_operation(box_session: Session):
    """AI用の別履歴を作っていない: WebSocket(人の操作)で積んだ操作がMCPの `undo` で取り消せる。"""
    box = box_session.document.bodies[0]
    server = McpServer(DocumentTools(box_session))

    # # 人の操作(アプリ層の経路)。材料に可視性は使えない(`set_visible` は操作ログに乗らず undo の対象外)。
    select_refs(server, [face_ref(box.id, 1)])
    box_session.extrude(5.0, mode="join")
    extruded = call(server, "describe_shape", {"body_id": box.id})["volume"]
    log_length = len(box_session.document.log.entries)

    payload = call(server, "undo")

    assert payload["verb"] == "undo"
    assert payload["undone"] == "extrude"
    assert call(server, "describe_shape", {"body_id": box.id})["volume"] != pytest.approx(extruded)
    assert len(box_session.document.log.entries) == log_length - 1
    assert payload["changed_bodies"] == [box.id]


def test_redo_through_mcp_replays_it(box_session: Session):
    box = box_session.document.bodies[0]
    server = McpServer(DocumentTools(box_session))
    select_refs(server, [face_ref(box.id, 1)])
    box_session.extrude(5.0, mode="join")
    extruded = call(server, "describe_shape", {"body_id": box.id})["volume"]
    call(server, "undo")

    payload = call(server, "redo")

    assert payload["verb"] == "redo" and payload["redone"] == "extrude"
    assert call(server, "describe_shape", {"body_id": box.id})["volume"] == pytest.approx(extruded)


def test_undo_with_nothing_to_undo_is_a_structured_failure(server: McpServer):
    """取り消せるものが無いことは**失敗として構造で返る**(黙って成功にしない)。"""
    error = call_error(server, "undo")
    assert error["code"] == "invalid_argument"


def test_describe_document_lists_sketches_and_their_entities(empty_session: Session):
    """`describe_document()` はボディ・スケッチ・パラメータの一覧。スケッチが見えないと `pattern_rectangular(entities=[...])` の引数をAIが作れない。"""
    server = McpServer(DocumentTools(empty_session))
    sketch_id = call(server, "sketch_create", {"base_plane": "XY"})["sketch_id"]
    entity_id = call(
        server, "add_circle", {"sketch_id": sketch_id, "center": [0, 0], "diameter": 20.0}
    )["entity_id"]

    payload = call(server, "describe_document")

    sketches = {sketch["sketch_id"]: sketch for sketch in payload["sketches"]}
    assert sketch_id in sketches
    assert sketches[sketch_id]["plane"]["name"] == "XY"
    entities = sketches[sketch_id]["entities"]
    assert [entity["entity_id"] for entity in entities] == [entity_id]
    assert entities[0]["kind"] == "circle"
    # # 解決済みの数値とパラメータ名の両方を出す(数値だけだと `set_parameter` の引数が作れず、名前だけだと今の値が分からない)。
    assert entities[0]["params"]["diameter"] == 20.0
    assert entities[0]["parameters"]["diameter"] in payload["parameters"]
    assert payload["parameters"][entities[0]["parameters"]["diameter"]] == 20.0


def test_region_counts_are_opt_in(empty_session: Session):
    """領域の数え上げは**計算**(splitter)なので既定では出さない。要るときだけ true。"""
    server = McpServer(DocumentTools(empty_session))
    sketch_id = call(server, "sketch_create", {"base_plane": "XY"})["sketch_id"]
    call(server, "add_circle", {"sketch_id": sketch_id, "center": [0, 0], "diameter": 20.0})

    assert "region_count" not in call(server, "describe_document")["sketches"][0]
    with_regions = call(server, "describe_document", {"include_regions": True})
    assert with_regions["sketches"][0]["region_count"] == 1


def test_every_mutating_tool_fires_exactly_one_event(empty_session: Session):
    """1ツール1イベント。verbごとに手で `_emit` する実装だと、新verbで忘れてもpytestは通りブラウザだけが古くなる。"""
    server = McpServer(DocumentTools(empty_session))
    seen: list = []
    empty_session.add_listener(seen.append)
    try:
        sketch_id = call(server, "sketch_create", {"base_plane": "XY"})["sketch_id"]
        assert len(seen) == 1, "sketch_create が発火していない/二重に発火した"
        call(server, "add_rectangle", {"sketch_id": sketch_id, "corner": [0, 0], "width": 30, "height": 30})
        assert len(seen) == 2
        select_refs(server, [{"sketch": sketch_id, "index": 1}])
        assert len(seen) == 3
        call(server, "extrude", {"distance": 10.0})
        assert len(seen) == 4
        call(server, "undo")
        assert len(seen) == 5
    finally:
        empty_session.remove_listener(seen.append)


def test_a_read_only_tool_fires_nothing(server: McpServer, box_session: Session):
    """問い合わせは発火しない(読んだだけで画面が描き直されるのは無駄な往復)。"""
    seen: list = []
    box_session.add_listener(seen.append)
    try:
        call(server, "describe_document")
        call(server, "describe_shape", {"body_id": box_session.document.bodies[0].id})
        call(server, "measure")
        call(server, "describe_predicates")
        call(server, "get_selection")
    finally:
        box_session.remove_listener(seen.append)
    assert seen == []


def test_a_failed_tool_fires_nothing(server: McpServer, box_session: Session):
    """失敗した操作でイベントを出さない(何も変わっていないのに画面を描き直させない)。"""
    seen: list = []
    box_session.add_listener(seen.append)
    try:
        call_error(server, "fillet", {"radius": 2.0})
        call_error(server, "set_visible", {"body_id": "ghost", "visible": False})
    finally:
        box_session.remove_listener(seen.append)
    assert seen == []


def test_a_verb_applied_through_mcp_reaches_the_browser(box_session: Session):
    """MCPで形を変えると、開いているブラウザへ版が変わったBodyだけが `delta` に新しい `geometry` つきで載る。"""
    import asyncio

    box = box_session.document.bodies[0]
    faces_before = kernel.face_count(box.shape)
    mcp = McpServer(DocumentTools(box_session))
    # # 選択は接続を作る前に済ませる(選択の変更もイベントを起こし、後だとどの配信に幾何の差分が載るかが実行順で変わる)。
    select_refs(mcp, [edge_ref(box.id, index) for index in vertical_edge_indices(box.shape)])

    payloads = pushed_by_mcp(box_session, lambda: call(mcp, "fillet", {"radius": 2.0}))
    assert len(payloads) > 1, "MCPで形を変えたのにWebSocketへ配信されていない"
    pushed = payloads[-1]
    assert pushed["type"] == "state"
    assert [body["body_id"] for body in pushed["delta"]["bodies"]] == [box.id]
    assert pushed["delta"]["bodies"][0]["geometry"] is not None
    assert pushed["delta"]["bodies"][0]["face_count"] > faces_before
    assert "applied" not in pushed, "出し手のいない変更に確認応答を付けている"
