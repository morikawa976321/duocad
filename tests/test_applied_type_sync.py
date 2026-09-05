"""TS側の `Applied` 型が、バックエンドが実際に返すdictと**同じ形**であることを機械で検査する。

`web/src/session.ts` の `Applied`(discriminated union)は、`Session` が組み立てて
WebSocketで配る確認応答の形を**手で書き写した主張**である。JSONの境界では `tsc` は何も
検証しないので、Python側がキー名を1つ変えても・verbを1つ足しても、**TS側は緑のまま静かに
古くなる**(実機で `undefined` を読むまで誰も気づかない)。`tests/test_entity_kind_tables.py`
が3つの表に対してやっているのと同じ「ソースを静的に読んで突き合わせる」やり方でその穴を塞ぐ。

**生成(codegen)はしない**。`catalog` が宣言しているのは**入力**(`params`)だけで、verbが
**何を返すか**はどこにも宣言が無い(`Session` のラッパが組み立てる戻り値dictが事実上の唯一の
正)。生成を導入するには先に宣言側へ出力の形を足す必要があり、それは別の設計判断である。

--------------------------------------------------------------------------------------
何を見て、何を見ていないか(**過信しないために先に書く**)
--------------------------------------------------------------------------------------

見ているのは4つ。1は静的な突合、2〜4は**実際にコマンドを走らせた戻り値**との突合である:

1. **verb名の集合** … `catalog.COMMANDS` のうち `Applied` の `verb:` 側として配信される
   ものと、union の `verb:` リテラル
2. **フィールド名の在不在** … 実応答に出たキーが全部宣言されていること、宣言した必須
   フィールドが全部実応答に出ていること
3. **配列かどうか / null許容か / オプショナルの要否** … 実応答の値が `list` なら宣言も
   `T[]`、`None` なら宣言も `| null`、**実行で一度も欠けなかったフィールドに `?` を
   付けていないこと**(逆に、欠けうるフィールドは `?` が要る)
4. **スカラの素の型** … `string` / `number` / `boolean` と実値の対応(文字列リテラルの
   union は `string` として見る)。配列は要素の素の型まで見る

**見ていないもの**(ここを直したければ検査を足す必要がある):

- **独自型の中身** … `selection: SelectionState` のように素の型でない宣言は、`null` /
  配列の判定だけして中身は見ない
- **観測できなかった値域** … 「1回も `null` にならなかったフィールドが実は `null` に
  なりうるか」は実行からは分からないので、`| null` の**付けすぎ**は検出しない
  (付け忘れは、実際に `null` が出るコマンドを走らせている分だけ検出できる)
- **数値の内訳** … TSの `number` は整数と小数を区別しないので、こちらも区別しない
- **`client_op` の型** … `_after_logged_verb` を通るverbの戻り値には入っておらず
  (`net/ws.py` が後から載せる。`TRANSPORT_FIELDS` 参照)、値を観測できない。**在ることだけ**
  を見る。自分で載せるverb(`extrude_face` など)については普通に型も見る

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
ただし**黙って捨てはしない** —— この3メンバも上の2〜4で突き合わせるし、
`test_the_excluded_commands_really_emit_change_not_verb` が「外した6コマンドが本当に
`verb` を返さない」ことを実行して確かめる(外す判断が間違っていたら、そこが赤くなる)。

`undo` / `redo` は `verb:` を持つ側にいるので**対象に含まれる**。

--------------------------------------------------------------------------------------
対象verbの導き方 —— **catalogのフラグから導く。名前をここに並べない**
--------------------------------------------------------------------------------------

verb名のハードコードした一覧をこの検査の中に作ると、**検査自身が4つ目の手動同期先**になり、
verbを足した人がここも直し忘れて緑のまま古くなる(塞ごうとしている穴と同じ穴が増える)。
`_catalog_applied_verbs()` が `COMMANDS` のフラグだけから導く(その4条件の理由は同関数)。

**走らせ方(`_RUNNERS`)が無いコマンドは `skip` ではなく `fail` にする。** catalogが対象と
判定したコマンドを走らせられないのは検査の欠落であって、飛ばしてよい状況ではない
(飛ばすと、verbをcatalogとTSの両方に足して走らせ方だけ忘れた人が緑で通ってしまい、
実応答と宣言のずれが誰にも見られないまま残る)。
"""
from __future__ import annotations

