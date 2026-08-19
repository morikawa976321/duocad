"""UIのコマンドIDが、サーバの表に**実在するか**を静的に検査する。

## なぜ要るか

同じverbの表面が3層にある:

1. `duocad/server.py` の `SKETCH_COMMANDS` / `FEATURE_COMMANDS` / `FILE_COMMANDS`(WebSocket)
2. `duocad/mcp_server.py` のツール(MCP。**`tests/test_mcp_verbs.py` が一致で固定済み**)
3. `web/src/commands/*.ts` の `CommandDef.id`(UIが引数を集めて送る先)

このうち **1と3の食い違いだけが誰にも見られていなかった**。TS側のIDを1文字打ち間違えても
`tsc` は通り(ただの文字列)、pytest も通り、**実ブラウザのsmokeを走らせて初めて**
`unknown command` で出る。この抜けを、`test_boundaries.py` と
同じ「ソースを静的に読む」やり方で塞ぐ。

## 対象集合はどの層でも引数ではない(2026-08-20)

かつてはWSだけが面・辺・領域を引数で受けており(`fillet(edges, radius, body_id)`)、
MCPの `fillet(radius)` と形が違った。**その差は無くなった** —— 対象集合は
どちらの層でもコアの確定選択から取る(`duocad/selection.py` の `require_confirmed` /
`require_edges`)ので、WSのコマンドに残る引数は数値・名前・パスだけである。
下の `test_no_websocket_command_takes_a_target_set` がそれを機械で固定する。

## 何を検査しないか

**3層を1つの表から生成することはしない。** 引数の形が揃ったので生成はできるように
なったが、3つの表は目的が違う —— MCPはAIに見せるスキーマ(説明文が主役)、WSは
「ソケットから呼べるものの許可リスト」、TSは引数の集め方(クリックの種別・入力欄のid)。
1つの表から生成すると、この3つを1つのデータ構造に押し込むための分岐が生成器へ移り、
読む側の負担は減らない。**名前の実在と、対象集合が引数に無いこと**を機械で見るのが
ここの役割である(2026-08-19までは「引数の形が違うから」を統合しない理由にしていたが、
その前提はこのタスクが消したので、理由を書き直した)。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
COMMANDS_DIR = REPO_ROOT / "web" / "src" / "commands"

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
}

#: `body_id` を引数に持ってよいコマンド -> なぜそれが対象集合ではないか。
#:
#: **Bodyは選択集合の項目ではない**(選べるのは面・辺・頂点・領域)ので、Bodyを指す
#: 引数は名前であって対象集合ではない。MCPの `extrude` も同じ `body_id` を持つ。
BODY_ID_IS_A_NAME_HERE = {
    "extrude": "どのBodyへ足す / から引くか(押し出す領域そのものは選択から取る)",
}


def _server_command_names() -> set[str]:
    """サーバが受け取れるコマンド名を、**実行せずに**読む。

    集めるのは2箇所:

    1. 3つの表(`SKETCH_COMMANDS` / `FEATURE_COMMANDS` / `FILE_COMMANDS`)のキー
    2. `Server._dispatch` の中の `command == "..."` 比較

    **2を落とすとこの検査は嘘になる** —— `extrude_face`(押し出し)と
    `open_document`(応答が状態の差し替えで、他と形が違う)は表に載らず個別に捌かれている。

    `import duocad.server` すると OCP まで読み込むので ast で読む
    (この検査は幾何が無い環境でも走ってほしい。`test_boundaries.py` と同じ方針)。
    """
    source = (REPO_ROOT / "duocad" / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    names: set[str] = set()
    wanted = {"SKETCH_COMMANDS", "FEATURE_COMMANDS", "FILE_COMMANDS"}
    for node in tree.body:
        if not isinstance(node, ast.AnnAssign) and not isinstance(node, ast.Assign):
            continue
        targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
        label = next(
            (t.id for t in targets if isinstance(t, ast.Name) and t.id in wanted), None
        )
        if label is None or not isinstance(node.value, ast.Dict):
            continue
        for key in node.value.keys:
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                names.add(key.value)
    # `command == "..."` の右辺(個別に捌いているもの)
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
    """4つの表から「コマンド名 -> 受け取る引数名」を**実行せずに**読む。

    値の形は `(メソッド名, (引数名, ...))` か `(メソッド名, (引数名, ...), verbか)`。
    表の名前が違っても2番目が引数のタプルであることは共通なので、そこだけを取る。
    """
    source = (REPO_ROOT / "duocad" / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted = {"PREVIEWABLE_VERBS", "SKETCH_COMMANDS", "FEATURE_COMMANDS", "FILE_COMMANDS"}
    found: dict[str, tuple] = {}
    for node in tree.body:
        if not isinstance(node, (ast.AnnAssign, ast.Assign)):
            continue
        targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
        if not any(isinstance(t, ast.Name) and t.id in wanted for t in targets):
            continue
        if not isinstance(node.value, ast.Dict):
            continue
        for key, value in zip(node.value.keys, node.value.values):
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                continue
            if not (isinstance(value, ast.Tuple) and len(value.elts) >= 2):
                continue
            args = value.elts[1]
            if not isinstance(args, ast.Tuple):
                continue
            names = tuple(
                item.value
                for item in args.elts
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            )
            found[key.value] = tuple(sorted(set(found.get(key.value, ()) + names)))
    return found


def _dispatch_message_keys() -> set[str]:
    """`Server._dispatch` が `message[...]` / `message.get(...)` で読む名前を集める。

    **表に載らないコマンドを取り逃さないため**(`extrude_face` と `open_document` は
    個別に捌かれている)。どのコマンドの引数かまでは分岐を追わないと分からないので、
    ここは「対象集合の名前が1つも出てこない」ことだけを見るのに使う。
    """
    source = (REPO_ROOT / "duocad" / "server.py").read_text(encoding="utf-8")
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


def _ui_command_ids() -> dict[str, str]:
    """`web/src/commands/*.ts` の `id: '...'` を集める(値 -> どのファイルか)。"""
    found: dict[str, str] = {}
    for path in sorted(COMMANDS_DIR.glob("*.ts")):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"^\s*id:\s*'([^']+)'", text, re.M):
            found[match.group(1)] = path.name
    return found


def test_server_command_tables_are_readable():
    """先に**走査そのものが効いている**ことを見る(0件なら下の検査は常に緑になる)。"""
    names = _server_command_names()
    assert len(names) >= 10, f"サーバの表を読めていない: {sorted(names)}"
    assert {"sketch_create", "extrude", "save_document"} <= names


def test_ui_command_ids_are_readable():
    ids = _ui_command_ids()
    assert len(ids) >= 10, f"UIのコマンド定義を読めていない: {sorted(ids)}"
    assert "extrude_face" in ids


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
    """**面・辺・領域を引数で受け取るWSコマンドは1つも無い**(2026-08-20)。

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


@pytest.mark.parametrize("command_id", ["import_step_editable", "make_editable_copy", "fillet"])
def test_the_names_that_bit_us_are_covered(command_id: str):
    """この検査が実際に効く名前を名指しで残す。

    `import_step_editable` は、コマンド名とログに載るverb名が食い違うバグの
    再発防止として効く。
    """
    assert command_id in _server_command_names()
    assert command_id in _ui_command_ids()
