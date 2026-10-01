"""verbの単一宣言 —— **1 verbの追加 = ここに1エントリ**。

**このファイルが正**で、以下はすべてここからの導出である:

- `duocad/server.py` … `PREVIEWABLE_VERBS` / `SKETCH_COMMANDS` / `FEATURE_COMMANDS` /
  `FILE_COMMANDS`(WebSocketの許可リスト)と、WebSocket が受け取る引数とその検査
- `duocad/mcp_server.py` … `tools/list` が返すツール定義と `tools/call` の引数の検査

frontend(`web/src/commands/*.ts`)だけは別に持つ —— TS側が持つのは**引数の集め方**
(どのクリックを待つか・入力欄のid・ボタンのid)であって、ここには無い情報だからである。

verbを1つ足すとき:

1. `duocad/verbs/` にverbのモジュールを書く —— 純関数(対象を引数で受ける)と `register_verb`、
   それに入口(`@entry`。対象を `selection.require_*` から取って純関数へ渡す。引数は数値・名前・
   パスだけ)。`duocad/verbs/__init__.py` に import の1行を足す
2. **このファイルの `COMMANDS` に1エントリ足す**(引数名は入口の引数と同じ綴り)
3. UIから踏むなら `web/src/commands/*.ts` に引数の集め方を書く(`category` を忘れない)

WebSocket・MCP・アプリ内AIはどれも `Session.apply` を通って入口を呼ぶので、verbごとに書く
配線は無い。

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

#: 形(面・辺・領域の添字)を変えうる verb のカテゴリ。ここに属し文書を変える宣言は、
#: `resets_selection` を**明示しないと import 時点で落ちる**(既定に頼ると、形を変える verb を
#: 足すときの付け忘れがどこにも現れない。issue #51)。
SHAPE_CATEGORIES = ("sketch", "feature", "history")

#: `Command.ws` に書ける値。
#:
#: - `table`   … `server.py` の表(`SKETCH_COMMANDS` 等)に載り、`_dispatch` が機械的に捌く。
#:               verb は `Session.apply` へ、verb でないファイル操作は `Session` のメソッドへ行く
#: - `special` … WebSocketから踏めるが `_dispatch` が個別に捌く。**verb ではないもの**
#:               (選択・可視性・履歴・状態の要求)と、応答の形が他と違うファイル操作
#:               (`open_document` / `save_document`)がここへ来る
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
    #: WebSocketのコマンドが受け取るか。`True` は受け取る(必須かどうかは `required` と同じ)、
    #: `False` は受け取らない(届いても渡さない)、**`"required"` は MCP では省略できても
    #: WebSocket では必ず送らせる** —— 画面は人が選んだ値・打った値をそのまま送る口なので、
    #: 既定値で補うと、打ち忘れた寸法や `mode` が黙って既定で入る(`extrude` の `mode` が
    #: `join` に落ちると「切るつもりで足した」が通る)。
    ws: Any = True
    #: **形の寸法を決める値か**。`agent_instructions()` の言う how のうち、
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
    #: **verb名と違ってよい**(`export_step` -> `export_step_file` のように、
    #: コア側の名前がverb名と一致しないことがある)。
    session: Optional[str] = None
    #: WebSocketでの露出のしかた(`WS_SURFACES`)。
    ws: str = "table"
    #: MCPのツールとして公開するか(`save_document` / `open_document` は人だけの操作)。
    mcp: bool = True
    #: `Session.preview` が投機実行してよいか。**巻き戻せることを確認したverbだけ**を true に
    #: する —— `set_parameter` のような操作ログを頭から再生するverbは `speculative` では戻らない
    #: (`speculative.py` の「戻さないもの」)。
    previewable: bool = False
    #: プレビューを投機実行でなく、**文書を変えずに結果だけ計算する** `Session` のメソッドで出すときの
    #: 名前(`previewable=True` と組で使う)。投機実行は verb が失敗すると何も返せないので、失敗の手前の
    #: 形(`envelope_regions` の分裂)も見せたい verb はこちら。
    preview_session: Optional[str] = None
    #: 文書を変えるか(= 操作ログに載り、応答が状態配信になるか)。保存・書き出しは文書を
    #: 変えないので、応答は書いた先のパスとバイト数だけを要求した接続へ返す。
    changes_document: bool = True
    #: 確定後に選択集合を全リセットするか。**面・辺・領域の添字は形状変更で壊れる**
    #: (kernel.pyの既知の地雷)ので、形を変えるverbの確定後は選択を空にする。
    #: `Session._reset_selection_if_needed` がここを見て1箇所で発火するので、UI経由・MCP経由の
    #: どちらでも同じに効く。**失敗したverb(ログに積まれない)には効かない**。
    #: **`undo`/`redo` も true**(添字が壊れていない可能性があっても、何を戻した/やり直したかで
    #: 場合分けしない単純な規則を優先する)。**`SHAPE_CATEGORIES` の verb は書くこと**(`None` のままなら
    #: `__post_init__` が落とす)。それ以外は省略すると false。
    resets_selection: Optional[bool] = None
    #: **対象集合をコアの確定選択から取るか**。`Session` の実体が
    #: `selection.require_confirmed` / `require_edges` を呼ぶverbだけが true。
    #:
    #: WebSocketの `_dispatch` は、ここが true のコマンドを受け取ったとき、選択が
    #: `origin="proposed"` のままなら**自動で `commit_selection` してから**verbを走らせる
    #: (**人がツールバーからコマンドを実行する行為そのものを承認とみなす**。
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
        if self.preview_session is not None and not self.previewable:
            raise ValueError(f"{self.name}: `preview_session` は `previewable=True` と組で使う")
        if self.resets_selection is None:
            if self.category in SHAPE_CATEGORIES and self.changes_document:
                raise ValueError(
                    f"{self.name}: 形を変えうる verb は `resets_selection` を宣言する(既定に頼らない)"
                )
            object.__setattr__(self, "resets_selection", False)

    @property
    def ws_params(self) -> tuple[str, ...]:
        """WebSocketのコマンドが受け取る引数名。"""
        return tuple(p.name for p in self.params if p.ws)

    def ws_schema(self) -> dict:
        """WebSocket が受け取る引数の JSON Schema(`input_schema` の WebSocket 版)。"""
        return _schema(
            [p for p in self.params if p.ws],
            [p.name for p in self.params if p.ws == "required" or (p.ws and p.required)],
        )

    def ws_arguments(self, message: dict) -> dict:
        """WebSocket のメッセージから、入口へ渡す引数を取り出して検査する。違反は ValueError。

        **`null` は「送っていない」と読む** —— 画面はモードが取らない欄を `null` のまま送る
        (`move_body` の並進での `angle`)。受け取らないと決めた引数(`ws=False`)は届いても渡さない。
        """
        arguments = {
            p.name: message[p.name] for p in self.params if p.ws and message.get(p.name) is not None
        }
        problem = check_arguments(self.ws_schema(), arguments)
        if problem is not None:
            raise ValueError(problem)
        return arguments

    @property
    def preview_params(self) -> tuple[str, ...]:
        """`preview` が受け取ってよい引数名。

        **WSの表より広いことがある** —— `extrude` の `body_id` はWSのコマンドでは受けない
        (アクティブなBodyへ足す)が、プレビューは行き先を指定した結果を見せられる。
        """
        return tuple(p.name for p in self.params)

    def input_schema(self) -> dict:
        """MCPの `inputSchema`(JSON Schema)。"""
        return _schema(self.params, [p.name for p in self.params if p.required])

    def descriptor(self) -> dict:
        """MCPの `tools/list` が返す1件。"""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema(),
        }


def _schema(params: Any, required: list) -> dict:
    properties = {p.name: {"type": p.type, "description": p.description} for p in params}
    # **必須が1つも無いときは `required` を書かない**(空配列を出すと、
    # クライアントによっては「必須が指定されている」と読まれうる)。
    return {
        "type": "object",
        "properties": properties,
        **({"required": required} if required else {}),
        "additionalProperties": False,
    }


def check_arguments(schema: dict, arguments: dict) -> Optional[str]:
    """入力スキーマとの突き合わせ(必須・未知キー・型)。違反の文を返す(無ければ None)。

    **黙って通さない**が、包み方は呼ぶ側が決める —— MCP はプロトコルエラー(-32602)、
    アプリ内AI(`DocumentTools.call`)は `invalid_argument` の封筒、WebSocket は画面のエラーの札。
    JSON Schemaの完全な検証はしない(依存を増やさないため)。見るのは必須・未知キーと
    `boolean` / `string` / `number` / `integer` / `array` / `object`、および型のリスト
    (例: `fillet.radius` の `["number","array"]`)。いずれも**間違ったまま実行すると
    静かに別のことをする**類の誤りである(例: `visible` に "false" → 真、`True` を
    数値として通すと 1.0、`radius: "12"` は1文字ずつ読まれて辺2本に半径1と2が付く)。
    **任意引数の `null` も型違い**(スキーマに `null` は無い。省略はキーごと落とすこと)。
    エラーメッセージには**引数名を必ず含める**。
    """
    properties = schema.get("properties") or {}
    required = schema.get("required") or []

    missing = [key for key in required if key not in arguments]
    if missing:
        return f"missing required argument(s): {missing}"
    if schema.get("additionalProperties") is False:
        unknown = [key for key in arguments if key not in properties]
        if unknown:
            return f"unknown argument(s): {unknown} (expected: {sorted(properties)})"
    for key, value in arguments.items():
        expected = (properties.get(key) or {}).get("type")
        if expected is None:
            continue
        if isinstance(expected, list):
            if not any(_value_matches_json_type(value, item) for item in expected):
                return f"argument {key!r} must be one of {expected}, got {value!r}"
            continue
        if not _value_matches_json_type(value, expected):
            return f"argument {key!r} must be a {expected}, got {value!r}"
    return None


def _value_matches_json_type(value: Any, expected: str) -> bool:
    """JSON Schemaの型名1つと Python 値の突き合わせ。**bool は number/integer にしない**。"""
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "string":
        return isinstance(value, str)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return True


# --------------------------------------------------------------------------------------
# 宣言(**verbを足すのはここ**)
# --------------------------------------------------------------------------------------
#: 並び順は `tools/list` に出る順序でもある(AIが最初に読む順)。
#: スケッチのverbが共通に受ける `sketch_id`(`Param` は frozen なので共有してよい)。
_SKETCH_ID = Param("sketch_id", "string", "対象スケッチのID")

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
            "(可視性は選択機構の一部)。操作ログには載らず undo では戻らないので、"
            "隠したものを戻すには visible=true でもう一度呼ぶこと。"
            "開いているブラウザの表示も即座に変わる。"
            "**export_step / export_stl の書き出し対象も変わる**(非表示のボディは書き出されない)。"
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
        name="set_sketch_visible",
        category="selection",
        description=(
            "スケッチの表示・非表示を切り替える(set_visible のスケッチ版)。隠したスケッチの"
            "領域・図形は選べず、開いているブラウザにも描かれない。操作ログには載らず undo では"
            "戻らないので、戻すには visible=true でもう一度呼ぶこと。"
            "押し出し済みのボディには影響しない(ボディの可視性は set_visible)。"
        ),
        params=(
            Param("sketch_id", "string", "describe_document が返した sketch_id"),
            Param("visible", "boolean", "true で表示、false で非表示"),
        ),
        session="set_sketch_visible",
        ws="special",
        note="`set_visible` と同じく WS では `client_op` を添えるので `_dispatch` が個別に捌く",
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
            "ちょうど2項目(頂点・直線の辺・平らな面)なら、そのあいだの距離が `between` に"
            "返る(直線・平面は無限に延ばして測る。平行でない線・面どうしは距離の代わりに"
            "角度)。verb ではないので commit 前に提案を確かめる往復に使う。"
        ),
        session="measure",
        ws="special",
        changes_document=False,
        note=(
            "画面の「測定」コマンドの実体。WSは要求した接続へ `{type:'measure', client_op, ...}` を"
            "返す(状態配信ではない。`get_edges` と同じ扱い)。"
        ),
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
        resets_selection=False,
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
        note=(
            "**MCPとWSで引数が同じ**。どちらも `base_plane` だけを取り、省略時は"
            "コアの確定選択の面(ちょうど1枚)に貼る"
        ),
    ),
    Command(
        name="sketch_create_offset",
        resets_selection=False,
        category="sketch",
        description=(
            "オフセット平面に新しいスケッチを作るときに呼ぶ。"
            "base_plane(XY/XZ/YZ)を渡すか確定選択の平面(ちょうど1枚)を基準にし、"
            "その法線方向へdistanceだけ離れた平面に貼る。"
        ),
        params=(
            Param(
                "base_plane",
                "string",
                "基準平面名 XY / XZ / YZ。省略時は確定選択の面",
                required=False,
            ),
            Param(
                "distance",
                "number",
                "基準平面からのオフセット距離(mm・法線方向が正)",
                dimension=True,
            ),
        ),
        uses_selection=True,
        session="sketch_create_offset",
        note=(
            "**`sketch_create` とは別verb**(`sketch.sketch_create` のdocstring参照)。"
            "貼る先(target)は同じ2状態(基準平面名 / 確定選択の面)だが、`distance` という"
            "howを持つのでEnterが要る —— 同じverb名に足すと `sketch_create` 自体の"
            "即適用(Enter不要)まで変わってしまうため、名前を分けてある。"
        ),
    ),
    Command(
        name="add_line",
        category="sketch",
        description="スケッチに線分を足すときに呼ぶ(平面上の2点)。",
        params=(
            _SKETCH_ID,
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
            _SKETCH_ID,
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
            _SKETCH_ID,
            Param("corner", "array", "基準コーナー [u, v]"),
            Param("width", "number", "幅(mm)", dimension=True),
            Param("height", "number", "高さ(mm)", dimension=True),
            Param(
                "angle",
                "number",
                "傾き(度・反時計。省略で0)",
                required=False,
                ws=False,
                dimension=True,
            ),
        ),
        session="add_rectangle",
        resets_selection=True,
    ),
    Command(
        name="add_arc",
        category="sketch",
        description="スケッチに円弧を足すときに呼ぶ(中心・半径・開始角・終了角)。",
        params=(
            _SKETCH_ID,
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
            "確定選択の領域、または同一平面・同一ボディのソリッドの面(何枚でも)を押し出して、"
            "ソリッドを足す/切る/新しいボディにするときに呼ぶ。"
            "対象は選択から取る(引数では受けない)。領域と面の混在は不可。"
        ),
        params=(
            Param(
                "distance",
                "number",
                "押し出し量(mm・法線方向)。面のときは外向き法線が正(面から削る cut は負の距離)",
                dimension=True,
            ),
            Param(
                "mode",
                "string",
                "join(足す・既定) / cut(切る) / new(独立した新しいボディを作る)",
                required=False,
                ws="required",
            ),
            Param(
                "body_id",
                "string",
                "対象ボディ。省略時は、領域ならアクティブ(無ければ新規作成)、面ならその面を持つボディ(面の選択は同一ボディだが、別のボディを指定すればそこへ足す/引く)。"
                "mode='new' では指定できない(行き先を持たない)",
                required=False,
                ws=False,
            ),
        ),
        uses_selection=True,
        session="extrude",
        previewable=True,
        resets_selection=True,
        note=(
            "**`mode` はWSでだけ必須**(既定の join に落ちると「切るつもりで足した」が"
            "黙って通る。UIは人に選ばせてから送る)。**`body_id` はWSのコマンドには無い** —— "
            "行き先を指せるのは MCP と `preview` だけで、WSは領域ならアクティブなBody、面ならその面を持つBodyへ足す"
        ),
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
        name="boolean_body",
        category="feature",
        description=(
            "ターゲットボディ(1つ)にツールボディ(1つ以上)を作用させ、結合(union)・"
            "切り取り(cut)・交差(intersect)するときに呼ぶ。ターゲットは `target_ref` で"
            "1枚の面から指す(確定選択には入れない)。ツールは確定選択から取る"
            "(面/辺/頂点で指せばよい)。演算後ターゲットの形だけが残り、ツールボディは消える。"
            "intersect でツールが複数のとき target ∩ (tool1 ∪ tool2 ∪ ...)。"
        ),
        params=(
            Param(
                "target_ref",
                "object",
                "ターゲットボディを指す面への参照 "
                '{"body": body_id, "index": 面の1始まりの添字}。'
                "get_selection が返す面の参照をそのまま渡せる。"
                "確定選択には入れない",
            ),
            Param(
                "mode",
                "string",
                "union(結合) / cut(切り取り) / intersect(交差)",
            ),
        ),
        uses_selection=True,
        session="boolean_body",
        previewable=True,
        resets_selection=True,
    ),
    Command(
        name="move_sketch_entities",
        category="sketch",
        description=(
            "スケッチの図形の位置・向きを直すときに呼ぶ。確定選択のスケッチの点"
            "(線の端点・円/弧の中心・長方形の基準の角・ベジエの制御点)または辺"
            "(図形1つ全体)を、mode='translate'(既定)なら dx, dy だけ動かし、"
            "mode='rotate' ならスケッチ平面の法線まわりに angle 度だけ回す。"
            "copy=true なら元を残して複製する。対象は選択から取る(引数では受けない)。"
            "**半径・幅高さのような寸法は動かせない**(位置と向きだけ)。"
        ),
        params=(
            Param(
                "dx",
                "number",
                "X方向(スケッチ平面のu軸)の移動量(mm)。mode='translate' で必須",
                required=False,
                dimension=True,
            ),
            Param(
                "dy",
                "number",
                "Y方向(スケッチ平面のv軸)の移動量(mm)。mode='translate' で必須",
                required=False,
                dimension=True,
            ),
            Param(
                "copy",
                "boolean",
                "true=元を残して複製 / false=移動(既定)",
                required=False,
            ),
            Param(
                "mode",
                "string",
                "translate(既定・dx, dy で動かす) / rotate(angle 度だけ回す)",
                required=False,
            ),
            Param(
                "angle",
                "number",
                "回転角(度)。mode='rotate' で必須。u軸からv軸へ向かう回り(反時計)が正で、"
                "pattern_circular の total_angle と同じ向き",
                required=False,
                dimension=True,
            ),
            Param(
                "center",
                "array",
                "回転中心 [u, v]。省略時は対象の代表点(線分は中点・円/弧は中心・"
                "長方形は中央・点を選んでいるならその点)の平均",
                required=False,
            ),
            Param(
                "center_entity",
                "object",
                "回転中心を既存の点で指す "
                '{"sketch": sketch_id, "entity": entity_id, "point": "x0"}。'
                "point を省くとその図形の最初の点(線は始点・円/弧は中心・長方形は基準の角)。"
                "center とは同時に渡せない",
                required=False,
            ),
        ),
        uses_selection=True,
        session="move_sketch_entities",
        resets_selection=True,
        note=(
            "**押し出し済みのソリッドは追従しない**(本人が許容済み)。"
            "画面のプレビューは**ブラウザだけで描く**(2026-09-05) —— 選んだ図形の今の座標を"
            " dx, dy だけずらした姿を半透明で重ねるだけで、確定するまでコアには何も届かない。"
            "**幽霊は回転にも追随する**(2026-09-06。`web/src/entityKinds.ts` の `ghostCopies`)"
            " —— ただし**中心が決まっているときだけ**描く。数値の `center` はそのまま、"
            "画面でピックした `center_entity` は `App.svelte` の `resolvePicksForGhost` が"
            "配られた `params` から [u, v] へ解決してから渡す。**省略時の既定**"
            "(対象の代表点の平均)だけは規則がコアにしか無く、画面側へ写すと片方だけ"
            "変わったときに幽霊と確定結果が別の場所へ出るため、描かないほうを選んである。"
            "**回転の引数はWSにも開いている**(2026-09-06。`ws=\"special\"`) —— 表(`ws=table`)は"
            "載せた引数を全部必須にする規律なので、`mode` で要る引数が入れ替わり中心も省略できる"
            "このverbは表に置けない。`_dispatch` が個別に捌き、**省略された引数は None のまま**"
            "verbへ渡す(既定で埋めると、モード違いの引数が黙って効く)。"
            "画面は「並進 / 回転」のボタンでモードを選ばせ、回転中心は**スケッチの点を"
            "直接クリック**して `center_entity` の参照で送る(`web/src/commands/command.ts` の"
            "`PickArgSpec`) —— 共有選択集合には入れない。中心を指さなければ引数ごと送らないので、"
            "既定(対象の代表点の平均)はコアが決めたままである"
        ),
    ),
    Command(
        name="move_body",
        category="feature",
        description=(
            "ボディの位置・向きを直すときに呼ぶ。確定選択の面/辺/頂点が**属するボディ**を、"
            "mode='translate'(既定)なら dx, dy, dz だけ動かし、mode='rotate' なら軸まわりに"
            " angle 度だけ回す(複数のボディにまたがる選択なら全部を同じだけ動かす/回す)。"
            "copy=true なら元を残して複製する。対象は選択から取る(引数では受けない) ——"
            "ボディを指すのに面を1枚選べばよい。"
        ),
        params=(
            Param(
                "dx",
                "number",
                "X方向の移動量(mm)。mode='translate' で必須",
                required=False,
                dimension=True,
            ),
            Param(
                "dy",
                "number",
                "Y方向の移動量(mm)。mode='translate' で必須",
                required=False,
                dimension=True,
            ),
            Param(
                "dz",
                "number",
                "Z方向の移動量(mm)。mode='translate' で必須",
                required=False,
                dimension=True,
            ),
            Param(
                "copy",
                "boolean",
                "true=元を残して複製 / false=移動(既定)",
                required=False,
            ),
            Param(
                "mode",
                "string",
                "translate(既定・dx, dy, dz で動かす) / rotate(軸まわりに angle 度だけ回す)",
                required=False,
            ),
            Param(
                "angle",
                "number",
                "回転角(度)。mode='rotate' で必須。軸の向きに対して右ねじが正",
                required=False,
                dimension=True,
            ),
            Param(
                "axis_edge",
                "object",
                "回転軸にする**まっすぐな辺**への参照 "
                '{"body": body_id, "index": 辺の1始まりの添字}。'
                "get_selection が返す辺の参照をそのまま渡せる。"
                "axis_point / axis_dir とは同時に渡せない",
                required=False,
            ),
            Param(
                "axis_point",
                "array",
                "軸が通る点 [x, y, z](axis_dir と組で使う)。省略時は対象の重心",
                required=False,
            ),
            Param(
                "axis_dir",
                "array",
                "軸の向き [x, y, z](axis_edge を使わないときに必須)",
                required=False,
            ),
            Param(
                "center_vertex",
                "object",
                "回転中心にする頂点への参照 "
                '{"body": body_id, "index": 頂点の1始まりの添字}。'
                "軸の向きはそのままに、軸がこの点を通るようにする。"
                "axis_point とは同時に渡せない(どちらも軸の位置を決めるため)",
                required=False,
            ),
        ),
        uses_selection=True,
        session="move_body",
        resets_selection=True,
        note=(
            "**投機実行(`previewable`)には載せない** —— 動かす前後で形は同じなので、"
            "コアに走らせても新しい形は1つも出てこない。"
            "画面のプレビューは**ブラウザだけで描く**(2026-09-05に実装) —— 既に届いている"
            "メッシュを dx, dy, dz だけずらして半透明で重ねる。**元の本体は消さない**ので、"
            "移動前と移動後が並んで見える(以前ここには「重なって見分けが付かない」と"
            "書いてあったが、移動量が0でない限り両者は別の場所に描かれる)。"
            "**幽霊は回転にも追随する**(2026-09-06。`viewport.BodyGhost.rotate` と"
            "`bodyGhostMatrix`)—— ただし**軸が決まっているときだけ**描く。数値の"
            "`axis_point` + `axis_dir` はそのまま、画面でピックした `axis_edge` /"
            "`center_vertex` は `App.svelte` の `resolvePicksForGhost` が**配られた辺の"
            "折れ線と頂点座標**(コアが出した数値そのもの)から解決してから渡す。"
            "**「軸の位置を省略 → 対象の合成重心」だけはブラウザ側では解けない**"
            "(体積重心は三角形分割の近似からしか出ない)ので、近似で置かずに描かない。"
            "曲線の辺も同じで、まっすぐでなければ幽霊を出さずコアの拒否を人に見せる。"
            "**回転の引数はWSにも開いている**(2026-09-06。`ws=\"special\"`。表に置けない理由は"
            " move_sketch_entities の note と同じ)。画面は回転軸を**3Dビューで辺をクリック**して"
            "`axis_edge` の参照で送り、中心は任意で頂点をクリックして `center_vertex` で送る ——"
            "**共有選択集合には入れない別チャンネル**(`web/src/commands/command.ts` の"
            "`PickArgSpec`)である。載せてしまうと `require_bodies` が軸の辺のボディまで"
            "「動かす対象」に数え、軸に使った部品まで一緒に回る(だからコア側も"
            "`commit_selection` を介さない参照で受けている)。"
            "**参照のまま送る**のは、辺が乗る直線を厳密に出せるのがコアだけだからで、"
            "まっすぐでない辺の拒否も `kernel.edge_axis` の既存の規則がそのまま効く"
        ),
    ),
    Command(
        name="mirror_sketch_entities",
        category="sketch",
        description=(
            "確定選択のスケッチ図形を、既存の線分1本(axis)を軸に鏡映複製するときに呼ぶ。"
            "常に複製する(in-place反転は無い)。対象は選択から取る(引数では受けない)。"
        ),
        params=(
            Param(
                "axis",
                "object",
                "鏡映軸にする既存の線分への参照 "
                '{"sketch": sketch_id, "entity": entity_id}(kindはlineであること)',
            ),
        ),
        uses_selection=True,
        session="mirror_sketch_entities",
        resets_selection=True,
        note=(
            "**WSにも開いている**(2026-09-06。`axis` は必須の引数1つなので表に載る)。"
            "画面は軸の線分を**スケッチ平面で直接クリック**して `axis` の参照で送る ——"
            "**共有選択集合には入れない別チャンネル**(`web/src/commands/command.ts` の"
            "`PickArgSpec`。`move_body.axis_edge` と同じ規律)である。載せてしまうと"
            "`require_sketch_entities` が軸の線分まで対象に数える(軸は自分自身へ映るので"
            "verb側は黙って外すが、画面の対象の件数と結果が食い違う)。"
        ),
    ),
    Command(
        name="envelope_regions",
        category="sketch",
        description=(
            "確定選択の複数の領域を、へこみに沿って包む**単一の閉じた領域線**を、同じスケッチへ足すときに呼ぶ。"
            "元の領域の線は残る。結果は線分と円弧だけ。結果が1つの領域にならない(何個かに分かれる・"
            "オフセットで消える)と失敗して何も変わらない。対象は選択から取る(引数では受けない)。"
            "選択が複数のスケッチにまたがるのも失敗する。"
        ),
        params=(
            Param(
                "radius",
                "number",
                "追従半径 R(mm・0以上)。幅 2R 未満の隙間とへこみを埋め、それより広いへこみには沿う。"
                "省略は 0",
                required=False,
                ws="required",
                dimension=True,
            ),
            Param(
                "offset",
                "number",
                "包んだ線のオフセット d(mm)。正で外へ広がり、負で内へ縮む。省略は 0",
                required=False,
                ws="required",
                dimension=True,
            ),
        ),
        uses_selection=True,
        session="envelope_regions",
        previewable=True,
        preview_session="preview_envelope_regions",
        resets_selection=True,
        note=(
            "**WSでは R・d ともに必須**(表は載せた引数を全部必須にする)。UIは空欄を 0 として送る"
            "(`NumberArgSpec.default`)ので、人が空欄のまま確定しても届く。MCPは省略=0。"
            "**必要な R・d の最小値は計算しない**(本人の決定。分裂したら個数を添えて失敗するだけ)。"
            "プレビューは投機実行でなく `preview_envelope_regions` で、分裂しても失敗にせず全部の線と"
            "個数を返す(トーストは確定時だけ。本人決定)"
        ),
    ),
    Command(
        name="mirror_body",
        category="feature",
        description=(
            "確定選択の面/辺/頂点が**属するボディ**を、鏡映面に対して鏡映複製するときに呼ぶ。"
            "常に複製する(in-place反転は無い。複数のボディにまたがる選択なら全部を同じ"
            "鏡映面で複製する)。鏡映面は base_plane(XY/XZ/YZ)か plane_face(既存の平面な"
            "面への参照)のどちらか一方。対象は選択から取る(引数では受けない)。"
        ),
        params=(
            Param(
                "base_plane",
                "string",
                "鏡映面を基準平面で指す XY / XZ / YZ(原点通過)。plane_face とは同時に渡せない",
                required=False,
            ),
            Param(
                "plane_face",
                "object",
                "鏡映面を既存ソリッドの平面な面で指す "
                '{"body": body_id, "index": 面の1始まりの添字}。base_plane とは同時に渡せない。'
                "この面自身が対象ボディに属していてもよい",
                required=False,
            ),
        ),
        uses_selection=True,
        session="mirror_body",
        resets_selection=True,
        note=(
            "**どちらの鏡映面もWSに開いている**(2つは排他で、どちらも省略できる)。"
            "画面は「鏡映面」の4択で、XY/XZ/YZ は `base_plane`、「面を選ぶ」を押すと"
            "3Dの面を1つクリックして `plane_face` の参照で送る ——"
            "**共有選択集合には入れない別チャンネル**(`web/src/commands/command.ts` の"
            "`PickArgSpec`)である。載せてしまうと `require_bodies` が鏡映面に使った面の"
            "ボディまで対象に数え、基準にした部品まで一緒に複製される。"
            "プレビュー(幽霊)は無い —— `previewable=False`。"
        ),
    ),
    # ---------------------------------------------------------------- 削除
    Command(
        name="delete",
        category="feature",
        description=(
            "間違えて作った / 要らなくなったものを消すときに呼ぶ。確定選択の**ボディ**"
            "(面/辺/頂点/ボディのどれで指してもよい)と**スケッチの図形**を、混ざっていても"
            "1回でまとめて削除する(1回のundoで全部戻る)。対象は選択から取る"
            "(引数では受けない)。**領域(region)は削除できない** —— 領域は図形の配置から"
            "計算されるものなので、選択に混ざっているとエラーになる(その領域を作っている"
            "図形を選び直すこと)。スケッチを丸ごと消す口は無い。"
        ),
        uses_selection=True,
        session="delete",
        resets_selection=True,
        note=(
            "**`previewable=False`** —— 消える前の姿を見せる幽霊が作れない(プレビューは"
            "「増えた形」を配る仕組みで、`Session.preview` が返すのは形の変わったBodyだけ)。"
            "`uses_selection=True` かつ `previewable=False` の組み合わせは形状最適化の4 verb"
            "が先例で、WSの自動 commit(`selection_consuming_commands`)だけが効く。"
            "**howが無い**ので引数は1つも取らない —— 消し方に選択肢は無い"
        ),
    ),
    # ---------------------------------------------------------------- 形状最適化の条件(第3のモード)
    #
    # **4つとも集合代入**である(「1つ足す」ではなく「この役割の集合を、いま確定している
    # 選択集合そのものに置き換える」)。だから**解除専用のverbが無い** —— verbの中で
    # 全部外して決定すれば、その役割は空になる。実体と契約は
    # `duocad/verbs/optimize_setup.py`、配りものの形は `duocad/optimize.py`。
    #
    # `category="feature"` にしてあるのは、ソリッドに対する操作でWSの `FEATURE_COMMANDS`
    # の捌き方(引数を表から読んで `Session` のメソッドを呼ぶ)がそのまま当たるからである。
    # **モードの分け方は画面側の話**で、コアの分類ではない —— 分類を1つ増やすと、
    # WSの表・`_dispatch` の分岐・UIの送信経路がverbの中身と関係なく増える。
    Command(
        name="set_preserve_bodies",
        resets_selection=False,
        category="feature",
        description=(
            "形状最適化で**削ってはいけないソリッド**を決めるときに呼ぶ。"
            "確定選択のボディ(kind='body' の参照)**そのもの**を保持ソリッドの集合にする"
            "(足すのではなく置き換える。空の選択で確定すれば全部外れる)。"
            "既に除外ソリッドに割り当てられているボディがあると失敗する(先勝ち)。"
        ),
        session="set_preserve_bodies",
        uses_selection=True,
        note=(
            "対象は引数で受けない。"
            "**howも無い** —— 役割を付けるだけなので数値も選択肢も要らない"
        ),
    ),
    Command(
        name="set_exclude_bodies",
        resets_selection=False,
        category="feature",
        description=(
            "形状最適化で**材料を置いてはいけないソリッド**(逃げ・干渉物)を決めるときに呼ぶ。"
            "確定選択のボディ(kind='body' の参照)**そのもの**を除外ソリッドの集合にする"
            "(足すのではなく置き換える。空の選択で確定すれば全部外れる)。"
            "既に保持ソリッドに割り当てられているボディがあると失敗する(先勝ち)。"
        ),
        session="set_exclude_bodies",
        uses_selection=True,
        note="`set_preserve_bodies` と対。設計空間のverbは作らない",
    ),
    Command(
        name="set_fixtures",
        resets_selection=False,
        category="feature",
        description=(
            "形状最適化で**固定される面**(拘束)を決めるときに呼ぶ。確定選択の面**そのもの**を"
            "拘束面の集合にする(足すのではなく置き換える。空の選択で確定すれば全部外れる)。"
            "既に荷重面に割り当てられている面があると失敗する(先勝ち)。"
        ),
        session="set_fixtures",
        uses_selection=True,
        note=(
            "面の参照は添字だけでなく重心・法線・面積の**指紋**も一緒に操作ログへ書く"
            "(添字は形が変われば別の面を指すため。照合は `duocad/optimize.py`)"
        ),
    ),
    Command(
        name="set_loads",
        resets_selection=False,
        category="feature",
        description=(
            "形状最適化で**力がかかる面**(荷重)を決めるときに呼ぶ。確定選択の面**だけ**に"
            "大きさと向きを焼く(**上書き代入**。選択に入っていない荷重面はそのまま残る)。"
            "既に拘束面に割り当てられている面があると失敗する(先勝ち)。"
            "**大きさと向きは面ごとに違ってよい** —— 面Aへ50Nの鉛直下向き、面Bへ20Nの法線方向、"
            "は2回呼んで作る(1回目の面Aは2回目で消えない)。荷重を外すのは `clear_loads`。"
            "**面を1枚も選ばずに呼ぶと失敗する**(外す操作と取り違えないため)。"
            "ここで置ける荷重は**同時にかかる1つの荷重系**である ——"
            "「あるときは面A、別のときは面B」という別シナリオは表現できない。"
        ),
        params=(
            Param(
                "magnitude",
                "number",
                "荷重の大きさ[N](0より大きい有限の数)。**この呼び出しで選んだ面に焼く値**",
                dimension=True,
            ),
            Param(
                "direction",
                ["string", "array"],
                "荷重の向き。'normal'=面の外向き法線 / '-normal'=面を押し込む向き / "
                "[x, y, z]=世界座標のベクトル(長さは無視して向きだけ使う。"
                "棚受けのように世界の -Z へ受ける荷重は [0, 0, -1])",
            ),
        ),
        session="set_loads",
        uses_selection=True,
        note=(
            "`magnitude` に `dimension` の印を付けてある —— **AIが勝手に決めてよい数ではない**。"
            "向きに世界軸の名前(`-Z` 等)を足さないのは、同じ向きの綴りが2系統になるため"
            "(ベクトルで表せる)"
        ),
    ),
    Command(
        name="clear_loads",
        resets_selection=False,
        category="feature",
        description=(
            "形状最適化で**荷重を外す**ときに呼ぶ。確定選択の面を荷重面から落とす"
            "(**面を1枚も選ばずに呼ぶと全部外れる**)。"
            "荷重が付いていない面が混ざっていても失敗しない。"
            "`set_loads` が上書き代入(選んだ面にだけ焼く)なので、外す操作はこちらにある。"
        ),
        session="clear_loads",
        uses_selection=True,
        note=(
            "**保持/除外/拘束の3つには解除専用verbが無い**(集合代入なので空で確定すれば外れる)。"
            "荷重だけ例外なのは、大きさ・向きを面ごとに持つために集合代入をやめたためである"
        ),
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
            "`entities` は対象集合ではなく引数で受ける。**投機実行(`previewable`)には載せない**のは"
            "安全のためではなく、変わるのがスケッチの図形でソリッドではないため —— "
            "`preview` が返すのは形が変わったBodyだけなので、載せても常に空になる。"
            "画面のプレビューは**ブラウザだけで描く**(2026-09-05に実装) —— 増える複製の座標は"
            "コアと同じ規則で `web/src/entityKinds.ts` が出せる"
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
                ws="required",
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
        note=(
            "**投機実行(`previewable`)には載せない**理由は矩形反復と同じ(変わるのはスケッチの"
            "図形でソリッドではない)。画面のプレビューは**ブラウザだけで描く**(2026-09-05に実装)"
            " —— 刻み角の場合分け(全周は `360/count`、全周未満は `total_angle/(count-1)`)も"
            "`web/src/entityKinds.ts` がコアと同じ規則で持つ。"
            "**`rotate_copies` は画面から送らない**(`ws=False`)ので、幽霊も既定の true で描く"
        ),
    ),
    # ---------------------------------------------------------------- ファイル操作
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
        name="import_svg",
        category="sketch",
        description=(
            "いま開いているスケッチへSVGの図形を足すときに呼ぶ。"
            "外で描いた輪郭(ロゴ・図面・アウトライン化した文字)を持ち込んで押し出したいとき用。"
            "図形は取り込んだ時点で領域として使えるので、続けて extrude を打てる。"
            "曲線はベジエのまま入る(折れ線に落ちない)。単位はmm、"
            "SVGが物理単位を持たなければ1ユーザー単位=1mm。"
        ),
        params=(
            _SKETCH_ID,
            Param("path", "string", "読み込むSVGファイルのパス"),
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
        resets_selection=True,
        note=(
            "**開いているスケッチへ足す**。貼る先を選ぶ引数は無い —— スケッチの外から使いたければ"
            "先に `sketch_create` で作ってから呼ぶ。**対象集合はもう選択から取らない**"
            "(`uses_selection` を外した) —— `sketch_id` を明示的に取る `add_line` と同じ形。"
            "取り込んだ曲線は `bezier` 種別で入るが、**`add_bezier` というverbは無い**"
            "(人もAIもベジエを1本ずつは足せない。2026-08-26本人裁定)。"
        ),
    ),
    Command(
        name="add_mesh_body",
        category="file",
        description=(
            "計算で作った三角形メッシュ(頂点＋三角形)をボディとして文書へ足すときに呼ぶ。"
            "**足したメッシュは編集できない** —— 表示 / STLへの書き出し / 削除 / "
            "表示の切り替えだけができる。押し出し・フィレット・面取りは断られる。"
            "三角形をそのまま操作ログへ積むので、手で並べるための口ではない。"
        ),
        params=(
            Param("positions", "array", "頂点座標[mm]を x,y,z の順に平らに並べた配列"),
            Param("indices", "array", "三角形を作る頂点添字(**0始まり**)を3つずつ並べた配列"),
            Param("name", "string", "ボディの名前(省略時 \"mesh\")", required=False, ws=False),
        ),
        session="add_mesh_body",
        # **WSには開けない**。画面から三角形を手で打つ操作は無く、入口は
        # MCPと、形状最適化の結果を入れるコード側だけである。UIから作る口が要るのは
        # 最適化を配線するとき(`20260904-1704`)で、そのときに `ws` を開ければよい。
        ws="none",
        note=(
            "**メッシュボディは編集verbを拒否する**(2026-09-04本人決定)。"
            "理由は `duocad/meshbody.py` のdocstring"
        ),
    ),
    Command(
        name="add_solid_body",
        category="file",
        description="",
        ws="none",
        mcp=False,
        note=(
            "**コードからだけ呼ぶverb**(実体は `duocad/verbs/add_solid.py`)。製品は呼ばない(形状最適化の"
            "結果は `add_mesh_body` のメッシュボディで入る)。この verb を含む保存済み文書の再生と"
            "`topopt_checks/replay_finalization.py` のために残す。人にもAIにも開けていない —— 引数はB-repの"
            "本文そのもので手で書くものではなく、CADを持ち込む口は `import_step` である。"
            "**それでもここに宣言するのは**、操作ログに載るverbとツールの顔ぶれを突き合わせる"
            "検査(`tests/test_mcp_verbs.py`)が、`mcp=False` かつ `ws=\"none\"` という宣言を"
            "「ツールを持たないverb」の唯一の出どころとして読むため。宣言を忘れるとそこで落ちる"
        ),
    ),
    Command(
        name="import_step",
        category="file",
        description=(
            "STEPを読み込むときに呼ぶ。**読んだ時点で編集できる** —— "
            "ソリッドごとに編集可能なボディができ、そのまま押し出し等の対象にできる。"
            "何部品あっても1操作(ログ1行・undo1回)。"
        ),
        params=(Param("path", "string", "読み込むSTEPファイルのパス"),),
        session="import_step",
        note="**STEPの読み込みはこの1 verbだけ。** 読んだ形はその場で編集できる形で入る",
    ),
    Command(
        name="export_step",
        category="file",
        description=(
            "いま表示中(visible)のボディをSTEPファイルへ書き出す。対象を選ぶ引数は無い —— "
            "含めたくないものがあれば、先に set_visible(visible=false) で隠してから呼ぶこと。"
            "文書は変えないのでログには載らない。戻り値でパス・バイト数・"
            "非表示で除外した件数(skipped_hidden)を確認する。"
        ),
        params=(Param("path", "string", "書き出し先パス(相対ならリポジトリ直下の export/ 基準)"),),
        session="export_step_file",
        changes_document=False,
    ),
    Command(
        name="export_stl",
        category="file",
        description=(
            "いま表示中(visible)のボディをSTLへ書き出す。対象を選ぶ引数は無い —— "
            "含めたくないものがあれば、先に set_visible(visible=false) で隠してから呼ぶこと。"
            "メッシュの粗さは固定で選べない(人の画面にも入力欄は無い)。文書は変えない。"
            "戻り値の skipped_hidden で非表示で除外した件数を確認できる。"
        ),
        params=(Param("path", "string", "書き出し先パス(相対ならリポジトリ直下の export/ 基準)"),),
        session="export_stl_file",
        changes_document=False,
    ),
    Command(
        name="new_document",
        category="file",
        description="",
        session="new_document",
        ws="special",
        mcp=False,
        note=(
            "**空の文書へ差し替える**(画面の「新規」)。`open_document` と同じく文書ごと"
            "差し替えるので応答の形が他と違い(全接続へ配る)、表からは外して `_dispatch` で"
            "個別に扱う。**AIには開けていない**(`mcp=False`) —— 今の文書を丸ごと捨てる操作で、"
            "undoでは戻らない。捨てる判断は人がする"
        ),
    ),
    Command(
        name="save_document",
        category="file",
        description="",
        params=(
            Param(
                "name",
                "string",
                "**新しい**保存先のファイル名だけ(models/ 配下に固定。区切り文字・絶対パス・"
                "..は不可)。**省略すると編集中のファイルへ上書きする**",
                required=False,
            ),
        ),
        session="save_document",
        ws="special",
        mcp=False,
        changes_document=False,
        note=(
            "**AIには開けていない**。保存形式は操作ログのJSONで、AIは describe_document で"
            "同じ内容を読めるので、ファイルとして書き出す必要があるのは人の側だけである。"
            "2026-08-22に引数を `path`(サーバのCWD基準の任意パス)から `name`"
            "(`models_dir` 配下のファイル名だけ)へ締めた。"
            "**2026-08-30に `name` を任意引数にし、表(`ws=\"table\"`)から外して "
            "`_dispatch` の個別分岐へ移した** —— 表は載せた引数を全部必須にする規律なので"
            "(`Param.ws` のdocstring)、省略を許すコマンドは表に置けない。"
            "契約は `Session.save_document`: 引数なし=編集中のファイルへ上書き / "
            "`name` あり=**新しい名前でのみ**保存(既存名は拒否)。"
            "**2026-09-16の自動保存化(issue #5)以後、UIが送るのは引数なし(Ctrl+S)だけ** —— "
            "名前を付けるのは `rename_document`、無題の初回は `Session.autosave` が `name` を渡す"
        ),
    ),
    Command(
        name="rename_document",
        category="file",
        description="",
        params=(
            Param(
                "name",
                "string",
                "いま編集しているファイルの**新しい**名前(models/ 配下に固定。既に在れば拒否)",
            ),
        ),
        session="rename_document",
        ws="special",
        mcp=False,
        changes_document=False,
        note=(
            "**編集中のファイルをディスク上で改名する**(issue #5、2026-09-16)。1操作ごとの自動保存が"
            "無題の文書に `untitled-<n>.json` を付けるので、人が名前を決める口はこれ1つになった"
            "(パネルの名前欄)。応答は `save_document` と同じく `file` の封筒 + 状態の配り直し"
            "(現在ファイル名が変わる)なので表から外して `_dispatch` で個別に捌く。人だけの操作"
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
    Command(
        name="copy_document",
        category="file",
        description="",
        params=(
            Param("source", "string", "複製元のファイル名(models/ 配下)"),
            Param("dest", "string", "複製先のファイル名(models/ 配下。既に在れば拒否)"),
        ),
        session="copy_document",
        mcp=False,
        changes_document=False,
        note=(
            "**一覧の行から別名で複製する**(issue #17)。ディスク上のJSONを写すだけで、"
            "いま編集している文書は変わらない。ファイルの出し入れは人の操作(`mcp=False`)。"
            "表に載る(`ws=\"table\"`)ので応答は `file` の封筒で要求した接続にだけ返る"
        ),
    ),
    Command(
        name="delete_document",
        category="file",
        description="",
        params=(Param("name", "string", "削除するファイル名(models/ 配下。編集中のファイルは拒否)"),),
        session="delete_document",
        mcp=False,
        changes_document=False,
        note=(
            "**一覧の行からディスク上のファイルを削除する**(issue #17)。編集中のファイルは"
            "拒否(どうすればできるかを言う)。確認は画面側が行内の2段階で聞く。"
            "不可逆な操作なので人だけ(`mcp=False`)"
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
        name="set_view_resolution",
        category="protocol",
        description="",
        params=(
            Param(
                "world_per_pixel",
                "number",
                "画面1画素ぶんの世界長さ[mm](注視点の面で測る)",
            ),
            Param(
                "size_hint",
                "number",
                "いま見えているものの外接対角[mm](上下限の丸めにだけ使う)",
                required=False,
            ),
        ),
        session="set_view_resolution",
        ws="special",
        mcp=False,
        changes_document=False,
        note=(
            "**文書を変えない画面の設定**(可変解像度)。カメラのズームに応じた目標の"
            "テセレーション精度を決め直し、変わったときだけ状態を配信し直す。"
            "**導出済みの偏差ではなくカメラの事実を受け取る** —— 変換式の持ち主を"
            "`duocad/resolution.py` 1つに保つため(`size_hint` を省略できるので "
            "`ws=\"special\"`)"
        ),
    ),
    # ---------------------------------------------------------------- 形状最適化を走らせる(protocol)
    #
    # **verbではない。** 文書へ積まれるのは `run_optimization` が中で呼ぶ `add_mesh_body`
    # 1本だけで、この4つは「点検する / 見積もる / 走らせる / 止める」であり操作ログに
    # 載らない(`changes_document=False`。`run_optimization` が結果を入れる経路は
    # `add_mesh_body` の側でログに載る)。したがって `catalog` の分類は `protocol` である。
    #
    # **4つとも `mcp=False`。** AIに走らせる口は今回は開けない —— 数分かかる同期の呼び出しを
    # MCPのツールとして出すと、進捗も中断も運べないまま呼び出し側がタイムアウトする
    # (WSは進捗イベントと `cancel_optimization` を持つのでこの経路だけを開ける)。
    # AI から走らせたくなったら、まず**進捗と中断をMCPで運ぶ形**を決めること。
    #
    # **`ws="special"`** —— 表(`ws="table"`)は「載せた引数は全部必須」であり、ここは
    # 省略できる引数しか無い(既定＝いちばん粗い設定で走る)。
    Command(
        name="optimize_conditions",
        category="protocol",
        description="",
        session="optimize_conditions",
        ws="special",
        mcp=False,
        changes_document=False,
        note=(
            "**走らせる前の点検**(`optimize_run.conditions_report`)。`problems` は"
            "そのまま人に見せる文で、画面は実行ボタンの可否をここで決める。"
            "拘束面・荷重面は**何枚でもよい**(0枚だとここが断る)"
        ),
    ),
    Command(
        name="optimize_resolution",
        category="protocol",
        description="",
        session="optimize_resolution",
        ws="special",
        mcp=False,
        changes_document=False,
        note=(
            "**軽い**(OCCTは条件面のbboxを数枚測るだけ)。返る `steps` が**粗い側の端から"
            "細かい方向へだけ**動く目盛り。**時間は付けない**(走らせる前は"
            "実測が無い)。**引数は無い**(密度フィルタ半径`rmin_mm`は二値探索に無い概念。"
            "届いたら `{\"error\": ...}` として断る、`OptimizeInputError` 経由)"
        ),
    ),
    Command(
        name="run_optimization",
        category="protocol",
        description="",
        params=(
            Param("element_size_mm", "number", "要素サイズ[mm]。省くと上限側(既定)", required=False),
            Param("target_safety_factor", "number", "目標安全率", required=False),
            Param(
                "max_evaluations", "number",
                "形状探索(初期合格探索とエネルギー順の削減)の評価予算。省くと既定(binary_search.find_best_feasibleの既定)",
                required=False,
            ),
        ),
        session="run_optimization",
        ws="special",
        mcp=False,
        # **文書は変わりうる**が、載るのは中で呼ぶ `add_mesh_body` のログ1行であり、
        # この名前でログに載るverbは無い。`changes_document` は「この名前がログに載るか」
        # を意味する(`registered_verbs()` との突き合わせに使われる)ので偽にする。
        changes_document=False,
        note=(
            "**重い**(既定の設定でも数十秒〜数分)。`net/ws.py` が `asyncio.to_thread` で"
            "回し、進捗は `optimize_progress`、結果は `optimize_result` として配る"
            "(応答は状態配信ではない)。**失敗しても文書は1ビットも変わらない**。"
            "旧引数(`rmin_mm`/`max_iter`/`material_min_mm3`/`material_max_mm3`)は"
            "この版では扱えず、届くと `OptimizeInputError` を伴う `optimize_error` になる"
        ),
    ),
    Command(
        name="cancel_optimization",
        category="protocol",
        description="",
        session="cancel_optimization",
        ws="special",
        mcp=False,
        changes_document=False,
        note=(
            "**旗を立てるだけ**。実際に降りるのは次に進捗が出た時点で、"
            "進捗を出さない短い処理の途中では止まらない(`20260904-1704` の契約③)"
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
        command.name: (command.preview_session or command.session, command.preview_params)
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

    **どこまで自分でやるかもここに書く**。規則は2段:

    - **確認を待たず適用してよい操作** … 対象を何個でも取る引数が無く、how(寸法・選択肢・
      パス)の入力も無いもの。人に聞くことが残っていないので、提案で止める意味が無い。
      **判定基準は手動UIのEnter要否と同一**(`web/src/commands/command.ts` の `needsEnter`)。個々のverb名をこの文に書かない —— verbを
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
        "set_visible の結果は人間が開いているブラウザの画面にも即座に反映される"
        "(undo では戻らないので、戻すには visible=true で呼び直す)。"
    )