import importlib.util
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

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
#: 実行側にこの分を**在るものとして**足す(値は観測できないので型は見ない。
#: `extrude_face` のように自分で載せるverbでは普通に観測できるので型も見る)。
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


def _target_commands() -> set[str]:
    """この検査が実際に走らせるコマンド(verb側 + change側)。"""
    return _catalog_applied_verbs() | _catalog_change_commands()


# --------------------------------------------------------------------------------------
# TS側(`web/src/session.ts`)—— パーサは入れず `re` でソースを読む
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Declared:
    """TS側の1フィールドの宣言。`arms` は型を `|` で割ったもの。"""

    name: str
    optional: bool
    arms: tuple[str, ...]

    @property
    def text(self) -> str:
        return " | ".join(self.arms)

    @property
    def is_array(self) -> bool:
        return any(arm.endswith("[]") or arm.startswith("Array<") for arm in self.arms)

    @property
    def is_nullable(self) -> bool:
        return "null" in self.arms

    @property
    def element_arms(self) -> tuple[str, ...]:
        return tuple(arm[:-2] for arm in self.arms if arm.endswith("[]"))


#: TSの素の型名 → 実値がその型かを見る述語。
#:
#: **`boolean` と `number` を取り違えない** —— Pythonの `bool` は `int` の派生なので、
#: `number` 側で明示的に弾かないと `True` が `number` として通ってしまう。
_PRIMITIVES: dict[str, Callable[[Any], bool]] = {
    "string": lambda value: isinstance(value, str),
    "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
    "boolean": lambda value: isinstance(value, bool),
}


def _primitive_arms(arms: tuple[str, ...]) -> Optional[list[str]]:
    """`arms` を素の型名の一覧に均す。**独自型が混ざっていたら `None`**(型を見ない)。

    文字列リテラル(`'extrude'`)は `string` として扱う。`null` は別で見るので落とす。
    """
    names: list[str] = []
    for arm in arms:
        if arm == "null":
            continue
        if len(arm) >= 2 and arm.startswith("'") and arm.endswith("'"):
            names.append("string")
        elif arm in _PRIMITIVES:
            names.append(arm)
        else:
            return None
    return names


def _type_mismatch(declared: Declared, value: Any) -> Optional[str]:
    """実値 `value` が `declared` の宣言に収まらない理由。収まっていれば `None`。"""
    if value is None:
        if not declared.is_nullable:
            return f"実応答が null だが宣言は `{declared.text}`(`| null` が要る)"
        return None
    if isinstance(value, list):
        if not declared.is_array:
            return f"実応答が配列だが宣言は `{declared.text}`(`T[]` が要る)"
        elements = _primitive_arms(declared.element_arms)
        if elements:
            for item in value:
                if not any(_PRIMITIVES[name](item) for name in elements):
                    return f"配列の要素 {item!r} が宣言 `{declared.text}` の要素型に合わない"
        return None
    if declared.is_array:
        return f"実応答が配列でない({value!r})のに宣言は `{declared.text}`"
    primitives = _primitive_arms(declared.arms)
    if primitives and not any(_PRIMITIVES[name](value) for name in primitives):
        return f"実応答 {value!r} が宣言 `{declared.text}` に合わない"
    return None


def _strip_comments(text: str) -> str:
    """`/* ... */` と `// ...` を落とす(コメント本文の日本語をフィールド名に拾わないため)。"""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def _field_specs(body: str) -> dict[str, Declared]:
    """`{ a: X; b?: Y | null }` / 改行区切りの本体から**宣言を型ごと**拾う。

    **読めない断片があったら黙って捨てず落ちる** —— 型を複数行に折るような書き方が
    入ったときに、静かにフィールドが1つ消えて検査がすり抜けるのを防ぐ。
    """
    specs: dict[str, Declared] = {}
    for part in re.split(r"[;\n]", body):
        if not part.strip():
            continue
        match = re.match(r"\s*(\w+)\s*(\?)?\s*:\s*(\S.*?)\s*$", part)
        assert match, (
            f"フィールド宣言として読めない断片がある(型を複数行に折っていないか): {part!r}"
        )
        name = match.group(1)
        arms = tuple(arm.strip() for arm in match.group(3).split("|") if arm.strip())
        assert arms, f"{name} の型を読めない: {part!r}"
        assert name not in specs, f"{name} が同じ宣言に2回現れる: {body!r}"
        specs[name] = Declared(name=name, optional=match.group(2) == "?", arms=arms)
    return specs


