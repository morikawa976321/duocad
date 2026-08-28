"""TS側の `Applied` 型が、バックエンドが実際に返すdictと**同じ形**であることを機械で検査する。

`web/src/session.ts` の `Applied`(discriminated union)は、`Session` が組み立てて
WebSocketで配る確認応答の形を**手で書き写した主張**である。JSONの境界では `tsc` は何も
検証しないので、Python側がキー名を1つ変えても・verbを1つ足しても、**TS側は緑のまま静かに
古くなる**(実機で `undefined` を読むまで誰も気づかない)。`tests/test_entity_kind_tables.py`
が3つの表に対してやっているのと同じ「ソースを静的に読んで突き合わせる」やり方でその穴を塞ぐ。

見るのは2つ:

1. **verb名の集合** —— `duocad/catalog.py` の `COMMANDS` のうち応答が `Applied` として
   配信されるものと、`Applied` union が持つ `verb:` リテラル。
2. **フィールド名の集合** —— 実際に verb を走らせて `Session` が返したdictのキーと、
   TS側の該当メンバが宣言しているフィールド名。

**生成(codegen)はしない**。`catalog` が宣言しているのは**入力**(`params`)だけで、verbが
**何を返すか**はどこにも宣言が無い(`Session` のラッパが組み立てる戻り値dictが事実上の唯一の
正)。生成を導入するには先に宣言側へ出力の形を足す必要があり、それは別の設計判断である。

--------------------------------------------------------------------------------------
検査の対象範囲 —— **`change` 系の3メンバは verb名の突合から外す**
--------------------------------------------------------------------------------------

`Applied` union には verb ではないメンバが混ざっている(`change: 'selection'` /
`'visibility'` / `'document'`)。これらは**操作ログに載らない変更の通知**であって
`COMMANDS` の verb と1対1に対応しない:

- `selection` … `modify_selection` / `commit_selection` / `clear_selection` の**3コマンドが
  同じ1メンバ**を発火する(さらに `propose_selection` はWSに開けていない)
- `visibility` … `set_visible` が発火する。verbではないのでundoで戻らない
- `document` … `open_document` / `new_document` の**2コマンドが同じ1メンバ**

したがって「catalogのverb名 == TSの `verb:` リテラル」の集合比較には入れられない。
ただし**黙って捨てはしない** —— この3メンバも下の `test_applied_member_fields_*` で
フィールド名を突き合わせるし、`test_the_excluded_commands_really_emit_change_not_verb` が
「外した6コマンドが本当に `verb` を返さない」ことを実行して確かめる(外す判断が
間違っていたら、そこが赤くなる)。

`undo` / `redo` は `verb:` を持つ側にいるので**対象に含まれる**。

--------------------------------------------------------------------------------------
対象verbの導き方 —— **catalogのフラグから導く。名前をここに並べない**
--------------------------------------------------------------------------------------

verb名のハードコードした一覧をこの検査の中に作ると、**検査自身が4つ目の手動同期先**になり、
verbを足した人がここも直し忘れて緑のまま古くなる(塞ごうとしている穴と同じ穴が増える)。
`_catalog_applied_verbs()` が `COMMANDS` のフラグだけから導く(その4条件の理由は同関数)。
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from typing import Any, Callable

import pytest

from duocad import kernel
from duocad.server import Session

REPO_ROOT = Path(__file__).resolve().parent.parent
SESSION_TS = REPO_ROOT / "web" / "src" / "session.ts"

DEVIATION = 0.2
SIDE = 20.0
#: 20mm立方体の辺に対して**通る**半径(`tests/test_feature_ui.py` の実測値と同じ)。
RADIUS_THAT_WORKS = 3.0

#: WebSocketが `_apply(...)` の**後から**載せるフィールド。
#:
#: `Session._after_logged_verb` を通るverbの戻り値には `client_op` が入っていない ——
#: `net/ws.py` の `_dispatch` が `applied["client_op"] = message.get("client_op")` で
#: 足してからブラウザへ送る。ブラウザが受け取る形はこれを含むので、突合するときは
#: 実行側にこの分を足す(`extrude_face` のように自分で載せるverbもあるので和で吸収する)。
TRANSPORT_FIELDS = frozenset({"client_op"})


# --------------------------------------------------------------------------------------
# 宣言側(`duocad/catalog.py`)
# --------------------------------------------------------------------------------------
def _catalog():
    """`duocad/catalog.py` を**パッケージを経由せずに**読み込む
    (`tests/test_ui_command_wiring.py` の `_catalog()` と同じ作り・同じ理由)。"""
    name = "duocad_catalog_static"
    if name in sys.modules:
        return sys.modules[name]
    path = REPO_ROOT / "duocad" / "catalog.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _ws_document_changing_commands() -> set[str]:
    """ブラウザから踏めて、応答が**状態配信**(`{"type":"state", "applied": ...}`)になるもの。

    - `ws != "none"` … WebSocketに開けていないもの(`describe_*` / `measure` /
      `get_selection` / `propose_selection`)は `Applied` の経路に居ない
    - `changes_document` … 偽のものは要求した接続へ結果だけ返る別の封筒である
      (`export_step` / `export_stl` / `save_document` / `list_documents` は
      `{"type":"file"}`、`get_state` / `preview` / `get_edges` / `get_vertices` は
      それぞれ専用のtype)

    ここまでが `applied` を持つ配信の全部で、**その中が verb 系と `change` 系に割れる**。
    """
    catalog = _catalog()
    return {
        command.name
        for command in catalog.COMMANDS
        if command.ws != "none" and command.changes_document
    }


def _catalog_applied_verbs() -> set[str]:
    """`Applied` の **`verb:` 側**として配信されるコマンド名。

    `_ws_document_changing_commands()` からさらに2つ落とす:

    - `category == "selection"` … 選択(`modify` / `commit` / `clear_selection`)と可視性
      (`set_visible`)。**verbではない**(操作ログに載らずundoで戻らない)ので、応答は
      `{"change": ...}` の封筒になる
    - `mcp == False` … `new_document` / `open_document`。**「人だけの操作」= 文書ごと
      差し替える口**で、応答は `{"change": "document"}` である(AIに開けていないのも、
      これがverbの適用ではなく文書の載せ替えだから)。`save_document` / `list_documents` も
      `mcp == False` だが、そちらは `changes_document == False` で既に落ちている

    **この4条件が正しいこと自体を検査で確かめている** —— 残った側は
    `test_applied_member_fields_match_the_runtime_response` が実行して `verb` を持つことを
    見るし、落とした側は `test_the_excluded_commands_really_emit_change_not_verb` が実行して
    `verb` を持たないことを見る。片方でも間違えていれば赤くなる。
    """
    catalog = _catalog()
    ws_document_changing = _ws_document_changing_commands()
    return {
        command.name
        for command in catalog.COMMANDS
        if command.name in ws_document_changing
        and command.category != "selection"
        and command.mcp
    }


def _catalog_change_commands() -> set[str]:
    """`Applied` の **`change:` 側**として配信されるコマンド名(上の残り)。"""
    return _ws_document_changing_commands() - _catalog_applied_verbs()


# --------------------------------------------------------------------------------------
# TS側(`web/src/session.ts`)—— パーサは入れず `re` でソースを読む
# --------------------------------------------------------------------------------------
def _strip_comments(text: str) -> str:
    """`/* ... */` と `// ...` を落とす(コメント本文の日本語をフィールド名に拾わないため)。"""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def _field_names(body: str) -> set[str]:
    """`{ a: X; b: Y }` / 改行区切りの本体から**宣言されたキー名**を拾う。

    `name?: T` の `?` も許す(基底の `change?: undefined` を見つけるため)。
    """
    names = set()
    for part in re.split(r"[;\n]", body):
        match = re.match(r"\s*(\w+)\s*\??\s*:", part)
        if match:
            names.add(match.group(1))
    return names


def _ts_base_fields(interface_name: str) -> set[str]:
    """`interface AppliedVerbBase { ... }` などが全メンバに配るフィールド名。

    **`?: undefined` と書かれたものは外す。** これはTS側だけのプレースホルダで、実行時には
    来ない(`session.ts` の `AppliedVerbBase` のコメントが理由を持つ —— discriminantが
    `verb` と `change` の2つに分かれているので、どちらの分岐からも `applied.change` を
    触れるようにするには全メンバーに(値が無くても)キーが要る)。
    """
    text = SESSION_TS.read_text(encoding="utf-8")
    start = text.index(f"interface {interface_name} {{")
    end = text.index("}", start)
    body = _strip_comments(text[start + text[start:].index("{") + 1 : end])
    placeholders = set(re.findall(r"(\w+)\s*\?\s*:\s*undefined", body))
    return _field_names(body) - placeholders


def _ts_applied_members() -> list[dict[str, Any]]:
    """`export type Applied = ...` の union を1メンバ1件に分解する。

    `export type Applied =` から `export interface PreviewPayload` の手前までに**区間を
    絞ってから**(`_ts_entity_kind_table_keys()` と同じ作法)、行頭2スペースの `  | ` で
    割る。各メンバは `(AppliedVerbBase & { ... })` の形。

    返すのは `{"base", "discriminant", "literals", "fields"}` の辞書。
    """
    text = SESSION_TS.read_text(encoding="utf-8")
    start = text.index("export type Applied =")
    end = text.index("export interface PreviewPayload")
    block = _strip_comments(text[start:end])

    head, *chunks = re.split(r"^  \| ", block, flags=re.M)
    assert head.strip() == "export type Applied =", (
        f"union の切り出しがずれている(先頭に想定外の中身がある): {head.strip()[:120]!r}"
    )

    members: list[dict[str, Any]] = []
    for chunk in chunks:
        base_match = re.search(r"\(\s*(Applied\w*Base)\s*&", chunk)
        assert base_match, f"メンバの基底を読めない: {chunk.strip()[:120]!r}"
        inner_start = chunk.index("{", base_match.end() - 1)
        inner_end = chunk.rindex("}")
        body = chunk[inner_start + 1 : inner_end]
        assert "{" not in body and "}" not in body, (
            "メンバの中に入れ子のオブジェクトがある —— このパーサは平坦な形しか読めない。"
            f"読み方を直すこと: {chunk.strip()[:120]!r}"
        )
        fields = _field_names(body)
        discriminant = "verb" if "verb" in fields else "change" if "change" in fields else None
        assert discriminant, f"メンバに verb / change のどちらも無い: {chunk.strip()[:120]!r}"
        declaration = re.search(rf"\b{discriminant}\s*:\s*([^;\n}}]+)", body)
        assert declaration, f"{discriminant} のリテラルを読めない: {chunk.strip()[:120]!r}"
        literals = set(re.findall(r"'([^']+)'", declaration.group(1)))
        assert literals, f"{discriminant} が文字列リテラルではない: {declaration.group(1)!r}"
        members.append(
            {
                "base": base_match.group(1),
                "discriminant": discriminant,
                "literals": literals,
                "fields": fields | _ts_base_fields(base_match.group(1)),
            }
        )
    return members


def _ts_members_by_literal() -> dict[tuple[str, str], set[str]]:
    """`("verb", "fillet")` → そのメンバが宣言しているフィールド名(基底込み)。

    `verb: 'add_line' | 'add_circle' | ...` のように**1メンバが複数のリテラルを束ねる**
    ことがあるので、リテラルごとに開いて持つ。
    """
    table: dict[tuple[str, str], set[str]] = {}
    for member in _ts_applied_members():
        for literal in member["literals"]:
            key = (member["discriminant"], literal)
            assert key not in table, f"{key} が2つのメンバに現れる(union が壊れている)"
            table[key] = member["fields"]
    return table


# --------------------------------------------------------------------------------------
# 走査そのものが効いているか(**0件でも緑になる検査を作らない**)
# --------------------------------------------------------------------------------------
def test_ts_applied_union_is_readable() -> None:
    """`session.ts` から union を読めている(`tests/test_entity_kind_tables.py` と同じ作法)。"""
    members = _ts_applied_members()
    verbs = [m for m in members if m["discriminant"] == "verb"]
    changes = [m for m in members if m["discriminant"] == "change"]
    assert len(verbs) >= 10, f"verb 側のメンバを読めていない: {len(verbs)}件"
    assert len(changes) >= 3, f"change 側のメンバを読めていない: {len(changes)}件"
    # **discriminantだけ拾えて本体が空、でも緑**を防ぐ。メンバ個々に「必ず固有の
    # フィールドがある」とは言えない(戻り値が `verb`/`seq` だけのverbはありうる)ので、
    # union全体で数える。走査が空振りすればここが0近辺に落ちる。
    body_fields: set[str] = set()
    for member in members:
        body_fields |= member["fields"] - {"verb", "change", "seq", "client_op"}
    assert len(body_fields) >= 10, (
        f"メンバ本体のフィールドを読めていない(走査が空振りしている): {sorted(body_fields)}"
    )


def test_ts_applied_base_fields_are_readable() -> None:
    """基底が配る共通フィールドを読めていて、**プレースホルダを外せている**。"""
    verb_base = _ts_base_fields("AppliedVerbBase")
    assert {"seq", "client_op"} <= verb_base, f"AppliedVerbBase を読めていない: {verb_base}"
    assert "change" not in verb_base, (
        "`change?: undefined` を外せていない —— 実行時には来ないプレースホルダなので"
        "突合に混ぜると全verbが赤くなる"
    )
    change_base = _ts_base_fields("AppliedChangeBase")
    assert "client_op" in change_base, f"AppliedChangeBase を読めていない: {change_base}"
    assert not ({"verb", "seq"} & change_base), (
        "`verb?: undefined` / `seq?: undefined` を外せていない —— 実行時には来ない"
        f"プレースホルダである: {sorted(change_base)}"
    )


def test_catalog_applied_verbs_is_readable() -> None:
    """宣言側から対象verbを導けている(フラグの綴りを変えて0件になったら赤)。"""
    verbs = _catalog_applied_verbs()
    assert len(verbs) >= 10, f"catalog から対象verbを導けていない: {sorted(verbs)}"
    changes = _catalog_change_commands()
    assert len(changes) >= 4, f"外した側が導けていない: {sorted(changes)}"


# --------------------------------------------------------------------------------------
# 完了条件1: verb名の集合が一致する
# --------------------------------------------------------------------------------------
def test_applied_union_covers_exactly_the_catalog_verbs() -> None:
    """`Applied` の `verb:` リテラルが catalog の対象verbと一致する。

    **verbを1つ足してTS側に足し忘れるとここが落ちる**(逆に、消したverbの型だけ残っていても)。
    """
    expected = _catalog_applied_verbs()
    declared = {
        literal
        for (discriminant, literal) in _ts_members_by_literal()
        if discriminant == "verb"
    }
    assert declared == expected, (
        "web/src/session.ts の Applied が duocad/catalog.py の verb と食い違う"
        f"(TSのみ={sorted(declared - expected)} / catalogのみ={sorted(expected - declared)})。"
        "verbを足したら Applied にも1メンバー足すこと。"
    )


# --------------------------------------------------------------------------------------
# 完了条件2: フィールド名の集合が一致する(**実際に走らせて返り値のキーを見る**)
# --------------------------------------------------------------------------------------
#: コマンド名 → `Session` を叩いて `applied` を1つ作る手続き。
#:
#: **表そのものは verb の一覧ではない**(一覧は `_catalog_applied_verbs()` が導く)。
#: 走らせ方がここに無いコマンドは `pytest.skip` で見えるようにする —— 黙って落とすと、
#: 「走らせられたものが0件でも緑」という空振りに戻ってしまう。
_RUNNERS: dict[str, Callable[..., dict]] = {}


def _runner(name: str) -> Callable[[Callable[..., dict]], Callable[..., dict]]:
    def register(function: Callable[..., dict]) -> Callable[..., dict]:
        _RUNNERS[name] = function
        return function

    return register


def _select(session: Session, refs: list) -> None:
    """選択を `refs` で置き換える(人がクリックで選び直すのと同じ入口)。"""
    session.clear_selection()
    session.modify_selection(refs, [])


def _sketch(session: Session) -> str:
    return session.sketch_create("XY")["sketch_id"]


def _box(session: Session):
    """スケッチ→長方形→領域押し出しで 20mm の立方体を1つ作る
    (`tests/test_feature_ui.py` の `_box` と同じ手順)。"""
    sketch_id = _sketch(session)
    session.add_rectangle(sketch_id, [0.0, 0.0], SIDE, SIDE)
    _select(session, [{"sketch": sketch_id, "index": 1}])
    session.extrude(SIDE, mode="join")
    session.clear_selection()
    return session.document.bodies[-1], sketch_id


def _step_file(tmp_path: Path) -> Path:
    """1ソリッドのSTEPを作って返す(`tests/test_files_ui.py` と同じ作り)。"""
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox

    path = tmp_path / "part.step"
    kernel.write_step(BRepPrimAPI_MakeBox(20.0, 10.0, 5.0).Shape(), str(path))
    return path


SVG_BODY = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 100">
  <circle cx="30" cy="30" r="10"/>
  <rect x="100" y="10" width="20" height="20"/>
</svg>
"""


