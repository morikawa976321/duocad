"""UIのコマンドIDが、サーバが受け取れる名前に**実在するか**を静的に検査する。

## なぜ要るか

同じverbの表面が3層にある:

1. `duocad/server.py` の `SKETCH_COMMANDS` / `FEATURE_COMMANDS` / `FILE_COMMANDS`(WebSocket)
2. `duocad/mcp_server.py` のツール(MCP。**`tests/test_mcp_verbs.py` が一致で固定済み**)
3. `web/src/commands/*.ts` の `CommandDef.id`(UIが引数を集めて送る先)

このうち **1と3の食い違いだけが誰にも見られていなかった**。TS側のIDを1文字打ち間違えても
`tsc` は通り(ただの文字列)、pytest も通り、**実ブラウザのsmokeを走らせて初めて**
`unknown command` で出る。この抜けを、`test_boundaries.py` と
同じ「ソースを静的に読む」やり方で塞ぐ。

## 1と2は宣言から導出になった(2026-08-20)

**1と2はもう手で書かれていない** —— `duocad/catalog.py` の `COMMANDS` が唯一の宣言で、
WSの表もMCPのスキーマもそこからの導出である。したがってこの検査が見るのは
「**宣言と3層目(TS)の食い違い**」になった。3層目を宣言から生成しないのは、TS側が持つのが
**引数の集め方**(どのクリックを待つか・入力欄のid・ボタンのid)であって宣言には無い
情報だからで、名前の一致だけをここで機械的に固定する。

## 対象集合はどの層でも引数ではない(2026-08-20)

かつてはWSだけが面・辺・領域を引数で受けており(`fillet(edges, radius, body_id)`)、
MCPの `fillet(radius)` と形が違った。**その差は無くなった** —— 対象集合は
どちらの層でもコアの確定選択から取る(`duocad/selection.py` の `require_confirmed` /
`require_edges`)ので、WSのコマンドに残る引数は数値・名前・パスだけである。
下の `test_no_websocket_command_takes_a_target_set` がそれを機械で固定する。
"""
from __future__ import annotations

import ast
import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
COMMANDS_DIR = REPO_ROOT / "web" / "src" / "commands"

#: `web/src/commands/*.ts` の `CommandDef.category` に書ける値。
#:
#: **送信経路の分岐がこれ1つで決まる**(順序依存の暗黙のフォールバックを置かない)。
#: `select_in_sketch` の `ui` はサーバへ何も送らない道具で、Python側の宣言には無い。
UI_CATEGORIES = {"sketch", "feature", "file", "solid", "ui"}


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
    # `plane` は2026-08-22に足した。**面参照がこの名前で検査を素通りしていた** ——
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
    tree = ast.parse((REPO_ROOT / "duocad" / "server.py").read_text(encoding="utf-8"))
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


def _ui_command_categories() -> dict[str, str]:
    """`web/src/commands/*.ts` の `CommandDef` から「ID -> category」を読む。

    `id` の次から**次の `id` の手前まで**を1つの定義とみなす(定義の途中に
    コメントが挟まっても効く)。`category` を書き忘れた定義は値が空文字になり、
    下の検査が名指しで落とす。
    """
    found: dict[str, str] = {}
    for path in sorted(COMMANDS_DIR.glob("*.ts")):
        text = path.read_text(encoding="utf-8")
        marks = list(re.finditer(r"^\s*id:\s*'([^']+)'", text, re.M))
        for index, match in enumerate(marks):
            end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
            chunk = text[match.end():end]
            category = re.search(r"^\s*category:\s*'([^']+)'", chunk, re.M)
            found[match.group(1)] = category.group(1) if category else ""
    return found


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
    source = (REPO_ROOT / "duocad" / "server.py").read_text(encoding="utf-8")
    dispatch = source[source.index("async def _dispatch("):]
    missing = [
        command.name
        for command in catalog.COMMANDS
        if command.ws == "special" and f'"{command.name}"' not in dispatch
    ]
    assert not missing, f"`_dispatch` に分岐が無い(宣言だけある): {missing}"


