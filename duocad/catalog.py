"""verbの単一宣言 —— **1 verbの追加 = ここに1エントリ**。

## なぜこれがあるか

同じverbのメタ情報(名前・引数・カテゴリ・プレビュー可否・AIへの説明)は、以前は
4ファイル9箇所に手で書かれていた —— `server.py` の4つの表、`mcp_server.py` の
ツール定義と `call()` のハンドラ表、frontendの3つの判定関数。**verbを1つ足すたびに
その全部を同期する作業が発生し、片方だけ直したときは誰も気づかなかった**
(実測: `fillet` は完全同一のタプルが2つの表に、`extrude` は `body_id` の有無が
表ごとに食い違った状態で放置されていた)。v1.1以降は move_face / revolve / shell /
primitive / boolean / hole とverbを足し続ける予定なので、同期作業と食い違いバグが
verb数だけ増える。

**このファイルが正**で、以下はすべてここからの導出である:

- `duocad/server.py` … `PREVIEWABLE_VERBS` / `SKETCH_COMMANDS` / `FEATURE_COMMANDS` /
  `FILE_COMMANDS`(WebSocketの許可リスト)
- `duocad/mcp_server.py` … `tools/list` が返すツール定義と `tools/call` のハンドラ表

frontend(`web/src/commands/*.ts`)だけは別に持つ —— TS側が持つのは
**引数の集め方**(どのクリックを待つか・入力欄のid・ボタンのid)であって、ここには
無い情報だからである。名前が食い違わないことは `tests/test_ui_command_wiring.py` が
機械で見る(TSのコマンドIDは全部ここに実在する)。

## 書き方(verbを1つ足すとき)

1. `duocad/verbs/` にverb本体を書き、`duocad/__init__.py` に登録の1行を足す
2. `Session` にラッパを書く(対象集合は `selection.require_*` から取る。引数は数値・名前・パスだけ)
3. `DocumentTools` にMCP側のラッパを書く(名前はverb名と同じ綴りにする)
4. **このファイルの `COMMANDS` に1エントリ足す**
5. UIから踏むなら `web/src/commands/*.ts` に引数の集め方を書く(`category` を忘れない)

## 対象集合は引数ではない(2026-08-20)

面・辺・領域はどの層でも引数で受けない —— コアの確定選択から取る
(`duocad/selection.py` の `require_confirmed` / `require_edges`)。したがって
ここに並ぶ引数は**数値・名前・パスだけ**である。`body_id` が残っているのは
「どのBodyへ足す/から引くか」のような**名前**であって対象集合ではないもの
(Bodyは選択集合の項目ではない)。`tests/test_ui_command_wiring.py` の
`test_no_websocket_command_takes_a_target_set` がこれを機械で固定する。

**OCP / build123d をimportしない**(純データ。`tests/test_boundaries.py` が検査する)。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

#: `Command.category` に書ける値。
#:
#: - `query`     … 読み取り専用(文書を変えない・ログに載らない)
#: - `selection` … 選択集合と可視性(「何が選べるか」。`set_visible` はログに載るverbだが、
#:                 見えない面は選べない以上、可視性は選択機構の一部である)
#: - `sketch`    … スケッチのverb(スケッチモードで踏む)
#: - `feature`   … ソリッドのverb
#: - `file`      … ファイル操作(開く・保存・読み込み・書き出し)
#: - `history`   … undo / redo
#: - `protocol`  … WebSocketだけの口(verbではない。状態の要求・プレビュー・辺の折れ線)
CATEGORIES = ("query", "selection", "sketch", "feature", "file", "history", "protocol")

#: `Command.ws` に書ける値。
#:
#: - `table`   … `server.py` の表(`SKETCH_COMMANDS` 等)に載り、`_dispatch` が機械的に捌く
#: - `special` … WebSocketから踏めるが `_dispatch` が個別に捌く(応答の形が他と違うもの)
#: - `none`    … WebSocketには開けていない(`propose_selection` はAIが書く経路)
WS_SURFACES = ("table", "special", "none")


@dataclass(frozen=True)
class Param:
    """verbが受け取る引数1つ。

    **1つの引数を2度書かない**ための持ち物である。`type` と `description` はMCPの
    入力スキーマになり、`ws` はWebSocketの表に載るかどうかを決める。
    """

    #: 引数名。**MCP側の綴りが正**(WSだけ違う場合は `ws_name` を書く)。
    name: str
    #: JSON Schema の `type`(`"number"` / `["number", "array"]` のような値)。
    type: Any
    #: AIに見せる説明。**何ができるかではなく、何を渡すか**を書く。
    description: str
    #: MCPで必須か。省略できる引数は実体側にも既定値があること。
    required: bool = True
    #: WebSocketのコマンドが受け取るか。**WSは載せたものを全部必須にする**
    #: (既定値で補うと、打ち忘れた寸法や `mode` が黙って既定で入る)。
    ws: bool = True
    #: WS側だけ名前が違うときの綴り(`sketch_create` の `base_plane` / `plane` だけ)。
    ws_name: Optional[str] = None

    @property
    def wire_name(self) -> str:
        """WebSocketのメッセージに載る名前(= `Session` のメソッドの引数名)。"""
        return self.ws_name or self.name


@dataclass(frozen=True)
class Command:
    """verb(またはコアへの口)1つの宣言。**追加はここ1箇所で済ませる。**"""

    #: **MCPツール名 == WSコマンド名 == UIコマンドID == 操作ログのverb名**。
    #: AIは `describe_document` のログを読んで次の一手を決めるので、綴りが違うと
    #: 対応表を推測させることになる(両方向のテストが固定している)。
    name: str
    #: `CATEGORIES` のどれか。WSの表の割り振りと、UI側の送信経路の分岐に対応する。
    category: str
    #: AIに見せる説明。**何ができるかではなく「いつ使うか」**を書く
    #: (説明文はAIがツールを選ぶ唯一の手がかり)。`mcp=False` のものは空でよい。
    description: str = ""
    params: tuple[Param, ...] = ()
    #: `Session` のメソッド名(WebSocket側の実体)。WSに開けていないものは None。
    #: **verb名と違ってよい**(`extrude` -> `extrude_regions` のように、
    #: コア側の歴史的な名前をverb名に合わせて改名しない)。
    session: Optional[str] = None
    #: WebSocketでの露出のしかた(`WS_SURFACES`)。
    ws: str = "table"
    #: MCPのツールとして公開するか(`save_document` / `open_document` は人だけの操作)。
    mcp: bool = True
    #: `Session.preview` が投機実行してよいか。**巻き戻せることを確認したverbだけ**を
    #: true にする —— `set_parameter` のような操作ログを頭から再生するverbは、再生が
    #: 過去のseqのundoスナップショットを書き換えるので `speculative` では戻らない
    #: (`speculative.py` の「戻さないもの」)。
    previewable: bool = False
    #: 文書を変えるか(= 操作ログに載り、応答が状態配信になるか)。`file` カテゴリだけが
    #: 使う —— 保存・書き出しは文書を変えないので、応答は書いた先のパスとバイト数だけを
    #: 要求した接続へ返す(`preview` と同じ扱い)。
    changes_document: bool = True
    #: 確定後に選択集合を全リセットするか(2026-08-20)。**面・辺・領域の添字は形状変更で
    #: 壊れる**(kernel.pyの既知の地雷)ので、形を変えるverbの確定後は選択を空にする ——
    #: 「失効/後継を返す契約」ではなく「全リセット」を選んだ(後継を返す契約は、連続性が
    #: 本当に必要なverbが現れてから)。`Session._reset_selection_if_needed` がここを見て
    #: 確定後に1箇所で発火するので、UI経由・MCP経由のどちらでも同じに効く。
    #: **失敗したverb(ログに積まれない)には効かない** —— 呼び出すのは成功パスだけ。
    resets_selection: bool = False
    #: 設計上の注記(なぜこの形か)。コードの読み手向けで、AIには渡さない。
    note: str = ""

    def __post_init__(self) -> None:
        if self.category not in CATEGORIES:
            raise ValueError(f"未知のカテゴリ: {self.category!r}({CATEGORIES})")
        if self.ws not in WS_SURFACES:
            raise ValueError(f"未知のWS露出: {self.ws!r}({WS_SURFACES})")
        if self.ws != "none" and self.session is None:
            raise ValueError(f"{self.name}: WSに開けるなら `session` が要る")
        if self.previewable and self.session is None:
            raise ValueError(f"{self.name}: プレビューは `Session` のメソッドを呼ぶ")

    @property
    def ws_params(self) -> tuple[str, ...]:
        """WebSocketのコマンドが受け取る引数名(**すべて必須**)。"""
        return tuple(p.wire_name for p in self.params if p.ws)

    @property
    def preview_params(self) -> tuple[str, ...]:
        """`preview` が受け取ってよい引数名。

        **WSの表より広いことがある** —— `extrude` の `body_id`(どのBodyへ足すか)は
        WSのコマンドでは受けない(アクティブなBodyへ足す)が、プレビューは
        行き先を指定した結果を見せられる。
        """
        return tuple(p.wire_name for p in self.params)

    def input_schema(self) -> dict:
        """MCPの `inputSchema`(JSON Schema)。"""
        properties = {
            p.name: {"type": p.type, "description": p.description} for p in self.params
        }
        required = [p.name for p in self.params if p.required]
        # **必須が1つも無いときは `required` を書かない**(空配列を出すと、
        # クライアントによっては「必須が指定されている」と読まれうる)。
        return {
            "type": "object",
            "properties": properties,
            **({"required": required} if required else {}),
            "additionalProperties": False,
        }

    def descriptor(self) -> dict:
        """MCPの `tools/list` が返す1件。"""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema(),
        }


# --------------------------------------------------------------------------------------
# 宣言(**verbを足すのはここ**)
# --------------------------------------------------------------------------------------
#: 並び順は `tools/list` に出る順序でもある(AIが最初に読む順)。
COMMANDS: tuple[Command, ...] = (
    # ---------------------------------------------------------------- 問い合わせ
    Command(
        name="describe_document",
        category="query",
        description=(
            "開いている文書のボディ一覧(body_id・名前・種別・可視性・面数・体積)と"
            "スケッチ・パラメータ一覧を返す。set_visible や describe_shape、"
            "pattern_* や set_parameter を呼ぶ前に、どのボディ・図形・寸法名が"
            "存在するかを知るために最初に呼ぶ。"
        ),
        params=(
            Param(
                "include_references",
                "boolean",
                "読み込んだSTEPの参照ボディ(kind='reference'・編集対象外で"
                "既定では隠されている)も含めるか。既定 false",
                required=False,
                ws=False,
            ),
            Param(
                "include_bbox",
                "boolean",
                "各ボディの境界箱(min/max/center/size)を含めるか。"
                "どの部品が手前・奥にあるかを知りたいときに true。既定 false",
                required=False,
                ws=False,
            ),
            Param(
                "include_regions",
                "boolean",
                "各スケッチの領域数(region_count)を含めるか。"
                "押し出す前に閉領域がいくつあるか確かめたいときに true。"
                "数え上げは計算なので既定 false",
                required=False,
                ws=False,
            ),
        ),
        ws="none",
    ),
    Command(
        name="set_visible",
        category="selection",
        description=(
            "ボディの表示・非表示を切り替える。手前の部品を隠すと奥の面が選べるようになる"
            "(可視性は選択機構の一部)。操作ログに載るので undo で戻せる。"
            "開いているブラウザの表示も即座に変わる。"
        ),
        params=(
            Param("body_id", "string", "describe_document が返した body_id"),
            Param("visible", "boolean", "true で表示、false で非表示"),
        ),
        session="set_visible",
        ws="special",
        note="WSでは `client_op`(どのクライアントの操作か)を添えるので `_dispatch` が個別に捌く",
    ),
    Command(
        name="describe_shape",
        category="query",
        description=(
            "1つのボディの形状を調べる(面数・辺数・面の種別内訳・体積・境界箱)。"
            "形は変えない読み取り専用。"
        ),
        params=(Param("body_id", "string", "describe_document が返した body_id"),),
        ws="none",
    ),
    Command(
        name="describe_predicates",
        category="query",
        description=(
            "propose_selection の predicate= に書ける述語の語彙(op・引数・種別)を"
            "知るときに呼ぶ。使える op を推測せず、ここに出た一覧だけを使う。"
        ),
        ws="none",
    ),
    Command(
        name="measure",
        category="query",
        description=(
            "いまの選択(提案のままでよい)の長さ・面積・法線などを測るときに呼ぶ。"
            "verb ではないので commit 前に提案を確かめる往復に使う。"
        ),
        ws="none",
    ),
    # ---------------------------------------------------------------- 選択
    # **コアの選択集合をそのまま叩く**(アプリ層に選択状態を持たせない)。人が直す経路
    # (`modify_selection`)とAIの提案を承認する経路(`commit_selection`)がUIから
    # 踏めないと、交互操作は片道のままになる。
    Command(
        name="get_selection",
        category="selection",
        description=(
            "いまの選択(項目・origin・件数)を読むときに呼ぶ。"
            "提案した直後や人が直したあとに、次の一手の対象を確認する。"
        ),
        ws="none",
    ),
    Command(
        name="propose_selection",
        category="selection",
        description=(
            "AIが選択を書き込むときに呼ぶ。refs(明示参照)か predicate(述語)の"
            "どちらか一方。結果は提案(origin=proposed)なので、verb の前に"
            "commit_selection が要る。"
        ),
        params=(
            Param(
                "refs",
                "array",
                "面・辺・頂点・領域への明示参照の配列。predicate とは同時に渡せない",
                required=False,
                ws=False,
            ),
            Param(
                "predicate",
                "object",
                "述語(describe_predicates の語彙)。refs とは同時に渡せない",
                required=False,
                ws=False,
            ),
        ),
        ws="none",
        note="**AIが書く経路なのでブラウザには開けない**(人はポインティングで直す)",
    ),
    Command(
        name="modify_selection",
        category="selection",
        description=(
            "選択の過不足を差分で直すときに呼ぶ(人が直す経路)。"
            "触ると origin は user になり、そのままで verb に渡せる。"
        ),
        params=(
            Param("add", "array", "選択に足す参照の配列", required=False),
            Param("remove", "array", "選択から外す参照の配列", required=False),
        ),
        session="modify_selection",
        ws="special",
        note="WSでは省略を空配列に落として `client_op` を添えるので個別に捌く",
    ),
    Command(
        name="commit_selection",
        category="selection",
        description=(
            "提案を確定するときに呼ぶ(中身は変えない)。"
            "AIが自分の提案を fillet 等へ渡す直前の明示の1手。"
        ),
        session="commit_selection",
        ws="special",
    ),
    Command(
        name="clear_selection",
        category="selection",
        description="選択を空にしたいときに呼ぶ。",
        session="clear_selection",
        ws="special",
    ),
    # ---------------------------------------------------------------- スケッチ
    # **既にMCPが持っていたverbをブラウザにも開けただけ**(選択の3コマンドと同じ)。
    # コアには何も足していない —— スケッチがAIからしか触れない状態は、この企画の
    # 中心命題「手動操作が一級の操作モード」に真正面から反する(実装計画書§8b C)。
    Command(
        name="sketch_create",
        category="sketch",
        description=(
            "新しいスケッチを作るときに呼ぶ。"
            "base_plane(XY/XZ/YZ)を渡すか、確定選択の平面(ちょうど1枚)に貼る。"
        ),
        params=(
            Param(
                "base_plane",
                "string",
                "基準平面名 XY / XZ / YZ。省略時は確定選択の面",
                required=False,
                ws_name="plane",
            ),
        ),
        session="sketch_create",
        note=(
            "**WSだけ引数名が `plane`** —— ブラウザは基準平面(`{kind:'base'}`)と"
            "面に貼る(`{kind:'face'}`)を同じ引数で送り分けるので、名前が `base_plane` だと嘘になる"
        ),
    ),
    Command(
        name="add_line",
        category="sketch",
        description="スケッチに線分を足すときに呼ぶ(平面上の2点)。",
        params=(
            Param("sketch_id", "string", "対象スケッチのID"),
            Param("p0", "array", "始点 [u, v]"),
            Param("p1", "array", "終点 [u, v]"),
        ),
        session="add_line",
        resets_selection=True,
    ),
    Command(
        name="add_circle",
        category="sketch",
        description="スケッチに円を足すときに呼ぶ。",
        params=(
            Param("sketch_id", "string", "対象スケッチのID"),
            Param("center", "array", "中心 [u, v]"),
            Param("radius", "number", "半径(mm)"),
        ),
        session="add_circle",
        resets_selection=True,
    ),
    Command(
        name="add_rectangle",
        category="sketch",
        description="スケッチに長方形を足すときに呼ぶ。",
        params=(
            Param("sketch_id", "string", "対象スケッチのID"),
            Param("corner", "array", "左下コーナー [u, v]"),
            Param("width", "number", "幅(mm)"),
            Param("height", "number", "高さ(mm)"),
        ),
        session="add_rectangle",
        resets_selection=True,
    ),
    Command(
        name="add_arc",
        category="sketch",
        description="スケッチに円弧を足すときに呼ぶ(中心・半径・開始角・終了角)。",
        params=(
            Param("sketch_id", "string", "対象スケッチのID"),
            Param("center", "array", "中心 [u, v]"),
            Param("radius", "number", "半径(mm)"),
            Param("start_deg", "number", "開始角(度・反時計)"),
            Param("end_deg", "number", "終了角(度・反時計)"),
        ),
        session="add_arc",
        resets_selection=True,
    ),
    # ---------------------------------------------------------------- フィーチャ
    Command(
        name="extrude",
        category="feature",
        description=(
            "確定選択の領域を押し出してソリッドを足す/切るときに呼ぶ。"
            "領域は選択から取る(引数では受けない)。"
        ),
        params=(
            Param("distance", "number", "押し出し量(mm・法線方向)"),
            Param(
                "mode",
                "string",
                "join(足す・既定) または cut(切る)",
                required=False,
            ),
            Param(
                "body_id",
                "string",
                "対象ボディ。省略時はアクティブ(無ければ新規作成)",
                required=False,
                ws=False,
            ),
        ),
        session="extrude_regions",
        previewable=True,
        resets_selection=True,
        note=(
            "**`mode` はWSでだけ必須**(既定の join に落ちると「切るつもりで足した」が"
            "黙って通る。UIは人に選ばせてから送る)。**`body_id` はWSのコマンドには無い** —— "
            "行き先を指せるのは MCP と `preview` だけで、WSはアクティブなBodyへ足す"
        ),
    ),
    Command(
        name="extrude_face",
        category="feature",
        description=(
            "確定選択の面(ちょうど1枚)を押し出すときに呼ぶ。"
            "既存ソリッドの面を伸ばす経路。"
        ),
        params=(Param("distance", "number", "押し出し量(mm)"),),
        session="extrude",
        ws="special",
        previewable=True,
        resets_selection=True,
        note="`Session` 側の名前だけ `extrude`(面の押し出しが先にあった)",
    ),
    Command(
        name="fillet",
        category="feature",
        description=(
            "確定選択の辺を丸めるときに呼ぶ。"
            "半径は1つの数値か、選択順に対応する配列。"
        ),
        params=(
            Param(
                "radius",
                ["number", "array"],
                "半径(mm)。辺ごとのリストも可(選択順)",
            ),
        ),
        session="fillet",
        previewable=True,
        resets_selection=True,
    ),
    Command(
        name="chamfer",
        category="feature",
        description=(
            "確定選択の辺を面取りするときに呼ぶ。"
            "距離は1つの数値か、選択順に対応する配列。"
        ),
        params=(
            Param(
                "distance",
                ["number", "array"],
                "面取り距離(mm)。辺ごとのリストも可(選択順)",
            ),
        ),
        session="chamfer",
        previewable=True,
        resets_selection=True,
    ),
    Command(
        name="pattern_rectangular",
        category="feature",
        description="スケッチ上の図形を矩形に反復するときに呼ぶ。",
        params=(
            Param("sketch_id", "string", "対象スケッチのID"),
            Param("entities", "array", "反復する図形IDの配列"),
            Param("dx", "number", "X方向の間隔(mm)"),
            Param("dy", "number", "Y方向の間隔(mm)"),
            Param("nx", "integer", "X方向の個数(元を含む)"),
            Param("ny", "integer", "Y方向の個数(元を含む)"),
        ),
        session="pattern_rectangular",
        resets_selection=True,
        note=(
            "**スケッチの図形は選択集合の項目ではない**(選べるのは面・辺・頂点・領域)ので、"
            "`entities` は対象集合ではなく引数で受ける。**プレビューに載せていない**のは"
            "安全のためではなく、変わるのがスケッチの図形でソリッドではないため —— "
            "`preview` が返すのは形が変わったBodyだけなので、載せても常に空になる(幽霊はブラウザが描く)"
        ),
    ),
    Command(
        name="pattern_circular",
        category="feature",
        description="スケッチ上の図形を円形に反復するときに呼ぶ。",
        params=(
            Param("sketch_id", "string", "対象スケッチのID"),
            Param("entities", "array", "反復する図形IDの配列"),
            Param("center", "array", "回転中心 [u, v]"),
            Param("count", "integer", "総数(元を含む)"),
            Param(
                "total_angle",
                "number",
                "全角度(度)。既定 360",
                required=False,
            ),
            Param(
                "rotate_copies",
                "boolean",
                "複製自体を回転するか。既定 true",
                required=False,
                ws=False,
            ),
        ),
        session="pattern_circular",
        resets_selection=True,
    ),
    # ---------------------------------------------------------------- ファイル操作
    Command(
        name="make_editable_copy",
        category="file",
        description=(
            "参照ボディから編集用ソリッドを作るときに呼ぶ。"
            "import_step_editable が編集可能なボディまで作るので通常は要らない —— "
            "import_step(参照だけ)で読んだボディを後から編集したいとき、"
            "または既存ボディの複製が欲しいときに使う。"
        ),
        params=(
            Param(
                "source_body_id",
                "string",
                "複製元の body_id。省略時はアクティブ",
                required=False,
            ),
            Param("name", "string", "新しいボディの名前", required=False, ws=False),
        ),
        session="make_editable_copy_body",
    ),
    Command(
        name="set_parameter",
        category="sketch",
        description=(
            "名前つき寸法を書き換えて形を追随させるときに呼ぶ。"
            "名前は describe_document の sketches[].entities[].parameters から辿る。"
        ),
        params=(
            Param("name", "string", "パラメータ名(d1 等)"),
            Param("value", "number", "新しい値"),
        ),
        session="set_parameter",
        resets_selection=True,
        note=(
            "**クリックで置いた図形の値を後から打ち直す**のがパラメトリック・プリミティブの"
            "効く場面そのものなので、スケッチのコマンドとして開けている。"
            "**プレビューには載せられない** —— 操作ログを頭から再生するverbで、"
            "再生が過去のseqのundoスナップショットを書き換える(`speculative.py`)"
        ),
    ),
    Command(
        name="import_step_editable",
        category="file",
        description=(
            "STEPを読み込むときに呼ぶ(通常はこちら)。"
            "ソリッドごとに編集可能なボディができ、そのまま押し出し等の対象にできる"
            "(参照ボディは原本として隠したまま文書に残る)。"
            "読んだ後の make_editable_copy は要らない。"
        ),
        params=(Param("path", "string", "読み込むSTEPファイルのパス"),),
        session="import_step_editable",
        note="**コマンド名 == ログのverb名**。読み込みは2つのverbがあるのでコマンドも2つに割る",
    ),
    Command(
        name="import_step",
        category="file",
        description=(
            "STEPを参照ボディ(読み取り専用)としてだけ足すときに呼ぶ。"
            "**編集するなら import_step_editable を使う** —— こちらで読むと、"
            "ソリッドの数だけ make_editable_copy を打つことになる。"
        ),
        params=(Param("path", "string", "読み込むSTEPファイルのパス"),),
        session="import_step",
    ),
    Command(
        name="export_step",
        category="file",
        description=(
            "いまの文書をSTEPファイルへ書き出すときに呼ぶ。"
            "文書は変えないのでログには載らない。戻り値でパスとバイト数を確認する。"
        ),
        params=(
            Param("path", "string", "書き出し先パス"),
            Param(
                "body_ids",
                "array",
                "書き出す body_id の配列。省略時は編集用ソリッド",
                required=False,
                ws=False,
            ),
        ),
        session="export_step_file",
        changes_document=False,
    ),
    Command(
        name="export_stl",
        category="file",
        description=(
            "いまの文書をSTLへ書き出すときに呼ぶ。"
            "deviation でメッシュ粗さを指定する。文書は変えない。"
        ),
        params=(
            Param("path", "string", "書き出し先パス"),
            Param(
                "deviation",
                "number",
                "線形偏差(mm・絶対)。省略時はセッション既定",
                required=False,
            ),
            Param(
                "body_ids",
                "array",
                "書き出す body_id の配列。省略時は編集用ソリッド",
                required=False,
                ws=False,
            ),
        ),
        session="export_stl_file",
        changes_document=False,
    ),
    Command(
        name="save_document",
        category="file",
        description="",
        params=(Param("path", "string", "保存先パス"),),
        session="save_document",
        mcp=False,
        changes_document=False,
        note=(
            "**AIには開けていない**。保存形式は操作ログのJSONで、AIは describe_document で"
            "同じ内容を読めるので、ファイルとして書き出す必要があるのは人の側だけである"
        ),
    ),
    Command(
        name="open_document",
        category="file",
        description="",
        params=(Param("path", "string", "開くファイルのパス"),),
        session="open_document",
        ws="special",
        mcp=False,
        note=(
            "**文書ごと差し替える**ので応答の形が他と違う(全接続へ配る)。"
            "表からは外して `_dispatch` で個別に扱う"
        ),
    ),
    # ---------------------------------------------------------------- 履歴
    Command(
        name="undo",
        category="history",
        description=(
            "直前の操作を取り消すときに呼ぶ。"
            "UIと同じ履歴。土台(読み込み等)は取り消せない。"
        ),
        session="undo",
        ws="special",
        resets_selection=True,
    ),
    Command(
        name="redo",
        category="history",
        description="undo した操作をやり直すときに呼ぶ。UIと同じ履歴。",
        session="redo",
        ws="special",
        resets_selection=True,
    ),
    # ---------------------------------------------------------------- WebSocketだけの口
    # **verbではない**(文書を変えない)。ここに置いてあるのは、下りコマンドの一覧を
    # 1箇所で読めるようにするためで、4つの表のどれにも載らない。
    Command(
        name="get_state",
        category="protocol",
        description="",
        session="state",
        ws="special",
        mcp=False,
        changes_document=False,
        note="接続直後と再同期。要求した接続にだけ返す",
    ),
    Command(
        name="preview",
        category="protocol",
        description="",
        params=(
            Param("verb", "string", "プレビューするverb名(previewable なものだけ)"),
            Param("args", "object", "そのverbの引数"),
        ),
        session="preview",
        ws="special",
        mcp=False,
        changes_document=False,
        note="**配信しない**。投機実行して形だけ測り、文書は `speculative` が元へ戻す",
    ),
    Command(
        name="get_edges",
        category="protocol",
        description="",
        params=(Param("body_id", "string", "辺の折れ線が要るボディ"),),
        session="edges",
        ws="special",
        mcp=False,
        changes_document=False,
        note=(
            "**配信しない**(`preview` と同じ)。辺は、辺を要求しているコマンドを"
            "起動している間だけ画面に出る(常時出すと面のクリックが辺に取られる)"
        ),
    ),
)

#: 名前 -> 宣言。
BY_NAME: dict = {command.name: command for command in COMMANDS}

if len(BY_NAME) != len(COMMANDS):
    raise RuntimeError("COMMANDS に同じ名前が2つある")


# --------------------------------------------------------------------------------------
# 導出(server.py / mcp_server.py はこれを使う)
# --------------------------------------------------------------------------------------
def previewable_verbs() -> dict:
    """`server.PREVIEWABLE_VERBS`: verb名 -> (`Session` のメソッド名, 受け取ってよい引数名)。"""
    return {
        command.name: (command.session, command.preview_params)
        for command in COMMANDS
        if command.previewable
    }


def ws_table(category: str) -> dict:
    """WebSocketの表: コマンド名 -> (`Session` のメソッド名, 必須の引数名)。"""
    return {
        command.name: (command.session, command.ws_params)
        for command in COMMANDS
        if command.category == category and command.ws == "table"
    }


def file_table() -> dict:
    """`server.FILE_COMMANDS`: 上に「文書を変えるか」を足したもの(応答の形が違う)。"""
    return {
        command.name: (command.session, command.ws_params, command.changes_document)
        for command in COMMANDS
        if command.category == "file" and command.ws == "table"
    }


def websocket_command_names() -> set:
    """WebSocketから踏めるコマンド名の全部(表に載るものも個別に捌くものも)。"""
    return {command.name for command in COMMANDS if command.ws != "none"}


def tool_descriptors() -> list:
    """MCPの `tools/list` が返すツール定義(宣言の順)。"""
    return [command.descriptor() for command in COMMANDS if command.mcp]


def tool_names() -> tuple:
    """MCPが公開するツール名(宣言の順)。"""
    return tuple(command.name for command in COMMANDS if command.mcp)
