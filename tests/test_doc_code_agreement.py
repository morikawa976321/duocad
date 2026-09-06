"""文書に**意図的に残した列挙**が、コード側の正と一致していることを機械で見る。

前段の減量タスク(`20260906-0100` / `-0250` / `-0800`)は「列挙の転記は消して参照へ畳む」を
原則にしたが、**読み手がそこで引けなくなる列挙は文書側に残した**。残したものは手で同期する
しかないので、ここがその最後の受け皿になる。

**落ちたときの意味**: コードを変えたのに、それを写した文書の行が古いまま。
**期待値を書き換えて緑にしない** —— 正はコード側にあるので、失敗メッセージが名指しする
文書の箇所を直すこと。

**`topopt_checks/check_design_space.py` の `[12-6]` と同じ穴を踏まない。** [12-6] は文字列を
文書全体から `in` で探すだけなので、(a) 文書側に**古い行が残っている**ことを検出できず、
(b) 節が消えても同じ文字列がどこかにあれば緑のままになる。ここの検査はすべて

1. **走査そのものが効いていることを先に見る**(見出しや表の形を変えたら「読めなかった」で落ちる)
2. **集合を両方向で比べる**(足りない側と余計な側の両方)

の2点でその穴を塞いでいる。
"""
from __future__ import annotations

import ast
import importlib
import re
from pathlib import Path

import pytest

from duocad import sketch
from duocad.mcp_server import DocumentTools, McpServer
from duocad.session import Session

REPO_ROOT = Path(__file__).resolve().parent.parent
MCP_MD = REPO_ROOT / "MCP.md"
TESTS_README = REPO_ROOT / "tests" / "README.md"
ROOT_README = REPO_ROOT / "README.md"
TOPOPT_MD = REPO_ROOT / "docs" / "TOPOPT.md"
MCP_SERVER_PY = REPO_ROOT / "duocad" / "mcp_server.py"


def _section(path: Path, heading: str) -> str:
    """`heading` の行から、**同じか浅い見出し**の手前までを切り出す。

    見出しが消えた/改名された場合はここで落ちる —— 空文字を返すと下の集合比較が
    「両方空で一致」になり、[12-6] と同じ静かな失効になる。

    **コードブロックの中は見出しとして数えない** —— PowerShellの手順には `# 説明` の行が
    並んでおり、素の行頭 `#` で切ると節がその1行目で終わる。
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


# --------------------------------------------------------------------------------------
# MCP.md 「公開ツール」表 —— AIが読む一覧。実際に生えている口と一致すること
# --------------------------------------------------------------------------------------
def _published_tool_names() -> set[str]:
    server = McpServer(DocumentTools(Session(None, 0.2)))
    response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    return {tool["name"] for tool in response["result"]["tools"]}


def _documented_tool_names() -> set[str]:
    """`MCP.md`「公開ツール」表のツール名。

    セルは `` `a` / `b` — 補足の文 `` の形なので、**全角ダッシュより前だけ**を見る
    (補足の中の識別子をツール名に数えない)。
    """
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
    ある道具を隠す)。`tests/test_mcp_verbs.py` の `EXPECTED_TOOLS` はコード側の顔ぶれしか
    固定しておらず、この表との一致は誰も見ていなかった。
    """
    documented = _documented_tool_names()
    published = _published_tool_names()
    assert documented == published, (
        "MCP.md「公開ツール」表が tools/list と食い違う"
        "(意図した変更なら表を更新すること): "
        f"表に無い={sorted(published - documented)} / 実在しない={sorted(documented - published)}"
    )


# --------------------------------------------------------------------------------------
# 失敗封筒の語彙 —— 正は `error_envelope` の実装。MCP.md と tests/README.md が写している
# --------------------------------------------------------------------------------------
def _error_codes_from_source() -> set[str]:
    """`duocad/mcp_server.py` が実際に返しうる `code` の語彙をソースから読む。

    2経路ある: `error_envelope` が直に置くリテラルと、`ToolError(code, ...)` の
    第1引数(封筒は `error.code` をそのまま通す)。**実行時に集められない** —— 全語彙を
    出す例外を並べる検査は、その並びがまた手動同期になる。
    """
    tree = ast.parse(MCP_SERVER_PY.read_text(encoding="utf-8"))
    envelope = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "error_envelope"
    )
    codes = set()
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
    assert "internal" in codes and "invalid_predicate" in codes, (
        f"2経路(封筒のリテラルと ToolError)の片方しか読めていない: {sorted(codes)}"
    )