@_runner("sketch_create")
def _run_sketch_create(session: Session, tmp_path: Path) -> dict:
    return session.sketch_create("XY")


@_runner("add_line")
def _run_add_line(session: Session, tmp_path: Path) -> dict:
    return session.add_line(_sketch(session), [0.0, 0.0], [10.0, 0.0])


@_runner("add_circle")
def _run_add_circle(session: Session, tmp_path: Path) -> dict:
    return session.add_circle(_sketch(session), [0.0, 0.0], 5.0)


@_runner("add_rectangle")
def _run_add_rectangle(session: Session, tmp_path: Path) -> dict:
    return session.add_rectangle(_sketch(session), [0.0, 0.0], 10.0, 10.0)


@_runner("add_arc")
def _run_add_arc(session: Session, tmp_path: Path) -> dict:
    return session.add_arc(_sketch(session), [0.0, 0.0], 5.0, 0.0, 90.0)


@_runner("set_parameter")
def _run_set_parameter(session: Session, tmp_path: Path) -> dict:
    session.add_circle(_sketch(session), [1.0, 2.0], 3.0)
    name = sorted(session.document.parameters)[0]
    return session.set_parameter(name, 4.0)


@_runner("pattern_rectangular")
def _run_pattern_rectangular(session: Session, tmp_path: Path) -> dict:
    sketch_id = _sketch(session)
    entity_id = session.add_circle(sketch_id, [0.0, 0.0], 2.0)["entity_id"]
    return session.pattern_rectangular(sketch_id, [entity_id], 10.0, 10.0, 2, 2)