def _ts_base_fields(interface_name: str) -> dict[str, Declared]:
    """`interface AppliedVerbBase { ... }` などが全メンバに配る宣言。

    **`?: undefined` と書かれたものは外す。** これはTS側だけのプレースホルダで、実行時には
    来ない(`session.ts` の `AppliedVerbBase` のコメントが理由を持つ —— discriminantが
    `verb` と `change` の2つに分かれているので、どちらの分岐からも `applied.change` を
    触れるようにするには全メンバーに(値が無くても)キーが要る)。
    """
    text = SESSION_TS.read_text(encoding="utf-8")
    start = text.index(f"interface {interface_name} {{")
    end = text.index("}", start)
    body = _strip_comments(text[start + text[start:].index("{") + 1 : end])
    return {
        name: spec
        for name, spec in _field_specs(body).items()
        if spec.arms != ("undefined",)
    }


def _ts_applied_members() -> list[dict[str, Any]]:
    """`export type Applied = ...` の union を1メンバ1件に分解する。

    `export type Applied =` から `export interface PreviewPayload` の手前までに**区間を
    絞ってから**(`_ts_entity_kind_table_keys()` と同じ作法)、行頭2スペースの `  | ` で
    割る。各メンバは `(AppliedVerbBase & { ... })` の形。

    **この形から外れたら黙って取りこぼさず落ちる。** `  | ` にアンカーして割っている以上、
    「1行に2メンバ」「メンバの末尾に `| SomeAlias` を足す」のような**有効なTS構文**は
    別チャンクにならず消える。各チャンクが `(<基底> & { ... })` **ちょうど**であることを
    前後から確かめて、そうでなければ assert で止める(取りこぼしを緑にしない)。
    """
    text = SESSION_TS.read_text(encoding="utf-8")
    start = text.index("export type Applied =")
    end = text.index("export interface PreviewPayload")
    block = _strip_comments(text[start:end])

    head, *chunks = re.split(r"^  \| ", block, flags=re.M)
    assert head.strip() == "export type Applied =", (
        "union の切り出しがずれている(先頭に想定外の中身がある。1メンバ目を行頭の "
        f"`  | ` で始めずに書いていないか): {head.strip()[:160]!r}"
    )
    assert chunks, "union のメンバを1つも切り出せていない"

    members: list[dict[str, Any]] = []
    for chunk in chunks:
        base_match = re.match(r"\s*\(\s*(Applied\w*Base)\s*&\s*(?=\{)", chunk)
        assert base_match, (
            "メンバが `(<基底> & {` の形で始まっていない(前に別の型を `|` で"
            f"並べていないか): {chunk.strip()[:160]!r}"
        )
        inner_start = chunk.index("{", base_match.end() - 1)
        inner_end = chunk.rindex("}")
        body = chunk[inner_start + 1 : inner_end]
        assert "{" not in body and "}" not in body, (
            "メンバの中に入れ子のオブジェクトがある(または1行に複数メンバを書いている)。"
            f"このパーサは平坦な形しか読めないので読み方を直すこと: {chunk.strip()[:160]!r}"
        )
        tail = chunk[inner_end + 1 :].strip()
        assert tail == ")", (
            "メンバが `})` で終わっていない —— 末尾に別の型を `|` で足すと、その型は"
            "どの検査にも現れないまま黙って無視される。1メンバにつき1つ、行頭の `  | ` で"
            f"始めて書くこと: {chunk.strip()[:160]!r}"
        )
        fields = _field_specs(body)
        discriminant = "verb" if "verb" in fields else "change" if "change" in fields else None
        assert discriminant, f"メンバに verb / change のどちらも無い: {chunk.strip()[:160]!r}"
        arms = fields[discriminant].arms
        literals = {arm[1:-1] for arm in arms if len(arm) >= 2 and arm.startswith("'")}
        assert len(literals) == len(arms), (
            f"{discriminant} に文字列リテラルでない型が混ざっている: "
            f"{fields[discriminant].text!r}"
        )
        merged = dict(_ts_base_fields(base_match.group(1)))
        merged.update(fields)
        members.append(
            {
                "base": base_match.group(1),
                "discriminant": discriminant,
                "literals": literals,
                "fields": merged,
            }
        )
    return members