@pytest.mark.parametrize(
    ("path", "heading", "anchor"),
    [
        (MCP_MD, "## 実装のメモ", "構造化封筒"),
        (TESTS_README, "### 失敗も封筒に入れて返す", "「実装のメモ」が正"),
    ],
)
def test_the_documented_error_vocabulary_matches_the_source(path: Path, heading: str, anchor: str):
    """失敗封筒の語彙の列挙が実装と一致する。

    落ちたら、**AIに配っている失敗コードの一覧が古い**。`MCP.md`「実装のメモ」が正の側で、
    `tests/README.md` はそこを名指ししたうえで括弧に写している(読み手がその場で引けるように
    残した列挙)。
    """
    documented = _enumeration_after(_section(path, heading), anchor)
    codes = _error_codes_from_source()
    assert documented == codes, (
        f"{path.relative_to(REPO_ROOT).as_posix()} の失敗語彙が `error_envelope` と食い違う"
        "(意図した変更なら文書を更新): "
        f"文書に無い={sorted(codes - documented)} / 実装に無い={sorted(documented - codes)}"
    )


def test_the_written_vocabulary_counts_match_the_source():
    """「N語彙」と書いてある数が実際の語彙数と合う。

    2026-09-05の監査時点でコード側の2箇所が「6語彙」のまま7つを列挙しており、
    リポジトリ内で6と7が混在していた。**数を書くのは列挙が並記されている場合だけ**という
    線引き(`20260906-0100` の契約)を残すなら、その数はここで押さえるしかない。
    """
    expected = len(_error_codes_from_source())
    wrong = {}
    for path in (MCP_MD, TESTS_README, MCP_SERVER_PY, REPO_ROOT / "tests" / "test_mcp_verbs.py"):
        written = {int(n) for n in re.findall(r"(\d+)語彙", path.read_text(encoding="utf-8"))}
        if written - {expected}:
            wrong[path.name] = sorted(written - {expected})
    assert not wrong, f"失敗語彙は {expected} 個だが、そう書いていない箇所がある: {wrong}"


# --------------------------------------------------------------------------------------
# tests/README.md の `Entity.params` のキー表 —— 正は `duocad/sketch.py` の `_ENTITY_KIND_TABLE`
# --------------------------------------------------------------------------------------
def _documented_entity_params() -> dict[str, list[str]]:
    """`tests/README.md` の「`Entity.params` のキーは図形ごとに固定」の箇条から種別→キーを取る。"""
    section = _section(TESTS_README, "## `duocad/sketch.py`")
    anchor = "`Entity.params` のキーは図形ごとに固定"
    assert anchor in section, f"tests/README.md に目印 {anchor!r} が無い(言い回しが変わった)"
    bullet = section[section.index(anchor):]
    end = bullet.find("\n- ")
    return {
        kind: keys.split(",")
        for kind, keys in re.findall(r"`([a-z]+): ([a-z0-9_,]+)`", bullet[:end if end > 0 else None])
    }


def test_the_documented_entity_param_table_is_readable():
    """先に**走査そのものが効いている**ことを見る(0件なら下の検査は常に緑になる)。"""
    documented = _documented_entity_params()
    assert len(documented) >= 4, f"tests/README.md のキー表を読めていない: {documented}"


def test_the_documented_entity_kinds_match_the_kind_table():
    """文書が並べている図形種別が `ENTITY_KINDS` と一致する。

    落ちたら、**種別を1つ足した(消した)のに `tests/README.md` が古い**。2026-09-05の監査で
    実際に `bezier` が抜けていた口である。`tests/test_entity_kind_tables.py` はコード側の
    3つの表(py/py/ts)だけを突き合わせており、文書は見ていない。
    """
    documented = set(_documented_entity_params())
    assert documented == set(sketch.ENTITY_KINDS), (
        "tests/README.md の図形種別が `_ENTITY_KIND_TABLE` と食い違う"
        "(意図した変更なら文書のキー表を更新すること): "
        f"文書に無い={sorted(set(sketch.ENTITY_KINDS) - documented)} / "
        f"実装に無い={sorted(documented - set(sketch.ENTITY_KINDS))}"
    )


def test_the_documented_param_keys_contain_every_movable_point_key():
    """種別ごとのキーの並びに、**動かせる点のキー**が全部載っている。

    落ちたら、点のキーを改名した(足した)のに文書が古い。`move_sketch_entities` の選択参照は
    xキーで点を名指しするので、ここがずれると文書を読んで組み立てた参照が通らない。
    """
    documented = _documented_entity_params()
    missing = {}
    for kind, keys in documented.items():
        declared = {key for pair in sketch.entity_point_keys(kind) for key in pair}
        if declared - set(keys):
            missing[kind] = sorted(declared - set(keys))
    assert not missing, f"tests/README.md のキー表に載っていない点のキーがある: {missing}"


