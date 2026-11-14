"""文書に**意図的に残した列挙**が、コード側の正と一致していることを機械で見る。

1. **走査そのものが効いていることを先に見る**(見出しや表の形を変えたら「読めなかった」で落ちる)
2. **集合を両方向で比べる**(足りない側と余計な側の両方)
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from duocad.mcp_server import DocumentTools, McpServer
from duocad.session import Session

from duocad.paths import REPO_ROOT
MCP_MD = REPO_ROOT / "MCP.md"
MCP_SERVER_PY = REPO_ROOT / "duocad" / "mcp_server.py"


def _section(path: Path, heading: str) -> str:
    """`heading` の行から、**同じか浅い見出し**の手前までを切り出す。

    見出しが消えた/改名された場合はここで落ちる —— 空文字を返すと下の集合比較が
    「両方空で一致」になり、[12-6] と同じ静かな失効になる。
    """
    text = path.read_text(encoding="utf-8")
    level = len(heading) - len(heading.lstrip("#"))
    lines = text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == heading)
    except StopIteration:
        raise AssertionError(
            f"{path.name} に見出し {heading!r} が無い —— 改名したなら "
            f"{Path(__file__).name} の参照も直すこと(見出しは検査の掛かり先)"
        ) from None
    fenced = False
    for offset, line in enumerate(lines[start + 1:], start=start + 1):
        stripped = line.strip()
        if stripped.startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        if stripped.startswith("#") and len(stripped) - len(stripped.lstrip("#")) <= level:
            return "\n".join(lines[start:offset])
    return "\n".join(lines[start:])


def _table_rows(section: str) -> list[list[str]]:
    """節の中のマークダウン表を行のリストで返す(ヘッダ行と区切り行は落とす)。"""
    rows = []
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if all(set(cell) <= set("-: ") for cell in cells):
            rows = []          # 区切り行 —— ここまでがヘッダなので捨てる
            continue
        rows.append(cells)
    return rows


def _published_tool_names() -> set[str]:
    server = McpServer(DocumentTools(Session(0.2)))
    response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    return {tool["name"] for tool in response["result"]["tools"]}


def _documented_tool_names() -> set[str]:
    """`MCP.md`「公開ツール」表のツール名。"""
    names: set[str] = set()
    for row in _table_rows(_section(MCP_MD, "## 公開ツール")):
        names.update(re.findall(r"`([a-z][a-z0-9_]*)`", row[-1].split("——")[0]))
    return names


def test_the_mcp_tool_table_is_readable():
    """先に**走査そのものが効いている**ことを見る(0件なら下の検査は常に緑になる)。"""
    documented = _documented_tool_names()
    assert len(documented) >= 20, f"MCP.md の公開ツール表を読めていない: {sorted(documented)}"


def test_the_mcp_tool_table_matches_the_tools_that_are_actually_published():
    """`MCP.md`「公開ツール」表と `tools/list` の顔ぶれが一致する。

    これが落ちたら、**AIが読む一覧と実際に呼べる口がずれている**(無い道具を勧める/
    ある道具を隠す)。ツールの顔ぶれの一致はここ1か所だけで見る。
    """
    documented = _documented_tool_names()
    published = _published_tool_names()
    assert documented == published, (
        "MCP.md「公開ツール」表が tools/list と食い違う"
        "(意図した変更なら表を更新すること): "
        f"表に無い={sorted(published - documented)} / 実在しない={sorted(documented - published)}"
    )


def _error_codes_from_source() -> set[str]:
    """`duocad/mcp_server.py` が実際に返しうる `code` の語彙をソースから読む。"""
    codes = set()
    loop_tree = ast.parse((REPO_ROOT / "duocad" / "agent" / "loop.py").read_text(encoding="utf-8"))
    for node in ast.walk(loop_tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name) and node.func.id == "_failure"
            and len(node.args) > 1 and isinstance(node.args[1], ast.Constant)
        ):
            codes.add(node.args[1].value)
    tree = ast.parse(MCP_SERVER_PY.read_text(encoding="utf-8"))
    envelope = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "error_envelope"
    )
    for node in ast.walk(envelope):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if (
                isinstance(key, ast.Constant) and key.value == "code"
                and isinstance(value, ast.Constant) and isinstance(value.value, str)
            ):
                codes.add(value.value)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name) and node.func.id == "ToolError"
            and node.args and isinstance(node.args[0], ast.Constant)
        ):
            codes.add(node.args[0].value)
    return codes


def _enumeration_after(text: str, anchor: str) -> set[str]:
    """`anchor` の直後の（…）に並ぶ、バッククォートで囲まれた識別子を取る。"""
    assert anchor in text, f"目印 {anchor!r} が見つからない(文書の言い回しが変わった)"
    tail = text[text.index(anchor) + len(anchor):]
    open_paren = tail.index("（")
    return set(re.findall(r"`([a-z][a-z0-9_]*)`", tail[open_paren:tail.index("）", open_paren)]))


def test_the_error_vocabulary_is_readable_from_the_source():
    """先に**走査そのものが効いている**ことを見る(0件なら下の検査は常に緑になる)。"""
    codes = _error_codes_from_source()
    assert len(codes) >= 6, f"error_envelope の語彙を読めていない: {sorted(codes)}"
    assert "internal" in codes and "invalid_predicate" in codes and "dimension_not_given" in codes, (
        f"3経路(封筒のリテラル・ToolError・ループの _failure)のどれかが読めていない: {sorted(codes)}"
    )


def test_the_vocabulary_accepted_by_call_error_matches_the_source():
    """落ちたら、テストの `call_error` が受け付ける語彙(`tests/mcp_support.py` の `ERROR_CODES`)が
    `error_envelope` とずれていて、新しいコードを返すツールのテストが「未知のエラーコード」で落ちる。"""
    from mcp_support import ERROR_CODES

    assert ERROR_CODES == _error_codes_from_source()


@pytest.mark.parametrize(
    ("path", "heading", "anchor"),
    [(MCP_MD, "## 実装のメモ", "構造化封筒")],
)
def test_the_documented_error_vocabulary_matches_the_source(path: Path, heading: str, anchor: str):
    """失敗封筒の語彙の列挙が実装と一致する。

    落ちたら、**AIに配っている失敗コードの一覧が古い**。`MCP.md`「実装のメモ」が唯一の写し先で、
    正は `error_envelope` の実装である。
    """
    documented = _enumeration_after(_section(path, heading), anchor)
    codes = _error_codes_from_source()
    assert documented == codes, (
        f"{path.relative_to(REPO_ROOT).as_posix()} の失敗語彙が `error_envelope` と食い違う"
        "(意図した変更なら文書を更新): "
        f"文書に無い={sorted(codes - documented)} / 実装に無い={sorted(documented - codes)}"
    )