def _ts_members_by_literal() -> dict[tuple[str, str], dict[str, Declared]]:
    """`("verb", "fillet")` → そのメンバが宣言しているフィールド(基底込み)。

    `verb: 'add_line' | 'add_circle' | ...` のように**1メンバが複数のリテラルを束ねる**
    ことがあるので、リテラルごとに開いて持つ。
    """
    table: dict[tuple[str, str], dict[str, Declared]] = {}
    for member in _ts_applied_members():
        for literal in member["literals"]:
            key = (member["discriminant"], literal)
            assert key not in table, f"{key} が2つのメンバに現れる(union が壊れている)"
            table[key] = member["fields"]
    return table


# --------------------------------------------------------------------------------------
# 走らせ方(**表そのものは verb の一覧ではない**。一覧は catalog が導く)
# --------------------------------------------------------------------------------------
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


@_runner("move_sketch_entities")
def _run_move_sketch_entities(session: Session, tmp_path: Path) -> dict:
    sketch_id = _sketch(session)
    entity_id = session.add_line(sketch_id, [0.0, 0.0], [10.0, 0.0])["entity_id"]
    _select(session, [{"sketch": sketch_id, "entity": entity_id, "point": None}])
    return session.move_sketch_entities(3.0, 4.0)


@_runner("move_body")
def _run_move_body(session: Session, tmp_path: Path) -> dict:
    body, _ = _box(session)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    return session.move_body(3.0, 4.0, 5.0)


@_runner("delete")
def _run_delete(session: Session, tmp_path: Path) -> dict:
    """**ボディと図形を混ぜて**走らせる —— 片方だけで走らせると、宣言の
    `bodies` / `entities` / `sketch_ids` のどれかが一度も中身を持たないまま緑になる。"""
    body, sketch_id = _box(session)
    entity_id = session.add_circle(sketch_id, [5.0, 5.0], 2.0)["entity_id"]
    _select(
        session,
        [
            {"body": body.id, "kind": "face", "index": 1},
            {"sketch": sketch_id, "entity": entity_id, "point": None},
        ],
    )
    return session.delete()


@_runner("set_preserve_bodies")
def _run_set_preserve_bodies(session: Session, tmp_path: Path) -> dict:
    body, _ = _box(session)
    _select(session, [{"body": body.id, "kind": "body"}])
    return session.set_preserve_bodies()


@_runner("set_exclude_bodies")
def _run_set_exclude_bodies(session: Session, tmp_path: Path) -> dict:
    body, _ = _box(session)
    _select(session, [{"body": body.id, "kind": "body"}])
    return session.set_exclude_bodies()


@_runner("set_fixtures")
def _run_set_fixtures(session: Session, tmp_path: Path) -> dict:
    body, _ = _box(session)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    return session.set_fixtures()


@_runner("set_loads")
def _run_set_loads(session: Session, tmp_path: Path) -> dict:
    """**ベクトルの向きで走らせる** —— `direction_vector` が `null` になる `'normal'` で
    走らせると、宣言の配列側(`number[]`)が一度も観測されないまま緑になる。"""
    body, _ = _box(session)
    _select(session, [{"body": body.id, "kind": "face", "index": 1}])
    return session.set_loads(250.0, [0.0, 0.0, -1.0])


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
    """**`path` / `name` が `null` で来る唯一の経路**(開いたファイルが無い)。

    `change: 'document'` の `| null` が実測で裏づくのはここだけなので、`open_document`
    (どちらも文字列)と対にして残しておくこと。
    """
    return session.new_document()


@_runner("open_document")
def _run_open_document(session: Session, tmp_path: Path) -> dict:
    session.add_circle(_sketch(session), [0.0, 0.0], 5.0)
    session.save_document("applied-type-sync.json")
    return session.open_document("applied-type-sync.json")