def test_every_ui_command_declares_the_same_category_as_the_declaration():
    """**UIの `category` はコアの宣言と同じ値**(`ui` を除く)。

    ここが落ちるのは「TS側でだけ分類を変えた/書き忘れた」とき。分類はUIの
    送信経路そのもの(`App.svelte` の `runCommand`)なので、ずれると
    **スケッチのverbがファイル操作として送られる**ような壊れ方をする。
    以前は3つの判定関数を順に当てる形で、どれにも当たらないverbは黙って
    `extrude_face` として送られていた。
    """
    declared = {command.name: command.category for command in _catalog().COMMANDS}
    wrong: dict[str, str] = {}
    for command_id, category in _ui_command_categories().items():
        if category not in UI_CATEGORIES:
            wrong[command_id] = f"未知の category: {category!r}"
        elif category == "ui":
            if command_id not in EXEMPT_IDS:
                wrong[command_id] = "`ui` はサーバへ送らない道具だけ"
        elif command_id in declared and declared[command_id] != category:
            wrong[command_id] = f"宣言は {declared[command_id]!r} / UIは {category!r}"
    assert not wrong, f"UIの category が宣言と合っていない: {wrong}"


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
# Enter要否(即適用)の表 —— 2026-08-22
# --------------------------------------------------------------------------------------
#: `web/src/commands/command.ts` の `isHowInput` と同じ顔ぶれ(howの入力)。
#: **TS側へ種別を足したらここへも足す**(下の `_needs_enter` のdocstring)。
HOW_ARG_KINDS = {"number", "choice", "text"}


def _args_of(chunk: str) -> list[tuple[str, str]]:
    """1つの `CommandDef` の本文から `args: [...]` を読み、`(kind, max)` の並びを返す。

    `max` は選択引数のときだけ意味があり(`'1'` / `'null'` / 数字)、他は空文字。
    `build(...)` の本体を巻き込まないように**角括弧の対応で args だけを切り出す**
    (本体には `values.face` 等が並ぶだけだが、将来の書き足しで `kind:` が現れうる)。
    """
    head = chunk.find("args: [")
    if head < 0:
        return []
    start = chunk.index("[", head)
    depth = 0
    end = -1
    for index in range(start, len(chunk)):
        if chunk[index] == "[":
            depth += 1
        elif chunk[index] == "]":
            depth -= 1
            if depth == 0:
                end = index
                break
    if end < 0:
        return []
    region = chunk[start + 1:end]
    marks = list(re.finditer(r"kind:\s*'([a-z]+)'", region))
    args: list[tuple[str, str]] = []
    for index, match in enumerate(marks):
        tail = region[match.end():marks[index + 1].start() if index + 1 < len(marks) else len(region)]
        maximum = re.search(r"\bmax:\s*(null|\d+)", tail)
        args.append((match.group(1), maximum.group(1) if maximum else ""))
    return args


def _ui_command_args() -> dict[str, list[tuple[str, str]]]:
    """`web/src/commands/*.ts` から「ID -> 引数の並び」を読む(`_ui_command_categories` と同じ切り方)。"""
    found: dict[str, list[tuple[str, str]]] = {}
    for path in sorted(COMMANDS_DIR.glob("*.ts")):
        text = path.read_text(encoding="utf-8")
        marks = list(re.finditer(r"^\s*id:\s*'([^']+)'", text, re.M))
        for index, match in enumerate(marks):
            end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
            found[match.group(1)] = _args_of(text[match.end():end])
    return found


def _needs_enter(args: list[tuple[str, str]]) -> bool:
    """TS側の `needsEnter`(`web/src/commands/command.ts`)と同じ判定を、宣言から作り直す。

    **写しであることに意味がある** —— 判定はTSの1関数にしか無く、そこを書き換えれば
    UIの挙動は静かに変わる。ここが同じ規則を独立に持っていると、下の表との突き合わせで
    「どのverbのEnter要否が変わったか」が名指しで出る。

    規則は2つ: **対象を複数取りうる**(選択の `max !== 1` / `entities`)か、
    **howの入力がある**(数値・選択肢・文字列)なら Enter が要る。どちらでもなければ即適用。
    **列挙していない種別は即適用の側**(TS側の既定と同じ。点がそれ)。
    """
    for kind, maximum in args:
        if kind == "selection" and maximum != "1":
            return True
        if kind == "entities" or kind in HOW_ARG_KINDS:
            return True
    return False