# --------------------------------------------------------------------------------------
# docs/TOPOPT.md 「現在の正」表 —— 既定値の索引。値そのものはコード側が正
# --------------------------------------------------------------------------------------
def _current_truth_rows() -> list[tuple[str, list[float], list[tuple[str, str]]]]:
    """「現在の正」表を (項目, 太字に書かれた数値, 参照している (モジュール, 名前)) で返す。

    3列目は `design_space.COARSE_ELEMENTS_LONG_AXIS` / `COARSE_MAX_ITER` のように
    **2つ目以降のモジュール名を省く**書き方があるので、行の中で直前のモジュールを引き継ぐ。
    """
    rows = []
    for cells in _table_rows(_section(TOPOPT_MD, "## 現在の正")):
        numbers = []
        for bold in re.findall(r"\*\*(.+?)\*\*", cells[1]):
            found = re.findall(r"-?\d+(?:\.\d+)?", bold)
            if not found:
                numbers = []   # 「総コアの半分」のように数で書いていない行 —— 値の比較はしない
                break
            numbers.extend(float(n) for n in found)
        symbols = []
        module = None
        for token in re.findall(r"`([^`]+)`", cells[2]):
            qualified = re.fullmatch(r"([a-z_]+)\.([A-Z][A-Z0-9_]*)", token)
            if qualified:
                module = qualified.group(1)
                symbols.append((module, qualified.group(2)))
            elif re.fullmatch(r"[A-Z][A-Z0-9_]*", token) and module:
                symbols.append((module, token))
        rows.append((cells[0], numbers, symbols))
    return rows


def test_the_current_truth_table_is_readable():
    """先に**走査そのものが効いている**ことを見る(0件なら下の検査は常に緑になる)。"""
    rows = _current_truth_rows()
    assert len(rows) >= 8, f"docs/TOPOPT.md「現在の正」表を読めていない: {rows}"
    comparable = [row for row in rows if row[1] and row[2]]
    assert len(comparable) >= 8, (
        "太字の数値かコードの記号を読めていない行が多すぎる(表の書き方が変わった): "
        f"読めた {len(comparable)} / 全 {len(rows)} 行"
    )


def test_the_current_truth_table_names_symbols_that_exist():
    """索引が名指しするコード上の記号が実在する。

    落ちたら、**定数を改名・削除したのに表が古い**。値の一致(下)より先にここで落として、
    「記号が消えたので比較する値が0件になって緑」を防ぐ。
    """
    missing = []
    for item, _numbers, symbols in _current_truth_rows():
        assert symbols, f"「{item}」の行がコード上の場所を指していない"
        for module, name in symbols:
            if not hasattr(importlib.import_module(f"duocad.topopt.{module}"), name):
                missing.append(f"{module}.{name}（{item}）")
    assert not missing, f"docs/TOPOPT.md「現在の正」が実在しない記号を指している: {missing}"


def test_the_current_truth_table_values_match_the_code():
    """索引に書いてある既定値がコードの定数と一致する。

    落ちたら、**既定値を変えたのに `docs/TOPOPT.md` の索引が古い**。この文書は節が時系列で
    並んでいて位置から新旧を判断できないため、**この表が古いと現行値を引く手段が無くなる**
    (それを解消するために `20260906-0800` が新設した表である)。
    """
    wrong = []
    for item, numbers, symbols in _current_truth_rows():
        if not numbers:
            continue           # 数で書いていない行(スレッド数=総コアの半分)は対象外
        actual = [
            float(getattr(importlib.import_module(f"duocad.topopt.{module}"), name))
            for module, name in symbols
        ]
        if numbers != actual:
            wrong.append(f"「{item}」文書={numbers} / コード={actual}")
    assert not wrong, (
        "docs/TOPOPT.md「現在の正」の既定値がコードと食い違う"
        f"(正はコード側。表の行を直すこと): {wrong}"
    )


# --------------------------------------------------------------------------------------
# README.md §3 の受入検査の並び —— 正は `topopt_checks/` の中身
# --------------------------------------------------------------------------------------
def test_the_readme_lists_every_acceptance_check_script():
    """`README.md`「形状最適化の受入検査」が `topopt_checks/check_*.py` を全部並べている。

    落ちたら、**受入検査を足した(消した)のに README の実行手順が古い**。この群は pytest に
    入れておらず、README のこのブロックを写して回すのが唯一の強制力なので、
    並べ忘れた検査は**誰も走らせないまま緑に見える**。
    """
    section = _section(ROOT_README, "### 形状最適化の受入検査")
    listed = set(re.findall(r"topopt_checks[\\/](check_[a-z_]+\.py)", section))
    actual = {path.name for path in (REPO_ROOT / "topopt_checks").glob("check_*.py")}
    assert listed == actual, (
        "README.md「形状最適化の受入検査」と topopt_checks/ の顔ぶれが食い違う"
        "(意図した変更なら README のブロックを更新すること): "
        f"READMEに無い={sorted(actual - listed)} / 実在しない={sorted(listed - actual)}"
    )