@pytest.fixture(scope="module")
def applied_by_command(tmp_path_factory) -> dict[str, Any]:
    """対象コマンドを**全部1回ずつ**走らせて `applied` を集める。

    メンバ単位の判定(オプショナルの要否)には**同じメンバを出す全コマンドの観測**が要る
    ので、テストごとではなくここでまとめて採る(`change: 'document'` は `new_document` と
    `open_document` の2コマンドが出し、`path` が `null` になるのは前者だけ)。

    1つが落ちても他の突合まで潰さないよう、例外は握って値の代わりに入れる(受け側が落とす)。
    """
    results: dict[str, Any] = {}
    for name in sorted(_target_commands()):
        runner = _RUNNERS.get(name)
        if runner is None:
            continue  # `test_every_target_command_has_a_runner` が落とす
        try:
            session = Session(None, DEVIATION, models_dir=tmp_path_factory.mktemp(name))
            results[name] = runner(session, tmp_path_factory.mktemp(f"{name}-files"))
        except Exception as error:  # noqa: BLE001 —— 受け側で赤くする
            results[name] = error
    return results


def _applied(command: str, applied_by_command: dict[str, Any]) -> dict:
    """1コマンドの実応答を取り出す。**走らせられていなければ落とす(skipしない)。**"""
    if command not in applied_by_command:
        pytest.fail(
            f"{command!r} を走らせる手続きが無い —— tests/test_applied_type_sync.py の "
            "`_RUNNERS` に登録すること(登録を忘れたまま緑になると、そのコマンドの実応答と "
            "web/src/session.ts の宣言のずれが誰にも見られない)"
        )
    applied = applied_by_command[command]
    if isinstance(applied, BaseException):
        raise AssertionError(
            f"{command!r} の走らせ方が失敗した(`_RUNNERS` の手続きを直すこと): {applied!r}"
        ) from applied
    return applied


def _compare(command: str, key: tuple[str, str], applied: dict) -> None:
    """実応答 `applied` を、TS側のメンバ `key` の宣言と突き合わせる(在不在 + 型)。"""
    declared = _ts_members_by_literal().get(key)
    assert declared is not None, f"Applied に {key[0]}: '{key[1]}' のメンバが無い"

    present = set(applied) | set(TRANSPORT_FIELDS)
    undeclared = present - set(declared)
    assert not undeclared, (
        f"{command} の実応答に web/src/session.ts が宣言していないキーがある: "
        f"{sorted(undeclared)}"
    )
    missing = {name for name, spec in declared.items() if not spec.optional} - present
    assert not missing, (
        f"web/src/session.ts が {key[0]}: '{key[1]}' に必須で宣言しているが "
        f"{command} の実応答に無いキーがある: {sorted(missing)}"
    )

    problems = [
        f"{name}: {reason}"
        for name, value in applied.items()
        if (reason := _type_mismatch(declared[name], value))
    ]
    assert not problems, (
        f"{command}({key[0]}: '{key[1]}')の型が web/src/session.ts と食い違う:\n  "
        + "\n  ".join(problems)
    )


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
        body_fields |= set(member["fields"]) - {"verb", "change", "seq", "client_op"}
    assert len(body_fields) >= 10, (
        f"メンバ本体のフィールドを読めていない(走査が空振りしている): {sorted(body_fields)}"
    )


def test_ts_applied_types_are_readable() -> None:
    """**型まで読めている**ことを見る(名前だけ拾えて型が空でも緑、を防ぐ)。

    型を見ない検査は「名前は同じで型だけ壊れた」食い違いを素通りさせる —— それが
    このファイルが塞ごうとしているドリフトの本体なので、走査の空振りを別立てで見る。
    """
    specs = [spec for member in _ts_applied_members() for spec in member["fields"].values()]
    arrays = sorted({spec.name for spec in specs if spec.is_array})
    assert len(arrays) >= 3, f"`T[]` の宣言を読めていない: {arrays}"
    nullables = sorted({spec.name for spec in specs if spec.is_nullable})
    assert len(nullables) >= 2, f"`| null` の宣言を読めていない: {nullables}"
    primitives = sorted(
        {spec.name for spec in specs if _primitive_arms(spec.arms) not in (None, [])}
    )
    assert len(primitives) >= 10, f"素の型の宣言を読めていない: {primitives}"