@_runner("pattern_circular")
def _run_pattern_circular(session: Session, tmp_path: Path) -> dict:
    sketch_id = _sketch(session)
    entity_id = session.add_circle(sketch_id, [10.0, 0.0], 2.0)["entity_id"]
    return session.pattern_circular(sketch_id, [entity_id], [0.0, 0.0], 4)


@_runner("import_svg")
def _run_import_svg(session: Session, tmp_path: Path) -> dict:
    path = tmp_path / "shapes.svg"
    path.write_text(SVG_BODY, encoding="utf-8")
    return session.import_svg(_sketch(session), str(path))


@_runner("extrude")
def _run_extrude(session: Session, tmp_path: Path) -> dict:
    sketch_id = _sketch(session)
    session.add_rectangle(sketch_id, [0.0, 0.0], SIDE, SIDE)
    _select(session, [{"sketch": sketch_id, "index": 1}])
    return session.extrude(SIDE, mode="join")


@_runner("extrude_face")
def _run_extrude_face(session: Session, tmp_path: Path) -> dict:
    body, _ = _box(session)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    return session.extrude_face(5.0)


@_runner("fillet")
def _run_fillet(session: Session, tmp_path: Path) -> dict:
    body, _ = _box(session)
    _select(session, [{"body": body.id, "kind": "edge", "index": 1}])
    return session.fillet(RADIUS_THAT_WORKS)


