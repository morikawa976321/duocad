"""UIのコマンドIDが、サーバが受け取れる名前に**実在するか**を検査する。"""
from __future__ import annotations

import ast
import functools
import importlib.util
import re
import sys

from duocad.paths import REPO_ROOT
from node_support import run_node


def _catalog():
    """`duocad/catalog.py` を**パッケージを経由せずに**読み込む。"""
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
#: 面・辺・領域はコアの選択から取る。ここに載る名前が表へ戻ってきたら、
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
    """サーバが受け取れるコマンド名を、**サーバを立てずに**集める。"""
    names = set(_catalog().websocket_command_names())
    tree = ast.parse((REPO_ROOT / "duocad" / "net" / "ws.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare) or not isinstance(node.left, ast.Name):
            continue
        if node.left.id != "command":
            continue
        for comparator in node.comparators:
            # `command == "x"` と `command in ("x", "y")` の両方(タプルの中の文字列も分岐の名前)。
            constants = comparator.elts if isinstance(comparator, ast.Tuple) else [comparator]
            for constant in constants:
                if isinstance(constant, ast.Constant) and isinstance(constant.value, str):
                    names.add(constant.value)
    return names


def _server_command_args() -> dict[str, tuple]:
    """4つの表の「コマンド名 -> 受け取る引数名」を宣言から組み立てる。"""
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
    """`Server._dispatch` が `message[...]` / `message.get(...)` で読む名前を集める。"""
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


#: 画面がソケットへ送る `{cmd: '...'}` の綴りを持ってよいファイル。**1つだけ**(issue #153「それ以外の経路では
#: 一切何もできない」)。ここ以外に `cmd:` が現れたら、ディスパッチャを迂回する送信口ができたということ。
SENDER_FILES = {"web/src/session.ts"}

#: `{cmd: '名前'}` / `{ cmd: "名前" }` / `{cmd:`名前`}`(空白と引用符の違いを許す)。
_CMD_LITERAL = re.compile(r"\bcmd\s*:\s*[\"'`]([a-z_]+)[\"'`]")
#: `session.ts` の送信メソッドへ名前を文字列で渡す間接経路(`sendCommand('export_step', …)` / `files.run('import_step', …)`)。
#: 引数が `string` なので型では守れない。
_INDIRECT_LITERAL = re.compile(r"\b(?:sendCommand|run)\(\s*[\"'`]([a-z_]+)[\"'`]")
#: ソケットそのもの。`session.ts` 以外に現れたら、ディスパッチャを迂回する送信口ができた。
_SOCKET_USE = re.compile(r"new WebSocket\(|\.send\(\s*JSON\.stringify")


def _web_sources():
    for path in (REPO_ROOT / "web" / "src").rglob("*"):
        if path.suffix in (".ts", ".svelte") and "generated" not in path.parts:
            yield path.relative_to(REPO_ROOT).as_posix(), path.read_text(encoding="utf-8")


def _code_lines(text: str):
    for line in text.splitlines():
        stripped = line.strip()
        if not (stripped.startswith("*") or stripped.startswith("//")):
            yield line


def _commands_sent_by_the_screen() -> dict[str, set[str]]:
    """`web/src` の中で `cmd: '名前'` を書いているファイル -> 名前の集合(コメント行は除く)。"""
    found: dict[str, set[str]] = {}
    for where, text in _web_sources():
        for line in _code_lines(text):
            for name in _CMD_LITERAL.findall(line):
                found.setdefault(where, set()).add(name)
    return found


def _commands_named_indirectly() -> dict[str, set[str]]:
    """`sendCommand('名前', …)` / `run('名前', …)` の形で名前を渡しているファイル -> 名前の集合。"""
    found: dict[str, set[str]] = {}
    for where, text in _web_sources():
        for line in _code_lines(text):
            for name in _INDIRECT_LITERAL.findall(line):
                found.setdefault(where, set()).add(name)
    return found


def _files_touching_the_socket() -> set[str]:
    return {where for where, text in _web_sources() if any(_SOCKET_USE.search(line) for line in _code_lines(text))}


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
    """宣言で「個別に捌く」と書いたコマンドは、**`_dispatch` に実際に分岐がある**。"""
    catalog = _catalog()
    source = (REPO_ROOT / "duocad" / "net" / "ws.py").read_text(encoding="utf-8")
    dispatch = source[source.index("async def _dispatch("):]
    missing = [
        command.name
        for command in catalog.COMMANDS
        if command.ws == "special" and f'"{command.name}"' not in dispatch
    ]
    assert not missing, f"`_dispatch` に分岐が無い(宣言だけある): {missing}"


def test_every_name_dispatched_by_hand_is_declared():
    """**`_dispatch` が分岐で捌く名前は、全部宣言にある**(サーバ側の門。`WS_COMMAND_NAMES` が宣言に無い名前を断る)。

    ここが落ちるのは「`_dispatch` に分岐を足したが `catalog.COMMANDS` に宣言しなかった」とき。宣言に無い名前は
    `WS_COMMAND_NAMES` の入口で断られるので、分岐があっても誰も呼べない。
    """
    declared = set(_catalog().websocket_command_names())
    by_hand = _server_command_names() - declared
    assert not by_hand, f"`_dispatch` に分岐があるのに宣言が無い: {sorted(by_hand)}"


def test_every_command_the_screen_sends_is_declared_and_sent_from_one_place():
    """**画面がソケットへ送る名前は全部宣言にあり、送る口は `session.ts` 1つ**(画面側の門)。

    ここが落ちるのは (1) `session.ts` に新しい `cmd` を足したが宣言していない、(2) `session.ts` 以外から
    直接ソケットへ送り始めた、のどちらか。どちらも「画面でできる操作は全部コマンド」(issue #153)の破れ。
    """
    sent = _commands_sent_by_the_screen()
    assert sent, "画面の送信を読めていない(`cmd: '...'` が1つも無い)"
    strays = {where: sorted(names) for where, names in sent.items() if where not in SENDER_FILES}
    assert not strays, f"`session.ts` 以外がソケットへ送っている: {strays}"
    sockets = _files_touching_the_socket()
    assert sockets == SENDER_FILES, f"ソケットを直接使うファイルが増えた/減った: {sorted(sockets)}"
    declared = set(_catalog().websocket_command_names())
    unknown = {name for names in sent.values() for name in names} - declared
    assert not unknown, f"画面が送る名前が宣言に無い: {sorted(unknown)}"
    indirect = _commands_named_indirectly()
    assert indirect, "間接経路の送信を読めていない(`sendCommand('…')` / `run('…')` が1つも無い)"
    unknown = {name for names in indirect.values() for name in names} - declared
    assert not unknown, f"画面が名前で送る(`sendCommand` / `run`)コマンドが宣言に無い: {sorted(unknown)}"


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
    # verb の引数は宣言から取り出すので `_dispatch` には現れない。現れるのは個別に捌く口の引数。
    assert "body_id" in _dispatch_message_keys()


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
    """`body_id` は名前として残っているだけ。**残ってよい場所を名指しで固定する**。"""
    takes_body_id = {
        command
        for command, names in _server_command_args().items()
        if "body_id" in names
    }
    assert takes_body_id == set(BODY_ID_IS_A_NAME_HERE), (
        "`body_id` を取るコマンドが変わった。対象集合でないなら "
        f"BODY_ID_IS_A_NAME_HERE に理由を書くこと: {sorted(takes_body_id)}"
    )


def test_ai_commands_are_declared_but_are_neither_tools_nor_verbs():
    """`ai_*`(G・アプリ内AIエージェントの配線)は宣言にある(ソケットの名前は宣言で尽くす)が、
    MCPの道具にも UI の CommandDef にも verb にもならない(`mcp=False` / `changes_document=False` / `protocol`)。
    """
    catalog = _catalog()
    ai_names = {name for name in _server_command_names() if name.startswith("ai_")}
    assert ai_names == {
        "ai_get_status", "ai_configure", "ai_set_thinking", "ai_set_api_key", "ai_prepare", "ai_send", "ai_cancel"
    }, (
        f"ai_* の顔ぶれが変わった(このテストを更新すること): {sorted(ai_names)}"
    )
    for name in sorted(ai_names):
        command = catalog.BY_NAME[name]
        assert command.category == "protocol" and not command.mcp and not command.changes_document, (
            f"{name} は protocol / mcp=False / changes_document=False で宣言する"
        )
    assert not ai_names & set(catalog.tool_names()), "ai_* が MCP の道具として出ている"
    # UIのコマンドID(`web/src/commands/*.ts`)にも現れない —— AIパネルは
    # `session.ts` の専用メソッドから直接送る(引数の集め方を持つ CommandDef の対象ではない)。
    assert not ai_names & set(_ui_command_ids()), (
        "ai_* が web/src/commands/*.ts の CommandDef として現れている"
        "(AIパネルは session.ts から直接送ること)"
    )


def test_which_commands_apply_without_enter():
    """落ちたら、`command.ts` の `needsEnter` か引数の書き方(`max` / `fromPoints`)が変わり、
    Enter無しで適用されるコマンドの顔ぶれが変わった(意図した変更なら docs/contracts/ui-sketch-input.md の `needsEnter` の項も直す)。"""
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


# プレビュー
#: ローカル方式(ブラウザだけで幽霊を描く)で幽霊を出す4verb。実際に幽霊が出ることは
#: `web/scripts/smoke-features.mjs` / `smoke-move.mjs` が実機で見る。
#:
#: **サーバの `previewable` は増やしていない** —— どれも変わるのが位置か図形で、
#: 投機実行しても「形が変わったBody」が返らない。
TRANSFORM_PREVIEW_VERBS = {
    "pattern_rectangular",
    "pattern_circular",
    "move_body",
    "move_sketch_entities",
}


def test_the_transform_verbs_are_not_added_to_speculative_execution():
    """ローカル方式に移したverbを**コアの投機実行へは足していない**。"""
    previewable = set(_catalog().previewable_verbs())
    leaked = sorted(TRANSFORM_PREVIEW_VERBS & previewable)
    assert not leaked, f"ローカル方式のverbが `previewable=True` になっている: {leaked}"