def test_ts_applied_base_fields_are_readable() -> None:
    """基底が配る共通フィールドを読めていて、**プレースホルダを外せている**。"""
    verb_base = _ts_base_fields("AppliedVerbBase")
    assert {"seq", "client_op"} <= set(verb_base), (
        f"AppliedVerbBase を読めていない: {sorted(verb_base)}"
    )
    assert "change" not in verb_base, (
        "`change?: undefined` を外せていない —— 実行時には来ないプレースホルダなので"
        "突合に混ぜると全verbが赤くなる"
    )
    change_base = _ts_base_fields("AppliedChangeBase")
    assert "client_op" in change_base, f"AppliedChangeBase を読めていない: {sorted(change_base)}"
    assert not ({"verb", "seq"} & set(change_base)), (
        "`verb?: undefined` / `seq?: undefined` を外せていない —— 実行時には来ない"
        f"プレースホルダである: {sorted(change_base)}"
    )


def test_catalog_applied_verbs_is_readable() -> None:
    """宣言側から対象verbを導けている(フラグの綴りを変えて0件になったら赤)。"""
    verbs = _catalog_applied_verbs()
    assert len(verbs) >= 10, f"catalog から対象verbを導けていない: {sorted(verbs)}"
    changes = _catalog_change_commands()
    assert len(changes) >= 4, f"外した側が導けていない: {sorted(changes)}"


def test_every_target_command_has_a_runner() -> None:
    """対象コマンドに**走らせ方が全部ある**。

    **ここを `skip` にしない。** catalogが対象と判定したコマンドを走らせられないのは
    検査の欠落であり、飛ばすと「新しいverbをcatalogとTSに足して `_RUNNERS` への登録だけ
    忘れた」が緑で通ってしまう(実応答と宣言のずれが誰にも見られない)。
    """
    missing = _target_commands() - set(_RUNNERS)
    assert not missing, (
        f"走らせ方が無いコマンドがある: {sorted(missing)}。"
        'tests/test_applied_type_sync.py の `_RUNNERS` に `@_runner("<名前>")` で'
        "登録すること"
    )


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
# 完了条件2: 実応答と宣言(名前 + 型)が一致する
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize("verb", sorted(_catalog_applied_verbs()))
def test_applied_member_fields_match_the_runtime_response(
    verb: str, applied_by_command: dict[str, Any]
) -> None:
    """**実際に verb を走らせた返り値**が、TS側の該当メンバの宣言に収まる。

    見るのはキーの在不在と、配列 / null / 素の型(モジュールdocstringの一覧)。
    """
    applied = _applied(verb, applied_by_command)
    assert applied.get("verb") == verb, (
        f"{verb!r} の応答が verb を名乗っていない({applied.get('verb')!r})。"
        "対象verbの導き方(_catalog_applied_verbs)が実態とずれている。"
    )
    _compare(verb, ("verb", verb), applied)


@pytest.mark.parametrize("command", sorted(_catalog_change_commands()))
def test_the_excluded_commands_really_emit_change_not_verb(
    command: str, applied_by_command: dict[str, Any]
) -> None:
    """verb名の突合から**外した**コマンドが、本当に `change` 系の封筒を返す。

    外す判断(`_catalog_applied_verbs()` の4条件)が間違っていれば、ここで `verb` が出て赤くなる。
    ついでにフィールドも突き合わせる —— 対象外にしたのは**verb名の集合比較**だけで、
    形の検査まで免除しているわけではない。
    """
    applied = _applied(command, applied_by_command)
    assert "verb" not in applied, (
        f"{command!r} は verb を返している —— verb名の突合から外したのは誤りである。"
        "_catalog_applied_verbs() の条件を直すこと。"
    )
    change = applied.get("change")
    assert change, f"{command!r} の応答が change を持たない: {sorted(applied)}"
    _compare(command, ("change", change), applied)