@_runner("chamfer")
def _run_chamfer(session: Session, tmp_path: Path) -> dict:
    body, _ = _box(session)
    _select(session, [{"body": body.id, "kind": "edge", "index": 1}])
    return session.chamfer(RADIUS_THAT_WORKS)


@_runner("import_step")
def _run_import_step(session: Session, tmp_path: Path) -> dict:
    return session.import_step(str(_step_file(tmp_path)))


@_runner("undo")
def _run_undo(session: Session, tmp_path: Path) -> dict:
    session.add_circle(_sketch(session), [0.0, 0.0], 5.0)
    return session.undo()


@_runner("redo")
def _run_redo(session: Session, tmp_path: Path) -> dict:
    session.add_circle(_sketch(session), [0.0, 0.0], 5.0)
    session.undo()
    return session.redo()


@_runner("set_visible")
def _run_set_visible(session: Session, tmp_path: Path) -> dict:
    body, _ = _box(session)
    return session.set_visible(body.id, False)


@_runner("modify_selection")
def _run_modify_selection(session: Session, tmp_path: Path) -> dict:
    body, _ = _box(session)
    return session.modify_selection([{"body": body.id, "kind": "face", "index": 1}], [])


@_runner("commit_selection")
def _run_commit_selection(session: Session, tmp_path: Path) -> dict:
    return session.commit_selection()


