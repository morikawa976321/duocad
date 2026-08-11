"""MCPサーバ — AIがコアを直接叩くための口(M4の最小の先取り・2026-08-11)。

**目的**: AIが「前面パネルを隠して」と言えるようにする。見えない面は選べない以上、
**可視性は選択機構の一部**であり(`duocad/selection.py`)、大規模モデルで交互操作を
成立させる前提になる。

**【2026-08-11 F1で拡張】** M4は下記の3つだけを公開し、選択の提案・修正
(`propose_selection` 等)を範囲外にしていた。**F(F1+F2)がその線を引き直す** ——
実装計画書§0の到達表のAI列(19操作)を埋めるため、公開ツールは**28個**になり、
選択5つと述語の自己記述もここから出る。契約は
`tests/test_mcp_verbs.py` / `tests/test_mcp_selection.py` と `tests/README.md` の「F」節。
F1が置いたスタブは `DocumentTools` の末尾にある(実体は F2)。

M4で公開した3つ(振る舞いの記述は現役):

    describe_document(include_references=False, include_bbox=False)
        ボディの一覧(id・名前・種別・可視性・面数・体積)とパラメータの一覧を返す。
        **`set_visible` だけを公開してもAIはどのBodyが存在するか知らないので使えない**ため、
        この2つは必ず同時に出す。
    set_visible(body_id, visible)
        コアの verb `set_visible` を経由して可視性を切り替える(新規ロジックを書かない)。
    describe_shape(body_id)
        面・辺の数と種別内訳、体積、境界箱(読み取りのみ)。

**層の位置づけ(ここを誤ると設計が崩れる)**: MCPサーバは**アプリ層ではなく、コアのもう一人の
クライアント**である。ブラウザ(web/)とMCPは同じ `Session`(=コア側の文書)を叩く対等な
クライアントで、MCPからブラウザを操作したりブラウザの状態を読んだりしない。したがって
`set_visible` はアプリ層(ブラウザ)を一切通らずに文書を変え、操作ログに載り、undoできる
(実装計画書§2「コアはUIが無くても機能的に完全」の実証)。

**上り方向(コアが変更を発火し、アプリ層が購読して描き直す)**: これが動かないと交互操作は
原理的に成立しない(§2「外してはいけない3つ」の2番目)。実体は `Session.add_listener` で、
`Server`(WebSocket)が購読して全接続へ配信する。MCPが可視性を変えると、**開いている
ブラウザの表示が自分では何もしていないのに変わる**。

**なぜ同じプロセスで両方(MCP・WebSocket)を動かすのか**: 別プロセスにすると、MCP側は
自分の `Document` を持ってしまい「AIが変えた文書」と「人が見ている文書」が別物になる。
文書は1つでなければならないので、`python -m duocad.mcp_server` が

    stdio(MCP) ─┐
                ├→ Session(文書・操作ログ) → WebSocket → ブラウザ
    ブラウザ ───┘

の全部を持つ。MCPクライアント(Claude Code 等)がこのプロセスを起動し、人間はそれが配信する
`http://127.0.0.1:8766/` を開く。接続方法は `MCP.md` が正。

**依存を増やさない(2026-08-11の判断)**: MCPの公式Python SDK(`mcp`)は入れず、
プロトコル(JSON-RPC 2.0 over stdio)を素で実装する。理由は3つ:
(1) この venv には `pip` すら無く(uv管理)、`mcp` は pydantic/anyio/httpx/starlette/uvicorn を
    連れてくる。ローカル専用ツールの依存としては重すぎる(pyproject は依存を4つに固定している)。
(2) 使うのは `initialize` / `tools/list` / `tools/call` の3メソッドだけで、SDKが吸収してくれる
    複雑さ(サンプリング・購読・複数トランスポート)を1つも使わない。
(3) 素で書けば**プロトコルの契約がテストできる**(`tests/test_mcp.py`)。SDKだとSDKを
    テストすることになる。

**stdoutに出してよいのはJSON-RPCの行だけ**(1メッセージ1行・改行区切り)。起動メッセージや
警告は必ず stderr へ出す — stdoutを1文字でも汚すとMCPクライアントが接続に失敗する。

**既知の割り切り(2026-08-11のQAで指摘・意図的に据え置き)**:

- `notifications/initialized` の受信を `tools/*` の前提条件に**しない**。仕様上クライアントは
  握手を済ませてから呼ぶが、そこを厳格に弾いても得るものが無く(拒否できるのは行儀の悪い
  クライアントだけ)、逆に握手の順序が違う実装で**繋がらない**という失敗を作る。
- ツールの実行はイベントループの中で同期に走る(重い `describe_*` の間はWebSocketの配信も
  止まる)。これは既存の `Session.extrude` が同じ性質を持つのと揃えたもので、
  実行器スレッドへ逃がすと `Server` 側のスレッド前提(`_dispatching` の印・ループの参照)を
  作り直すことになる。**M4本体で交互操作を作るときに、両方まとめて再訪する**。

**OCPを直接importしない**(境界は kernel.py だけ。tests/test_boundaries.py が検査する)。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import webbrowser
from pathlib import Path
from typing import Any, Callable, Optional

from websockets.asyncio.server import serve

from . import kernel, query
from .server import (
    DEFAULT_MEASURE_LOG,
    DEFAULT_STEP,
    DEFAULT_WEB_DIST,
    Server,
    Session,
    start_http_server,
)

#: MCPクライアントへ名乗る名前と版(`serverInfo`)
SERVER_NAME = "duocad"
SERVER_VERSION = "0.0.1"

#: 対応するMCPプロトコル版。**新しい順**に並べる(先頭が既定)。
#: クライアントが要求した版がこの中にあればそれをそのまま返し、無ければ先頭を返す
#: (仕様どおり。クライアントは返ってきた版を見て続けるか切るかを決める)。
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

JSONRPC_VERSION = "2.0"

# JSON-RPC 2.0 の標準エラーコード
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


# --------------------------------------------------------------------------------------
# ツール(コアへの入口)
# --------------------------------------------------------------------------------------
class DocumentTools:
    """`Session`(コア)を叩くツール群。**ここに新しい幾何ロジックを書かない**。

    M4では3つ(`describe_document` / `set_visible` / `describe_shape`)だった。
    **F1(2026-08-11)で契約を28個へ広げた** —— 実装計画書§0の到達表のAI列を埋めるため。
    増えるぶんは下の「F1で契約を確定した分」にスタブとして置いてあり、実体は F2 が入れる。

    どのツールも戻り値は**JSON化できる素の値だけ**(MCPのレスポンスに載るため)。
    失敗は例外のまま上げる — 呼び出し側(`McpServer`)が `isError` の結果へ変換して
    AIに見せる(黙って空の成功を返さない。実装計画書§6予防6)。
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------ 記述
    def descriptors(self) -> list[dict]:
        """`tools/list` が返すツール定義(名前・説明・入力スキーマ)。

        説明文はAIがツールを選ぶ唯一の手がかりなので、**何ができるかではなく
        「いつ使うか」**を書く。
        """
        return [
            {
                "name": "describe_document",
                "description": (
                    "開いている文書のボディ一覧(body_id・名前・種別・可視性・面数・体積)と"
                    "パラメータ一覧を返す。set_visible や describe_shape を呼ぶ前に、"
                    "どのボディが存在するかを知るために最初に呼ぶ。"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "include_references": {
                            "type": "boolean",
                            "description": (
                                "読み込んだSTEPの参照ボディ(kind='reference'・編集対象外で"
                                "既定では隠されている)も含めるか。既定 false"
                            ),
                        },
                        "include_bbox": {
                            "type": "boolean",
                            "description": (
                                "各ボディの境界箱(min/max/center/size)を含めるか。"
                                "どの部品が手前・奥にあるかを知りたいときに true。既定 false"
                            ),
                        },
                    },
                    "additionalProperties": False,
                },
            },
            {
                "name": "set_visible",
                "description": (
                    "ボディの表示・非表示を切り替える。手前の部品を隠すと奥の面が選べるようになる"
                    "(可視性は選択機構の一部)。操作ログに載るので undo で戻せる。"
                    "開いているブラウザの表示も即座に変わる。"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "body_id": {
                            "type": "string",
                            "description": "describe_document が返した body_id",
                        },
                        "visible": {
                            "type": "boolean",
                            "description": "true で表示、false で非表示",
                        },
                    },
                    "required": ["body_id", "visible"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "describe_shape",
                "description": (
                    "1つのボディの形状を調べる(面数・辺数・面の種別内訳・体積・境界箱)。"
                    "形は変えない読み取り専用。"
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "body_id": {
                            "type": "string",
                            "description": "describe_document が返した body_id",
                        },
                    },
                    "required": ["body_id"],
                    "additionalProperties": False,
                },
            },
        ]

    def call(self, name: str, arguments: dict) -> dict:
        """ツールを実行して結果の辞書を返す。未知の名前は KeyError。"""
        handlers: dict[str, Callable[..., dict]] = {
            "describe_document": self.describe_document,
            "set_visible": self.set_visible,
            "describe_shape": self.describe_shape,
        }
        try:
            handler = handlers[name]
        except KeyError:
            raise KeyError(f"unknown tool: {name!r} (available: {sorted(handlers)})") from None
        return handler(**arguments)

    # ------------------------------------------------------------------ 実体
    def describe_document(
        self, include_references: bool = False, include_bbox: bool = False
    ) -> dict:
        """ボディ一覧とパラメータ一覧。

        **既定では参照ボディ(kind="reference")を出さない**。STEPを読むとソリッドごとに
        「参照(読み取り専用・隠されている)」と「編集用コピー」の2つができる
        (`Session.load_step`)ので、そのまま全部見せると同じ部品が2回並び、AIが
        隠しても画面が変わらない方(参照)を掴む。**編集対象だけを既定にする**のが
        AIから見た文書の姿として正しい。件数は `reference_count` で必ず知らせるので、
        隠していることは分かる(黙って落とさない)。
        """
        document = self.session.document
        bodies = []
        reference_count = 0
        for body in document.bodies:
            if body.kind == "reference":
                reference_count += 1
                if not include_references:
                    continue
            entry = {
                "body_id": body.id,
                "name": body.name,
                "kind": body.kind,
                "visible": body.visible,
                "face_count": kernel.face_count(body.shape),
                "volume": query.volume(body.shape),
            }
            if include_bbox:
                entry["bbox"] = kernel.bounding_box(body.shape)
            bodies.append(entry)

        active = document.active_body()
        return {
            "units": document.units,
            "active_body_id": active.id if active is not None else None,
            "body_count": len(bodies),
            "visible_count": sum(1 for body in bodies if body["visible"]),
            "reference_count": reference_count,
            "reference_included": bool(include_references),
            "bodies": bodies,
            "parameters": dict(document.parameters),
            # **`Session.can_undo` を使う**(生の `log.can_undo` ではない)。文書の土台
            # (部品の読み込み・編集用ボディ)は取り消せないので、素のログを見ると
            # 「undoできる」と答えたのに実際は拒否される、という食い違いが出る。
            "can_undo": self.session.can_undo,
            "can_redo": document.log.can_redo,
            "applied_count": len(document.log.entries),
        }

    def set_visible(self, body_id: str, visible: bool) -> dict:
        """可視性を切り替える。**コアの verb を経由する**(`Session.set_visible`)。

        `Session` はアプリ層ではなくコア側の文書の持ち主で、ブラウザとMCPはその対等な
        クライアントである(モジュールdocstring)。ここを通ることで
        (a) 操作ログに1行積まれ (b) undoで戻り (c) 変更イベントが発火して
        開いているブラウザが描き直す、の3つが同時に成立する。

        存在しない `body_id` は KeyError(verbの契約。文書もログも変更しない)。
        """
        body_id = str(body_id)
        if not isinstance(visible, bool):
            raise ValueError(f"visible must be a boolean, got {type(visible).__name__}")
        before = self._find_body(body_id)
        if before.kind == "reference":
            # **参照ボディの可視性は触らせない**(2026-08-11のQAで指摘)。参照は読み込んだ
            # STEPの原本で、編集用コピーを作った直後に隠してある(`Session.load_step`)。
            # ここを表示に戻すと**同じソリッドが二重に描かれ**、M3.5が「1ソリッドにつき
            # 同時に見えるのは1つ」として塞いだ状態がAI経路だけで復活する。
            # `include_references=True` は**見せる**ためのもので、触ってよいという意味ではない。
            raise ValueError(
                f"body {body_id!r} is a reference body: 参照ボディは読み取り専用の原本なので"
                "表示を切り替えられない(編集用コピーの body_id を使うこと)"
            )
        previous = before.visible
        applied = self.session.set_visible(body_id, visible)
        return {
            "body_id": body_id,
            "name": before.name,
            "visible": before.visible,
            "previous": previous,
            "changed": previous != before.visible,
            "verb": applied["verb"],
            "seq": applied["seq"],
            "can_undo": self.session.document.log.can_undo,
            "visible_count": sum(
                1
                for body in self.session.document.bodies
                if body.kind != "reference" and body.visible
            ),
        }

    def describe_shape(self, body_id: str) -> dict:
        """1ボディの形状(面・辺の数と種別内訳・体積・境界箱)。読み取りのみ。"""
        body = self._find_body(str(body_id))
        described = query.describe_shape(body.shape)
        return {
            "body_id": body.id,
            "name": body.name,
            "kind": body.kind,
            "visible": body.visible,
            "face_count": described["face_count"],
            "edge_count": described["edge_count"],
            "faces_by_type": described["faces_by_type"],
            "volume": query.volume(body.shape),
            "bbox": kernel.bounding_box(body.shape),
        }

    def _find_body(self, body_id: str):
        body = next((b for b in self.session.document.bodies if b.id == body_id), None)
        if body is None:
            known = [b.id for b in self.session.document.bodies if b.kind != "reference"]
            raise KeyError(f"no such body: {body_id!r} (editable bodies: {known})")
        return body

    # ==================================================================================
    # F1(2026-08-11)で契約を確定した分 —— **すべてスタブ。実装は F2**
    # ==================================================================================
    # 契約の正は `tests/test_mcp_verbs.py` / `tests/test_mcp_selection.py` と
    # `tests/README.md` の「F」節。ここには**実装するときに読む順で要点だけ**書く。
    #
    # ## 貫いている3つの規則
    #
    # 1. **形は選択から、数値は引数から。** 面・辺・領域を引数で受けるツールは1つも無い
    #    (`fillet(radius)` であって `fillet(edges, radius)` ではない)。§5の
    #    `apply(verb, selection, args)` をverbごとに分解した形で、これにより A1 の
    #    「提案のままでは verb に渡せない」が**全verbへ自動的にかかる**。
    #    Bodyだけは `body_id` 引数で受ける —— Bodyは選択集合の項目ではない。
    # 2. **成功は封筒に入れて返す。** ログに1行積むツールは
    #    `verb` / `seq` / `changed_bodies` / `bodies` / `can_undo` / `can_redo` を必ず持つ。
    #    **`changed_bodies` は `Session.body_revision` の呼び出し前後の差**から導くこと。
    #    verbごとに「これはbodyを変える」と数え上げると、verbが増えたときに静かに漏れる
    #    (版が「経路ではなく状態」から導かれているのはこのためである。`Session.body_revision`)。
    # 3. **失敗も封筒に入れて返す。** `{"error": {"code","message","tool","exception", ...}}`
    #    を `json.dumps` した本文 + `isError: true`。コードは6語彙
    #    (`not_found` / `invalid_argument` / `selection_not_committed` / `selection_empty` /
    #    `invalid_predicate` / `internal`)。**例外のテキストを素で返さない。**
    #    型→コードの対応は `KeyError`/`FileNotFoundError` → `not_found`、
    #    `ValueError` → `invalid_argument`、それ以外 → `internal`。**`FileNotFoundError` を
    #    忘れない** —— パスの間違いは AI が直せる誤りなので、`internal` に落とすと諦める。
    #
    # ## stdout を汚さない(F1で実測した落とし穴)
    #
    # **OCCTのSTEP書き出しはC++側から標準出力へ統計を吐く**("Statistics on Transfer (Write)")。
    # stdoutはJSON-RPC専用(モジュールdocstring)なので、これが混ざると接続が壊れる。
    # Python層の `contextlib.redirect_stdout` では止まらない(C++が実fdへ直接書く)ため、
    # **ツール実行の間だけ fd 1 を fd 2 へ振り替える**こと(`os.dup2`)。
    # 検査は `tests/test_mcp_verbs.py::test_exporting_does_not_corrupt_the_stdio_channel`
    # (**実プロセスを起こさないと原理的に検出できない**)。
    #
    # ## 拒否はコアがする。MCPは名前を付けるだけ
    #
    # `selection_not_committed` を返すために `origin` を**先に見て弾いてはいけない**。
    # 拒否するのは `selection.confirmed_items`(A1が「区別を実際に効かせている唯一の場所」と
    # 定めた場所)だけであり、MCPは**落ちたあとに** `get_selection()` を見てコードを選ぶ。
    # 先に弾くと同じ規則が2箇所に生まれ、片方だけ緩んだときに誰も気づかない。
    #
    # ## `descriptors()` はまだ書かない(F1の意図)
    #
    # ツールの説明文と入力スキーマは**F2の成果物**である。F1は「何が公開されているべきか」を
    # テストで固定するところまでを持つ。したがってF1完了時点では `tools/list` は3つのままで、
    # 新しいツールの呼び出しは「未知のツール」として落ちる —— これが赤の主因である。

    #: F2が `descriptors()` と `call()` へ**足す**ツール名(25個)。M4の既存3つと合わせて
    #: **合計28個**になる。**実装の目印**であって契約ではない
    #: (契約は `tests/test_mcp_verbs.py` の `EXPECTED_TOOLS`)。
    F2_TOOLS = (
        "describe_predicates", "measure",
        "get_selection", "propose_selection", "modify_selection",
        "commit_selection", "clear_selection",
        "sketch_create", "add_line", "add_circle", "add_rectangle", "add_arc",
        "extrude", "extrude_face", "fillet", "chamfer",
        "pattern_rectangular", "pattern_circular",
        "make_editable_copy", "set_parameter",
        "import_step", "export_step", "export_stl",
        "undo", "redo",
    )

    def _not_implemented(self, name: str) -> dict:
        raise NotImplementedError(f"{name}: F2で実装する(スタブ)")

    # ------------------------------------------------------------------ 選択(5 + 自己記述)
    def get_selection(self) -> dict:
        """現在の選択を返す。**発火しない**(読むだけ)。

        戻り値は `{"selection": {"items", "origin", "count"}}`。**変更する4つと同じ形**に
        揃える —— `Session` 側は変更4メソッドがイベント、getが `to_dict()` と形が違うが、
        それはコアの都合(イベントの形を1箇所で作る)であってAIに見せる理由が無い。
        """
        return self._not_implemented("get_selection")

    def propose_selection(self, refs: Optional[list] = None, predicate: Optional[dict] = None) -> dict:
        """**AIが書き込む経路**。`refs`(明示的な参照)か `predicate`(述語)のどちらか一方。

        §0のAI側の合格条件「引数を**述語**または明示的な参照で与えられ」がこの1つに載る。
        - 一致0件は**成功**(`count: 0`)。エラーにするとAIが述語の書式を疑い始める
        - 不正な述語は `invalid_predicate` で、**`valid_ops` に
          `predicates.describe_ops()` をそのまま添える**(AIが自力で直せるように)
        - 不正な参照は `invalid_argument`(形)/ `not_found`(Body)。いずれも選択を変えない
        """
        return self._not_implemented("propose_selection")

    def modify_selection(self, add: Optional[list] = None, remove: Optional[list] = None) -> dict:
        """**人が直す経路**(差分編集)。`origin` は "user" になる —— 人が触ること自体が承認。"""
        return self._not_implemented("modify_selection")

    def commit_selection(self) -> dict:
        """提案を確定する(中身は変えない)。AIが自分の提案を実行する前に踏む明示の1手。"""
        return self._not_implemented("commit_selection")

    def clear_selection(self) -> dict:
        """選択を空にする。"""
        return self._not_implemented("clear_selection")

    def describe_predicates(self) -> dict:
        """述語の語彙をAIへ自己記述する。**`predicates.describe_ops()` をそのまま出す**。

        戻り値: `{"kinds": [...], "ops": <describe_ops()>, "where_is": "and", "example": {...}}`。
        opの定義を書き直すと、スキーマとバリデータが二重管理になり、**片方だけ増えたopが
        黙って通る**(`predicates.describe_ops` の docstring がこれを禁じている)。
        `where_is: "and"` と **ORが無いこと**まで出すのは、AIが無い機能を試して失敗するのを
        防ぐため(ORが要るときは2回評価して `modify_selection(add=...)`)。
        `example` は `predicates.validate()` を通る本物であること(古くなって嘘にならない)。
        """
        return self._not_implemented("describe_predicates")

    def measure(self) -> dict:
        """選択を測る(§5 `measure(selection)`)。**verbではないので提案のままでも通る**。

        AIが自分の提案を確かめてから確定する往復がここで閉じる。`confirmed_items` 経由に
        すると、AIは中身を見ずに commit する動機を持ってしまう。

        値は**問い合わせ層のもの**をそのまま渡す(`query.describe_faces` /
        `describe_edges` / `region.regions`)。測り直すと述語が使う値と食い違いうる。
        頂点は位置を返す問い合わせが無いので `{"measured": False, "reason": ...}` で返す
        (黙って落とすとAIは自分の誤りと解釈する)。
        """
        return self._not_implemented("measure")

    # ------------------------------------------------------------------ スケッチ
    def sketch_create(self, base_plane: Optional[str] = None) -> dict:
        """スケッチを作る。`base_plane` 省略時は**確定選択の面**(ちょうど1枚)に貼る。

        平面も選択も無ければエラー —— 既定でXYに貼ると、図形が静かに別の場所に出る。
        """
        return self._not_implemented("sketch_create")

    def add_line(self, sketch_id: str, p0: list, p1: list) -> dict:
        return self._not_implemented("add_line")

    def add_circle(self, sketch_id: str, center: list, radius: float) -> dict:
        return self._not_implemented("add_circle")

    def add_rectangle(self, sketch_id: str, corner: list, width: float, height: float) -> dict:
        return self._not_implemented("add_rectangle")

    def add_arc(
        self, sketch_id: str, center: list, radius: float, start_deg: float, end_deg: float
    ) -> dict:
        return self._not_implemented("add_arc")

    # ------------------------------------------------------------------ フィーチャ
    def extrude(self, distance: float, mode: str = "join", body_id: Optional[str] = None) -> dict:
        """**確定選択の領域**を押し出す。領域は `RegionRef`(`{"sketch","index"}`)。"""
        return self._not_implemented("extrude")

    def extrude_face(self, distance: float) -> dict:
        """**確定選択の面**(ちょうど1枚)を押し出す。対象Bodyは参照が持っている。

        2枚以上選ばれていたら黙ってどれかを選ばない —— AIから見て指示と結果がずれる
        唯一の経路になる。
        """
        return self._not_implemented("extrude_face")

    def fillet(self, radius) -> dict:
        """**確定選択の辺**を丸める。`radius` は数値か辺ごとのリスト。

        辺の順序は `selection.edge_indices` が返す**選択順**であり、昇順ではない。
        `fillet` は `edges` と `radius` を位置で対応させるので、並べ替えると
        半径の割り当てが黙ってずれる(A1の実装で確認済み)。
        """
        return self._not_implemented("fillet")

    def chamfer(self, distance) -> dict:
        """**確定選択の辺**を面取りする(`fillet` と同じ規律)。"""
        return self._not_implemented("chamfer")

    def pattern_rectangular(
        self, sketch_id: str, entities: list, dx: float, dy: float, nx: int, ny: int
    ) -> dict:
        """図形の矩形反復。**スケッチの図形は選択集合の項目ではない**ので引数で受ける。"""
        return self._not_implemented("pattern_rectangular")

    def pattern_circular(
        self,
        sketch_id: str,
        entities: list,
        center: list,
        count: int,
        total_angle: float = 360.0,
        rotate_copies: bool = True,
    ) -> dict:
        return self._not_implemented("pattern_circular")

    # ------------------------------------------------------------------ ボディ・パラメータ
    def make_editable_copy(
        self, source_body_id: Optional[str] = None, name: Optional[str] = None
    ) -> dict:
        return self._not_implemented("make_editable_copy")

    def set_parameter(self, name: str, value: float) -> dict:
        """名前つきの寸法を書き換える(形が追随する)。

        **どの名前がどの寸法か**は `describe_document` の
        `sketches[].entities[].parameters` から辿れること —— 辿れないとAIは
        `d1` `d2` … を総当たりするしかない。
        """
        return self._not_implemented("set_parameter")

    # ------------------------------------------------------------------ 入出力
    def import_step(self, path: str) -> dict:
        """STEPを**参照ボディとして**読む(コアの verb そのもの)。

        起動時の `Session.load_step` は「参照 + 編集用コピー + 参照を隠す」までやるが、
        それは利便のための組み合わせであって verb ではない。MCPが出すのは verb の側で、
        足りない1手(`make_editable_copy`)を戻り値の `hint` で必ず知らせること ——
        黙って参照ボディだけ増やすと、AIは編集できない相手に verb を打ち続ける。
        """
        return self._not_implemented("import_step")

    def export_step(self, path: str, body_ids: Optional[list] = None) -> dict:
        """STEPを書き出す。**ログに載らない**(文書を変えないので verb ではない)。

        そのぶん `{"path", "bytes", "body_ids"}` を返す —— AIは自分でファイルを
        確かめられないので、戻り値が唯一の確認手段である。
        """
        return self._not_implemented("export_step")

    def export_stl(
        self, path: str, deviation: Optional[float] = None, body_ids: Optional[list] = None
    ) -> dict:
        """STLを書き出す(`deviation` 省略時は `Session.deviation`)。"""
        return self._not_implemented("export_stl")

    # ------------------------------------------------------------------ 履歴
    def undo(self) -> dict:
        """**UIから見た履歴と同じもの**を戻す(AI用の別履歴を作らない)。

        取り消せるかの判定は `Session.can_undo` が唯一の出どころ ——
        `describe_document` の答えと実際に通るかが一致する(M4のQAで塞いだ食い違い)。
        """
        return self._not_implemented("undo")

    def redo(self) -> dict:
        return self._not_implemented("redo")