@pytest.mark.parametrize("member", sorted(_ts_members_by_literal()))
def test_optional_declarations_are_justified(
    member: tuple[str, str], applied_by_command: dict[str, Any]
) -> None:
    """**`?` が付いているのは、実行で本当に欠けたフィールドだけ**であること。

    1回の実行では「たまたま入っていた」と区別できないので、**そのメンバを出す全コマンドの
    観測**を束ねて見る(`change: 'document'` は `new_document` と `open_document` の2つ)。

    - 一度も欠けなかったのに `?` が付いている → 実際には必須なのにUI側が毎回ガードを強いられる
    - 欠けることがあるのに `?` が無い → `_compare` の必須チェックが先に落ちるが、
      どちらの言い方でも同じずれを指す
    """
    kind, literal = member
    runs = {
        name: applied
        for name, applied in applied_by_command.items()
        if isinstance(applied, dict) and applied.get(kind) == literal
    }
    assert runs, (
        f"{kind}: '{literal}' を出すコマンドを1つも走らせていない —— 宣言だけあって実体を"
        "確かめていないメンバである(`_RUNNERS` を足すか、宣言のほうが古い)"
    )
    for name, spec in _ts_members_by_literal()[member].items():
        absent_in = sorted(
            command
            for command, applied in runs.items()
            if name not in (set(applied) | set(TRANSPORT_FIELDS))
        )
        if spec.optional:
            assert absent_in, (
                f"{kind}: '{literal}' の `{name}?` は optional だが、走らせた "
                f"{sorted(runs)} のどれでも実応答に入っている。`?` を外すこと"
            )
        else:
            assert not absent_in, (
                f"{kind}: '{literal}' の `{name}` は必須だが {absent_in} の実応答に無い。"
                "`?` を付けるか、Python側を揃えること"
            )


# --------------------------------------------------------------------------------------
# 完了条件3: 状態配信(`ServerState`)のフィールド名も食い違わない
# --------------------------------------------------------------------------------------
# `Applied` と同じ穴が**状態配信の側**にもある。`Session.state_meta()` はWebSocketで
# 操作のたびに丸ごと配られるが、キーを1つ足しても消しても `tsc` は何も言わない。
#
# **ここで見るのはフィールド名の集合だけ**である。`Applied` 側のような型の突合まではしない
# —— `ServerState` は `BodyMeta[]` / `SelectionState` / `SketchState[]` のような独自型が
# 大半で、素の型として見られるものが少なく、名前の一致だけでも「足し忘れ・消し忘れ」という
# 実際に起きるずれは全部捕まる。型まで見たくなったら `_compare` と同じ道具が使える。
def _ts_interface_body(name: str) -> str:
    """`export interface <name> { ... }` の中身を**波かっこを数えて**取り出す。

    `_ts_base_fields` の「最初の `}` まで」では足りない —— `ServerState` の `log` は
    `Array<{ ... }>` を1行で書いており、途中の `}` で切れてしまう。
    """
    text = SESSION_TS.read_text(encoding="utf-8")
    start = text.index(f"interface {name} {{")
    open_at = text.index("{", start)
    depth = 0
    for index in range(open_at, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[open_at + 1 : index]
    raise AssertionError(f"interface {name} の `}}` が見つからない")


def _ts_server_state_fields() -> set[str]:
    """`ServerState` が宣言しているフィールド名(**入れ子の中は数えない**)。

    行頭2スペースちょうどの `名前:` / `名前?:` だけを拾う。入れ子の型は同じ行の中か
    より深い字下げにいるので、この形にアンカーすれば混ざらない。
    """
    body = _strip_comments(_ts_interface_body("ServerState"))
    return set(re.findall(r"^  (\w+)\??:", body, flags=re.M))


def test_server_state_fields_match_the_runtime_state(tmp_path) -> None:
    """`Session.state_meta()` が実際に返すキーと `ServerState` の宣言が一致する。

    落ちるのは「Python側にキーを足してTSに足し忘れた」「TSに宣言だけ残した」とき。
    どちらも実機で `undefined` を読むまで誰も気づかない類のずれである。
    """
    declared = _ts_server_state_fields()
    assert len(declared) >= 8, f"ServerState を読めていない: {sorted(declared)}"

    session = Session(None, DEVIATION, models_dir=tmp_path / "models")
    _box(session)  # 空の文書でも全キーは出るが、中身のある文書で見るほうが実態に近い
    actual = set(session.state_meta())

    assert actual == declared, (
        "web/src/session.ts の ServerState が Session.state_meta() と食い違う"
        f"(実応答のみ={sorted(actual - declared)} / TSのみ={sorted(declared - actual)})"
    )