@_runner("clear_selection")
def _run_clear_selection(session: Session, tmp_path: Path) -> dict:
    return session.clear_selection()


@_runner("new_document")
def _run_new_document(session: Session, tmp_path: Path) -> dict:
    return session.new_document()


@_runner("open_document")
def _run_open_document(session: Session, tmp_path: Path) -> dict:
    session.add_circle(_sketch(session), [0.0, 0.0], 5.0)
    session.save_document("applied-type-sync.json")
    return session.open_document("applied-type-sync.json")


@pytest.fixture()
def session(tmp_path: Path) -> Session:
    """**STEPを読まない空の文書**(`tests/test_feature_ui.py` と同じ)。"""
    return Session(None, DEVIATION, models_dir=tmp_path)


def _run(name: str, session: Session, tmp_path: Path) -> dict:
    runner = _RUNNERS.get(name)
    if runner is None:
        pytest.skip(
            f"{name!r} を走らせる手続きが tests/test_applied_type_sync.py の _RUNNERS に無い"
            "(フィールド名の突合ができていない。走らせ方を足すこと)"
        )
    return runner(session, tmp_path)


@pytest.mark.parametrize("verb", sorted(_catalog_applied_verbs()))
def test_applied_member_fields_match_the_runtime_response(
    verb: str, session: Session, tmp_path: Path
) -> None:
    """**実際に verb を走らせた返り値のキー**が、TS側の該当メンバの宣言と一致する。

    実行側には `TRANSPORT_FIELDS`(`client_op`)を足す —— `net/ws.py` が `_apply` の後から
    載せるので、`Session` の戻り値だけを見ると足りない。
    """
    applied = _run(verb, session, tmp_path)
    assert applied.get("verb") == verb, (
        f"{verb!r} の応答が verb を名乗っていない({applied.get('verb')!r})。"
        "対象verbの導き方(_catalog_applied_verbs)が実態とずれている。"
    )
    runtime = set(applied) | set(TRANSPORT_FIELDS)
    declared = _ts_members_by_literal().get(("verb", verb))
    assert declared is not None, f"Applied に verb: '{verb}' のメンバが無い"
    assert runtime == declared, (
        f"verb '{verb}' のフィールドが web/src/session.ts と食い違う"
        f"(実行時のみ={sorted(runtime - declared)} / TSのみ={sorted(declared - runtime)})"
    )


