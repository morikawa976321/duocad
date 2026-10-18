"""verbの単一宣言 —— **1 verbの追加 = ここに1エントリ**。

**このファイルが正**で、以下はすべてここからの導出である:

- `duocad/server.py` … `PREVIEWABLE_VERBS` / `SKETCH_COMMANDS` / `FEATURE_COMMANDS` /
  `FILE_COMMANDS`(WebSocketの許可リスト)と、WebSocket が受け取る引数とその検査
- `duocad/mcp_server.py` … `tools/list` が返すツール定義と `tools/call` の引数の検査

frontend(`web/src/commands/*.ts`)だけは別に持つ —— TS側が持つのは**引数の集め方**
(どのクリックを待つか・入力欄のid・ボタンのid)で、ここには無い情報だから。

verbを1つ足すとき:

1. `duocad/verbs/` にverbのモジュールを書く —— 純関数(対象を引数で受ける)と `register_verb`、
   それに入口(`@entry`。対象を `selection.require_*` から取って純関数へ渡す。引数は数値・名前・
   パスだけ)。`duocad/verbs/__init__.py` に import の1行を足す
2. **このファイルの `COMMANDS` に1エントリ足す**(引数名は入口の引数と同じ綴り)
3. UIから踏むなら `web/src/commands/*.ts` に引数の集め方を書く(`category` を忘れない)

WebSocket・MCP・アプリ内AIはどれも `Session.apply` を通って入口を呼ぶので、verbごとに書く
配線は無い。

**対象集合は引数ではない** —— 面・辺・領域はどの層でも引数で受けず、コアの選択から取る
(`selection.require_selected` / `require_edges`)。したがってここに並ぶ引数は**数値・名前・
パスだけ**である。`body_id` は「どのBodyへ足す/から引くか」という**名前**であって対象集合
ではない(Bodyは選択集合の項目ではない)。

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
#: - `protocol`  … WebSocketだけの口(verbではない。状態の要求・プレビュー・辺の折れ線・アプリ内AIと計算の設定)。
#:                 **ソケットが受け取る名前はこの宣言で全部**(`net/ws.py` の `_dispatch` は宣言に無い名前を断る)
#: - `workspace` … 作業空間(起動中のコマンド・モード。`duocad/workspace.py`)。選択と同じ共有の短命状態で、人も AI も同じ操作で触る
CATEGORIES = ("query", "selection", "sketch", "feature", "file", "history", "protocol", "workspace")

#: 形(面・辺・領域の添字)を変えうる verb のカテゴリ。ここに属し文書を変える宣言は、
#: `resets_selection` を**明示しないと import 時点で落ちる**(既定に頼ると付け忘れが
#: どこにも現れない。issue #51)。
SHAPE_CATEGORIES = ("sketch", "feature", "history")

#: `Command.ws` に書ける値。
#:
#: - `table`   … `server.py` の表(`SKETCH_COMMANDS` 等)に載り、`_dispatch` が機械的に捌く。
#:               verb は `Session.apply` へ、verb でないファイル操作は `Session` のメソッドへ行く
#: - `special` … WebSocketから踏めるが `_dispatch` が個別に捌く。**verb ではないもの**
#:               (選択・可視性・履歴・状態の要求)と、応答の形が他と違うファイル操作
#:               (`open_document` / `save_document`)がここへ来る
#: - `none`    … WebSocketには開けていない(読み取り・述語の語彙など、画面が使わないもの)
WS_SURFACES = ("table", "special", "none")

#: 版によって隠せる機能(`Command.feature`)。どの版で隠すかは `channel.hidden_features`。
#: `local_ai` は宣言に属さない(アプリ内AIのプロバイダ `openai_compat` を隠す。`net/ws.py` の `Server.ai_providers`)。
FEATURES = ("shape_optimization", "local_ai")


@dataclass(frozen=True)
class Param:
    """verbが受け取る引数1つ。`type` / `description` はMCPの入力スキーマになり、
    `ws` はWebSocketの表に載るかどうかを決める。
    """

    #: 引数名。**MCP・WS・`Session` のメソッドで同じ綴り**である。
    name: str
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
    #: **形の寸法を決める値か**。`agent_instructions()` の言う how のうち**数値の分だけ**を
    #: 印す —— 半径・距離・間隔・個数・角度。
    #:
    #: 選択肢(`mode` / `visible`)とパス(`path`)は how だが印さない。言葉で指定されるので
    #: 「人が数量を言ったか」では測れず、印すと誤って止まる(「カバーを隠して」に数字は無い)。
    #: **AIが勝手に決めてしまうのは寸法**であり、undoでしか戻せない形を左右するのも寸法である。
    #:
    #: 対象を指すもの(`body_id` / `sketch_id` / `entities`)、点の座標(`center` / `corner` /
    #: `p0`)、貼る先の平面名(`base_plane`)、命名(`name`)も how ではないので印さない
    #: (`agent_instructions()` の但し書きと同じ線)。
    #:
    #: 使い道は `duocad/agent/loop.py` のガード1つだけ。
    dimension: bool = False


@dataclass(frozen=True)
class Result:
    """verb が応答(変更イベント `applied`)に載せる値1つ。

    `type` は **TypeScript の型の書き方そのまま**(`"string"` / `"number"` / `"boolean"` /
    `"string[]"` / `"number[]"` と、その `" | null"` つき)。`web/src/generated/protocol.ts` の
    `Applied` はここからの生成物で、実際の応答と一致することは `tests/test_protocol.py` が
    verb を走らせて見る。
    """

    name: str
    type: str


@dataclass(frozen=True)
class Command:
    """verb(またはコアへの口)1つの宣言。**追加はここ1箇所で済ませる。**"""

    #: **MCPツール名 == WSコマンド名 == UIコマンドID == 操作ログのverb名**。AIは
    #: `describe_document` のログを読んで次の一手を決めるので、綴りが違うと対応表を推測させる。
    name: str
    category: str
    #: AIに見せる説明。**何ができるかではなく「いつ使うか」**を書く
    #: (説明文はAIがツールを選ぶ唯一の手がかり)。`mcp=False` のものは空でよい。
    description: str = ""
    params: tuple[Param, ...] = ()
    #: verb が応答(`applied`)に載せる値。入口(`duocad/entry.py`)の戻り値のキーと一致させる
    #: (`verb` / `seq` / `client_op` はどの verb にも付くので書かない)。verb でないものは空。
    returns: tuple[Result, ...] = ()
    #: `Session` のメソッド名(WebSocket側の実体)。WSに開けていないものは None。
    #: **verb名と違ってよい**(`export_step` -> `export_step_file`)。
    #: `ws="special"` で実体が `Session` でなく `net/ws.py` の `Server` にある口(アプリ内AI・計算の設定)も None。
    session: Optional[str] = None
    #: WebSocketでの露出のしかた(`WS_SURFACES`)。
    ws: str = "table"
    #: MCPのツールとして公開するか(`save_document` / `open_document` は人だけの操作)。
    mcp: bool = True
    #: `Session.preview` が投機実行してよいか。**巻き戻せることを確認したverbだけ**を true に
    #: する —— 操作ログを頭から再生するverb(`set_parameter`。公開面には無い)は `speculative` では戻らない
    #: (`speculative.py` の「戻さないもの」)。
    previewable: bool = False
    #: プレビューを投機実行でなく、**文書を変えずに結果だけ計算する** `Session` のメソッドで出すときの
    #: 名前(`previewable=True` と組で使う)。投機実行は verb が失敗すると何も返せないので、失敗の手前の
    #: 形(`envelope_regions` の分裂)も見せたい verb はこちら。
    preview_session: Optional[str] = None
    #: 文書を変えるか(= 操作ログに載り、応答が状態配信になるか)。保存・書き出しは文書を
    #: 変えないので、応答は書いた先のパスだけを要求した接続へ返す。
    changes_document: bool = True
    #: 確定後に選択集合を全リセットするか。**面・辺・領域の添字は形状変更で壊れる**
    #: (kernel.pyの既知の地雷)ので、形を変えるverbの確定後は選択を空にする。
    #: `Session._reset_selection_if_needed` が1箇所で発火するので、UI経由・MCP経由の
    #: どちらでも同じに効く。**失敗したverb(ログに積まれない)には効かない**。
    #: **`undo`/`redo` も true**(何を戻したかで場合分けしない単純な規則を優先する)。
    #: **`SHAPE_CATEGORIES` の verb は書くこと**(`None` のままなら `__post_init__` が落とす)。
    #: それ以外は省略すると false。
    resets_selection: Optional[bool] = None
    #: この宣言が属する、版によって隠せる機能(`FEATURES`)。隠した版ではMCPの道具にもWSの口にも出さない。
    feature: Optional[str] = None
    #: 設計上の注記(なぜこの形か)。コードの読み手向けで、AIには渡さない。
    note: str = ""

    def __post_init__(self) -> None:
        if self.category not in CATEGORIES:
            raise ValueError(f"未知のカテゴリ: {self.category!r}({CATEGORIES})")
        if self.ws not in WS_SURFACES:
            raise ValueError(f"未知のWS露出: {self.ws!r}({WS_SURFACES})")
        if self.ws == "table" and self.session is None:
            raise ValueError(f"{self.name}: 表で捌くなら `session` が要る")
        if self.previewable and self.session is None:
            raise ValueError(f"{self.name}: プレビューは `Session` のメソッドを呼ぶ")
        if self.preview_session is not None and not self.previewable:
            raise ValueError(f"{self.name}: `preview_session` は `previewable=True` と組で使う")
        if self.feature is not None and self.feature not in FEATURES:
            raise ValueError(f"{self.name}: 未知の機能 {self.feature!r}({FEATURES})")
        if self.resets_selection is None:
            if self.category in SHAPE_CATEGORIES and self.changes_document:
                raise ValueError(
                    f"{self.name}: 形を変えうる verb は `resets_selection` を宣言する(既定に頼らない)"
                )
            object.__setattr__(self, "resets_selection", False)

    @property
    def ws_params(self) -> tuple[str, ...]:
        return tuple(p.name for p in self.params if p.ws)

    def ws_schema(self) -> dict:
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
        return _schema(self.params, [p.name for p in self.params if p.required])

    def partial_schema(self) -> dict:
        """途中入力(`Session.begin_command` / `set_command_args`)の検査: 未知のキーと型だけで、必須は見ない。"""
        return _schema(self.params, [])

    def staged_schema(self) -> dict:
        """構えた verb を確定する(`Session.apply_command`)ときの検査。キーは全引数、必須は**画面と同じ厳しさ**
        (`ws="required"` も省けない)。人と AI が一緒に埋める口なので、MCP の既定値(`extrude.mode` の join)で黙って補わない。
        """
        return _schema(self.params, [p.name for p in self.params if p.ws == "required" or (p.ws and p.required)])

    def descriptor(self) -> dict:
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
    **任意引数の `null` も型違い**(省略はキーごと落とすこと)。
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
    if expected == "null":
        return value is None
    return True


#: 並び順は `tools/list` に出る順序でもある(AIが最初に読む順)。
#: スケッチのverbが共通に受ける `sketch_id`(`Param` は frozen なので共有してよい)。
_SKETCH_ID = Param("sketch_id", "string", "対象スケッチのID")
#: `sketch_create` / `sketch_create_offset` の応答に載せるスケッチの枠(`kernel_sketch.sketch_plane_frame`)。
_SKETCH_FRAME = Result("frame", "{ origin: number[]; u: number[]; v: number[]; normal: number[] }")
#: 同じ2つの説明に足す座標系の注意。面に貼ったスケッチの枠は面の平面の座標系そのままで、原点も
#: 向きも面ごとに違う(実測: 100x60x10 の板で上面の原点は面の中心、+X 面は角 (100, 0, 0) で
#: v は世界の -Z)。AIは基準平面と同じ世界座標だと思い込んで描き、形が黙って別の場所にできる
#: (アプリ内AIが上面中央のポケットを4分の1だけ削った例が8題×3条件で3回。Issue #125)。
_SKETCH_FRAME_HELP = (
    "**図形の座標 (u, v) は戻り値の frame で測る**(3D位置 = origin + u*frame.u + v*frame.v)。"
    "基準平面なら世界座標のままだが、**面を基準にすると原点も軸の向きも面ごとに違う**"
    "(面の中心とは限らず角のこともあり、u/v が世界の軸と逆向きのこともある)。"
    "世界の点 P に描くなら u = (P - origin)・frame.u、v = (P - origin)・frame.v で換算する。"
)

#: `set_selection` の述語の書き方。**説明に載せておく** —— 書き方を知らずに呼ぶと失敗して語彙が返り、
#: 知るために `describe_predicates` を呼んでも1往復かかるので、どちらにせよ選択のたびに
#: 1往復を払っていた。語彙の正は `predicates.describe_ops()` で、ここに無い op が増えたら
#: `tests/test_mcp_selection.py` が落ちる(このモジュールは `predicates` を import しない)。
PREDICATE_HELP = (
    '述語。{"kind": "face"|"edge", "body": body_id, "where": [条件, ...]} の形。'
    "body は省略でき、省略すると見えているボディ全部が対象になる"
    "(1つのボディだけに掛けたいときは必ず付ける。fillet / chamfer は1つのボディの辺しか受けない)。"
    'where は全条件のAND。省略はできず、条件なし(その種別の全部)は "where": [] と書く。ORは無い。'
    '面の条件: {"op": "normal_dot_gt", "axis": "X"|"Y"|"Z"|[x,y,z], "value": 数}'
    "(外向き法線と軸の内積が value より大きい。上面なら axis Z・value 0.9) / "
    '{"op": "area_gt", "value": 数} / '
    '{"op": "kind_is", "value": "plane"|"cylinder_full"|"cylinder_partial"|"torus"|"cone"|"bspline"|"other"}。'
    '辺の条件: {"op": "parallel_to", "axis": 軸} / {"op": "length_gt", "value": 数} / '
    '{"op": "length_lt", "value": 数} / {"op": "kind_is", "value": "line"|"circle"|"other"} / '
    '{"op": "on_boundary_of_face", "face": 面の番号}(その面の外周の辺。body が要る)。'
    "refs とは同時に渡せない"
)

COMMANDS: tuple[Command, ...] = (
    Command(
        name="describe_document",
        category="query",
        description=(
            "開いている文書のボディ一覧(body_id・名前・種別・可視性・面数・体積)と"
            "スケッチ・パラメータ一覧を返す。set_visible や describe_shape、"
            "pattern_* を呼ぶ前に、どのボディ・図形・寸法名が"
            "存在するかを知るために最初に呼ぶ。"
            "document_open が false なら人が文書を開いていない(トップ画面)ので、"
            "形を変えるツールは断られる。人に画面で新規か既存の文書を開いてもらうこと。"
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
            "set_selection の predicate= に書ける述語の語彙(op・引数・種別)を返す。"
            "同じ内容が set_selection の説明にあるので、それを読めば呼ばなくてよい。"
        ),
        ws="none",
    ),
    Command(
        name="measure",
        category="query",
        description=(
            "いまの選択の長さ・面積・法線・重心などを測るときに呼ぶ。"
            "面・領域の `center` は面積重心、ボディの `center` は体積重心(どちらもワールド座標)で、"
            "領域はスケッチ座標の面積重心も `center_uv` に返る。"
            "重心に穴をあける・重心に合わせて置くときは、重心を公式から自分で計算せず、"
            "領域・面・ボディを選んでこれで読む(押し出す前の位置決めは領域で読む)。"
            "面は種類を `surface_kind` に、円(円弧)の辺は `radius` と `center` を、"
            "スケッチの線分は長さ、楕円は周長を `length` に返す。"
            "ちょうど2項目(頂点・直線の辺・平らな面)なら、そのあいだの距離が `between` に"
            "返る(直線・平面は無限に延ばして測る。平行でない線・面どうしは距離の代わりに"
            "角度)。verb ではないので、選んだ対象を確かめる往復に使う。"
        ),
        session="measure",
        ws="special",
        changes_document=False,
        note=(
            "画面の「測定」コマンドの実体。WSは要求した接続へ `{type:'measure', client_op, ...}` を"
            "返す(状態配信ではない。`get_edges` と同じ扱い)。"
        ),
    ),
    Command(
        name="get_selection",
        category="selection",
        description=(
            "いまの選択(項目・件数)を読むときに呼ぶ。"
            "選んだ直後や人が直したあとに、次の一手の対象を確認する。"
        ),
        ws="none",
    ),
    Command(
        name="set_selection",
        category="selection",
        description=(
            "選択を丸ごと置き換えるときに呼ぶ。refs(明示参照)か predicate(述語)の"
            "どちらか一方。そのまま verb に渡せる。"
            "選択だけで止めるか verb まで進めるかは、人の指示の文面から判断する"
            "(判断の基準は instructions)。"
        ),
        params=(
            Param(
                "refs",
                "array",
                "明示参照の配列。面・辺・頂点は "
                '{"body": body_id, "kind": "face"|"edge"|"vertex", "index": 番号}、'
                'ボディ丸ごとは {"body": body_id, "kind": "body"}、'
                'スケッチの領域は {"sketch": sketch_id, "index": 番号}(番号は1始まり)、'
                'スケッチの図形は {"sketch": sketch_id, "entity": entity_id, "point": null}'
                "(point に x0 などの点の名前を入れるとその点だけ)。"
                "predicate とは同時に渡せない",
                required=False,
                ws=False,
            ),
            Param(
                "predicate",
                "object",
                PREDICATE_HELP,
                required=False,
                ws=False,
            ),
        ),
        ws="none",
        note="述語はAIが書く語彙なのでブラウザには開けない(人はクリックで選ぶ・直す)",
    ),
    Command(
        name="modify_selection",
        category="selection",
        description=(
            "選択の過不足を差分で直すときに呼ぶ。"
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
        name="toggle_selection",
        category="selection",
        description=(
            "選択の参照ごとに、あれば外し無ければ足すときに呼ぶ"
            "(画面でクリックして付け外しするのと同じ操作)。"
        ),
        params=(Param("refs", "array", "付け外しする参照の配列"),),
        session="toggle_selection",
        ws="special",
        note="WSでは `client_op` を添えるので個別に捌く",
    ),
    Command(
        name="clear_selection",
        category="selection",
        description="選択を空にしたいときに呼ぶ。",
        session="clear_selection",
        ws="special",
    ),
    Command(
        name="sketch_create",
        returns=(Result("sketch_id", "string"), _SKETCH_FRAME),
        resets_selection=False,
        category="sketch",
        description=(
            "新しいスケッチを作るときに呼ぶ。"
            "base_plane(XY/XZ/YZ)を渡すか、選択の平面(ちょうど1枚)に貼る。" + _SKETCH_FRAME_HELP
        ),
        params=(
            Param(
                "base_plane",
                "string",
                "基準平面名 XY / XZ / YZ。省略時は選択の面",
                required=False,
            ),
        ),
        session="sketch_create",
        note=(
            "**MCPとWSで引数が同じ**。どちらも `base_plane` だけを取り、省略時は"
            "コアの選択の面(ちょうど1枚)に貼る"
        ),
    ),
    Command(
        name="sketch_create_offset",
        returns=(Result("sketch_id", "string"), _SKETCH_FRAME),
        resets_selection=False,
        category="sketch",
        description=(
            "オフセット平面に新しいスケッチを作るときに呼ぶ。"
            "base_plane(XY/XZ/YZ)を渡すか選択の平面(ちょうど1枚)を基準にし、"
            "その法線方向へdistanceだけ離れた平面に貼る。" + _SKETCH_FRAME_HELP
        ),
        params=(
            Param(
                "base_plane",
                "string",
                "基準平面名 XY / XZ / YZ。省略時は選択の面",
                required=False,
            ),
            Param(
                "distance",
                "number",
                "基準平面からのオフセット距離(mm・法線方向が正)",
                dimension=True,
            ),
        ),
        session="sketch_create_offset",
        note=(
            "**`sketch_create` とは別verb**(`sketch.sketch_create` のdocstring参照)。"
            "貼る先(target)は同じ2状態(基準平面名 / 選択の面)だが、`distance` という"
            "howを持つのでEnterが要る —— 同じverb名に足すと `sketch_create` 自体の"
            "即適用(Enter不要)まで変わってしまうため、名前を分けてある。"
        ),
    ),
    Command(
        name="add_line",
        returns=(Result("entity_id", "string"),),
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
        returns=(Result("entity_id", "string"),),
        category="sketch",
        description="スケッチに円を足すときに呼ぶ。",
        params=(
            _SKETCH_ID,
            Param("center", "array", "中心 [u, v]"),
            Param("diameter", "number", "直径(mm)", dimension=True),
        ),
        session="add_circle",
        resets_selection=True,
    ),
    Command(
        name="add_ellipse",
        returns=(Result("entity_id", "string"),),
        category="sketch",
        description="スケッチに楕円を足すときに呼ぶ(中心・2方向の直径・傾き)。",
        params=(
            _SKETCH_ID,
            Param("center", "array", "中心 [u, v]"),
            Param("width", "number", "angle の向きの直径(mm)", dimension=True),
            Param("height", "number", "width に直交する向きの直径(mm)", dimension=True),
            Param(
                "angle",
                "number",
                "width の向き(度・反時計・X軸から。省略で0)",
                required=False,
                ws="required",
                dimension=True,
            ),
        ),
        session="add_ellipse",
        resets_selection=True,
    ),
    Command(
        name="add_rectangle",
        returns=(Result("entity_id", "string"),),
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
        returns=(Result("entity_id", "string"),),
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
    Command(
        name="extrude",
        returns=(
            Result("body_id", "string"),
            Result("distance", "number"),
            Result("mode", "string"),
            Result("changed_bodies", "string[]"),
        ),
        category="feature",
        description=(
            "選択の領域、または同一平面・同一ボディのソリッドの面(何枚でも)を押し出して、"
            "ソリッドを足す/切る/新しいボディにするときに呼ぶ。"
            "対象は選択から取る(引数では受けない)。領域と面の混在は不可。"
            'スケッチの領域は set_selection(refs=[{"sketch": sketch_id, "index": 番号}]) で先に選ぶ'
            "(領域は述語では選べない。図形を1つ描いたスケッチなら番号は1)。"
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
        name="revolve",
        returns=(
            Result("body_id", "string"),
            Result("angle", "number"),
            Result("mode", "string"),
            Result("changed_bodies", "string[]"),
        ),
        category="feature",
        description=(
            "選択の領域(1つのスケッチの、何個でも)を、同じスケッチ平面上の軸のまわりに回して"
            "回転体(軸・瓶・プーリーなど軸対称の形、外周の溝や皿穴の切り取り)を作るときに呼ぶ。"
            "ボディを回して置き直すのは move_body で、これではない。"
            "対象は選択から取る(引数では受けない)。"
            'スケッチの領域は set_selection(refs=[{"sketch": sketch_id, "index": 番号}]) で先に選ぶ。'
            "軸が領域を横切る(領域が軸の両側にまたがる)と失敗する。軸が領域の辺に重なるのはよい。"
        ),
        params=(
            Param(
                "axis",
                "string",
                '回転軸がスケッチの原点を通る軸のとき "x" / "y"。axis_line と排他(どちらか一方を必ず渡す)',
                required=False,
            ),
            Param(
                "axis_line",
                "object",
                "回転軸にする、領域と同じスケッチの線分への参照 "
                '{"sketch": sketch_id, "entity": entity_id}(線分を延ばした直線が軸)。axis と排他',
                required=False,
            ),
            Param(
                "angle",
                "number",
                "回す角度(度・0 < |angle| <= 360・既定 360)。正は軸の向き(線分の始点→終点、x / y 軸は正の向き)"
                "まわりの右ねじ、負は逆向き",
                required=False,
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
                "symmetric",
                "boolean",
                "true ならスケッチ平面から両側へ angle の半分ずつ回す(既定 false)",
                required=False,
            ),
            Param(
                "body_id",
                "string",
                "対象ボディ。省略時はアクティブ(無ければ新規作成)。mode='new' では指定できない(行き先を持たない)",
                required=False,
                ws=False,
            ),
        ),
        session="revolve",
        previewable=True,
        resets_selection=True,
        note=(
            "**`mode` はWSでだけ必須**(`extrude` と同じ理由)。**`body_id` はWSのコマンドには無い**(`extrude` と同じ)。"
            "軸は選択ではなく引数 —— `mirror_sketch_entities` の `axis` と同じ別チャンネルで、共有選択集合に"
            "線分を入れると領域の選択と混ざる。**`axis` と `axis_line` を1つの引数にしない** —— 画面の欄(軸の3択と"
            "線分のピック)と作業空間の引数が名前で1対1に往復できないと、再読み込みや AI の引数変更で指した線分が消える"
            "(`mirror_body` の `base_plane` / `plane_face` と同じ形)。"
        ),
    ),
    Command(
        name="loft",
        returns=(
            Result("body_id", "string"),
            Result("sections", "number"),
            Result("mode", "string"),
            Result("changed_bodies", "string[]"),
        ),
        category="feature",
        description=(
            "別々の平面にある断面(スケッチの領域)を、選んだ順になめらかにつないで立体(ロフト)を作るときに呼ぶ。"
            "四角から丸へ変わる継手・ノズル・断面が変わりながら細る形など。断面ごとに形も大きさも違ってよい。"
            "対象は選択から取る(引数では受けない)。断面の領域を、つなぐ順に "
            'set_selection(refs=[{"sketch": sketch_id, "index": 番号}, ...]) で先に選ぶ(1断面につき1領域、'
            "隣り合う断面は別の平面)。穴のある領域は断面にできない —— 外周を join でロフトし、内側の領域を cut で"
            "ロフトする。断面2つの間は直線的につながる(曲げたいなら間に断面を足す)。"
        ),
        params=(
            Param(
                "mode",
                "string",
                "join(足す・既定) / cut(切る) / new(独立した新しいボディを作る)",
                required=False,
                ws="required",
            ),
            Param(
                "start_point",
                "object",
                "最初の断面の前を収束させる点(尖らせる)。スケッチの図形の点 "
                '{"sketch": sketch_id, "entity": entity_id, "point": "x0"}(線分の端点 x0 / x1、円・楕円・円弧の中心 cx など)',
                required=False,
            ),
            Param(
                "end_point",
                "object",
                "最後の断面の後を収束させる点。形は start_point と同じ。断面1つと点1つでもロフトになる",
                required=False,
            ),
            Param(
                "shifts",
                "array",
                "断面ごとの整数(選んだ順・断面の数と同じ長さ・省略は全部0)。自動で揃えた始点を頂点 k 個ぶん進める"
                "(負は戻す。頂点の無い円・楕円は 90° 単位)。結果がねじれた・潰れたときに隣の断面との対応をずらす",
                required=False,
            ),
            Param(
                "body_id",
                "string",
                "対象ボディ。省略時はアクティブ(無ければ新規作成)。mode='new' では指定できない(行き先を持たない)",
                required=False,
                ws=False,
            ),
        ),
        session="loft",
        previewable=True,
        resets_selection=True,
        note=(
            "**`mode` はWSでだけ必須**、**`body_id` はWSのコマンドには無い**(`extrude` と同じ)。"
            "断面は選択の領域を**選んだ順**に使う(選択集合は追加順を保つ)。点と `shifts` は選択ではなく引数。"
            "向き・始点・辺の数は OCCT の自動の揃え(`CheckCompatibility`)に任せず `kernel_extrude.loft_regions` が"
            "揃える(任せると 90° の平面どうしや楕円で黙って潰れる。docs/CAD_LIBRARY_PITFALLS.md「ロフト」)。"
        ),
    ),
    Command(
        name="fillet",
        returns=(Result("body_id", "string"), Result("changed_bodies", "string[]")),
        category="feature",
        description=(
            "選択の辺を丸めるときに呼ぶ。"
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
        session="fillet",
        previewable=True,
        resets_selection=True,
    ),
    Command(
        name="chamfer",
        returns=(Result("body_id", "string"), Result("changed_bodies", "string[]")),
        category="feature",
        description=(
            "選択の辺を面取りするときに呼ぶ。"
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
        session="chamfer",
        previewable=True,
        resets_selection=True,
    ),
    Command(
        name="shell_body",
        returns=(Result("body_id", "string"), Result("changed_bodies", "string[]")),
        category="feature",
        description=(
            "ボディを中空の殻(箱・ケース・カップの形)にするときに呼ぶ。"
            "選択の面(1枚以上・1つのボディ)を抜いて開口にし、残りの壁を外形から内側へ同じ厚さで残す"
            "(外形寸法は変わらない)。面を選ばない閉じた中空は作れない。"
            "抜く面に丸めた面が接していると失敗するので、そのときは先にシェルを掛けてから丸める。"
        ),
        params=(
            Param("thickness", "number", "壁の厚さ(mm)。外形から内側へ取る", dimension=True),
        ),
        session="shell_body",
        previewable=True,
        resets_selection=True,
    ),
    Command(
        name="boolean_body",
        returns=(
            Result("target_body_id", "string"),
            Result("mode", "string"),
            Result("removed_bodies", "string[]"),
            Result("changed_bodies", "string[]"),
        ),
        category="feature",
        description=(
            "ターゲットボディ(1つ)にツールボディ(1つ以上)を作用させ、結合(union)・"
            "切り取り(cut)・交差(intersect)するときに呼ぶ。ターゲットは `target_ref` で"
            "1枚の面から指す(選択には入れない)。ツールは選択から取る"
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
                "選択には入れない",
            ),
            Param(
                "mode",
                "string",
                "union(結合) / cut(切り取り) / intersect(交差)",
            ),
        ),
        session="boolean_body",
        previewable=True,
        resets_selection=True,
    ),
    Command(
        name="move_sketch_entities",
        returns=(
            Result("mode", "string"),
            Result("dx", "number | null"),
            Result("dy", "number | null"),
            Result("angle", "number | null"),
            Result("center", "number[] | null"),
            Result("copy", "boolean"),
            Result("created", "string[]"),
            Result("sketch_ids", "string[]"),
        ),
        category="sketch",
        description=(
            "スケッチの図形の位置・向きを直すときに呼ぶ。選択のスケッチの点"
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
        returns=(
            Result("mode", "string"),
            Result("dx", "number | null"),
            Result("dy", "number | null"),
            Result("dz", "number | null"),
            Result("angle", "number | null"),
            Result("axis_point", "number[] | null"),
            Result("axis_dir", "number[] | null"),
            Result("copy", "boolean"),
            Result("created", "string[]"),
            Result("changed_bodies", "string[]"),
        ),
        category="feature",
        description=(
            "ボディの位置・向きを直すときに呼ぶ。選択の面/辺/頂点が**属するボディ**を、"
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
            "選択を介さない参照で受けている)。"
            "**参照のまま送る**のは、辺が乗る直線を厳密に出せるのがコアだけだからで、"
            "まっすぐでない辺の拒否も `kernel.edge_axis` の既存の規則がそのまま効く"
        ),
    ),
    Command(
        name="mirror_sketch_entities",
        returns=(
            Result("axis_p0", "number[]"),
            Result("axis_dir", "number[]"),
            Result("created", "string[]"),
            Result("sketch_ids", "string[]"),
        ),
        category="sketch",
        description=(
            "選択のスケッチ図形を、既存の線分1本(axis)を軸に鏡映複製するときに呼ぶ。"
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
        returns=(
            Result("radius", "number"),
            Result("offset", "number"),
            Result("created", "string[]"),
            Result("sketch_ids", "string[]"),
        ),
        category="sketch",
        description=(
            "選択の複数の領域を、へこみに沿って包む**単一の閉じた領域線**を、同じスケッチへ足すときに呼ぶ。"
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
        returns=(
            Result("plane_point", "number[]"),
            Result("plane_normal", "number[]"),
            Result("created", "string[]"),
            Result("changed_bodies", "string[]"),
        ),
        category="feature",
        description=(
            "選択の面/辺/頂点が**属するボディ**を、鏡映面に対して鏡映複製するときに呼ぶ。"
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
    Command(
        name="delete",
        returns=(
            Result("bodies", "string[]"),
            Result("entities", "string[]"),
            Result("sketch_ids", "string[]"),
        ),
        category="feature",
        description=(
            "間違えて作った / 要らなくなったものを消すときに呼ぶ。選択の**ボディ**"
            "(面/辺/頂点/ボディのどれで指してもよい)と**スケッチの図形**を、混ざっていても"
            "1回でまとめて削除する(1回のundoで全部戻る)。対象は選択から取る"
            "(引数では受けない)。**領域(region)は削除できない** —— 領域は図形の配置から"
            "計算されるものなので、選択に混ざっているとエラーになる(その領域を作っている"
            "図形を選び直すこと)。スケッチを丸ごと消すのは delete_items。"
        ),
        session="delete",
        resets_selection=True,
        note=(
            "**`previewable=False`** —— 消える前の姿を見せる幽霊が作れない(プレビューは"
            "「増えた形」を配る仕組みで、`Session.preview` が返すのは形の変わったBodyだけ)。"
            "**howが無い**ので引数は1つも取らない —— 消し方に選択肢は無い"
        ),
    ),
    Command(
        name="delete_items",
        returns=(Result("bodies", "string[]"), Result("sketches", "string[]")),
        category="feature",
        description=(
            "ボディやスケッチを**IDで指して**丸ごと消すときに呼ぶ(スケッチは図形・反復ごと"
            "入れ物を消す)。選択は使わない(確定後は他の verb と同じく選択が空になる)。"
            "隠したボディも消せる。ボディとスケッチが混ざっていても"
            "1回でまとめて削除する(1回のundoで全部戻る)。スケッチを消しても、そこから"
            "押し出したボディは残る。面・辺や図形を選んで消すなら delete。"
        ),
        params=(
            Param("bodies", "array", "消すボディのIDの配列", required=False),
            Param("sketches", "array", "消すスケッチのIDの配列", required=False),
        ),
        session="delete_items",
        resets_selection=True,
        note=(
            "**`delete` と do / undo を共有する別 verb**(`duocad/verbs/delete.py` の docstring)。"
            "引数は対象を指す ID であって how ではない。`previewable=False` は `delete` と同じ理由。"
            "`resets_selection=True` —— 消したボディ・スケッチを指す選択が残るため"
        ),
    ),
    Command(
        name="rename",
        returns=(Result("target", "string"), Result("kind", "string"), Result("name", "string")),
        category="feature",
        description=(
            "ボディかスケッチの名前を変えるときに呼ぶ(ソリッド・メッシュ・スケッチのどれでも)。"
            "target は body_id か sketch_id。同じ名前が並んでもよい。空・前後の空白・改行は不可。"
            "名前は保存され、undo で戻る。"
        ),
        params=(
            Param("target", "string", "名前を変えるボディかスケッチのID"),
            Param("name", "string", "新しい名前"),
        ),
        session="rename",
        resets_selection=False,
        note=(
            "**ログに載せる**(保存はログだけなので、可視性のようにログの外に置くと開き直しで"
            "消える)。`resets_selection=False` —— 形も添字も変わらない"
        ),
    ),
    # 形状最適化の条件(第3のモード)。**4つとも集合代入**(「この役割の集合を、いま確定して
    # いる選択集合そのものに置き換える」)なので**解除専用のverbが無い** —— verbの中で全部
    # 外して決定すれば、その役割は空になる。実体と契約は `duocad/verbs/optimize_setup.py`、
    # 配りものの形は `duocad/optimize.py`。
    # `category="feature"` なのは、ソリッドに対する操作でWSの `FEATURE_COMMANDS` の捌き方
    # (引数を表から読んで `Session` のメソッドを呼ぶ)がそのまま当たるから。**モードの分け方は
    # 画面側の話**で、コアの分類を1つ増やすと、WSの表・`_dispatch` の分岐・UIの送信経路が
    # verbの中身と関係なく増える。
    Command(
        name="set_preserve_bodies",
        feature="shape_optimization",
        returns=(Result("bodies", "string[]"),),
        resets_selection=False,
        category="feature",
        description=(
            "形状最適化で**削ってはいけないソリッド**を決めるときに呼ぶ。"
            "選択のボディ(kind='body' の参照)**そのもの**を保持ソリッドの集合にする"
            "(足すのではなく置き換える。空の選択で確定すれば全部外れる)。"
            "既に除外ソリッドに割り当てられているボディがあると失敗する(先勝ち)。"
        ),
        session="set_preserve_bodies",
        note=(
            "対象は引数で受けない。"
            "**howも無い** —— 役割を付けるだけなので数値も選択肢も要らない"
        ),
    ),
    Command(
        name="set_exclude_bodies",
        feature="shape_optimization",
        returns=(Result("bodies", "string[]"),),
        resets_selection=False,
        category="feature",
        description=(
            "形状最適化で**材料を置いてはいけないソリッド**(逃げ・干渉物)を決めるときに呼ぶ。"
            "選択のボディ(kind='body' の参照)**そのもの**を除外ソリッドの集合にする"
            "(足すのではなく置き換える。空の選択で確定すれば全部外れる)。"
            "既に保持ソリッドに割り当てられているボディがあると失敗する(先勝ち)。"
        ),
        session="set_exclude_bodies",
        note="`set_preserve_bodies` と対。設計空間のverbは作らない",
    ),
    Command(
        name="set_fixtures",
        feature="shape_optimization",
        returns=(Result("face_count", "number"),),
        resets_selection=False,
        category="feature",
        description=(
            "形状最適化で**固定される面**(拘束)を決めるときに呼ぶ。選択の面**そのもの**を"
            "拘束面の集合にする(足すのではなく置き換える。空の選択で確定すれば全部外れる)。"
            "既に荷重面に割り当てられている面があると失敗する(先勝ち)。"
        ),
        session="set_fixtures",
        note=(
            "面の参照は添字だけでなく重心・法線・面積の**指紋**も一緒に操作ログへ書く"
            "(添字は形が変われば別の面を指すため。照合は `duocad/optimize.py`)"
        ),
    ),
    Command(
        name="set_loads",
        feature="shape_optimization",
        returns=(
            Result("face_count", "number"),
            Result("applied_count", "number"),
            Result("magnitude", "number"),
            Result("direction", "string"),
            Result("direction_vector", "number[] | null"),
        ),
        resets_selection=False,
        category="feature",
        description=(
            "形状最適化で**力がかかる面**(荷重)を決めるときに呼ぶ。選択の面**だけ**に"
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
        note=(
            "`magnitude` に `dimension` の印を付けてある —— **AIが勝手に決めてよい数ではない**。"
            "向きに世界軸の名前(`-Z` 等)を足さないのは、同じ向きの綴りが2系統になるため"
            "(ベクトルで表せる)"
        ),
    ),
    Command(
        name="clear_loads",
        feature="shape_optimization",
        returns=(Result("face_count", "number"),),
        resets_selection=False,
        category="feature",
        description=(
            "形状最適化で**荷重を外す**ときに呼ぶ。選択の面を荷重面から落とす"
            "(**面を1枚も選ばずに呼ぶと全部外れる**)。"
            "荷重が付いていない面が混ざっていても失敗しない。"
            "`set_loads` が上書き代入(選んだ面にだけ焼く)なので、外す操作はこちらにある。"
        ),
        session="clear_loads",
        note=(
            "**保持/除外/拘束の3つには解除専用verbが無い**(集合代入なので空で確定すれば外れる)。"
            "荷重だけ例外なのは、大きさ・向きを面ごとに持つために集合代入をやめたためである"
        ),
    ),
    Command(
        name="pattern_rectangular",
        returns=(Result("created", "string[]"),),
        category="feature",
        description=(
            "スケッチ上の図形を矩形に反復するときに呼ぶ。複製は独立した図形として作られ、"
            "作った図形IDの配列が返る(元の図形は残り、元を後から動かしても複製は付いてこない)。"
        ),
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
            "`entities` は図形IDの並びを引数で受ける(画面は共有選択の図形から作る)。"
            "**投機実行(`previewable`)には載せない**のは"
            "安全のためではなく、変わるのがスケッチの図形でソリッドではないため —— "
            "`preview` が返すのは形が変わったBodyだけなので、載せても常に空になる。"
            "画面のプレビューは**ブラウザだけで描く**(2026-09-05に実装) —— 増える複製の座標は"
            "コアと同じ規則で `web/src/entityKinds.ts` が出せる"
        ),
    ),
    Command(
        name="pattern_circular",
        returns=(Result("created", "string[]"),),
        category="feature",
        description=(
            "スケッチ上の図形を円形に反復するときに呼ぶ。複製は独立した図形として作られ、"
            "作った図形IDの配列が返る(元の図形は残り、元を後から動かしても複製は付いてこない)。"
        ),
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
    Command(
        name="import_svg",
        returns=(
            Result("sketch_id", "string"),
            Result("entity_ids", "string[]"),
            Result("source_name", "string"),
            Result("entity_count", "number"),
        ),
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
            "先に `sketch_create` で作ってから呼ぶ。**対象集合は選択から取らない** —— "
            "`sketch_id` を明示的に取る `add_line` と同じ形。"
            "取り込んだ曲線は `bezier` 種別で入るが、**`add_bezier` というverbは無い**"
            "(人もAIもベジエを1本ずつは足せない。2026-08-26本人裁定)。"
        ),
    ),
    Command(
        name="add_mesh_body",
        returns=(Result("body_id", "string"),),
        category="file",
        description=(
            "計算で作った三角形メッシュ(頂点＋三角形)をボディとして文書へ足すときに呼ぶ。"
            "**足したメッシュは編集できない** —— 表示 / STLへの書き出し / 削除 / "
            "表示の切り替え / 名前の変更(rename)だけができる。押し出し・フィレット・面取りは断られる。"
            "三角形をそのまま操作ログへ積むので、手で並べるための口ではない。"
        ),
        params=(
            Param("positions", "array", "頂点座標[mm]を x,y,z の順に平らに並べた配列"),
            Param("indices", "array", "三角形を作る頂点添字(**0始まり**)を3つずつ並べた配列"),
            Param("name", "string", "ボディの名前(省略時は通し番号の「ボディN」)", required=False, ws=False),
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
            "結果は `add_mesh_body` のメッシュボディで入る)。この verb を含む保存済み文書の再生と、"
            "`check_final_shape.py --solid-body` の検査対象を作るテストのために残す。人にもAIにも開けていない —— 引数はB-repの"
            "本文そのもので手で書くものではなく、CADを持ち込む口は `import_step` である。"
            "**それでもここに宣言するのは**、操作ログに載るverbとツールの顔ぶれを突き合わせる"
            "検査(`tests/test_mcp_verbs.py`)が、`mcp=False` かつ `ws=\"none\"` という宣言を"
            "「ツールを持たないverb」の唯一の出どころとして読むため。宣言を忘れるとそこで落ちる"
        ),
    ),
    Command(
        name="import_step",
        returns=(Result("body_ids", "string[]"),),
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
            "文書は変えないのでログには載らない。戻り値でパス・"
            "非表示で除外したボディ(skipped_hidden)を確認する。"
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
            "戻り値の skipped_hidden で非表示で除外したボディを確認できる。"
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
        name="close_document",
        category="file",
        description="",
        session="close_document",
        ws="special",
        mcp=False,
        note=(
            "**文書を閉じてトップ画面(何も開いていない状態)へ戻す**。`new_document` と同じく"
            "文書ごと差し替える(全接続へ配る)ので `_dispatch` で個別に扱う。閉じている間は"
            "文書を変える操作が `NoDocument` で断られる。**AIには開けていない**(`mcp=False`)"
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
            "**`models_dir` の中身を一覧にする**(名前/更新日時/作成日時/サムネイルの版)。"
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
        params=(Param("name", "string", "削除するファイル名(models/ 配下。編集中なら消してから閉じる)"),),
        session="delete_document",
        mcp=False,
        changes_document=False,
        note=(
            "**一覧の行からディスク上のファイルを削除する**(issue #17)。編集中のファイルなら"
            "消してから文書を閉じ(`close_document`)、応答に `closed: true` が載る。そのときは"
            "`net/ws.py` が状態も全接続へ配り直す。確認は画面側が行内の2段階で聞く。"
            "不可逆な操作なので人だけ(`mcp=False`)"
        ),
    ),
    Command(
        name="save_thumbnail",
        category="file",
        description="",
        params=(
            Param("name", "string", "サムネイルを付ける文書のファイル名(models/ 配下)"),
            Param("png", "string", "PNGのbase64。空文字なら消す"),
        ),
        session="save_thumbnail",
        mcp=False,
        changes_document=False,
        note=(
            "**一覧のサムネイルを預ける**。撮るのは画面(`web/src/thumbnail.ts`)で、サーバは"
            "`models/.thumbnails/` へ書くだけ。文書は変えない。画面の都合の口なので人の側だけ"
        ),
    ),
    Command(
        name="get_thumbnail",
        category="file",
        description="",
        params=(Param("name", "string", "文書のファイル名(models/ 配下)"),),
        session="get_thumbnail",
        mcp=False,
        changes_document=False,
        note=(
            "**一覧のサムネイルを1件返す**(base64)。`list_documents` は版(`thumb`)だけを"
            "載せ、画像は行が見えたときにこれで取る"
        ),
    ),
    Command(
        name="undo",
        returns=(Result("undone", "string"),),
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
        returns=(Result("redone", "string"),),
        category="history",
        description="undo した操作をやり直すときに呼ぶ。UIと同じ履歴。",
        session="redo",
        ws="special",
        resets_selection=True,
    ),
    # WebSocketだけの口。**verbではない**(文書を変えない)。下りコマンドの一覧を1箇所で読める
    # ようここへ置いてあるだけで、4つの表のどれにも載らない。
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
        name="inspect_selection",
        category="protocol",
        description="",
        params=(Param("request_id", "integer", "画面が古い返事を捨てるための通し番号(そのまま返す)"),),
        session="inspect_selection",
        ws="special",
        mcp=False,
        changes_document=False,
        note=(
            "**配信しない**(`get_edges` と同じ)。画面の選択情報の表示(issue #140)が選択・文書の変わるたびに呼ぶ。"
            "返事は `{type:'selection_info', request_id, item, error}` で、`item` は選択がちょうど1項目のときの"
            "`measure` の1項目(それ以外は null)。失敗は `error` に入れて返し、`{type:'error'}` にしない"
            "(裏の要求の失敗でトーストを出さず、人が待っている操作の待ちも解かない)"
        ),
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
    # 形状最適化を走らせる(protocol)。**verbではない**。文書へ積まれるのは `run_optimization` が
    # 中で呼ぶ `add_mesh_body` のログ1行だけで、この4つ(点検する / 見積もる / 走らせる / 止める)は
    # 操作ログに載らない(`changes_document=False`)。したがって分類は `protocol`。
    # **4つとも `mcp=False`**。数分かかる同期の呼び出しをMCPのツールにすると、進捗も中断も運べ
    # ないまま呼び出し側がタイムアウトする(WSは進捗イベントと `cancel_optimization` を持つ)。
    # AIから走らせるなら、まず**進捗と中断をMCPで運ぶ形**を決めること。
    # **`ws="special"`** —— 表(`ws="table"`)は「載せた引数は全部必須」で、ここは省略できる
    # 引数しか無い(既定＝いちばん粗い設定で走る)。
    Command(
        name="optimize_conditions",
        feature="shape_optimization",
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
        feature="shape_optimization",
        category="protocol",
        description="",
        params=(
            Param(
                "element_size_mm", "number",
                "要素サイズ[mm]。渡すとその値を選んだときの段を selected に、範囲外なら理由を selected_error に返す",
                required=False,
            ),
        ),
        session="optimize_resolution",
        ws="special",
        mcp=False,
        changes_document=False,
        note=(
            "**軽い**(OCCTは条件面のbboxを数枚測るだけ)。返る `steps` が**粗い側の端から"
            "細かい方向へだけ**動く目盛り。**時間は付けない**(走らせる前は"
            "実測が無い)。密度フィルタ半径`rmin_mm`は二値探索に無い概念で、"
            "届いたら `{\"error\": ...}` として断る(`OptimizeInputError` 経由)"
        ),
    ),
    Command(
        name="run_optimization",
        feature="shape_optimization",
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
        feature="shape_optimization",
        category="protocol",
        description="",
        session="cancel_optimization",
        ws="special",
        mcp=False,
        changes_document=False,
        note=(
            "**旗を立てるだけ**。実際に降りるのは次に進捗が出た時点で、"
            "進捗を出さない短い処理の途中では止まらない(`docs/contracts/ui-optimize-integration.md`「最適化の配線」)"
        ),
    ),
    Command(
        name="finish_optimization",
        feature="shape_optimization",
        category="protocol",
        description="",
        session="request_finish",
        ws="special",
        mcp=False,
        changes_document=False,
        note=(
            "「このステップで仕上げる」。走っている間は探索を止めて `step`(省くと最新)を仕上げるよう伝える(旗だけ)。"
            "走っていなければ直近の実行のステップを探索をやり直さずに仕上げる(`Session.finish_optimization_step`。重いので別タスク)。"
            "`docs/contracts/ui-optimize-integration.md`「ステップ」"
        ),
    ),
    Command(
        name="optimize_steps",
        feature="shape_optimization",
        category="protocol",
        description="",
        session="steps_for_current",
        ws="special",
        mcp=False,
        changes_document=False,
        note="いま開いている文書の直近の実行のステップ(探索の合格形)の一覧。要求した接続にだけ返す",
    ),
    Command(
        name="optimize_step_voxels",
        feature="shape_optimization",
        category="protocol",
        description="",
        session="steps_for_current",
        ws="special",
        mcp=False,
        changes_document=False,
        note="ステップ1つの形(表面のセルの通し添字)。要求した接続にだけ返す",
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
    Command(
        name="get_edge_chains",
        category="protocol",
        description="",
        params=(
            Param("body_id", "string", "連なりが要る辺のボディ"),
            # `edges` と綴らない: 対象集合の名前(`tests/test_ui_command_wiring.py` の `TARGET_SET_ARGS`)はソケットで読まない。
            # これは連なりを調べる読み取りで、verb の対象ではない。
            Param("chain_of", "array", "連なりが要る辺の添字(1始まり)の配列"),
        ),
        session="edge_chains",
        ws="special",
        mcp=False,
        changes_document=False,
        note=(
            "**配信しない**(`get_edges` と同じ)。フィレット・面取りで1本として振る舞う辺の組を、"
            "画面がホバー・クリックした辺の分だけ返す(全辺ぶんは大きな部品で 0.5 秒かかる)"
        ),
    ),
    # アプリ内AI(protocol)。**verbではなく、実体は `net/ws.py` の `Server`**(`session=None`)。
    # 応答は専用イベント(`ai_status` / `ai_tool_call` / `ai_result` / `ai_error`)で、AIが文書を変えた分は
    # 通常の状態配信として別途届く。**ここに宣言するのは、ソケットが受け取る名前を宣言で尽くすため**
    # (画面でできる操作は全部コマンド。issue #153)。AIがAIの設定を触る意味は無いので `mcp=False`。
    Command(
        name="ai_get_status",
        category="protocol",
        ws="special",
        mcp=False,
        changes_document=False,
        note="プロバイダ・モデル・思考の設定・キーの有無を `ai_status` で要求した接続にだけ返す",
    ),
    Command(
        name="ai_configure",
        category="protocol",
        params=(
            Param("provider", "string", "`Server.ai_providers` の綴り(`openai_compat` / `anthropic`)"),
            Param("model", "string", "モデル名(省略で既定)", required=False),
            Param("base_url", "string", "llama-server の URL(`openai_compat` だけ。省略で既定)", required=False),
        ),
        ws="special",
        mcp=False,
        changes_document=False,
        note="結果の `ai_status` は全接続へ配る(設定はサーバに1つ)",
    ),
    Command(
        name="ai_set_thinking",
        category="protocol",
        params=(Param("thinking", "string", "思考の設定(プロバイダごとの綴り。`Server._ai_set_thinking`)"),),
        ws="special",
        mcp=False,
        changes_document=False,
    ),
    Command(
        name="ai_set_api_key",
        category="protocol",
        params=(Param("key", ["string", "null"], "Claude API のキー(null で保存済みのキーを消す)。値は画面へ戻さず `duocad_secrets.json` へ書く"),),
        ws="special",
        mcp=False,
        changes_document=False,
    ),
    Command(
        name="ai_prepare",
        category="protocol",
        ws="special",
        mcp=False,
        changes_document=False,
        note="ローカルLLMのプロンプトキャッシュを温める(応答は無い)",
    ),
    Command(
        name="ai_send",
        category="protocol",
        params=(Param("message", "string", "人の依頼の本文"),),
        ws="special",
        mcp=False,
        changes_document=False,
        note="実行器スレッドで `AgentLoop.run()` を走らせる。進みは `ai_tool_call` / `ai_result` / `ai_error`",
    ),
    Command(
        name="ai_cancel",
        category="protocol",
        ws="special",
        mcp=False,
        changes_document=False,
    ),
    # 計算の設定(protocol)。FEM の装置(CPU/GPU)。**verbではなく実体は `Server`**。CuPy を読む(初回1秒前後)ので実行器で作る。
    Command(
        name="fem_get_status",
        category="protocol",
        ws="special",
        mcp=False,
        changes_document=False,
        note="`fem_status` を要求した接続にだけ返す",
    ),
    Command(
        name="fem_set_device",
        category="protocol",
        params=(Param("device", "string", "`cpu` / `gpu`"),),
        ws="special",
        mcp=False,
        changes_document=False,
        note="結果の `fem_status` は全接続へ配る(設定はサーバに1つ)",
    ),
    # 作業空間(workspace)。**起動中のコマンドとモードを共有する口**(issue #153。`duocad/workspace.py`)。選択と同じく
    # MCP にも WS にも同じ顔で出す(`get_command` は読むだけなので WS には無い: 画面は状態配信で受け取る)。
    # verb ではなく、操作ログに載らず undo でも戻らない。応答は `change: "command"` / `change: "mode"` のイベント。
    # 人(画面のツールバー・Esc・Enter・スケッチへの出入り)も AI も同じ操作で触り、出所は持たない。
    Command(
        name="get_command",
        category="workspace",
        description=(
            "起動中のコマンド(verb・途中の引数)とモードを読むときに呼ぶ。人が画面で verb を起動して引数を打っている最中か、"
            "AI が begin_command で構えたものを人が直したかを確かめる。"
        ),
        ws="none",
        mcp=True,
        changes_document=False,
    ),
    Command(
        name="begin_command",
        category="workspace",
        description=(
            "verb を起動して、対象(選択)と引数を人と一緒に集め始めるときに呼ぶ(画面のツールバーのクリックと同じ)。"
            "人の画面にその verb が起動して対象の候補が見える。半径などの引数と確定を人に残して止まるならここで止まる。"
            "別の verb が起動中なら置き換え、そのとき選択は空になる。"
        ),
        params=(
            Param("verb", "string", "起動する verb の名前(スケッチかフィーチャの verb。例: fillet / extrude / add_rectangle)"),
            Param("args", "object", "最初から入れておく引数(その verb のツールと同じ名前。省略可)", required=False),
        ),
        session="begin_command",
        ws="special",
        mcp=True,
    ),
    Command(
        name="set_command_args",
        category="workspace",
        description=(
            "起動中の verb の引数を足す・直すときに呼ぶ(人が入力欄に打つのと同じ)。渡したキーだけ変わり、null で外す。"
            "必須が揃っていなくてよい。起動中のコマンドが無ければ失敗する。"
        ),
        params=(Param("args", "object", "その verb のツールと同じ名前の引数。null の値はそのキーを外す"),),
        session="set_command_args",
        ws="special",
        mcp=True,
    ),
    Command(
        name="end_command",
        category="workspace",
        description="起動中の verb を確定せずに畳むときに呼ぶ(人の Esc と同じ)。選択も空になる。",
        session="end_command",
        ws="special",
        mcp=True,
    ),
    Command(
        name="apply_command",
        category="workspace",
        description=(
            "起動中の verb を今の選択と引数で確定するときに呼ぶ(人の Enter と同じ)。成功すると起動中のコマンドは無くなり、"
            "操作ログに1行積まれる。必須の引数が無い・選択が足りないなら失敗し、コマンドは起動したまま残る。"
        ),
        params=(Param("args", "object", "確定の直前に併合する引数(省略可)", required=False),),
        session="apply_command",
        ws="special",
        mcp=True,
        note=(
            "応答は構えていた verb の封筒そのもの(`change` のイベントではない)。`tests/test_protocol.py` の "
            "`_change_commands` はこの名前を除く"
        ),
    ),
    Command(
        name="enter_sketch",
        category="workspace",
        description=(
            "既存のスケッチを編集するモードに入るときに呼ぶ(人の画面もそのスケッチに入る)。"
            "スケッチを作った直後は呼ばなくてよい(作った人が誰でも入る)。起動中のコマンドは畳まれる。"
        ),
        params=(Param("sketch_id", "string", "describe_document が返した sketch_id"),),
        session="enter_sketch",
        ws="special",
        mcp=True,
    ),
    Command(
        name="leave_sketch",
        category="workspace",
        description="スケッチの編集を終えて通常のモードへ戻るときに呼ぶ。起動中のコマンドは畳まれる。",
        session="leave_sketch",
        ws="special",
        mcp=True,
    ),
    Command(
        name="enter_optimize",
        category="workspace",
        feature="shape_optimization",
        description="形状最適化のモードに入るときに呼ぶ(条件の verb はモードに関わらず使える。画面の出し分けだけが変わる)。",
        session="enter_optimize",
        ws="special",
        mcp=True,
    ),
    Command(
        name="leave_optimize",
        category="workspace",
        feature="shape_optimization",
        description="形状最適化のモードから通常のモードへ戻るときに呼ぶ。",
        session="leave_optimize",
        ws="special",
        mcp=True,
    ),
)

#: verb ではない変更イベント(操作ログに載らない)。`change` の値 -> 載せる値。
#: `Session` が `_emit` で配り、WebSocket は要求した画面へ `applied` として返す。
#:
#: - `selection`  … `set_selection` / `modify_selection` / `toggle_selection` / `clear_selection`
#: - `visibility` … `set_visible`(`body_id`)/ `set_sketch_visible`(`sketch_id`)。どちらか片方が載る
#: - `document`   … `open_document` / `new_document` / `close_document`(新規と閉じるは開いたファイルが無いので `path` / `name` が null)
#: - `command`    … `begin_command` / `set_command_args` / `end_command`(起動中のコマンド。null は無起動)
#: - `mode`       … `enter_sketch` / `leave_sketch` / `enter_optimize` / `leave_optimize`
CHANGE_EVENTS: dict = {
    "selection": (Result("selection", "SelectionState"),),
    "visibility": (
        Result("body_id?", "string"),
        Result("sketch_id?", "string"),
        Result("visible", "boolean"),
        Result("previous", "boolean"),
        Result("changed", "boolean"),
    ),
    "document": (
        Result("action", "string"),
        Result("path", "string | null"),
        Result("name", "string | null"),
    ),
    "command": (Result("command", "CommandState | null"),),
    "mode": (Result("mode", "ModeState"),),
}

#: 状態配信(`Session.state_meta()`)が持つキー。`web/src/session.ts` の `ServerState` と
#: 一致することを型検査が見る(`web/src/generated/protocol.ts` の `ServerStateKey`)。
STATE_KEYS: tuple[str, ...] = (
    "bodies",
    "sketches",
    "parameters",
    "selection",
    "optimize",
    "log",
    "applied_count",
    "redo_count",
    "can_undo",
    "can_redo",
    "active_body_id",
    "units",
    "current_name",
    "saved",
    "document_open",
    # 作業空間(`duocad/workspace.py`)。起動中のコマンドとモード
    "command",
    "mode",
    # 文書の世代(`Session.generation`。開く・新規・閉じるで進む)。画面が「同じ文書のままか」を見る(スケッチの戻り先の記憶を捨てる判定)
    "generation",
)

BY_NAME: dict = {command.name: command for command in COMMANDS}

if len(BY_NAME) != len(COMMANDS):
    raise RuntimeError("COMMANDS に同じ名前が2つある")


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


def websocket_command_names() -> set:
    return {command.name for command in COMMANDS if command.ws != "none"}


def tool_descriptors(hidden: frozenset = frozenset()) -> list:
    """MCPの道具の定義。`hidden` の機能(`Command.feature`)に属するものは除く。"""
    return [command.descriptor() for command in COMMANDS if command.mcp and command.feature not in hidden]


def tool_names(hidden: frozenset = frozenset()) -> tuple:
    return tuple(command.name for command in COMMANDS if command.mcp and command.feature not in hidden)


def feature_command_names(hidden: frozenset) -> frozenset:
    """`hidden` の機能に属する宣言の名前(MCPの道具とWSの口の両方)。"""
    return frozenset(command.name for command in COMMANDS if command.feature is not None and command.feature in hidden)


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


def agent_instructions(hidden: frozenset = frozenset()) -> str:
    """**道具の使い方をAIへ渡す唯一の文**(MCPの `instructions` とアプリ内エージェントの
    システムプロンプトは同じものを読む)。

    AIから文書を触る道は2本ある —— Claude Codeなどが起動するMCPサーバ(`duocad/mcp_server.py`)と、
    アプリ内から呼ぶエージェント(`duocad/agent/`)。**手順は同じ**なので、2箇所に書くと
    片方だけ直った状態に気づけない。ツール数も宣言から数える —— 手で書いた「29」は
    verbを足した日に古くなる。

    **どこまで自分でやるかの規則は置かない**(本人の決定):
    「選択だけにするか verb までするかは人間からの指示から AI がその都度判断する」。
    固定の規則(曖昧なら止める等)を足すと、依頼の文面から読める判断を機械的な線引きが
    塗りつぶす。選択だけで止めたときに何を選んだかを述べることだけを求める。
    人は画面で選択を見て、そのまま採用するか、AIへ直しを頼むか、自分でトグルして直す。

    **この文を書き換える人へ**: 「必ず止める」「必ず進める」の類を足さないこと。
    `tests/test_agent_loop.py` が代表的な言い回しは落とすが、散文の矛盾を文字列検査で
    全部は捕まえられない —— 最後の防波堤はこの注意書きである。
    """
    return (
        f"duocad(自作3DCAD)の開いている文書を操作する。公開ツールは{len(tool_names(hidden))}個。"
        "面・辺・領域などの形は引数で受けず、選択から取る"
        "（数値・名前・パスだけが引数）。"
        "手順: describe_document で概要を知る → "
        "set_selection(predicate または refs) → "
        "fillet / extrude などの verb。"
        "選択は人もAIも同じ操作(set_selection / modify_selection / toggle_selection / "
        "clear_selection)で触り、そのまま verb に渡せる。"
        "選択だけで止めるか verb まで進めるかは、人の指示の文面からその都度判断する。"
        "選択だけで止めたときは、何を選んだかを述べる"
        "（人は画面で見て、そのまま採用するか、直すよう頼むか、自分でトグルして直す）。"
        "人と一緒に1つの verb を進めるには、begin_command(verb) で起動して対象を選び、半径などの引数と確定を人に残して"
        "止まる（人の画面にその verb が起動し、選んだ対象が見える）。人が画面で起動した verb と途中の引数は "
        "describe_document / get_command の command に見える。その verb の引数は set_command_args で足し、確定は "
        "apply_command で行う（確定まで進めるかも人の指示の文面から判断する）。人が打っている途中の引数を上書きしない。"
        "人は画面に見えている面・辺・頂点・スケッチの点・線・領域をクリックで選べ、選択は種類を問わず画面に強調して出る"
        "(verb の起動中は、人が選べるのはその verb が受け付ける種類だけ)。人は立体の裏に隠れたものを指せないが、AI は参照で選べる。"
        "非表示のボディ・非表示のスケッチは選べず、メッシュのボディは面・辺・頂点を指せない(ボディ丸ごとなら指せる)。"
        "enter_sketch / leave_sketch で人の画面のモードが切り替わる（スケッチを作ると誰が作ってもそこへ入るので、"
        "描き終えて押し出したら leave_sketch で通常へ戻す）。"
        "形を変える verb が確定すると選択は空になるので、次の対象は選び直す。"
        "寸法の差が0.01mm以内、体積や面積の差が0.1%以内なら指示どおりにできているとみなし、"
        "それ以上の微調整や作り直しはしないこと。"
        "作業の報告は1文で足りる。"
        "set_visible の結果は人間が開いているブラウザの画面にも即座に反映される"
        "(undo では戻らないので、戻すには visible=true で呼び直す)。"
    )
