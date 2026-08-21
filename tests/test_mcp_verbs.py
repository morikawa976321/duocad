"""F1契約: **v1の全verbをMCPツールとして公開する**(実装は F2)。

実装計画書§0の到達表は **AI列が 1/19** しか埋まっていない(`set_visible` だけ)。
F(F1+F2)の仕事はそれを全部埋めることであり、**このファイルがその「埋まった」の定義**である。

§0の合格条件(AI側):

> MCPツールとして呼べ／引数を**述語**または明示的な参照で与えられ／**結果と失敗が構造化して返る**

`tests/test_mcp_selection.py` が選択・述語の側を持ち、こちらが**操作(verb)と問い合わせ**の側を持つ。
契約の散文は `tests/README.md` の「F」節が正。

## このファイルが固定する4つ

1. **到達性** — `tools/list` の集合が**期待する28個とちょうど一致**する。新しいverbを足したときに
   MCPへ出し忘れるのを防ぐ(集合が一致「以上」ではなく「一致」なのはそのため)。
   各ツールが引数スキーマ(型・必須・説明)を持つこと。
2. **形は選択から、数値は引数から** — 面・辺・領域は**確定した選択**から取り、数値・名前・パスだけを
   引数で渡す。§5の `apply(verb, selection, args)` をverbごとのツールへ分解した形であり、
   これにより「提案のままでは適用できない」(A1の決定)が**全verbに自動的にかかる**。
   例外はBody(`body_id`)だけ —— Bodyは選択集合の項目ではない。
3. **構造化された結果** — verbツールは `verb`/`seq`/`changed_bodies`/`bodies`/`can_undo`/`can_redo`
   を必ず返す。自然文だけを返さない。**`changed_bodies` は `Session.body_revision` の前後差分**で
   導くこと(verbごとに数え上げると、新しいverbを足すたびに数え漏れる)。
4. **構造化された失敗** — 失敗はJSONで返す(`{"error": {"code","message","tool","exception"}}`)。
   例外のテキストをそのまま投げない。コードは6語彙に固定する。

**このタスク(F1)は契約だけを書き、実装は F2 が入れる。赤で終わるのが正常。**
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

from duocad import kernel, query
from duocad.document import Body
from duocad.mcp_server import DocumentTools, McpServer
from duocad.server import Server, Session

# JSON-RPC 2.0 の標準エラーコード。**実装から import しない**(test_mcp.py と同じ理由:
# importすると実装が変わった瞬間にテストも追従して緑になり、契約にならない)。
INVALID_PARAMS = -32602

BOX_X, BOX_Y, BOX_Z = 30.0, 30.0, 40.0

REPO_ROOT = Path(__file__).resolve().parent.parent
from models import MODELS  # テスト共通のSTEPの置き場(tests/models.py)
from models import SHELF  # noqa: F401


# --------------------------------------------------------------------------------------
# 公開する道具の全体像(**この表が到達表のAI列そのもの**)
# --------------------------------------------------------------------------------------
#: §0の到達表の19操作。ツール名は**操作ログのverb名と同じ綴り**にする —— AIが
#: `describe_document` のログを読んで次の操作を決めるとき、ログのverb名と呼べるツール名が
#: 違うと対応表を推測させることになる。
VERB_TOOLS = {
    # スケッチ
    "sketch_create",
    "add_line",
    "add_circle",
    "add_rectangle",
    "add_arc",
    # フィーチャ
    "extrude",
    "extrude_face",
    "fillet",
    "chamfer",
    "pattern_rectangular",
    "pattern_circular",
    # ボディ・パラメータ
    "set_visible",
    "make_editable_copy",
    "set_parameter",
    # 入出力
    "import_step",
    "export_step",
    "export_stl",
    # 履歴
    "undo",
    "redo",
}

#: 問い合わせ(§5「問い合わせ」)と、述語の語彙の自己記述。
QUERY_TOOLS = {"describe_document", "describe_shape", "measure", "describe_predicates"}

#: 選択(§5「選択(中心)」)。契約の本体は `tests/test_mcp_selection.py`。
SELECTION_TOOLS = {
    "get_selection",
    "propose_selection",
    "modify_selection",
    "commit_selection",
    "clear_selection",
}

#: 到達表の19操作には数えないが、**ログに載るverbと同じ綴りで呼べる**ために要るツール。
#:
#: 「STEPを読む」という1操作に verb が2つある(`import_step_editable` = 編集可能なボディまで
#: 作る通常の経路 / `import_step` = 参照ボディだけ)。`editable` 引数で1つのツールが
#: 打ち分ける形だと、**ログに出る `import_step_editable` を呼べるツールが存在しない**。
#: ツールを2つに割り、名前をverbに揃えている。
EXTRA_LOGGING_TOOLS = {"import_step_editable"}

EXPECTED_TOOLS = VERB_TOOLS | QUERY_TOOLS | SELECTION_TOOLS | EXTRA_LOGGING_TOOLS

#: 各ツールの引数(全体 / 必須)。**「増えていないこと」まで固定する** —— 引数がひとつ増えれば
#: AIから見た呼び方が変わるので、契約の一部である。
#:
#: ここに `face_index` / `edges` / `regions` が**無い**ことがこのファイルの主張の半分である
#: (形は選択から取る。モジュールdocstring 2)。
ARGUMENTS: dict = {
    # ---- 問い合わせ
    "describe_document": (
        {"include_references", "include_bbox", "include_regions"},
        set(),
    ),
    "describe_shape": ({"body_id"}, {"body_id"}),
    "measure": (set(), set()),
    "describe_predicates": (set(), set()),
    # ---- 選択
    "get_selection": (set(), set()),
    "propose_selection": ({"refs", "predicate"}, set()),
    "modify_selection": ({"add", "remove"}, set()),
    "commit_selection": (set(), set()),
    "clear_selection": (set(), set()),
    # ---- スケッチ
    "sketch_create": ({"base_plane"}, set()),
    "add_line": ({"sketch_id", "p0", "p1"}, {"sketch_id", "p0", "p1"}),
    "add_circle": (
        {"sketch_id", "center", "radius"},
        {"sketch_id", "center", "radius"},
    ),
    "add_rectangle": (
        {"sketch_id", "corner", "width", "height"},
        {"sketch_id", "corner", "width", "height"},
    ),
    "add_arc": (
        {"sketch_id", "center", "radius", "start_deg", "end_deg"},
        {"sketch_id", "center", "radius", "start_deg", "end_deg"},
    ),
    # ---- フィーチャ(形は選択から。数値だけが引数)
    "extrude": ({"distance", "mode", "body_id"}, {"distance"}),
    "extrude_face": ({"distance"}, {"distance"}),
    "fillet": ({"radius"}, {"radius"}),
    "chamfer": ({"distance"}, {"distance"}),
    "pattern_rectangular": (
        {"sketch_id", "entities", "dx", "dy", "nx", "ny"},
        {"sketch_id", "entities", "dx", "dy", "nx", "ny"},
    ),
    "pattern_circular": (
        {"sketch_id", "entities", "center", "count", "total_angle", "rotate_copies"},
        {"sketch_id", "entities", "center", "count"},
    ),
    # ---- ボディ・パラメータ
    "set_visible": ({"body_id", "visible"}, {"body_id", "visible"}),
    "make_editable_copy": ({"source_body_id", "name"}, set()),
    "set_parameter": ({"name", "value"}, {"name", "value"}),
    # ---- 入出力
    # STEP読み込みは**2ツール**。`import_step_editable` が通常の経路で、
    # `import_step` は参照ボディだけ。**引数で打ち分けない** —— 引数で分けると、
    # ログに載るverb名と呼べるツール名が食い違う(`EXTRA_LOGGING_TOOLS` の理由)。
    "import_step_editable": ({"path"}, {"path"}),
    "import_step": ({"path"}, {"path"}),
    "export_step": ({"path", "body_ids"}, {"path"}),
    "export_stl": ({"path", "deviation", "body_ids"}, {"path"}),
    # ---- 履歴
    "undo": (set(), set()),
    "redo": (set(), set()),
}

#: 操作ログに1行積むツール。**ツール名 == verb名**であること自体が契約(上記の理由)。
LOGGING_TOOLS = (VERB_TOOLS - {"export_step", "export_stl", "undo", "redo"}) | EXTRA_LOGGING_TOOLS

#: 失敗コードの語彙。**増やすときはここを直す**(AI側は未知のコードを扱えないので、
#: 実装が勝手に新しいコードを作れる状態にしない)。
ERROR_CODES = {
    "not_found",            # 存在しないBody / sketch / parameter(KeyError)
    "invalid_argument",     # 形・型・範囲が不正(ValueError)
    "selection_not_committed",  # 提案のまま verb を呼んだ(A1の門)
    "selection_empty",      # 確定選択が**丸ごと空**(要求した種別以前の問題)
    "selection_kind_mismatch",  # 空ではないが要求した種別が無い(2026-08-20に分離)
    "invalid_predicate",    # 述語が不正(有効なopの一覧を必ず添える)
    "internal",             # 上記以外(黙って握りつぶさないための受け皿)
}


# --------------------------------------------------------------------------------------
# 道具
# --------------------------------------------------------------------------------------
@pytest.fixture()
def empty_session(tmp_path: Path) -> Session:
    """STEPを読まない空のセッション(スケッチから作る経路の出発点)。"""
    return Session(None, 0.2)


@pytest.fixture()
def box_session(tmp_path: Path) -> Session:
    """30×30×40 の直方体を1つ持つセッション(面6・辺12・頂点8)。

    立方体にしないのは A1 と同じ理由 —— 3辺の長さが違えば、述語やフィレットの
    対象がたまたま一致して緑になる余地が減る。
    """
    made = Session(None, 0.2)
    made.document.add_body(
        Body(id="box", name="box", kind="solid", shape=BRepPrimAPI_MakeBox(BOX_X, BOX_Y, BOX_Z).Shape())
    )
    return made


@pytest.fixture()
def server(box_session: Session) -> McpServer:
    return McpServer(DocumentTools(box_session))


def request(server: McpServer, method: str, params: dict | None = None) -> dict:
    message: dict = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        message["params"] = params
    response = server.handle(message)
    assert response is not None, f"{method} はレスポンスを返さなければならない"
    return response


def tool_list(server: McpServer) -> list:
    return request(server, "tools/list")["result"]["tools"]


def call(server: McpServer, name: str, arguments: dict | None = None) -> dict:
    """成功を期待してツールを呼び、返ってきたJSONを辞書で返す。"""
    response = request(server, "tools/call", {"name": name, "arguments": arguments or {}})
    assert "error" not in response, response
    result = response["result"]
    assert result["isError"] is False, result["content"][0]["text"]
    return json.loads(result["content"][0]["text"])


def call_error(server: McpServer, name: str, arguments: dict | None = None) -> dict:
    """失敗を期待してツールを呼び、**構造化されたエラー本体**(`payload["error"]`)を返す。

    ここで `json.loads` に成功すること自体が契約である —— 例外のテキストをそのまま
    投げていれば(`"KeyError: no such body ..."`)ここで落ちる。
    """
    response = request(server, "tools/call", {"name": name, "arguments": arguments or {}})
    assert "error" not in response, f"実行時エラーをプロトコルエラーにしている: {response}"
    result = response["result"]
    assert result["isError"] is True, f"エラーになるはずが成功した: {result}"
    text = result["content"][0]["text"]
    try:
        payload = json.loads(text)
    except ValueError:
        pytest.fail(f"失敗が機械可読でない(例外のテキストを素で返している): {text!r}")
    assert "error" in payload, payload
    error = payload["error"]
    assert error["code"] in ERROR_CODES, f"未知のエラーコード: {error['code']!r}"
    assert error["tool"] == name
    assert error["message"].strip()
    return error


def select_refs(server: McpServer, refs: list) -> None:
    """明示的な参照で選択を置き、**確定させる**(verbへ渡せる状態にする)。"""
    call(server, "propose_selection", {"refs": refs})
    call(server, "commit_selection")


def face_ref(body_id: str, index: int) -> dict:
    return {"body": body_id, "kind": "face", "index": index}


def edge_ref(body_id: str, index: int) -> dict:
    return {"body": body_id, "kind": "edge", "index": index}


def top_face_index(shape) -> int:
    """外向き法線が +Z の面の1始まり添字(**決め打ちしない**。A1と同じ流儀)。"""
    for face in query.describe_faces(shape):
        if face["normal"][2] > 0.9:
            return face["index"]
    raise AssertionError("上向きの面が無い(テストの前提が崩れている)")


def vertical_edge_indices(shape) -> list:
    """Zに平行で長い辺の添字(直方体なら4本)。"""
    found = []
    for edge in query.describe_edges(shape):
        start, end = edge["start"], edge["end"]
        if abs(end[2] - start[2]) > 1.0 and abs(end[0] - start[0]) < 1e-6 and abs(end[1] - start[1]) < 1e-6:
            found.append(edge["index"])
    return found


# --------------------------------------------------------------------------------------
# 1. 到達性 — §0の到達表のAI列が全部埋まること(このタスクの本体・項番1〜3)
# --------------------------------------------------------------------------------------
def test_every_v1_verb_is_reachable_as_an_mcp_tool(server: McpServer):
    """**このファイルで最も重要な1本**(タスクファイル項番1)。

    §0の到達表のAI列にある全操作がMCPツールとして存在し、かつ**余計なものが無い**。
    集合の一致で見るのは、新しいverbを足したときにMCPへ出し忘れるのを防ぐため
    ——「19操作のうち18個ある」は実装計画書の観点では未達だが、部分集合の検査では緑になる。
    """
    names = {tool["name"] for tool in tool_list(server)}
    assert names == EXPECTED_TOOLS, {
        "足りない": sorted(EXPECTED_TOOLS - names),
        "余計": sorted(names - EXPECTED_TOOLS),
    }


def test_tool_names_match_the_verb_names_in_the_operation_log(server: McpServer):
    """ログに載るverbは、**同じ綴りのツール名**で呼べる。**両方向で見る。**

    AIは `describe_document` のログを読んで次の一手を決める。ログのverb名と呼べるツール名が
    違うと、対応表を推測させることになる(推測させた分だけ静かに間違う)。

    **片方向の検査(`LOGGING_TOOLS <= registered`)では、ツール名と綴りの違うverbが登録されても
    検出できない**(`import_step` が `editable` 引数で2つのverbを打ち分ける設計だと起こりうる)。
    集合の**一致**にすると、次の3つが全部この1行で落ちる:

    1. ツール名と綴りの違うverbを新設した
    2. verbを登録せずにツールだけ足した
    3. verbを消したのにツールが残った

    `LOGGING_TOOLS` の側に「ログに載らないツール」(`export_*` / `undo` / `redo`)を
    入れていないことも、この一致が成り立つ条件である。
    """
    from duocad.oplog import registered_verbs

    registered = set(registered_verbs())
    assert registered == LOGGING_TOOLS, {
        "ツールが無いverb": sorted(registered - LOGGING_TOOLS),
        "verbが無いツール": sorted(LOGGING_TOOLS - registered),
    }


def test_every_tool_has_a_description_and_an_object_schema(server: McpServer):
    """説明文と入力スキーマはAIがツールを選ぶ唯一の手がかり(項番2)。"""
    for tool in tool_list(server):
        assert tool["description"].strip(), f"{tool['name']} に説明が無い"
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False, f"{tool['name']} が未知の引数を許している"


def test_every_argument_is_typed_and_documented(server: McpServer):
    """引数ひとつひとつが**型と説明**を持つ(項番2の「型・必須・説明」)。

    型が無いと `_check_arguments` が検査できず、説明が無いとAIは何を入れるか推測する。
    型は文字列か、複数を許すなら文字列のリスト(`fillet.radius` は数値と配列の両方を取る)。
    """
    known = {"object", "array", "string", "number", "integer", "boolean"}
    for tool in tool_list(server):
        for name, spec in (tool["inputSchema"].get("properties") or {}).items():
            where = f"{tool['name']}.{name}"
            declared = spec.get("type")
            assert declared, f"{where} に型が無い"
            names = [declared] if isinstance(declared, str) else list(declared)
            assert names and set(names) <= known, f"{where} の型が不明: {declared!r}"
            assert (spec.get("description") or "").strip(), f"{where} に説明が無い"


def test_the_argument_of_every_tool_is_fixed(server: McpServer):
    """引数の顔ぶれと必須の別を**ツールごとに固定する**(`ARGUMENTS` の表が契約)。

    ここが「形は選択から取る」を実際に効かせている場所である —— `fillet` に `edges` が
    生えていたら、選択を経由しない裏口ができたということなので、この検査が落ちる。
    """
    tools = {tool["name"]: tool for tool in tool_list(server)}
    assert set(tools) == set(ARGUMENTS), "ARGUMENTS の表とツールの顔ぶれがずれている"
    for name, (properties, required) in ARGUMENTS.items():
        schema = tools[name]["inputSchema"]
        assert set(schema.get("properties") or {}) == properties, name
        assert set(schema.get("required") or []) == required, name


def test_no_verb_tool_takes_a_shape_argument(server: McpServer):
    """**形の引数(面・辺・領域)を持つverbツールは1つも無い**(モジュールdocstring 2)。

    `face_index=` や `edges=` を受ける裏口があると、A1が決めた「提案のままでは
    verb に渡せない」がその経路だけ素通りになる。上の表の検査と重なるが、
    **なぜその表なのか**を独立に固定しておく。
    """
    forbidden = {"face_index", "face", "edges", "edge_indices", "regions", "refs", "selection"}
    for tool in tool_list(server):
        if tool["name"] not in VERB_TOOLS:
            continue
        leaked = forbidden & set(tool["inputSchema"].get("properties") or {})
        assert not leaked, f"{tool['name']} が形を直接受けている: {sorted(leaked)}"


# --------------------------------------------------------------------------------------
# 2. スキーマ検証 — 黙って通さない(型)
# --------------------------------------------------------------------------------------
def _assert_rejects_argument(server: McpServer, name: str, arguments: dict, argument: str) -> None:
    """スキーマ違反は INVALID_PARAMS で、**どの引数が悪いかを名指しする**。

    名指しを要求するのは2つ理由がある: (a) AIが自力で直せる (b) このテストが
    「そんなツールは無い」でも同じ INVALID_PARAMS になるため、**名指しが無いと
    ツール未実装のまま緑になる**(F1の時点で偽の緑を作らない)。
    """
    response = request(server, "tools/call", {"name": name, "arguments": arguments})
    assert "error" in response, f"{name}.{argument} を素通しした: {response}"
    assert response["error"]["code"] == INVALID_PARAMS
    assert argument in response["error"]["message"], response["error"]["message"]


def test_numbers_are_not_accepted_as_strings(server: McpServer):
    """`radius: "3"` を通すと、コア側で `float("3")` が成功して**黙って動く**。

    スキーマが `number` と言っている以上、プロトコル層で弾く(実行時エラーではなく
    INVALID_PARAMS。AIが型を間違えたのはプロトコルの誤りである)。
    """
    _assert_rejects_argument(server, "fillet", {"radius": "3"}, "radius")


def test_booleans_are_not_accepted_as_numbers(server: McpServer):
    """Pythonでは `True` が `int` の一種なので、`radius: true` は素通りすると 1.0 になる。

    `visible: "false"` が真になるのと同じ型の事故(test_mcp.py 参照)を、数値側でも塞ぐ。
    """
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


# --------------------------------------------------------------------------------------
# 3. 構造化された結果 — 何が起きたかが返る(項番4)
# --------------------------------------------------------------------------------------
#: `bodies[]` の1要素が持つキー。**過不足なく固定する** —— 「面数だけ合っていれば緑」に
#: しておくと、名前や可視性を落とした実装が通り、AIは次の一手のために
#: `describe_document` をもう1往復することになる。
BODY_KEYS = {"body_id", "name", "kind", "visible", "face_count", "volume"}


def _assert_verb_envelope(payload: dict, verb: str) -> None:
    """verbツールの戻り値が持つべき最低限(モジュールdocstring 3)。"""
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
    """成功したら**変わったBody・新しい面数・操作のseq**が構造で返る(項番4)。

    自然文だけを返すと、AIは次の一手を文章から推測することになる。
    """
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
    """スケッチだけを触った操作は `changed_bodies` が空(嘘の変更を報告しない)。

    `changed_bodies` を**版の前後差分**から導けばこれは自動的に成立する。
    verbごとに「このverbはbodyを変える」と数え上げる実装だと、ここが静かに間違う。
    """
    server = McpServer(DocumentTools(empty_session))
    payload = call(server, "sketch_create", {"base_plane": "XY"})
    _assert_verb_envelope(payload, "sketch_create")
    assert payload["changed_bodies"] == []
    assert payload["sketch_id"]


def test_set_visible_still_reports_the_same_shape(server: McpServer, box_session: Session):
    """M4で決めた `set_visible` の戻り値(`previous` / `changed`)を壊さない。

    既存の `tests/test_mcp.py` が見ている契約であり、封筒を足すのは**追加**であって
    置き換えではない。
    """
    box = box_session.document.bodies[0]
    payload = call(server, "set_visible", {"body_id": box.id, "visible": False})
    _assert_verb_envelope(payload, "set_visible")
    assert payload["previous"] is True and payload["visible"] is False and payload["changed"] is True
    assert payload["changed_bodies"] == [box.id]


def test_every_sketch_verb_is_reachable_and_returns_its_id(empty_session: Session):
    """スケッチの5verbを**実際に1回ずつ成功させる**(集合とスキーマだけでは足りない)。

    到達性のテストは `tools/list` の顔ぶれしか見ないので、`descriptors()` に名前を書いて
    偽の封筒を返すだけでも緑になる。**コアへ届いていること**(ログのverbと図形の増加)と
    **作ったもののIDが返ること**(返さないとAIは次の呼び出しで指せない)まで見る。
    """
    server = McpServer(DocumentTools(empty_session))
    sketch_id = call(server, "sketch_create", {"base_plane": "XY"})["sketch_id"]

    created = {
        "add_line": call(
            server, "add_line", {"sketch_id": sketch_id, "p0": [0, 0], "p1": [30, 0]}
        ),
        "add_circle": call(
            server, "add_circle", {"sketch_id": sketch_id, "center": [60, 0], "radius": 10}
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


def test_both_patterns_are_reachable_and_return_their_id(empty_session: Session):
    """反復2verb(矩形・円形)も1回ずつ成功させる。`pattern_id` が返ること。

    **1つの図形を2つの反復で使い回せない**(コアが ValueError で拒む)ので、図形を2つ作る。
    """
    server = McpServer(DocumentTools(empty_session))
    sketch_id = call(server, "sketch_create", {"base_plane": "XY"})["sketch_id"]
    first = call(server, "add_circle", {"sketch_id": sketch_id, "center": [10, 0], "radius": 2})[
        "entity_id"
    ]
    second = call(server, "add_circle", {"sketch_id": sketch_id, "center": [0, 10], "radius": 2})[
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
    assert isinstance(rectangular["pattern_id"], str) and rectangular["pattern_id"]
    assert isinstance(circular["pattern_id"], str) and circular["pattern_id"]
    assert [pattern.id for pattern in empty_session.document.sketches[0].patterns] == [
        rectangular["pattern_id"],
        circular["pattern_id"],
    ]


# --------------------------------------------------------------------------------------
# 4. 構造化された失敗 — 理由が機械可読で返る(項番5)
# --------------------------------------------------------------------------------------
def test_a_missing_body_is_reported_as_not_found(server: McpServer):
    """`KeyError` は `not_found`。**例外のテキストをそのまま投げない**。"""
    error = call_error(server, "describe_shape", {"body_id": "ghost"})
    assert error["code"] == "not_found"
    assert error["exception"] == "KeyError"


def test_a_bad_value_is_reported_as_invalid_argument(server: McpServer, box_session: Session):
    """`ValueError` は `invalid_argument`(半径0は形を変えないので verb が拒む)。"""
    box = box_session.document.bodies[0]
    select_refs(server, [edge_ref(box.id, index) for index in vertical_edge_indices(box.shape)])
    error = call_error(server, "fillet", {"radius": 0.0})
    assert error["code"] == "invalid_argument"


def test_a_failed_verb_leaves_the_document_untouched(server: McpServer, box_session: Session):
    """失敗したら**文書もログも動かない**(§6予防6)。MCP経由でも同じ。"""
    box = box_session.document.bodies[0]
    shape_before = box.shape
    log_before = len(box_session.document.log.entries)

    select_refs(server, [edge_ref(box.id, index) for index in vertical_edge_indices(box.shape)])
    call_error(server, "fillet", {"radius": -1.0})

    assert box.shape is shape_before
    assert len(box_session.document.log.entries) == log_before


def test_applying_a_verb_with_nothing_selected_says_so(server: McpServer):
    """選択が空のまま verb を呼んだら `selection_empty`(何が要るかも返す)。

    ここを `invalid_argument` に潰すと、AIは「引数が悪い」と読んで数値をいじり始める。
    **次に何をすればよいかが変わる失敗は、別のコードにする。**

    選択に起因する失敗は `required_kind`(その verb が食う種別)と
    `selection`(今の中身の要約)を必ず載せる —— これがAIの次の一手を決める材料である。
    """
    error = call_error(server, "fillet", {"radius": 2.0})
    assert error["code"] == "selection_empty"
    assert error["required_kind"] == "edge"
    assert error["selection"] == {"count": 0, "origin": "user"}


def test_selecting_the_wrong_kind_has_its_own_code(server: McpServer, box_session: Session):
    """面を選んで `fillet` を呼ぶと `selection_kind_mismatch`(**種別の取り違え**)。

    **2026-08-20に本人が決定3の「コードは分けない」を覆した。** 2026-08-20の実装時点では
    「どちらも次の一手が『辺を選び直す』で同じだから `selection_empty` でよい。区別の材料は
    `selection.count` で足りる」としていたが、覆した理由は語彙の数ではなく**名前が嘘をつく**
    こと —— 面が1枚入っている選択に対して `selection_empty`(選択が空)と名乗ると、後で読む人を
    必ず一度混乱させる。加えてアプリ層が種別違いにだけ反応する余地(「この面の辺を選ぶ」等)を
    コードで分岐できる。

    `present_kinds` に**実際に確定している種別と件数**が入るので、AIは `get_selection` を
    呼び直さずに次の一手を決められる(矛盾指示でモデルが様子見を繰り返す暴走の予防。
    engine `2026-08-20-llm-agent-thinking-runaway-on-contradictory-task`)。
    """
    box = box_session.document.bodies[0]
    select_refs(server, [face_ref(box.id, 1)])
    error = call_error(server, "fillet", {"radius": 2.0})
    assert error["code"] == "selection_kind_mismatch"
    assert error["required_kind"] == "edge"
    assert error["present_kinds"] == {"face": 1}
    assert error["selection"]["count"] == 1
    assert "edge" in error["hint"]


def test_an_empty_selection_still_says_empty(server: McpServer, box_session: Session):
    """**対照**: 何も選んでいないときは今までどおり `selection_empty`。

    上のテストと対にしてある —— 2つを1つのコードへ潰す実装(あるいは逆に、空のときまで
    `selection_kind_mismatch` を返す実装)が入ったら、どちらかが必ず落ちる。
    `SelectionKindMismatch` は `SelectionEmpty` の**派生**なので、`_error_payload` の
    判定順を入れ替えるだけで種別違いが `selection_empty` に吸われる。そこを固定する。
    """
    error = call_error(server, "fillet", {"radius": 2.0})
    assert error["code"] == "selection_empty"
    assert error["required_kind"] == "edge"
    assert error["selection"]["count"] == 0
    assert "present_kinds" not in error


def test_the_error_says_which_tool_failed(server: McpServer):
    """`tool` を必ず載せる(AIが並行して複数呼ぶとき、どれの失敗か分からないと直せない)。"""
    error = call_error(server, "set_visible", {"body_id": "ghost", "visible": False})
    assert error["tool"] == "set_visible"
    assert error["code"] == "not_found"


def test_an_execution_error_is_not_a_protocol_error(server: McpServer):
    """実行時の失敗は `isError` で返す(JSON-RPCエラーにするとAIに理由が届かない)。

    M4で決めた区別を、増えたツールでも守る。
    """
    response = request(
        server, "tools/call", {"name": "describe_shape", "arguments": {"body_id": "ghost"}}
    )
    assert "error" not in response
    assert response["result"]["isError"] is True


# --------------------------------------------------------------------------------------
# 5. スケッチ → 領域 → 押し出し(MCPだけで形が作れること)
# --------------------------------------------------------------------------------------
def test_a_solid_can_be_built_from_scratch_through_mcp(empty_session: Session):
    """**空の文書からMCPだけで箱が作れる**(スケッチ4verb + `extrude` の到達性の実証)。

    領域は「描くもの」ではなく計算されるもの(§3.3)なので、AIは
    `propose_selection(refs=[{"sketch":.., "index":..}])` で領域を選んでから押し出す。
    """
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
    """`mode="cut"` が実際にコアへ届く(スキーマに書いてあるだけにしない)。

    `join` しか通っていないと、`mode` を読み飛ばす実装 —— つまり**切り取りのつもりが
    足し算になる** —— でも緑になる。本人が日常的に使う「円を線で分けて片側だけ押し出す」は
    この引数の上に乗っている(§1)。
    """
    server = McpServer(DocumentTools(empty_session))
    plate = call(server, "sketch_create", {"base_plane": "XY"})["sketch_id"]
    call(server, "add_rectangle", {"sketch_id": plate, "corner": [0, 0], "width": BOX_X, "height": BOX_Y})
    select_refs(server, [{"sketch": plate, "index": 1}])
    call(server, "extrude", {"distance": BOX_Z, "mode": "join"})

    hole = call(server, "sketch_create", {"base_plane": "XY"})["sketch_id"]
    call(server, "add_circle", {"sketch_id": hole, "center": [15, 15], "radius": 5})
    select_refs(server, [{"sketch": hole, "index": 1}])
    payload = call(server, "extrude", {"distance": BOX_Z, "mode": "cut"})

    _assert_verb_envelope(payload, "extrude")
    body = empty_session.document.bodies[0]
    import math

    assert query.volume(body.shape) == pytest.approx(
        BOX_X * BOX_Y * BOX_Z - math.pi * 25.0 * BOX_Z, rel=1e-3
    ), "cut が足し算になっている(または穴が開いていない)"
    assert payload["changed_bodies"] == [body.id]


def test_extrude_face_takes_its_face_from_the_selection(server: McpServer, box_session: Session):
    """`extrude_face` は**選択された面**を押す(添字を引数で受けない)。"""
    box = box_session.document.bodies[0]
    volume_before = query.volume(box.shape)
    select_refs(server, [face_ref(box.id, top_face_index(box.shape))])

    payload = call(server, "extrude_face", {"distance": 5.0})

    _assert_verb_envelope(payload, "extrude_face")
    assert payload["changed_bodies"] == [box.id]
    assert query.volume(box.shape) == pytest.approx(volume_before + BOX_X * BOX_Y * 5.0, rel=1e-6)


def test_extrude_face_refuses_an_ambiguous_selection(server: McpServer, box_session: Session):
    """面が2枚選ばれていたら**どちらを押すか黙って決めない**。

    「1枚だけ」を要求するのは `extrude_face` がコア側で1枚しか取らないためで、
    ここで先頭を勝手に選ぶと、AIから見て**指示と結果がずれる**唯一の経路になる。
    """
    box = box_session.document.bodies[0]
    select_refs(server, [face_ref(box.id, 1), face_ref(box.id, 2)])
    error = call_error(server, "extrude_face", {"distance": 5.0})
    assert error["code"] == "invalid_argument"
    assert error["required_kind"] == "face"
    assert error["selection"]["count"] == 2
    assert box_session.document.log.entries == [], "拒否したのに操作が積まれた"


def test_sketch_create_can_sit_on_a_selected_face(server: McpServer, box_session: Session):
    """`base_plane` を省いたら、**確定選択の面**にスケッチを貼る(§1 `sketch_create` の2つ目の引数形)。"""
    box = box_session.document.bodies[0]
    select_refs(server, [face_ref(box.id, top_face_index(box.shape))])
    payload = call(server, "sketch_create")
    _assert_verb_envelope(payload, "sketch_create")
    sketch = box_session.document.sketches[-1]
    assert sketch.plane["kind"] == "face"
    assert sketch.plane["body"] == box.id


def test_sketch_create_without_a_plane_or_a_selection_is_an_error(server: McpServer):
    """平面も選択も無ければ**推測しない**(既定でXYに貼ると静かに別の場所に図形が出る)。"""
    error = call_error(server, "sketch_create")
    assert error["code"] in {"selection_empty", "invalid_argument"}


# --------------------------------------------------------------------------------------
# 6. フィーチャ・パラメータ・ボディ
# --------------------------------------------------------------------------------------
def test_chamfer_reaches_the_core_verb(server: McpServer, box_session: Session):
    box = box_session.document.bodies[0]
    faces_before = kernel.face_count(box.shape)
    select_refs(server, [edge_ref(box.id, index) for index in vertical_edge_indices(box.shape)])

    payload = call(server, "chamfer", {"distance": 2.0})

    _assert_verb_envelope(payload, "chamfer")
    assert kernel.face_count(box.shape) > faces_before
    assert box_session.document.log.entries[-1].verb == "chamfer"


def test_fillet_accepts_a_radius_per_edge(server: McpServer, box_session: Session):
    """半径は辺ごとにも指定できる(コアの契約)。**順序は選択順**である。

    `edge_indices` が選択順を保つ(A1の決定)ので、半径のリストは選択した順に対応する。
    ここが昇順に並べ替えられていると、半径の割り当てが黙ってずれる。
    """
    box = box_session.document.bodies[0]
    edges = vertical_edge_indices(box.shape)
    select_refs(server, [edge_ref(box.id, index) for index in edges])

    call(server, "fillet", {"radius": [1.0, 2.0, 3.0, 4.0][: len(edges)]})

    entry = box_session.document.log.entries[-1]
    assert entry.verb == "fillet"
    assert entry.args["edges"] == edges, "選択順が保たれていない(半径の対応がずれる)"


def test_set_parameter_changes_the_shape_through_the_log(empty_session: Session):
    """`set_parameter` で寸法を編集すると形が追随する(§1の横断機能)。

    **どのパラメータ名がどの寸法か**は `describe_document` から辿れなければならない
    (下の `test_describe_document_lists_sketches_and_their_entities` が固定している)。
    名前が引けないと、AIは `d1` `d2` … を総当たりするしかない。
    """
    server = McpServer(DocumentTools(empty_session))
    sketch_id = call(server, "sketch_create", {"base_plane": "XY"})["sketch_id"]
    call(server, "add_circle", {"sketch_id": sketch_id, "center": [0, 0], "radius": 10.0})
    select_refs(server, [{"sketch": sketch_id, "index": 1}])
    call(server, "extrude", {"distance": 5.0})
    volume_before = query.volume(empty_session.document.bodies[0].shape)

    sketch = call(server, "describe_document")["sketches"][0]
    name = sketch["entities"][0]["parameters"]["radius"]
    payload = call(server, "set_parameter", {"name": name, "value": 20.0})

    _assert_verb_envelope(payload, "set_parameter")
    assert payload["name"] == name
    assert payload["value"] == 20.0 and payload["previous"] == 10.0
    assert payload["changed_bodies"] == [empty_session.document.bodies[0].id]
    assert query.volume(empty_session.document.bodies[0].shape) > volume_before


def test_set_parameter_reports_a_missing_name_as_not_found(empty_session: Session):
    server = McpServer(DocumentTools(empty_session))
    error = call_error(server, "set_parameter", {"name": "d99", "value": 1.0})
    assert error["code"] == "not_found"


def test_make_editable_copy_turns_a_reference_into_a_solid(tmp_path: Path):
    """読み込んだ参照ボディを編集できるようにする経路がAIから届く。

    **`import_step`(参照だけ)で読んだときの続き**。通常の読み込み
    (`import_step_editable`)は編集用ボディまで作るのでこの1手は要らないが、参照だけ読む
    経路を残した以上、その続きも届いていること。
    """
    if not SHELF.exists():
        pytest.skip(f"参照STEPが無い: {SHELF}")
    session = Session(None, 0.2)
    server = McpServer(DocumentTools(session))

    imported = call(server, "import_step", {"path": str(SHELF)})
    reference_id = imported["body_ids"][0]
    payload = call(server, "make_editable_copy", {"source_body_id": reference_id})

    _assert_verb_envelope(payload, "make_editable_copy")
    new_id = payload["body_id"]
    assert session.document.is_editable(new_id) is True
    assert payload["changed_bodies"] == [new_id]


def test_import_step_gives_editable_bodies_without_any_extra_call(tmp_path: Path):
    """**AIも「読んだら使える」**。`import_step_editable` は編集可能なボディまで作る。

    読み込みのツールが参照ボディだけを足す設計だと、AIは `make_editable_copy` をソリッドの
    数だけ打つ必要がある(PCケースなら27回)。**MCP1呼び出し・ログ1行**で編集できる状態まで
    行くのが今の契約。**ツール名はログに載るverb名と同じ綴り**であり、その一致は
    `test_tool_names_match_the_verb_names_in_the_operation_log` が両方向で固定する。
    """
    if not SHELF.exists():
        pytest.skip(f"参照STEPが無い: {SHELF}")
    session = Session(None, 0.2)
    server = McpServer(DocumentTools(session))

    payload = call(server, "import_step_editable", {"path": str(SHELF)})

    _assert_verb_envelope(payload, "import_step_editable")
    assert len(payload["body_ids"]) == 17, "17ソリッドぶんの編集可能なボディを返していない"
    assert all(
        session.document.is_editable(body_id) is True for body_id in payload["body_ids"]
    ), "読んだ直後に編集できないボディが混ざっている"
    assert len(payload["reference_ids"]) == 17, "原本(参照ボディ)のIDも返すこと"
    # hintは「そのまま編集できる」と言うこと。`make_editable_copy` に触れてよいのは
    # **要らないと打ち消す**文脈だけ(古い手順を知っているAIを止めるため)で、
    # 「続けるには make_editable_copy を呼ぶ」と促したら嘘になる。
    assert "そのまま編集できる" in payload["hint"], payload["hint"]
    assert "続けるには" not in payload["hint"], payload["hint"]
    assert len(session.document.log.entries) == 1, "MCP1呼び出しは操作ログ1行"


def test_import_step_can_still_add_reference_bodies_only(tmp_path: Path):
    """`import_step` は参照ボディだけを足し、続きの1手を `hint` で知らせる。

    **通常の経路は `import_step_editable`**(本人の指示は「読んだら使える」)だが、
    参照だけ欲しい経路は残す。
    """
    if not SHELF.exists():
        pytest.skip(f"参照STEPが無い: {SHELF}")
    session = Session(None, 0.2)
    server = McpServer(DocumentTools(session))

    payload = call(server, "import_step", {"path": str(SHELF)})

    _assert_verb_envelope(payload, "import_step")
    assert payload["body_ids"], "読み込んだBodyIdを返していない"
    assert all(
        session.document.is_editable(body_id) is False for body_id in payload["body_ids"]
    )
    assert "make_editable_copy" in payload["hint"]


def test_import_step_reports_a_missing_file_as_not_found(empty_session: Session):
    """無いファイルは `FileNotFoundError` で来る。**`internal` に落とさない**。

    AIが直せる誤り(パスの間違い)を「内部エラー」と名付けると、AIは直せるものを
    直さずに諦める。型→コードの対応表に `FileNotFoundError` を入れておくこと。
    """
    server = McpServer(DocumentTools(empty_session))
    error = call_error(server, "import_step", {"path": str(REPO_ROOT / "no-such-file.step")})
    assert error["code"] == "not_found"
    assert error["exception"] == "FileNotFoundError"


def test_export_writes_a_file_and_reports_where(server: McpServer, tmp_path: Path):
    """書き出しは**ログに載らない**(文書を変えないので verb ではない)。

    そのぶん「どこへ何バイト書いたか」を返す —— AIが自分で `ls` できない以上、
    戻り値が唯一の確認手段である。
    """
    step_path = tmp_path / "out.step"
    payload = call(server, "export_step", {"path": str(step_path)})
    assert Path(payload["path"]) == step_path
    assert payload["bytes"] == step_path.stat().st_size > 0

    stl_path = tmp_path / "out.stl"
    payload = call(server, "export_stl", {"path": str(stl_path), "deviation": 0.1})
    assert Path(payload["path"]) == stl_path
    assert payload["bytes"] == stl_path.stat().st_size > 0


def test_exporting_does_not_corrupt_the_stdio_channel(tmp_path: Path):
    """**OCCTのSTEP書き出しはC++側から標準出力へ統計を吐く**(実測)。

        ****        Statistics on Transfer (Write)                 ****
        Step File Name : ...(662 ents)  Write  Done

    stdioトランスポートは**stdoutをJSON-RPC専用**にしているので、これが1行でも混ざると
    MCPクライアントとの接続が壊れる(`duocad/mcp_server.py` のモジュールdocstring)。
    しかも**Python層の `redirect_stdout` では止まらない**(C++が実fdへ直接書く)ので、
    ツール実行の間だけ**fdごと** stderr へ振り替える必要がある。

    このテストは**実プロセスを起こして**確かめる —— 同一プロセス内で `DocumentTools` を
    叩くテストでは、この事故は原理的に検出できない(汚れるのは親のstdoutであって
    JSON-RPCの経路ではないため)。
    """
    import os
    import subprocess
    import sys

    if not SHELF.exists():
        pytest.skip(f"参照STEPが無い: {SHELF}")
    out_step = tmp_path / "roundtrip.step"
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "import_step", "arguments": {"path": str(SHELF)}},
        },
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "export_step", "arguments": {"path": str(out_step)}},
        },
        {"jsonrpc": "2.0", "id": 4, "method": "ping"},
    ]
    completed = subprocess.run(
        [
            sys.executable, "-m", "duocad.mcp_server",
            "--port", "18775", "--http-port", "18776",
            "--web-dist", str(REPO_ROOT / "web" / "does-not-exist"),
        ],
        input="\n".join(json.dumps(message) for message in messages) + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        cwd=str(REPO_ROOT),
        timeout=300,
    )
    assert completed.returncode == 0, completed.stderr

    ids = []
    for line in completed.stdout.splitlines():
        if not line.strip():
            continue
        try:
            ids.append(json.loads(line).get("id"))
        except ValueError:
            pytest.fail(f"stdoutにJSON-RPC以外の行が出ている(接続が壊れる): {line!r}")
    assert ids == [1, 2, 3, 4], "書き出しの後もJSON-RPCが続いていない"
    assert out_step.exists() and out_step.stat().st_size > 0


def test_a_browser_export_does_not_corrupt_the_stdio_channel(tmp_path: Path):
    """**人がブラウザから書き出しても** MCPのstdioが壊れない。

    上のテストは「MCPツールとしての書き出し」を守っているが、`duocad.mcp_server` の
    プロセスは**MCPとWebSocketの両方**を持っている。人がブラウザで書き出すと、
    同じプロセスのC++が同じ実fdへ統計を吐く —— その経路はツール実行の囲みの外にあり、
    JSON-RPCの行に `****** Statistics on Transfer (Write) ******` が割り込んで接続が壊れる。

    **交互操作は人とAIが同じプロセスを同時に触る形**なので、v1では普通に起きる。
    ここはWebSocketクライアントとして繋ぎ、`export_step` を人の経路で踏んでから、
    MCPの応答がまだJSONとして読めることを見る。
    """
    import os
    import subprocess
    import sys
    import textwrap

    out_step = tmp_path / "from-browser.step"
    # ブラウザの代わりにWebSocketで `export_step` を送る子プロセス(サーバ起動後に走る)
    poker = textwrap.dedent(
        f"""
        import asyncio, json, sys
        from websockets.asyncio.client import connect

        async def main():
            for _ in range(60):
                try:
                    async with connect("ws://127.0.0.1:18785") as socket:
                        await socket.recv()  # 初期状態
                        await socket.send(json.dumps({{"cmd": "sketch_create",
                            "plane": {{"kind": "base", "name": "XY", "provenance": None}}, "client_op": 1}}))
                        state = json.loads(await socket.recv())
                        sketch_id = state["applied"]["sketch_id"]
                        await socket.send(json.dumps({{"cmd": "add_rectangle", "sketch_id": sketch_id,
                            "corner": [0, 0], "width": 10, "height": 10, "client_op": 2}}))
                        await socket.recv()
                        # **押し出す領域は選択から取る**(コマンドの引数ではない)
                        await socket.send(json.dumps({{"cmd": "modify_selection",
                            "add": [{{"sketch": sketch_id, "index": 1}}], "client_op": 3}}))
                        await socket.recv()
                        await socket.send(json.dumps({{"cmd": "extrude",
                            "distance": 10, "mode": "join", "client_op": 4}}))
                        await socket.recv()
                        await socket.send(json.dumps({{"cmd": "export_step", "path": r"{out_step}"}}))
                        await socket.recv()
                        return 0
                except OSError:
                    await asyncio.sleep(0.5)
            return 1

        sys.exit(asyncio.run(main()))
        """
    )
    script = tmp_path / "poke.py"
    script.write_text(poker, encoding="utf-8")

    server_process = subprocess.Popen(
        [
            sys.executable, "-m", "duocad.mcp_server",
            "--port", "18785", "--http-port", "18786",
            "--web-dist", str(REPO_ROOT / "web" / "does-not-exist"),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        cwd=str(REPO_ROOT),
    )
    try:
        assert server_process.stdin is not None
        for message in (
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        ):
            server_process.stdin.write(json.dumps(message) + "\n")
        server_process.stdin.flush()

        poked = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(REPO_ROOT),
        )
        assert poked.returncode == 0, f"WebSocket経由の書き出しが通らない: {poked.stderr}"

        # 書き出しの**後で**MCPが生きているか(ここが壊れると接続ごと死ぬ)
        server_process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}) + "\n")
        server_process.stdin.flush()
        server_process.stdin.close()
        stdout, stderr = server_process.communicate(timeout=300)
    finally:
        if server_process.poll() is None:
            server_process.kill()

    assert out_step.exists() and out_step.stat().st_size > 0, stderr
    ids = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            ids.append(json.loads(line).get("id"))
        except ValueError:
            pytest.fail(f"stdoutにJSON-RPC以外の行が出ている(接続が壊れる): {line!r}")
    assert ids == [1, 2], "人の書き出しの後もJSON-RPCが続いていない"


def test_export_does_not_touch_the_operation_log(server: McpServer, box_session: Session, tmp_path: Path):
    before = len(box_session.document.log.entries)
    call(server, "export_stl", {"path": str(tmp_path / "x.stl")})
    assert len(box_session.document.log.entries) == before


# --------------------------------------------------------------------------------------
# 7. undo / redo — **UIから見た履歴と同じものを動かす**(項番11)
# --------------------------------------------------------------------------------------
def test_undo_through_mcp_undoes_the_ui_operation(box_session: Session):
    """**AI用の別履歴を作っていないこと**。

    WebSocket(=人の操作)で積んだ操作が、MCPの `undo` で取り消せる。ここが別履歴なら
    「人が押し出して、AIが取り消す」という交互操作の基本形が成立しない。
    """
    box = box_session.document.bodies[0]
    server = McpServer(DocumentTools(box_session))

    # 人の操作(アプリ層の経路)
    box_session.set_visible(box.id, False)
    assert box.visible is False
    log_length = len(box_session.document.log.entries)

    payload = call(server, "undo")

    assert payload["verb"] == "undo"
    assert payload["undone"] == "set_visible"
    assert box.visible is True
    assert len(box_session.document.log.entries) == log_length - 1
    assert payload["changed_bodies"] == [box.id]


def test_redo_through_mcp_replays_it(box_session: Session):
    box = box_session.document.bodies[0]
    server = McpServer(DocumentTools(box_session))
    box_session.set_visible(box.id, False)
    call(server, "undo")

    payload = call(server, "redo")

    assert payload["verb"] == "redo" and payload["redone"] == "set_visible"
    assert box.visible is False


def test_undo_with_nothing_to_undo_is_a_structured_failure(server: McpServer):
    """取り消せるものが無いことは**失敗として構造で返る**(黙って成功にしない)。"""
    error = call_error(server, "undo")
    assert error["code"] == "invalid_argument"


def test_undo_refuses_to_dismantle_the_document_floor(tmp_path: Path):
    """土台(STEPの読み込み・編集用ボディ)はMCP経由でも取り消せない。

    `Session.can_undo` が唯一の出どころであり、`describe_document` の答えと
    実際に通るかが一致する(食い違うと、AIは答えを信じて誤った判断をする)。
    """
    if not SHELF.exists():
        pytest.skip(f"参照STEPが無い: {SHELF}")
    session = Session(SHELF, 0.2)
    server = McpServer(DocumentTools(session))

    assert call(server, "describe_document")["can_undo"] is False
    error = call_error(server, "undo")
    assert error["code"] == "invalid_argument"


# --------------------------------------------------------------------------------------
# 8. 問い合わせ — describe_document がスケッチまで見せる(§5)
# --------------------------------------------------------------------------------------
def test_describe_document_lists_sketches_and_their_entities(empty_session: Session):
    """§5の `describe_document()` は「ボディ・**スケッチ**・パラメータの一覧」である。

    スケッチが見えないと、`pattern_rectangular(entities=[...])` の引数をAIが作れない
    ——自分が同じ会話の中で作った図形のIDしか知らないことになり、undo/redoや再接続で
    盲目になる。
    """
    server = McpServer(DocumentTools(empty_session))
    sketch_id = call(server, "sketch_create", {"base_plane": "XY"})["sketch_id"]
    entity_id = call(
        server, "add_circle", {"sketch_id": sketch_id, "center": [0, 0], "radius": 10.0}
    )["entity_id"]

    payload = call(server, "describe_document")

    sketches = {sketch["sketch_id"]: sketch for sketch in payload["sketches"]}
    assert sketch_id in sketches
    assert sketches[sketch_id]["plane"]["name"] == "XY"
    entities = sketches[sketch_id]["entities"]
    assert [entity["entity_id"] for entity in entities] == [entity_id]
    assert entities[0]["kind"] == "circle"
    # 解決済みの**数値**と、その数値を持つ**パラメータ名**の両方を出す。
    # 数値だけだと `set_parameter` の引数が作れず、名前だけだと今の値が分からない。
    assert entities[0]["params"]["radius"] == 10.0
    assert entities[0]["parameters"]["radius"] in payload["parameters"]
    assert payload["parameters"][entities[0]["parameters"]["radius"]] == 10.0


def test_region_counts_are_opt_in(empty_session: Session):
    """領域の数え上げは**計算**(splitter)なので既定では出さない。要るときだけ true。"""
    server = McpServer(DocumentTools(empty_session))
    sketch_id = call(server, "sketch_create", {"base_plane": "XY"})["sketch_id"]
    call(server, "add_circle", {"sketch_id": sketch_id, "center": [0, 0], "radius": 10.0})

    assert "region_count" not in call(server, "describe_document")["sketches"][0]
    with_regions = call(server, "describe_document", {"include_regions": True})
    assert with_regions["sketches"][0]["region_count"] == 1


# --------------------------------------------------------------------------------------
# 9. 上り方向 — MCPの変更が1回だけ発火し、開いている画面へ届く(項番10の操作側)
# --------------------------------------------------------------------------------------
def test_every_mutating_tool_fires_exactly_one_event(empty_session: Session):
    """**1ツール1イベント**。多いと画面が二重に描き直し、少ないと画面が古いまま残る。

    ここを verb ごとに手で `_emit` する実装だと、新しい verb を足したときに
    **忘れても誰も気づかない**(pytestは通り、ブラウザだけが黙って古くなる)。
    """
    server = McpServer(DocumentTools(empty_session))
    seen: list = []
    empty_session.add_listener(seen.append)
    try:
        sketch_id = call(server, "sketch_create", {"base_plane": "XY"})["sketch_id"]
        assert len(seen) == 1, "sketch_create が発火していない/二重に発火した"
        call(server, "add_rectangle", {"sketch_id": sketch_id, "corner": [0, 0], "width": 30, "height": 30})
        assert len(seen) == 2
        select_refs(server, [{"sketch": sketch_id, "index": 1}])  # 選択も発火する(2回)
        assert len(seen) == 4
        call(server, "extrude", {"distance": 10.0})
        assert len(seen) == 5
        call(server, "undo")
        assert len(seen) == 6
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
        call_error(server, "fillet", {"radius": 2.0})  # 選択が空
        call_error(server, "set_visible", {"body_id": "ghost", "visible": False})
    finally:
        box_session.remove_listener(seen.append)
    assert seen == []


def test_a_verb_applied_through_mcp_reaches_the_browser(box_session: Session):
    """**MCPで形を変えると、開いているブラウザの絵が変わる**(F2の実機確認のpytest版)。

    M4は `set_visible` でこれを実証した。増えた verb でも同じ経路に乗ることを、
    幾何が変わる verb (`fillet`)で確かめる —— 版が変わったBodyだけが `delta` に載り、
    そこに新しい `geometry` が入っていること。
    """
    import asyncio

    box = box_session.document.bodies[0]
    faces_before = kernel.face_count(box.shape)
    mcp = McpServer(DocumentTools(box_session))
    # **選択は接続を作る前に済ませる**。選択の変更もイベントを起こすので、接続後にやると
    # 「配信タスクは全部あとでまとめて走る」都合で、どの配信に幾何の差分が載るかが
    # 実行順で変わる(このテストが見たいのは verb の配信であって、その順序ではない)。
    select_refs(mcp, [edge_ref(box.id, index) for index in vertical_edge_indices(box.shape)])

    class FakeConnection:
        def __init__(self) -> None:
            self.payloads: list = []

        async def send(self, raw: str) -> None:
            self.payloads.append(json.loads(raw))

    async def scenario() -> list:
        server = Server(box_session)
        server.bind_loop(asyncio.get_running_loop())
        browser = FakeConnection()
        server.clients.add(browser)
        await server._push(browser, applied=None, only=browser)  # 接続直後の絵
        before = len(browser.payloads)

        call(mcp, "fillet", {"radius": 2.0})
        for _ in range(50):
            await asyncio.sleep(0)
            if len(browser.payloads) > before:
                break
        return browser.payloads

    payloads = asyncio.run(scenario())
    assert len(payloads) > 1, "MCPで形を変えたのにWebSocketへ配信されていない"
    pushed = payloads[-1]
    assert pushed["type"] == "state"
    assert [body["body_id"] for body in pushed["delta"]["bodies"]] == [box.id]
    assert pushed["delta"]["bodies"][0]["geometry"] is not None
    assert pushed["delta"]["bodies"][0]["face_count"] > faces_before
    assert "applied" not in pushed, "出し手のいない変更に確認応答を付けている"


# --------------------------------------------------------------------------------------
# 10. 境界(既存の規律を壊していないこと)
# --------------------------------------------------------------------------------------
def test_the_mcp_layer_holds_no_geometry_of_its_own():
    """MCPは**コアのもう一人のクライアント**であって、幾何を持たない。

    `tests/test_boundaries.py` が `duocad/` 全体を走査するが、ツールが19個に増える
    このタスクは境界を破る動機が最も強いので、名指しでも固定する。
    """
    from test_boundaries import DUOCAD_DIR, _imported_module_roots

    roots = _imported_module_roots(DUOCAD_DIR / "mcp_server.py")
    assert "OCP" not in roots
    assert "build123d" not in roots


def test_the_mcp_layer_does_not_reimplement_verbs():
    """verbの実体を書いていないこと —— `duocad.verbs` 等を**呼ぶ**だけであること。

    「ロジックをMCP層に書かない」は散文だと守られたかどうか分からないので、
    **コアのverb関数を実際に経由しているか**を、ログのverb名で見る他のテストと合わせて
    ここで宣言的に固定する: `mcp_server.py` は幾何モジュール(`kernel`)へ
    形を変える依頼をしない(読み取りの `face_count` 等は使ってよい)。
    """
    source = (Path(__file__).resolve().parent.parent / "duocad" / "mcp_server.py").read_text(
        encoding="utf-8"
    )
    for forbidden in ("kernel.fillet_edges", "kernel.chamfer_edges", "kernel.extrude_regions",
                      "kernel.extrude_face_on_shape", "kernel.write_step", "kernel.write_stl"):
        assert forbidden not in source, f"MCP層が幾何を直接叩いている: {forbidden}"
