"""UIのコマンドIDが、サーバが受け取れる名前に**実在するか**を検査する。

同じverbの表面が3層にある:

1. `duocad/server.py` の `SKETCH_COMMANDS` / `FEATURE_COMMANDS` / `FILE_COMMANDS`(WebSocket)
2. `duocad/mcp_server.py` のツール(MCP。**`tests/test_mcp_verbs.py` が一致で固定済み**)
3. `web/src/commands/*.ts` の `CommandDef.id`(UIが引数を集めて送る先)

**1と3の食い違いだけが誰にも見られていない**。TS側のIDを1文字打ち間違えても `tsc` は通り
(ただの文字列)、pytest も通り、**実ブラウザのsmokeを走らせて初めて** `unknown command` で
出る。この抜けを、TS側の `CommandDef` を Node で実際に読み込んで(`tests/eval_command_defs.mjs`)塞ぐ。

**1と2は `duocad/catalog.py` の `COMMANDS` からの導出**なので、この検査が見るのは「宣言と
3層目(TS)の食い違い」である。3層目を宣言から生成しないのは、TS側が持つのが**引数の集め方**
(どのクリックを待つか・入力欄のid・ボタンのid)であって宣言には無い情報だからで、名前の
一致だけをここで機械的に固定する。

**対象集合はどの層でも引数ではない** —— 面・辺・領域はコアの確定選択から取るので、WSの
コマンドに残る引数は数値・名前・パスだけである(`test_no_websocket_command_takes_a_target_set`)。
"""
from __future__ import annotations

import ast
import functools
import importlib.util
import re
import sys

import pytest

from duocad.paths import REPO_ROOT
from node_support import run_node
COMMANDS_DIR = REPO_ROOT / "web" / "src" / "commands"


def _catalog():
    """`duocad/catalog.py` を**パッケージを経由せずに**読み込む。

    `import duocad.catalog` にすると `duocad/__init__.py` が verb を登録するために
    `io_step` → `kernel` → OCP まで読み込む。この検査は幾何が無い環境でも走ってほしいので
    (`test_boundaries.py` と同じ方針)、ファイル1つだけをその場で読む。
    **宣言が純データであること**(相対importを持たないこと)がこの読み方の前提で、
    ここが壊れたらそれ自体が設計の逸脱である。
    """
    name = "duocad_catalog_static"
    if name in sys.modules:
        return sys.modules[name]
    path = REPO_ROOT / "duocad" / "catalog.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # **exec より先に登録する** —— `@dataclass` は `from __future__ import annotations` の
    # 下で型注釈を解決するのに `sys.modules[cls.__module__]` を引く。
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

#: TS側のIDのうち、サーバの表に**無くてよい**もの。
#:
#: - `select_in_sketch` … スケッチモードで「選ぶだけ」の道具。サーバへ何も送らない
EXEMPT_IDS = {"select_in_sketch"}

#: **WebSocketのコマンドが受け取ってはいけない引数名**(対象集合)。
#:
#: 面・辺・領域はコアの確定選択から取る。ここに載る名前が表へ戻ってきたら、
#: 「対象集合の指定方法」が再び2つに増えたということである。
TARGET_SET_ARGS = {
    "edges",
    "edge_indices",
    "regions",
    "region",
    "faces",
    "face",
    "face_index",
    "face_indices",
    "vertices",
    "vertex_index",
    # `plane` は**面参照がこの名前で検査を素通りしていた**ため足してある ——
    # WSの `sketch_create` だけが `plane={"kind":"face","body":...,"index":...}` を受けており、
    # 「対象集合を引数で渡さない」の唯一の例外がここに残っていたのに、上の名前の
    # どれにも当たらないので誰も落とさなかった(名前で見る検査の弱点そのもの)。
    "plane",
}

#: `body_id` を引数に持ってよいコマンド -> なぜそれが対象集合ではないか。
#:
#: **Bodyは選択集合の項目ではない**(選べるのは面・辺・頂点・領域)ので、Bodyを指す
#: 引数は名前であって対象集合ではない。MCPの `extrude` も同じ `body_id` を持つ。
BODY_ID_IS_A_NAME_HERE = {
    "extrude": "どのBodyへ足す / から引くか(押し出す領域そのものは選択から取る)",
}