#: **Enterが要るか**の表(True = Enterで確定 / False = 引数が揃ったクリックで即適用)。
#:
#: 2026-08-22の本人決定(engine `2026-08-22-duocad-ui-interaction-principles`)で、
#: 「省けるクリックは徹底的に省く」の実装として入った。**即適用は3つ**である:
#: `add_line`(点2つで揃う)・`make_editable_copy`(面1つで揃う)・
#: `sketch_create`(貼る先の平面1つで揃う。2026-08-22に足した)。
#: verbを足したらここへ1行足すことになる —— そのとき**Enterが要るかを1回考える**のが狙いで、
#: 表そのものが実装の入力になっているわけではない(判定は `args` からの導出が正)。
#:
#: **`save_document`/`open_document` はこの表に無い**(2026-08-22。engine
#: `duocad-save-location-fixed-folder`)。以前は「パス」を打たせる `text` 引数で
#: Enterが要る側だったが、`CommandHost` から外れて `FilesPanel.svelte`
#: (`duocad/models/` の一覧をクリック / 名前を打って新規保存)へ移ったため、
#: この表(`web/src/commands/*.ts` の `CommandDef` から読む)には現れなくなった。
#: サーバ側のverbとしては変わらず存在する(`tests/test_files_ui.py` が固定)。
ENTER_REQUIRED = {
    "extrude_face": True,          # 面1つ + 距離(how)
    "extrude": True,               # 領域は何個でも + 距離 + 結合/切り取り
    "fillet": True,                # 辺は何本でも + 半径
    "chamfer": True,               # 辺は何本でも + 距離
    "pattern_rectangular": True,   # 図形は何個でも + 個数と間隔
    "pattern_circular": True,      # 図形は何個でも + 中心 + 個数と角度
    "import_step_editable": True,  # パス
    "export_step": True,           # パス
    "export_stl": True,            # パス + 偏差
    "make_editable_copy": False,   # **面1つだけ。howが無い → 即適用**
    "add_line": False,             # **点2つだけ。howが無い → 即適用**
    "sketch_create": False,        # **貼る先を1つ指すだけ。howが無い → 即適用**
    "add_circle": True,            # 中心 + 半径
    "add_rectangle": True,         # 左下 + 幅 + 高さ
    "add_arc": True,               # 中心 + 半径 + 角度2つ
    "select_in_sketch": False,     # 引数が無い(完成させるクリックが無いので発火もしない)
}


def test_the_command_arguments_are_readable():
    """先に**走査そのものが効いている**ことを見る(0件なら下の検査は常に緑になる)。"""
    args = _ui_command_args()
    assert len(args) == len(ENTER_REQUIRED), (
        f"UIのコマンドの数が変わった(Enter要否の表を更新すること): {sorted(args)}"
    )
    assert args["extrude_face"] == [("selection", "1"), ("number", "")], (
        f"引数の読み取りが壊れている: {args['extrude_face']}"
    )
    assert args["select_in_sketch"] == [], f"引数の読み取りが壊れている: {args['select_in_sketch']}"


def test_only_three_commands_apply_without_enter():
    """**Enter要否は `args` からの導出**であり、いま即適用になるのは3つだけである。

    per-commandのフラグを置かない設計(`command.ts` の `needsEnter`)なので、引数の書き方を
    変えると**別のverbのEnter要否が黙って変わる** —— 例えば `make_editable_copy` の
    `max` を `null` にすると「面を選び終わったと言う手」が復活し、`add_circle` から
    半径を外すと**中心をクリックした瞬間に円ができる**。ここはその変化を名指しで落とす。
    """
    derived = {name: _needs_enter(args) for name, args in _ui_command_args().items()}
    assert derived == ENTER_REQUIRED, (
        "Enter要否が表と食い違う(意図した変更なら表と README「操作の型」を更新すること): "
        f"{ {name: value for name, value in derived.items() if ENTER_REQUIRED.get(name) != value} }"
    )
    immediate = sorted(name for name, value in derived.items() if not value)
    assert immediate == [
        "add_line",
        "make_editable_copy",
        "select_in_sketch",
        "sketch_create",
    ], f"即適用になるコマンドの顔ぶれが変わった: {immediate}"


def test_the_immediate_apply_fires_only_on_the_users_own_click():
    """即適用の引き金は**この画面のクリック**であって、選択集合の中身ではない。

    2026-08-22のQA指摘。選択集合は**AIも `modify_selection` で書き換えられる**ので、
    「対象が1つ揃った」だけで発火させると、**人が何も指していないのに文書が変わる**
    (実測: 直す前のビルドで `smoke-mcp.mjs` が部品 27 -> 28 を踏んだ)。配線は3段あり、
    1つでも外れると穴が開くので、ブラウザを立てなくても気づけるようにここで固定する:

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
