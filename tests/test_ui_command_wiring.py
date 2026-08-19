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

## 何を検査しないか

**3層を1つの表から生成することはしない。** 3つは引数の形が違う ——
MCPは形を選択集合から取る(`fillet(radius)`)のに対し、WSは面や辺の添字を引数で受ける
(`fillet(edges, radius, body_id)`)。生成器を1つにすると、その違いを表現するための
分岐が生成器の中へ移るだけで、読む側の負担は減らない。
**「名前が実在すること」だけを機械で見る**のがここの役割である。
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


@pytest.mark.parametrize("command_id", ["import_step_editable", "make_editable_copy", "fillet"])
def test_the_names_that_bit_us_are_covered(command_id: str):
    """この検査が実際に効く名前を名指しで残す。

    `import_step_editable` は、コマンド名とログに載るverb名が食い違うバグの
    再発防止として効く。
    """
    assert command_id in _server_command_names()
    assert command_id in _ui_command_ids()