def _server_command_names() -> set[str]:
    """サーバが受け取れるコマンド名を、**サーバを立てずに**集める。

    集めるのは2箇所:

    1. 宣言(`catalog.websocket_command_names()`)—— 表に載るものも、`_dispatch` が
       個別に捌くもの(`ws="special"`)も含む
    2. `Server._dispatch` の中の `command == "..."` 比較

    **2を落とさない** —— 宣言に無いコマンドを `_dispatch` へ直接生やしたときも、
    ここが拾えば「UIのIDがサーバに実在する」の判定は正しいままになる
    (その逆、宣言にあるのに `_dispatch` に無い方は
    `test_every_hand_dispatched_command_has_a_branch` が見る)。
    """
    names = set(_catalog().websocket_command_names())
    tree = ast.parse((REPO_ROOT / "duocad" / "net" / "ws.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare) or not isinstance(node.left, ast.Name):
            continue
        if node.left.id != "command":
            continue
        for comparator in node.comparators:
            if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str):
                names.add(comparator.value)
    return names


def _server_command_args() -> dict[str, tuple]:
    """4つの表の「コマンド名 -> 受け取る引数名」を宣言から組み立てる。

    表そのもの(`PREVIEWABLE_VERBS` / `SKETCH_COMMANDS` / `FEATURE_COMMANDS` /
    `FILE_COMMANDS`)は `duocad/server.py` が宣言から導出しているので、ここでは
    **同じ導出関数を呼ぶ**。同じコマンドが複数の表に出るときは引数を合併する
    (`extrude` は `preview` だけが `body_id` を受け取る)。
    """
    catalog = _catalog()
    tables = [
        catalog.previewable_verbs(),
        catalog.ws_table("sketch"),
        catalog.ws_table("feature"),
        catalog.file_table(),
    ]
    found: dict[str, tuple] = {}
    for table in tables:
        for command, value in table.items():
            found[command] = tuple(sorted(set(found.get(command, ()) + tuple(value[1]))))
    return found


def _dispatch_message_keys() -> set[str]:
    """`Server._dispatch` が `message[...]` / `message.get(...)` で読む名前を集める。

    **表に載らないコマンドを取り逃さないため**(`open_document` のように個別に捌かれる
    ものがある)。どのコマンドの引数かまでは分岐を追わないと分からないので、
    ここは「対象集合の名前が1つも出てこない」ことだけを見るのに使う。
    """
    source = (REPO_ROOT / "duocad" / "net" / "ws.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    keys: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
            if node.value.id == "message" and isinstance(node.slice, ast.Constant):
                if isinstance(node.slice.value, str):
                    keys.add(node.slice.value)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            target = node.func.value
            if node.func.attr == "get" and isinstance(target, ast.Name) and target.id == "message":
                for argument in node.args[:1]:
                    if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                        keys.add(argument.value)
    return keys


@functools.cache
def _ui_commands() -> tuple:
    """`web/src/commands/*.ts` の `CommandDef` を Node で読み込んだもの(`tests/eval_command_defs.mjs`)。"""
    return tuple(run_node(REPO_ROOT / "tests" / "eval_command_defs.mjs", "UIのコマンド定義", json_out=True))


def _ui_command_ids() -> dict[str, str]:
    """UIのコマンドID -> 定義しているファイル名。"""
    return {command["id"]: command["file"] for command in _ui_commands()}


def test_server_command_tables_are_readable():
    """先に**走査そのものが効いている**ことを見る(0件なら下の検査は常に緑になる)。"""
    names = _server_command_names()
    assert len(names) >= 10, f"サーバの表を読めていない: {sorted(names)}"
    assert {"sketch_create", "extrude", "save_document"} <= names


def test_every_hand_dispatched_command_has_a_branch():
    """宣言で「個別に捌く」と書いたコマンドは、**`_dispatch` に実際に分岐がある**。

    導出にしたことで生まれた抜け —— 宣言に1行足せばUI側の検査は通るようになるが、
    `ws="special"` は表に載らないので `_dispatch` に何も無くても誰も気づかない
    (実行時に `unknown command` で返るだけ)。ここで名前の実在を見る。
    """
    catalog = _catalog()
    source = (REPO_ROOT / "duocad" / "net" / "ws.py").read_text(encoding="utf-8")
    dispatch = source[source.index("async def _dispatch("):]
    missing = [
        command.name
        for command in catalog.COMMANDS
        if command.ws == "special" and f'"{command.name}"' not in dispatch
    ]
    assert not missing, f"`_dispatch` に分岐が無い(宣言だけある): {missing}"


def test_every_ui_command_declares_the_same_category_as_the_declaration():
    """落ちたら、UIの `category`(送信経路。`App.svelte` の `runCommand`)がコアの宣言とずれ、
    スケッチのverbがファイル操作として送られるような壊れ方をする。"""
    declared = {command.name: command.category for command in _catalog().COMMANDS}
    wrong: dict[str, str] = {}
    for command in _ui_commands():
        command_id, category = command["id"], command["category"]
        if category == "ui":
            if command_id not in EXEMPT_IDS:
                wrong[command_id] = "`ui` はサーバへ送らない道具だけ"
        elif command_id in declared and declared[command_id] != category:
            wrong[command_id] = f"宣言は {declared[command_id]!r} / UIは {category!r}"
    assert not wrong, f"UIの category が宣言と合っていない: {wrong}"


def test_ui_command_ids_are_readable():
    ids = _ui_command_ids()
    assert len(ids) >= 10, f"UIのコマンド定義を読めていない: {sorted(ids)}"
    assert "extrude" in ids


def test_every_ui_command_id_exists_in_a_server_table():
    """**UIが送れる名前は、全部サーバが受け取れる**。

    ここが落ちるのは「TS側のIDを変えた/足したのに、サーバの表へ入れ忘れた」とき。
    実ブラウザのsmokeでしか出なかった食い違いが、pytestで出るようになる。
    """
    server = _server_command_names()
    unknown = {
        command_id: where
        for command_id, where in _ui_command_ids().items()
        if command_id not in server and command_id not in EXEMPT_IDS
    }
    assert not unknown, (
        "UIのコマンドIDがサーバの表に無い(`duocad/server.py` の "
        f"SKETCH_COMMANDS / FEATURE_COMMANDS / FILE_COMMANDS を見ること): {unknown}"
    )


def test_the_argument_tables_are_readable():
    """先に**走査そのものが効いている**ことを見る(0件なら下の2本は常に緑になる)。"""
    args = _server_command_args()
    assert {"fillet", "extrude", "add_line", "export_stl"} <= set(args)
    assert args["add_line"] == ("p0", "p1", "sketch_id")
    assert "distance" in _dispatch_message_keys()


def test_no_websocket_command_takes_a_target_set():
    """**面・辺・領域を引数で受け取るWSコマンドは1つも無い**。

    ここが落ちるのは「対象集合をまた引数で渡し始めた」とき。同じ変換が
    ブラウザとコアの2箇所に生まれ、片方だけ直したときに**指した辺と違う辺が丸まる**
    (MCP側は選択から取り続けるので、AIと人で結果が食い違う形で出る)。
    """
    offenders = {
        command: sorted(set(names) & TARGET_SET_ARGS)
        for command, names in _server_command_args().items()
        if set(names) & TARGET_SET_ARGS
    }
    assert not offenders, f"対象集合を引数で受けているコマンド: {offenders}"

    strays = _dispatch_message_keys() & TARGET_SET_ARGS
    assert not strays, f"`_dispatch` が対象集合を読んでいる: {sorted(strays)}"


def test_body_id_survives_only_where_it_is_not_a_target_set():
    """`body_id` は名前として残っているだけ。**残ってよい場所を名指しで固定する**。

    Bodyは選択集合の項目ではない(選べるのは面・辺・頂点・領域)ので、
    「どのBodyへ足すか」は対象集合ではなく引数でよい。ただし `fillet` の対象Bodyのように
    **選択が持っている情報を引数でも受ける**形へ戻ると、2つの真実源が復活する。
    """
    takes_body_id = {
        command
        for command, names in _server_command_args().items()
        if "body_id" in names
    }
    assert takes_body_id == set(BODY_ID_IS_A_NAME_HERE), (
        "`body_id` を取るコマンドが変わった。対象集合でないなら "
        f"BODY_ID_IS_A_NAME_HERE に理由を書くこと: {sorted(takes_body_id)}"
    )


def test_ai_commands_are_not_verbs():
    """`ai_*`(G・アプリ内AIエージェントの配線)はWebSocketから踏めるが、
    「MCPツール名==WSコマンド名==UIコマンドID==verb名」の一致規律の対象集合には入らない。

    `ai_get_status` / `ai_configure` / `ai_send` / `ai_cancel` は文書を直接は変えない
    (操作ログに載らずundoの単位でもない)。**この検査はその除外を明示的に固定する**
    (`_server_command_names()` はソースを機械的に読むだけなので、宣言に無い名前が
    無言で増減しても他の検査は気づかない)。
    """
    ai_names = {name for name in _server_command_names() if name.startswith("ai_")}
    assert ai_names == {"ai_get_status", "ai_configure", "ai_send", "ai_cancel"}, (
        f"ai_* の顔ぶれが変わった(このテストを更新すること): {sorted(ai_names)}"
    )
    declared = {command.name for command in _catalog().COMMANDS}
    assert not ai_names & declared, (
        f"ai_* は verb ではないので catalog.COMMANDS に入れない(混入: {sorted(ai_names & declared)})"
    )
    # UIのコマンドID(`web/src/commands/*.ts`)にも現れない —— AIパネルは
    # `session.ts` の専用メソッドから直接送る(引数の集め方を持つ CommandDef の対象ではない)。
    assert not ai_names & set(_ui_command_ids()), (
        "ai_* が web/src/commands/*.ts の CommandDef として現れている"
        "(AIパネルは session.ts から直接送ること)"
    )


# --------------------------------------------------------------------------------------
# Enter要否(即適用)
# --------------------------------------------------------------------------------------
def test_which_commands_apply_without_enter():
    """落ちたら、`command.ts` の `needsEnter` か引数の書き方(`max` / `fromPoints`)が変わり、
    Enter無しで適用されるコマンドの顔ぶれが変わった(意図した変更なら README「操作の型」も直す)。"""
    immediate = sorted(command["id"] for command in _ui_commands() if not command["needsEnter"])
    assert immediate == [
        "add_arc",
        "add_circle",
        "add_line",
        "add_rectangle",
        "measure",
        "select_in_sketch",
        "sketch_create",
    ], f"即適用になるコマンドの顔ぶれが変わった: {immediate}"


def test_the_immediate_apply_fires_only_on_the_users_own_click():
    """即適用の引き金は**この画面のクリック**であって、選択集合の中身ではない。

    選択集合は**AIも `modify_selection` で書き換えられる**ので、「対象が1つ揃った」だけで
    発火させると、**人が何も指していないのに文書が変わる**(実測: 直す前のビルドで
    `smoke-mcp.mjs` が部品 27 -> 28 を踏んだ)。配線は3段あり、1つでも外れると穴が開くので、
    ブラウザを立てなくても気づけるようにここで固定する:

    1. `App.svelte` が**自分が出した**選択変更だけを数える(`myClientOps` で照合)
    2. その数を `CommandHost` へ渡している
    3. `CommandHost` の即適用は**その数**を見ている(選択の印 `selectionPrint` ではない)

    実挙動は `smoke-mcp.mjs`(AIが選択を書き換えても発火しない/人のクリックでは発火する)。
    """
    app = (COMMANDS_DIR.parent / "App.svelte").read_text(encoding="utf-8")
    host = (COMMANDS_DIR / "CommandHost.svelte").read_text(encoding="utf-8")

    # 1. 数える場所が「自分が出した操作か」の判定の中にある
    increment = re.search(r"^\s*selfSelections \+= 1", app, re.M)
    assert increment is not None, "App.svelte が選択変更を数えていない(即適用の引き金が無い)"
    guard = app[:increment.start()].splitlines()[-3:]
    assert any("myClientOps" in line for line in guard), (
        "選択変更の数え上げが `myClientOps` の照合の中にない —— "
        f"AIが出した選択変更まで数えると即適用が誤発火する: {guard}"
    )

    # 2. コマンド層へ渡している
    assert re.search(r"^\s*\{selfSelections\}\s*$", app, re.M), (
        "App.svelte が CommandHost へ selfSelections を渡していない"
    )

    # 3. 即適用の $effect が見ているのはその数である
    anchor = host.find("armedSelections === null")
    assert anchor > 0, "CommandHost に即適用の待ち受け(armedSelections)が無い"
    start = host.rfind("$effect(() => {", 0, anchor)
    end = host.find("\n  })", anchor)
    assert start > 0 and end > start, "即適用の $effect を切り出せない(検査の側が古い)"
    effect = host[start:end]
    assert "selfSelections" in effect, (
        "即適用の $effect が selfSelections を見ていない(何を引き金にしているか要確認)"
    )
    assert "selectionPrint" not in effect, (
        "即適用の $effect が選択の印を引き金にしている —— "
        "AIの modify_selection でも走るので、人のクリックの数で判定すること"
    )


# --------------------------------------------------------------------------------------
# プレビュー —— 「`preview` の値を変えたのに幽霊を描く配線が無い」を塞ぐ
# --------------------------------------------------------------------------------------
#: ローカル方式(ブラウザだけで幽霊を描く)で幽霊を出す4verb。
#:
#: **サーバの `previewable` は増やしていない** —— どれも変わるのが位置か図形で、
#: 投機実行しても「形が変わったBody」が返らない。
TRANSFORM_PREVIEW_VERBS = {
    "pattern_rectangular",
    "pattern_circular",
    "move_body",
    "move_sketch_entities",
}


def _ui_command_previews() -> dict[str, str]:
    """UIのコマンドID -> `preview`。"""
    return {command["id"]: command["preview"] for command in _ui_commands()}


def _local_ghost_routes() -> dict[str, str]:
    """**ローカル幽霊を実際に描ける verb** を「ID -> どの経路か」で集める。

    経路は3つ(`App.svelte` の `showLocalGhost` のdocstringが正):

    a. `entityKinds.ts` の表の `verb`(これから打つ図形。`ghostEntity`)
    b. `entityKinds.ts` の `ghostCopies`(既にある図形の反復・平行移動)
    c. `App.svelte` の `showLocalGhost`(既にあるボディの平行移動)

    **`preview: 'local'` と書くだけでは幽霊は出ない**。宣言と配線がずれると、画面は
    静かに何も出さないまま型検査もsmokeの既存項目も通る —— そこをここで塞ぐ。
    """
    routes: dict[str, str] = {}
    kinds = (REPO_ROOT / "web" / "src" / "entityKinds.ts").read_text(encoding="utf-8")
    copies_at = kinds.index("export function ghostCopies(")
    for verb in re.findall(r"^\s*verb:\s*'([^']+)'", kinds[:copies_at], re.M):
        routes[verb] = "ghostEntity"
    for verb in re.findall(r"verb === '([^']+)'", kinds[copies_at:]):
        routes[verb] = "ghostCopies"

    app = (REPO_ROOT / "web" / "src" / "App.svelte").read_text(encoding="utf-8")
    start = app.index("function showLocalGhost(")
    end = app.index("function requestPreview(", start)
    consts = {name: command["id"] for command in _ui_commands() for name in command["exports"]}
    for name in re.findall(r"\b([A-Z][A-Z0-9_]*)\.id\b", app[start:end]):
        if name in consts:
            routes[consts[name]] = "showLocalGhost"
    return routes


def test_ui_preview_modes_are_readable():
    """先に**走査そのものが効いている**ことを見る(空なら下の検査は常に緑になる)。"""
    previews = _ui_command_previews()
    assert previews.get("extrude") == "server", f"preview を読めていない: {previews}"
    assert previews.get("add_line") == "local", f"preview を読めていない: {previews}"
    routes = _local_ghost_routes()
    assert routes.get("add_line") == "ghostEntity", f"幽霊の経路を読めていない: {routes}"


def test_the_four_transform_verbs_declare_a_local_preview():
    """反復2つと移動2つは `preview: 'local'` である。"""
    previews = _ui_command_previews()
    wrong = {
        verb: previews.get(verb, "<宣言が無い>")
        for verb in TRANSFORM_PREVIEW_VERBS
        if previews.get(verb) != "local"
    }
    assert not wrong, f"ローカル方式のプレビューになっていない: {wrong}"


def test_every_local_preview_verb_can_actually_draw_a_ghost():
    """**`preview: 'local'` と書いたverbは、幽霊を描く経路を必ず持つ**。

    値だけ変えて配線が空だと、起動しても画面には何も出ないまま `tsc` も既存のsmokeも
    通ってしまう(**症状が「無言」なので実機で触るまで気づかない**)。
    """
    previews = _ui_command_previews()
    routes = _local_ghost_routes()
    unwired = sorted(
        verb for verb, mode in previews.items() if mode == "local" and verb not in routes
    )
    assert not unwired, (
        f"`preview: 'local'` だが幽霊を描く経路が無い: {unwired} "
        f"(経路がある verb: {sorted(routes)})"
    )


def test_the_transform_verbs_are_not_added_to_speculative_execution():
    """ローカル方式に移したverbを**コアの投機実行へは足していない**。

    足すと「Escで中断しても文書が一切変わらない」が、`Session.preview` の巻き戻しに
    依存する話になる。ローカル方式はそもそも送らないので、その依存が要らない。
    """
    previewable = set(_catalog().previewable_verbs())
    leaked = sorted(TRANSFORM_PREVIEW_VERBS & previewable)
    assert not leaked, f"ローカル方式のverbが `previewable=True` になっている: {leaked}"


def _app_function_body(name: str) -> str:
    """`App.svelte` の `function <name>(` から**次のトップレベルの関数まで**を切り出す。"""
    app = (COMMANDS_DIR.parent / "App.svelte").read_text(encoding="utf-8")
    start = app.index(f"function {name}(")
    tail = app[start:]
    following = re.search(r"\n  (?:function|const|async function) ", tail[10:])
    return tail[: following.start() + 10] if following else tail


@pytest.mark.parametrize("handler", ["onPick", "onSketchPick"])
def test_the_direct_pick_is_read_before_the_selection_is_edited(handler: str) -> None:
    """ここが落ちたら、**鏡映軸や回転軸に指したものが対象の選択へ入る**。

    コアの `require_bodies` / `require_sketch_entities` は選択から対象を逆引きするので、
    基準に使った辺・面・線分が選択に混ざると、その部品まで一緒に回る/複製される。
    """
    body = _app_function_body(handler)
    pick = body.index("wantsPick()")
    edit = body.index("editSelection(")
    assert pick < edit, (
        f"{handler}: 直接ピックの分岐が `editSelection` より後にある"
        "(指したものが共有選択へ入る)"
    )


@pytest.mark.parametrize("handler", ["onPick", "onSketchPick"])
def test_a_modified_click_never_goes_to_the_direct_pick(handler: str) -> None:
    """ここが落ちたら、**基準を待っている間に対象を選び直す手が無くなる**。

    必須のピックは対象が揃った時点で自動的に構えるので、素のクリックを全部ピックが
    食う。修飾キー(Shift / Ctrl)付きだけは今までどおり選択の編集へ通す。
    """
    body = _app_function_body(handler)
    guard = body[: body.index("wantsPick()")]
    assert "event.mode === 'replace'" in guard.split("if (")[-1], (
        f"{handler}: 直接ピックの分岐が修飾キー無しのクリックに限定されていない"
    )
