"""verbの単一宣言 —— **1 verbの追加 = ここに1エントリ**。

**このファイルが正**で、以下はすべてここからの導出である:

- `duocad/server.py` … `PREVIEWABLE_VERBS` / `SKETCH_COMMANDS` / `FEATURE_COMMANDS` /
  `FILE_COMMANDS`(WebSocketの許可リスト)
- `duocad/mcp_server.py` … `tools/list` が返すツール定義と `tools/call` のハンドラ表

frontend(`web/src/commands/*.ts`)だけは別に持つ —— TS側が持つのは**引数の集め方**
(どのクリックを待つか・入力欄のid・ボタンのid)であって、ここには無い情報だからである。

verbを1つ足すとき:

1. `duocad/verbs/` にverb本体を書き、`duocad/__init__.py` に登録の1行を足す
2. `Session` にラッパを書く(対象集合は `selection.require_*` から取る。引数は数値・名前・パスだけ)
3. `DocumentTools` にMCP側のラッパを書く(名前はverb名と同じ綴りにする)
4. **このファイルの `COMMANDS` に1エントリ足す**
5. UIから踏むなら `web/src/commands/*.ts` に引数の集め方を書く(`category` を忘れない)

**対象集合は引数ではない** —— 面・辺・領域はどの層でも引数で受けず、コアの確定選択から取る
(`selection.require_confirmed` / `require_edges`)。したがってここに並ぶ引数は**数値・名前・
パスだけ**である。`body_id` が残っているのは「どのBodyへ足す/から引くか」のような**名前**で
あって対象集合ではないため(Bodyは選択集合の項目ではない)。

**OCP / build123d をimportしない**(純データ)。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

#: `Command.category` に書ける値。
#:
#: - `query`     … 読み取り専用(文書を変えない・ログに載らない)
#: - `selection` … 選択集合と可視性(見えない面は選べない以上、可視性は選択機構の一部)
#: - `sketch`    … スケッチのverb(スケッチモードで踏む)
#: - `feature`   … ソリッドのverb
#: - `file`      … ファイル操作(開く・保存・読み込み・書き出し)
#: - `history`   … undo / redo
#: - `protocol`  … WebSocketだけの口(verbではない。状態の要求・プレビュー・辺の折れ線)
CATEGORIES = ("query", "selection", "sketch", "feature", "file", "history", "protocol")

#: `Command.ws` に書ける値。
#:
#: - `table`   … `server.py` の表(`SKETCH_COMMANDS` 等)に載り、`_dispatch` が機械的に捌く
#: - `special` … WebSocketから踏めるが `_dispatch` が個別に捌く。**表の規約に合わない
#:               もの**がここへ来る —— 応答の形が他と違う(`open_document`)か、
#:               **省略できる引数がある**(`sketch_create` の `base_plane`)かのどちらか。
#:               表の「載せた引数は全部必須」はそのまま保つ(`Param.ws` 参照)
#: - `none`    … WebSocketには開けていない(`propose_selection` はAIが書く経路)
WS_SURFACES = ("table", "special", "none")


@dataclass(frozen=True)
class Param:
    """verbが受け取る引数1つ。`type` / `description` はMCPの入力スキーマになり、
    `ws` はWebSocketの表に載るかどうかを決める。
    """

    #: 引数名。**MCP・WS・`Session` のメソッドで同じ綴り**である。
    name: str
    #: JSON Schema の `type`(`"number"` / `["number", "array"]` のような値)。
    type: Any
    #: AIに見せる説明。**何ができるかではなく、何を渡すか**を書く。
    description: str
    #: MCPで必須か。省略できる引数は実体側にも既定値があること。
    required: bool = True
    #: WebSocketのコマンドが受け取るか。**表(`Command.ws == "table"`)は載せたものを
    #: 全部必須にする**(既定値で補うと、打ち忘れた寸法や `mode` が黙って既定で入る)。
    #: 省略を許したい引数があるコマンドは表に載せず `ws="special"` にすること。
    ws: bool = True
    #: **形の寸法を決める値か**(2026-08-23)。`agent_instructions()` の言う how のうち、
    #: **数値の分だけ**をここで印す —— 半径・距離・間隔・個数・角度。
    #:
    #: 選択肢(`mode` / `visible`)とパス(`path`)は how だが印さない。あれらは言葉で
    #: 指定されるものなので、「人が数量を言ったか」では測れず、印すと誤って止まる
    #: (「カバーを隠して」に数字は無い)。**AIが勝手に決めてしまうのは寸法**であり、
    #: undoでしか戻せない形を左右するのも寸法である。
    #:
    #: 対象を指すもの(`body_id` / `sketch_id` / `entities`)、点の座標
    #: (`center` / `corner` / `p0`)、貼る先の平面名(`base_plane`)、命名(`name`)は
    #: how ではないので印さない(`agent_instructions()` の但し書きと同じ線)。
    #:
    #: 使い道は `duocad/agent/loop.py` のガード1つだけ。
    dimension: bool = False


@dataclass(frozen=True)
class Command:
    """verb(またはコアへの口)1つの宣言。**追加はここ1箇所で済ませる。**"""

    #: **MCPツール名 == WSコマンド名 == UIコマンドID == 操作ログのverb名**。AIは
    #: `describe_document` のログを読んで次の一手を決めるので、綴りが違うと対応表を推測させる。
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
    #: `Session.preview` が投機実行してよいか。**巻き戻せることを確認したverbだけ**を true に
    #: する —— `set_parameter` のような操作ログを頭から再生するverbは `speculative` では戻らない
    #: (`speculative.py` の「戻さないもの」)。
    previewable: bool = False
    #: 文書を変えるか(= 操作ログに載り、応答が状態配信になるか)。保存・書き出しは文書を
    #: 変えないので、応答は書いた先のパスとバイト数だけを要求した接続へ返す。
    changes_document: bool = True
    #: 確定後に選択集合を全リセットするか。**面・辺・領域の添字は形状変更で壊れる**
    #: (kernel.pyの既知の地雷)ので、形を変えるverbの確定後は選択を空にする。
    #: `Session._reset_selection_if_needed` がここを見て1箇所で発火するので、UI経由・MCP経由の
    #: どちらでも同じに効く。**失敗したverb(ログに積まれない)には効かない**。
    resets_selection: bool = False
    #: **対象集合をコアの確定選択から取るか**(2026-08-23)。`Session` の実体が
    #: `selection.require_confirmed` / `require_edges` を呼ぶverbだけが true。
    #:
    #: WebSocketの `_dispatch` は、ここが true のコマンドを受け取ったとき、選択が
    #: `origin="proposed"` のままなら**自動で `commit_selection` してから**verbを走らせる
    #: (**人がツールバーからコマンドを実行する行為そのものを承認とみなす**。2026-08-23本人裁定。
    #: MCP側は素通しで、AIは今までどおり `commit_selection` を明示的に呼ぶ)。宣言と実体の
    #: 一致は `tests/test_vertex_pick.py` が構文木で照合する。
    uses_selection: bool = False
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
        return tuple(p.name for p in self.params if p.ws)

    @property
    def preview_params(self) -> tuple[str, ...]:
        """`preview` が受け取ってよい引数名。

        **WSの表より広いことがある** —— `extrude` の `body_id` はWSのコマンドでは受けない
        (アクティブなBodyへ足す)が、プレビューは行き先を指定した結果を見せられる。
        """
        return tuple(p.name for p in self.params)

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
            "人の承認を待つ義務は無く、自分で commit して verb まで行ってよい。"
            "ここで止めて人に見せるかを考えるのは、対象を何個でも取るか"
            "how の入力がある操作のときだけでよい(判断の基準は instructions)。"
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
            "AIが自分の提案を fillet 等へ渡す直前の明示の1手で、"
            "AIが自分で呼んでよい(人が画面で承認するのと同じ手)。"
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
            ),
        ),
        uses_selection=True,
        session="sketch_create",
        ws="special",
        note=(
            "**MCPとWSで引数が同じ**(2026-08-22)。以前はWSだけ `plane` という名前で"
            "面参照(`{kind:'face'}`)そのものを受けており、**対象集合を引数で渡す経路が"
            "ここだけ残っていた**。今はどちらも `base_plane` だけを取り、省略時は"
            "コアの確定選択の面(ちょうど1枚)に貼る。"
            "省略できる引数があるので表には載せられず(表は全引数必須)、"
            '`ws="special"` で `_dispatch` が個別に捌く'
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
            Param("radius", "number", "半径(mm)", dimension=True),
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
            Param("width", "number", "幅(mm)", dimension=True),
            Param("height", "number", "高さ(mm)", dimension=True),
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
            Param("radius", "number", "半径(mm)", dimension=True),
            Param("start_deg", "number", "開始角(度・反時計)", dimension=True),
            Param("end_deg", "number", "終了角(度・反時計)", dimension=True),
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
            Param("distance", "number", "押し出し量(mm・法線方向)", dimension=True),
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
        uses_selection=True,
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
        params=(Param("distance", "number", "押し出し量(mm)", dimension=True),),
        uses_selection=True,
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
                dimension=True,
            ),
        ),
        uses_selection=True,
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
                dimension=True,
            ),
        ),
        uses_selection=True,
        session="chamfer",
        previewable=True,
        resets_selection=True,
    ),
    Command(
        name="pattern_rectangular",
        category="feature",
        description="スケッチ上の図形を矩形に反復するときに呼ぶ。",
        params=(
            Param(
                "sketch_id",
                "string",
                "対象スケッチのID。describe_document の sketches[].sketch_id",
            ),
            Param(
                "entities",
                "array",
                "反復する図形IDの配列。describe_document の"
                " sketches[].entities[].entity_id をそのまま渡す。"
                "**IDを推測して組み立てない**(存在しないIDはエラーになる)",
            ),
            Param("dx", "number", "X方向の間隔(mm)", dimension=True),
            Param("dy", "number", "Y方向の間隔(mm)", dimension=True),
            Param("nx", "integer", "X方向の個数(元を含む)", dimension=True),
            Param("ny", "integer", "Y方向の個数(元を含む)", dimension=True),
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
            Param(
                "sketch_id",
                "string",
                "対象スケッチのID。describe_document の sketches[].sketch_id",
            ),
            Param(
                "entities",
                "array",
                "反復する図形IDの配列。describe_document の"
                " sketches[].entities[].entity_id をそのまま渡す。"
                "**IDを推測して組み立てない**(存在しないIDはエラーになる)",
            ),
            Param("center", "array", "回転中心 [u, v]"),
            Param("count", "integer", "総数(元を含む)", dimension=True),
            Param(
                "total_angle",
                "number",
                "全角度(度)。既定 360",
                required=False,
                dimension=True,
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
            Param("value", "number", "新しい値", dimension=True),
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
        name="import_svg",
        category="file",
        description=(
            "SVGを1枚のスケッチとして読み込むときに呼ぶ。"
            "外で描いた輪郭(ロゴ・図面・アウトライン化した文字)を持ち込んで押し出したいとき用。"
            "図形は取り込んだ時点で領域として使えるので、続けて extrude を打てる。"
            "曲線はベジエのまま入る(折れ線に落ちない)。単位はmm、"
            "SVGが物理単位を持たなければ1ユーザー単位=1mm。"
        ),
        params=(
            Param("path", "string", "読み込むSVGファイルのパス"),
            # **引数名は `sketch_create` と同じ `base_plane`** —— 綴りを変えると、
            # スケッチを作る2つのverbで同じ意味の引数が違う名前になる。`plane` という名前は
            # かつて面参照そのものをWSで受けていた抜け道で、`tests/test_ui_command_wiring.py`
            # の `TARGET_SET_ARGS` が名指しで禁じている。
            Param(
                "base_plane",
                "string",
                "基準平面名 XY / XZ / YZ。省略時は確定選択の面",
                required=False,
            ),
            # **UIには出さない**(`ws=False`)。倍率は単位の合わないSVGを直すための逃げ道で、
            # 普段は要らない —— 常に出すと「毎回1.0と打つ欄」が1つ増える。
            Param(
                "scale",
                "number",
                "解決後のmmへさらに掛ける倍率(省略時1.0)",
                required=False,
                ws=False,
                dimension=True,
            ),
        ),
        session="import_svg",
        uses_selection=True,
        ws="special",
        resets_selection=True,
        note=(
            "**図形を作るverbだが category は file** —— 入口はファイルを選ぶことで、"
            "スケッチのコマンド群(点をクリックして描く)とは操作の形が違う。"
            "取り込んだ曲線は `bezier` 種別で入るが、**`add_bezier` というverbは無い**"
            "(人もAIもベジエを1本ずつは足せない。2026-08-26本人裁定)。"
            "省略できる引数があるので表には載せられず(表は全引数必須)、"
            '`ws=\"special\"` で `_dispatch` が個別に捌く(`sketch_create` と同じ)'
        
        ),
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
                dimension=True,
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
        params=(
            Param(
                "name",
                "string",
                "保存先の**ファイル名だけ**(models/ 配下に固定。区切り文字・絶対パス・"
                "..は不可)",
            ),
        ),
        session="save_document",
        mcp=False,
        changes_document=False,
        note=(
            "**AIには開けていない**。保存形式は操作ログのJSONで、AIは describe_document で"
            "同じ内容を読めるので、ファイルとして書き出す必要があるのは人の側だけである。"
            "2026-08-22に引数を `path`(サーバのCWD基準の任意パス)から `name`"
            "(`models_dir` 配下のファイル名だけ)へ締めた"
            "(engine `duocad-save-location-fixed-folder`)"
        ),
    ),
    Command(
        name="open_document",
        category="file",
        description="",
        params=(
            Param(
                "name",
                "string",
                "開くファイルの**ファイル名だけ**(models/ 配下に固定。save_document と同じ)",
            ),
        ),
        session="open_document",
        ws="special",
        mcp=False,
        note=(
            "**文書ごと差し替える**ので応答の形が他と違う(全接続へ配る)。"
            "表からは外して `_dispatch` で個別に扱う"
        ),
    ),
    Command(
        name="list_documents",
        category="file",
        description="",
        session="list_documents",
        mcp=False,
        changes_document=False,
        note=(
            "**`models_dir` の中身を一覧にする**(名前/更新日時/サイズ/操作数)。"
            "保存/開くをファイル名の一覧から選べるようにするための穴で、"
            "2026-08-22まではディレクトリ列挙のコードがリポジトリに1つも無かった。"
            "`save_document`/`open_document` と同じく人だけの操作(`mcp=False`)。"
            "文書は変えない(`changes_document=False`)"
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
    # **verbではない**(文書を変えない)。下りコマンドの一覧を1箇所で読めるように
    # ここへ置いてあるだけで、4つの表のどれにも載らない。
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
        name="get_vertices",
        category="protocol",
        description="",
        params=(Param("body_id", "string", "頂点の座標が要るボディ"),),
        session="vertices",
        ws="special",
        mcp=False,
        changes_document=False,
        note=(
            "**配信しない**(`get_edges` と同じ)。頂点は辺と違い、**選択に入っているものだけ**"
            "が画面に出る —— 頂点を引数に取るverbが無いので候補を全部見せる意味が無く、"
            "要るのは「AIが選んだ頂点が今どれか」を人が見て外せることだけである"
        ),
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


def selection_consuming_commands() -> set:
    """**対象集合をコアの確定選択から取る**コマンド名(`Command.uses_selection`)。

    `server._dispatch` の自動 `commit_selection` がこれを見る。
    """
    return {command.name for command in COMMANDS if command.uses_selection}


def websocket_command_names() -> set:
    """WebSocketから踏めるコマンド名の全部(表に載るものも個別に捌くものも)。"""
    return {command.name for command in COMMANDS if command.ws != "none"}


def tool_descriptors() -> list:
    """MCPの `tools/list` が返すツール定義(宣言の順)。"""
    return [command.descriptor() for command in COMMANDS if command.mcp]


def tool_names() -> tuple:
    """MCPが公開するツール名(宣言の順)。"""
    return tuple(command.name for command in COMMANDS if command.mcp)


def dimension_params(name: str) -> tuple[str, ...]:
    """そのverbが取る**形の寸法を決める引数**の名前(`Param.dimension`)。

    未知の名前は空を返す —— 知らないverbを「寸法を取らない」扱いにするのは、
    ガード側(`agent/loop.py`)が**印のあるものだけを止める**設計だからである
    (知らないものを止めると、verbを足した日に別の理由で止まる)。
    """
    for command in COMMANDS:
        if command.name == name:
            return tuple(p.name for p in command.params if p.dimension)
    return ()


def agent_instructions() -> str:
    """**道具の使い方をAIへ渡す唯一の文**(MCPの `instructions` とアプリ内エージェントの
    システムプロンプトは同じものを読む)。

    AIから文書を触る道は2本ある —— Claude Codeなどが起動するMCPサーバ(`duocad/mcp_server.py`)と、
    アプリ内から呼ぶエージェント(`duocad/agent/`)。**手順(propose→commit→verb)は同じ**なので、
    2箇所に書くと片方だけ直った状態に気づけない。ツール数も宣言から数える —— 手で書いた「29」は
    verbを足した日に古くなる。

    **どこまで自分でやるかもここに書く**(2026-08-22本人決定。engine
    `2026-08-22-duocad-ui-interaction-principles`)。以前は「提案して人の承認を待つ」を
    無条件の規則にしていたが、`commit_selection` は最初からAIにも公開されていたので、
    人の承認を強制していたのは**この文だけ**だった。新しい規則は2段:

    - **確認を待たず適用してよい操作** … 対象を何個でも取る引数が無く、how(寸法・選択肢・
      パス)の入力も無いもの。人に聞くことが残っていないので、提案で止める意味が無い。
      **判定基準は手動UIのEnter要否と同一**(`web/src/commands/command.ts` の `needsEnter`。
      表は `tests/test_ui_command_wiring.py`)。個々のverb名をこの文に書かない —— verbを
      足した日に古くなるうえ、基準さえ書けばツール定義の引数から導ける。
    - **止まるか進むかをAIが決める操作** … 上以外。依頼の具体性から判断する。

    `SelectionNotCommitted` の門(`duocad/selection.py`)は**残してある** —— これが無いと
    「提案で止める」が単なる無操作になり、選択肢として成立しない。

    **この文を書き換える人へ**: 後段で第1段の許可を打ち消さないこと(「ただし対象が1つの
    操作でも提案で止める」の類)。第1段を骨抜きにする変更は、コアが何も止めないので
    **実行時には何の異常も出ない**。`tests/test_agent_loop.py` が代表的な言い回しは落とすが、
    散文の矛盾を文字列検査で全部は捕まえられない —— 最後の防波堤はこの注意書きである。
    """
    return (
        f"duocad(自作3DCAD)の開いている文書を操作する。公開ツールは{len(tool_names())}個。"
        "面・辺・領域などの形は引数で受けず、確定した選択から取る"
        "（数値・名前・パスだけが引数）。"
        "手順: describe_document で概要を知る → "
        "propose_selection(predicate または refs) → commit_selection → "
        "fillet / extrude などの verb。"
        "提案のままでは verb に渡せない。"
        "ただし commit_selection はAI自身が呼んでよい —— 人の承認は要らない。"
        "どこまで自分で進めるかは次の2段で決める。"
        "(1) 対象を何個でも取る引数が無く、how(寸法・選択肢・パス)の入力も無い操作は、"
        "確認を待たず propose_selection → commit_selection → verb を一息に実行してよい"
        "（人に聞くことが残っていない。手動UIでEnterが要らない操作と同じ基準。"
        "how は操作の結果を決める値のことで、既存の対象を指すID・点の座標・"
        "貼る先の平面名は、文字列や列挙に見えても how ではなく対象である。"
        "新しく作るものへの命名のような省略できる付帯入力も how に数えない）。"
        "(2) それ以外(対象を何個でも取る、または how の入力がある操作)は、"
        "提案で止めるか適用まで行くかを依頼の文面から自分で判断する: "
        "対象も数値も定まる具体的な指示なら適用まで行き、"
        "曖昧・探索的な依頼なら propose_selection で止めて何を選んだかを述べ、"
        "確定してよいかを尋ねる（止めたら verb は呼ばない）。"
        "迷ったら (2) では止める側に倒す —— 提案は人が直せるが、"
        "適用してしまった形は undo で戻すしかない。"
        "形を変える verb が確定すると選択は空になるので、次の対象は選び直す。"
        "set_visible の結果は人間が開いているブラウザの画面にも即座に反映される。"
    )