@pytest.mark.parametrize("command", sorted(_catalog_change_commands()))
def test_the_excluded_commands_really_emit_change_not_verb(
    command: str, session: Session, tmp_path: Path
) -> None:
    """verb名の突合から**外した**コマンドが、本当に `change` 系の封筒を返す。

    外す判断(`_catalog_applied_verbs()` の4条件)が間違っていれば、ここで `verb` が出て赤くなる。
    ついでにフィールド名も突き合わせる —— 対象外にしたのは**verb名の集合比較**だけで、
    形の検査まで免除しているわけではない。
    """
    applied = _run(command, session, tmp_path)
    assert "verb" not in applied, (
        f"{command!r} は verb を返している —— verb名の突合から外したのは誤りである。"
        "_catalog_applied_verbs() の条件を直すこと。"
    )
    change = applied.get("change")
    assert change, f"{command!r} の応答が change を持たない: {sorted(applied)}"
    runtime = set(applied) | set(TRANSPORT_FIELDS)
    declared = _ts_members_by_literal().get(("change", change))
    assert declared is not None, f"Applied に change: '{change}' のメンバが無い"
    assert runtime == declared, (
        f"change '{change}'({command})のフィールドが web/src/session.ts と食い違う"
        f"(実行時のみ={sorted(runtime - declared)} / TSのみ={sorted(declared - runtime)})"
    )