# --------------------------------------------------------------------------------------
# MCP(JSON-RPC 2.0)
# --------------------------------------------------------------------------------------
class McpServer:
    """MCPの3メソッド(`initialize` / `tools/list` / `tools/call`)＋ `ping` を処理する。

    **トランスポートを知らない**(1メッセージの辞書を受け取り、返す辞書を返すだけ)ので、
    stdioを立てなくてもテストから直接叩ける。`handle` が None を返したら
    「返事を書いてはいけない」(通知・レスポンスの受信)という意味。
    """

    def __init__(self, tools: DocumentTools, name: str = SERVER_NAME, version: str = SERVER_VERSION) -> None:
        self.tools = tools
        self.name = name
        self.version = version
        self.initialized = False
        self.client_info: Optional[dict] = None
        self.protocol_version: Optional[str] = None

    # ------------------------------------------------------------------ 入口
    def handle(self, message: Any) -> Optional[dict]:
        """1メッセージを処理して返すレスポンス(通知なら None)。

        **例外を外へ出さない** — プロトコル層で落ちるとMCPクライアントから見て
        サーバが黙って死んだのと同じになるため、想定外の例外も内部エラーの
        レスポンスへ畳んで返す。
        """
        if isinstance(message, list):
            # JSON-RPCのバッチはMCP 2025-06-18で廃止された。受け取っても解釈しない。
            return _error(None, INVALID_REQUEST, "batch requests are not supported")
        if not isinstance(message, dict):
            return _error(None, INVALID_REQUEST, f"message must be a JSON object, got {type(message).__name__}")
        if message.get("jsonrpc") != JSONRPC_VERSION:
            return _error(
                message.get("id"), INVALID_REQUEST, f"jsonrpc must be {JSONRPC_VERSION!r}"
            )

        method = message.get("method")
        request_id = message.get("id")
        if method is None:
            # レスポンス(result/error)が来た。こちらからリクエストは出さないので無視する。
            return None
        if request_id is None:
            self._handle_notification(str(method))
            return None

        try:
            result = self._handle_request(str(method), message.get("params") or {})
        except _RpcError as error:
            return _error(request_id, error.code, error.message)
        except Exception as error:  # プロトコル層で死なない(上記docstring)
            return _error(request_id, INTERNAL_ERROR, f"{type(error).__name__}: {error}")
        return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result}

    # ------------------------------------------------------------------ メソッド
    def _handle_notification(self, method: str) -> None:
        """通知(idなし)。返事を書いてはいけない。"""
        if method == "notifications/initialized":
            self.initialized = True
        # 未知の通知は無視する(仕様どおり。エラーを返すとプロトコル違反になる)

    def _handle_request(self, method: str, params: dict) -> dict:
        if method == "initialize":
            return self._initialize(params)
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": self.tools.descriptors()}
        if method == "tools/call":
            return self._call_tool(params)
        raise _RpcError(METHOD_NOT_FOUND, f"unknown method: {method!r}")

    def _initialize(self, params: dict) -> dict:
        requested = params.get("protocolVersion")
        # 要求された版に対応していればそれを返し、していなければ自分の既定を返す(仕様)。
        version = (
            requested
            if requested in SUPPORTED_PROTOCOL_VERSIONS
            else SUPPORTED_PROTOCOL_VERSIONS[0]
        )
        self.protocol_version = version
        client = params.get("clientInfo")
        self.client_info = client if isinstance(client, dict) else None
        return {
            "protocolVersion": version,
            # ツールしか持たない。**変更通知は出さない**(ツールの顔ぶれは起動中変わらない)ので
            # listChanged は宣言しない。
            "capabilities": {"tools": {}},
            "serverInfo": {"name": self.name, "version": self.version},
            "instructions": (
                "duocad(自作3DCAD)の開いている文書を操作する。まず describe_document を呼んで "
                "body_id を得てから set_visible / describe_shape を使う。"
                "set_visible の結果は人間が開いているブラウザの画面にも即座に反映される。"
            ),
        }

    def _call_tool(self, params: dict) -> dict:
        name = params.get("name")
        if not isinstance(name, str):
            raise _RpcError(INVALID_PARAMS, "tools/call requires a string 'name'")
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise _RpcError(INVALID_PARAMS, "'arguments' must be a JSON object")

        descriptor = next((d for d in self.tools.descriptors() if d["name"] == name), None)
        if descriptor is None:
            # 「そんなツールは無い」はプロトコル側の誤り(仕様の例も -32602)。
            raise _RpcError(INVALID_PARAMS, f"unknown tool: {name!r}")
        _check_arguments(descriptor["inputSchema"], arguments)

        try:
            payload = self.tools.call(name, arguments)
        except Exception as error:
            # **ツールの実行時エラーはプロトコルエラーにしない**(仕様)。isError の結果として
            # 返すことで、AIが理由を読んでやり直せる。ここをJSON-RPCエラーにすると、
            # クライアントによってはAIに理由が届かない。
            return {
                "content": [{"type": "text", "text": f"{type(error).__name__}: {error}"}],
                "isError": True,
            }
        return {
            "content": [
                {"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}
            ],
            "isError": False,
        }


class _RpcError(Exception):
    """JSON-RPCのエラーレスポンスへ畳む例外(プロトコル層の誤りだけに使う)。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _error(request_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "error": {"code": code, "message": message}}


def _check_arguments(schema: dict, arguments: dict) -> None:
    """入力スキーマとの突き合わせ(必須・未知キー・型)。**黙って通さない**。

    JSON Schemaの完全な検証はしない(依存を増やさないため)。ここで見るのは
    「必須が欠けている」「知らないキーが来た」「boolean/string が違う」の3つだけで、
    いずれも**間違ったまま実行すると静かに別のことをする**類の誤りである
    (例: `visible` に文字列 "false" が来ると Python では真になる)。
    """
    properties = schema.get("properties") or {}
    required = schema.get("required") or []

    missing = [key for key in required if key not in arguments]
    if missing:
        raise _RpcError(INVALID_PARAMS, f"missing required argument(s): {missing}")
    if schema.get("additionalProperties") is False:
        unknown = [key for key in arguments if key not in properties]
        if unknown:
            raise _RpcError(
                INVALID_PARAMS, f"unknown argument(s): {unknown} (expected: {sorted(properties)})"
            )
    for key, value in arguments.items():
        expected = (properties.get(key) or {}).get("type")
        if expected == "boolean" and not isinstance(value, bool):
            raise _RpcError(INVALID_PARAMS, f"argument {key!r} must be a boolean, got {value!r}")
        if expected == "string" and not isinstance(value, str):
            raise _RpcError(INVALID_PARAMS, f"argument {key!r} must be a string, got {value!r}")


# --------------------------------------------------------------------------------------
# stdio トランスポート
# --------------------------------------------------------------------------------------
async def serve_stdio(server: McpServer, reader: Any = None, writer: Any = None) -> None:
    """標準入出力でMCPを喋る(1メッセージ1行・UTF-8・改行区切り)。EOFで戻る。

    `readline` はブロックするので実行器スレッドへ逃がす(Windowsのイベントループは
    パイプの非同期読み取りに癖があるため、素直にスレッドで読むほうが壊れにくい)。
    **stdoutに出すのはこの関数だけ**(モジュールdocstring)。
    """
    loop = asyncio.get_running_loop()
    reader = reader if reader is not None else sys.stdin.buffer
    writer = writer if writer is not None else sys.stdout.buffer

    while True:
        line = await loop.run_in_executor(None, reader.readline)
        if not line:
            return  # EOF: MCPクライアントが接続を閉じた
        text = line.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        try:
            message = json.loads(text)
        except ValueError as error:
            response: Optional[dict] = _error(None, PARSE_ERROR, str(error))
        else:
            response = server.handle(message)
        if response is not None:
            writer.write(json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n")
            writer.flush()


# --------------------------------------------------------------------------------------
# 起動
# --------------------------------------------------------------------------------------
def _log(message: str) -> None:
    """**stderrへ**出す(stdoutはJSON-RPC専用)。"""
    print(message, file=sys.stderr, flush=True)


async def _run(session: Session, args: argparse.Namespace) -> None:
    """WebSocket・静的配信・MCPを1つのイベントループで動かす。"""
    server = Server(session)
    server.bind_loop(asyncio.get_running_loop())

    ws_url = f"ws://127.0.0.1:{args.port}"
    page_url = f"http://127.0.0.1:{args.http_port}/"
    try:
        start_http_server(args.web_dist, args.http_port, ws_url)
        served = True
    except (FileNotFoundError, OSError) as error:
        # ブラウザへ配れなくてもMCPのツールは動く。**黙って落とさず**理由を残して続ける。
        _log(f"warning: 静的配信を立てられなかった(MCPだけで続行): {error}")
        served = False

    async with serve(server.handle, "127.0.0.1", args.port, max_size=8 * 1024 * 1024):
        _log(f"duocad MCP server: stdio / WebSocket {ws_url}")
        if served:
            _log(f"  ブラウザで開く: {page_url}")
            if args.open:
                webbrowser.open(page_url)
        body_count = len([b for b in session.document.bodies if b.kind != "reference"])
        _log(f"  bodies={body_count} (参照ボディを除く)")
        await serve_stdio(McpServer(DocumentTools(session)))
    _log("duocad MCP server: stdin closed, bye")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="duocad MCPサーバ(stdio) + WebSocket + web/dist の静的配信"
    )
    parser.add_argument("--step", type=Path, default=DEFAULT_STEP)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--http-port", type=int, default=8766)
    parser.add_argument("--deviation", type=float, default=kernel.DEFAULT_DEVIATION)
    parser.add_argument("--measure-log", type=Path, default=DEFAULT_MEASURE_LOG)
    parser.add_argument("--web-dist", type=Path, default=DEFAULT_WEB_DIST)
    parser.add_argument("--no-step", action="store_true", help="STEPを読まずに空で起動する")
    parser.add_argument(
        "--open",
        action="store_true",
        help="起動時にブラウザを開く(既定は開かない。MCPクライアントが起動する経路では邪魔になるため)",
    )
    args = parser.parse_args(argv)

    step_path: Optional[Path] = None if args.no_step else args.step
    if step_path is not None and not step_path.exists():
        _log(f"STEPが見つからない: {step_path}")
        return 2

    session = Session(step_path, args.deviation, args.measure_log)
    try:
        asyncio.run(_run(session, args))
    except KeyboardInterrupt:
        _log("bye")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
