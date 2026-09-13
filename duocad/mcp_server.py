"""MCPサーバ — AIがコアを直接叩くための口。

**層の位置づけ(ここを誤ると設計が崩れる)**: MCPサーバは**アプリ層ではなく、コアのもう一人の
クライアント**である。ブラウザ(web/)とMCPは同じ `Session`(=コア側の文書)を叩く対等な
クライアントで、MCPからブラウザを操作したりブラウザの状態を読んだりしない。したがって
`set_visible` はアプリ層を一切通らずに文書を変える(ただし**操作ログには載らず、undoで戻らない**
—— 理由は `duocad.selection.set_visible`)。

**上り方向(コアが変更を発火し、アプリ層が購読して描き直す)**: 実体は `Session.add_listener`
で、`Server`(WebSocket)が購読して全接続へ配信する。MCPが可視性を変えると、**開いている
ブラウザの表示が自分では何もしていないのに変わる**。

**MCPとWebSocketを同じプロセスで動かす**。別プロセスにするとMCP側が自分の `Document` を
持ってしまい「AIが変えた文書」と「人が見ている文書」が別物になる:

    HTTP /mcp ─┐
    stdio(MCP) ┼→ Session(文書・操作ログ) → WebSocket → ブラウザ
    ブラウザ ──┘

**トランスポートは2本ある**。`handle()` はどちらも知らない(1メッセージの辞書を
受け取って返すだけ)ので、増えたのは口だけである:

- **HTTP** —— 静的配信と同じ8766の `/mcp`。**アプリを先に立てて、MCPクライアントが後から
  繋ぎに来る**。これが通常の道で、`claude mcp add --transport http duocad
  http://127.0.0.1:8766/mcp` の一度きりの登録で繋がる。**アプリの寿命がクライアントの
  セッションに縛られない**
- **stdio** —— クライアントがこのプロセスを所有する古典的な形。Claude Desktopのように
  stdioしか話せないクライアントのために残してある

接続方法は `MCP.md` が正。

**依存を増やさない**: MCPの公式Python SDK(`mcp`)は入れず、プロトコル(JSON-RPC 2.0 over
stdio / HTTP)を素で実装する。この venv には `pip` すら無く(uv管理)、`mcp` は
pydantic/anyio/httpx/starlette/uvicorn を連れてくる。使うのは `initialize` / `tools/list` /
`tools/call` の3メソッドだけ。

**stdoutに出してよいのはJSON-RPCの行だけ**(1メッセージ1行・改行区切り)。起動メッセージや
警告は必ず stderr へ出す — stdoutを1文字でも汚すとMCPクライアントが接続に失敗する。

**既知の割り切り(意図的に据え置き)**:

- `notifications/initialized` の受信を `tools/*` の前提条件に**しない**。厳格に弾いても
  拒否できるのは行儀の悪いクライアントだけで、逆に握手の順序が違う実装で**繋がらない**
  という失敗を作る。
- ツールの実行はイベントループの中で同期に走る(重い `describe_*` の間はWebSocketの配信も
  止まる)。実行器スレッドへ逃がすと `Server` 側のスレッド前提を作り直すことになる。
  **stdioの経路は `serve_stdio` 自体がイベントループの上にいるので、これで済む**。
  HTTPの経路だけは `ThreadingHTTPServer` のワーカースレッドから入ってくるので、
  `ToolsOnLoop`(`duocad/server.py`)で包んでイベントループへ投げ返している。
- `/mcp` は**リクエストを直列化する**(`WebHandler.mcp_lock`)。MCPクライアントは1つしか
  想定していないので、並行で捌く価値より握手の状態が混ざらないことを取った。

**OCPを直接importしない**(境界は kernel.py だけ)。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import webbrowser
from pathlib import Path
from typing import Any, Optional

from websockets.asyncio.server import serve

from . import (
    catalog,
    io_step,
    kernel,
    meshbody,
    predicates,
    query,
    region,
    selection,
    sketch,
)
from .document import unknown_id_message
from .server import (
    DEFAULT_WEB_DIST,
    MCP_HTTP_PATH,
    Server,
    Session,
    ToolsOnLoop,
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
class ToolError(Exception):
    """ツール実行の構造化失敗(`isError` の封筒へ畳む)。プロトコル層の誤りには使わない。"""

    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


class DocumentTools:
    """`Session`(コア)を叩くツール群。**ここに新しい幾何ロジックを書かない**。

    どのツールも戻り値は**JSON化できる素の値だけ**(MCPのレスポンスに載るため)。
    失敗は例外のまま上げる — 呼び出し側(`McpServer`)が `isError` の結果へ変換して
    AIに見せる(黙って空の成功を返さない)。
    """

    def __init__(self, session: Session) -> None:
        self.session = session
        #: ツールの顔ぶれは起動中変わらない(`listChanged` を宣言していない)ので**1度だけ組む**。
        #: `tools/call` は呼び出しのたびにこの表を引くため、都度組み直すと29個のdictを毎回作る。
        self._descriptors = self._build_descriptors()
        #: 名前 → 定義。`tools/call` の線形探索を避けるための索引。
        self._descriptor_by_name = {d["name"]: d for d in self._descriptors}
        #: 名前 → 実体のメソッド(`tools/call` が引く)。
        self._handlers = self._build_handlers()

    # ------------------------------------------------------------------ 記述
    def descriptors(self) -> list[dict]:
        """`tools/list` が返すツール定義(名前・説明・入力スキーマ)。

        中身は `__init__` で1度だけ組み、以降は**同じリストを返す**。
        返ったリスト・辞書を書き換えないこと(全呼び出しで共有している)。
        """
        return self._descriptors

    def descriptor(self, name: str) -> Optional[dict]:
        """名前でツール定義を1つ引く(未知なら None)。"""
        return self._descriptor_by_name.get(name)

    def _build_descriptors(self) -> list[dict]:
        """ツール定義の実体(`__init__` から1度だけ呼ぶ)。

        **中身は `duocad/catalog.py` の宣言そのもの**(名前・説明・入力スキーマ)。ツールを
        1つ足す/引数を1つ変えるときに触るのは宣言の1箇所だけである。説明文はAIがツールを
        選ぶ唯一の手がかりなので、**何ができるかではなく「いつ使うか」**を書く。
        """
        return catalog.tool_descriptors()

    def _build_handlers(self) -> dict:
        """ツール名 -> 実体のメソッド(`__init__` から1度だけ組む)。

        **宣言に載っている名前と同じ綴りのメソッドを引く**(「ツール名 == verb名」の規律が
        あるので対応表は導出で足りる)。**`__init__` の時点で組む**ので、宣言に足したのに
        メソッドを書き忘れると `DocumentTools` を作った瞬間に AttributeError になる ——
        AIがそのツールを呼ぶまで気づかない、という失敗の仕方をしない。
        """
        return {name: getattr(self, name) for name in catalog.tool_names()}

    def call(self, name: str, arguments: dict) -> dict:
        """ツールを実行して結果の辞書を返す。未知の名前は KeyError。"""
        try:
            handler = self._handlers[name]
        except KeyError:
            raise KeyError(
                f"unknown tool: {name!r} (available: {sorted(self._handlers)})"
            ) from None
        return handler(**arguments)

    def current_seq(self) -> tuple[int, str]:
        """文書+選択の版。**モデルには見せない** —— `descriptors()` には出てこず、
        `AgentLoop` だけが読む `ToolSurface` の4つ目のメンバー。

        `AgentLoop.run()` はモデルへの1往復のたび・ツールを1つ実行するたびにこれを控え、値が
        変わっていたら残りのツール呼び出しを実行せずに構造化エラーへ差し替える(楽観ロック)。

        戻り値は `(len(document.log.entries), selection.fingerprint(document))`。**2つとも要る**:

        - **ログの本数だけでは足りない**。選択の変更は操作ログに一切乗らないので、「人が辺Aの
          選択を外して辺Bを選び直して確定する」という**入れ替え**(件数も種別も変わらない)を
          検出できない —— `fillet(radius)` は確定選択から対象を取るので、見逃すとAIは辺Aの
          つもりで辺Bを黙って丸める。
        - **選択の指紋だけでも足りない**(形状が変わっても選択の中身が同じままのことがある)。

        - **可視性の指紋も要る**。`set_visible` は操作ログに載らなくなったので、
          ログの本数でも選択の指紋でも「AIが考えている間に人がボディを隠した」が動かない。
          抜けると、AIが**人が隠したものを黙って表示に戻す**(逆も同じ)。

        **それでも塞ぎきれない範囲が残る**: `set_parameter` のように選択を経由しないverbが
        対象にするパラメータ名は削除も改名もされないので、版がABAで偶然一致しても
        **verbが「別の対象」へ効くことはない**。残るのは「人が既に同じ変更をしていたら
        上書きが起こりうる」程度の意図の陳腐化である。
        """
        return (
            len(self.session.document.log.entries),
            selection.fingerprint(self.session.document),
            selection.visibility_fingerprint(self.session.document),
        )

    # ------------------------------------------------------------------ 実体
    def describe_document(
        self,
        include_bbox: bool = False,
        include_regions: bool = False,
    ) -> dict:
        """ボディ一覧・スケッチ一覧とパラメータ一覧。"""
        document = self.session.document
        bodies = []
        for body in document.bodies:
            # **メッシュボディはB-repの測り方を通せない**(`kernel.face_count(None)` が落ちる)。
            # 面は1枚として数える(`view._face_count` と同じ規約)。
            is_mesh = body.mesh is not None
            entry = {
                "body_id": body.id,
                "name": body.name,
                "kind": body.kind,
                "visible": body.visible,
                "face_count": 1 if is_mesh else kernel.face_count(body.shape),
                "volume": meshbody.volume(body.mesh) if is_mesh else query.volume(body.shape),
            }
            if include_bbox:
                entry["bbox"] = (
                    meshbody.bbox(body.mesh)
                    if is_mesh
                    else kernel.bounding_box(body.shape, cache=document.bbox_cache)
                )
            bodies.append(entry)

        sketches_out = []
        for sk in document.sketches:
            entities_out = [
                {
                    "entity_id": entity.id,
                    "kind": entity.kind,
                    "params": sketch.entity_params(document, sk.id, entity.id),
                    "parameters": dict(entity.params),
                }
                for entity in sk.entities
            ]
            sketch_entry: dict[str, Any] = {
                "sketch_id": sk.id,
                "plane": dict(sk.plane),
                "entities": entities_out,
            }
            if include_regions:
                sketch_entry["region_count"] = len(region.regions(document, sk.id))
            sketches_out.append(sketch_entry)

        active = document.active_body()
        return {
            "units": document.units,
            "active_body_id": active.id if active is not None else None,
            "body_count": len(bodies),
            "visible_count": sum(1 for body in bodies if body["visible"]),
            "bodies": bodies,
            "sketches": sketches_out,
            "parameters": dict(document.parameters),
            # **`Session.can_undo` を使う**(生の `log.can_undo` ではない)。文書の土台
            # (部品の読み込み・編集用ボディ)は取り消せないので、素のログを見ると
            # 「undoできる」と答えたのに実際は拒否される、という食い違いが出る。
            "can_undo": self.session.can_undo,
            "can_redo": document.log.can_redo,
            # **`Session.applied_count` を使う**(ログの長さ `len(log.entries)` ではない)。
            # ログの長さはundoで減るので、AIが見る数とブラウザが見る数がundoの後でずれる。
            "applied_count": self.session.applied_count,
        }

    def set_visible(self, body_id: str, visible: bool) -> dict:
        """可視性を切り替える。**コアを経由する**(`Session.set_visible`)。

        ここを通ることで (a) 文書の `Body.visible` が変わり (b) 変更イベントが発火して
        開いているブラウザが描き直す、の2つが同時に成立する。

        **verbではないので封筒に `verb` / `seq` を載せない**(契約2)。可視性は操作ログに
        積まれず、undo / redo でも戻らない —— 理由は `duocad.selection.set_visible`。
        AIが「隠したのを元に戻す」には `set_visible(visible=True)` を呼ぶ。

        存在しない `body_id` は KeyError(コアの契約。文書は変更しない)。
        """
        body_id = str(body_id)
        if not isinstance(visible, bool):
            raise ValueError(f"visible must be a boolean, got {type(visible).__name__}")
        before = self._find_body(body_id)
        revisions = self._revision_snapshot()
        applied = self.session.set_visible(body_id, visible)
        return {
            "change": "visibility",
            "body_id": body_id,
            "name": before.name,
            "visible": applied["visible"],
            "previous": applied["previous"],
            "changed": applied["changed"],
            "changed_bodies": self._changed_bodies(revisions),
            "visible_count": sum(1 for body in self.session.document.bodies if body.visible),
        }

    def describe_shape(self, body_id: str) -> dict:
        """1ボディの形状(面・辺の数と種別内訳・体積・境界箱)。読み取りのみ。"""
        body = self._find_body(str(body_id))
        if body.mesh is not None:
            # **面・辺の内訳を作らない**(メッシュには面の種別も辺の同一性も無い)。
            # キーは落とさず0を入れる —— 封筒の形が種別で変わると、読む側が両方の形を
            # 知っていないと壊れる。三角形の枚数だけは `triangle_count` で別に見せる。
            return {
                "body_id": body.id,
                "name": body.name,
                "kind": body.kind,
                "visible": body.visible,
                "face_count": 1,
                "edge_count": 0,
                "faces_by_type": {},
                "triangle_count": body.mesh.triangle_count,
                "volume": meshbody.volume(body.mesh),
                "bbox": meshbody.bbox(body.mesh),
            }
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
            "bbox": kernel.bounding_box(
                body.shape, cache=self.session.document.bbox_cache
            ),
        }

    def _find_body(self, body_id: str):
        body = next((b for b in self.session.document.bodies if b.id == body_id), None)
        if body is None:
            # **文面は `document.unknown_id_message` に一本化してある**(スケッチ・図形・
            # パラメータのエラーと同じ形で、有効IDかその出し方を必ず添える)。
            known = [b.id for b in self.session.document.bodies]
            raise KeyError(unknown_id_message("body", body_id, known))
        return body

    # ------------------------------------------------------------------ 封筒(成功)
    def _revision_snapshot(self) -> dict[str, int]:
        """全Bodyの現在の版。呼び出し前後の差から `changed_bodies` を導くための材料。"""
        return {
            body.id: self.session.body_revision(body.id)
            for body in self.session.document.bodies
        }

    def _body_summary(self, body) -> dict:
        """封筒の `bodies[]` 1要素。キーは6つちょうど(`BODY_KEYS`)。

        実体は `Session.body_meta`。MCP封筒は `revision` を持たない契約なので落とす。
        """
        meta = self.session.body_meta(body)
        del meta["revision"]
        return meta

    def _verb_envelope(self, verb: str, seq: int, before: dict[str, int]) -> dict:
        """ログに1行積むツールの成功封筒。

        `changed_bodies` は verbごとの数え上げではなく、`Session.body_revision` の前後差。
        消えたBodyは出さない(現存Bodyだけを見る)。
        """
        changed_bodies = self._changed_bodies(before)
        by_id = {body.id: body for body in self.session.document.bodies}
        return {
            "verb": verb,
            "seq": seq,
            "changed_bodies": changed_bodies,
            "bodies": [self._body_summary(by_id[body_id]) for body_id in changed_bodies],
            "can_undo": self.session.can_undo,
            "can_redo": self.session.document.log.can_redo,
        }

    def _changed_bodies(self, before: dict) -> list[str]:
        """`Session.body_revision` の**前後差**で「変わったBody」を出す(消えたBodyは出さない)。

        verbごとに「これはbodyを変える」と数え上げると、verbが増えたときに静かに漏れる
        (契約2)。`body_revision` は形状の同一性と可視性のタプルから版を導くので、
        押し出しでも `set_visible` でも同じこの1本で拾える。
        """
        after = self._revision_snapshot()
        return [
            body.id
            for body in self.session.document.bodies
            if before.get(body.id) != after.get(body.id)
        ]

    def _selection_summary(self) -> dict:
        """エラー封筒へ載せる選択の要約(`count` / `origin` だけ)。"""
        current = selection.get_selection(self.session.document)
        return {"count": len(current.items), "origin": current.origin}

    def error_envelope(self, tool: str, error: Exception) -> dict:
        """例外を決定3の語彙へ畳む。MCPは先に弾かず、落ちたあとに名前を付ける。

        **公開API**(`descriptors()` / `call()` と同じ面)。MCPとアプリ内エージェント
        (`duocad/agent/loop.py`)がどちらもこれを通るので、AIが見る失敗の形は経路によらず同じ。
        エージェント側で語彙を書き写すと、片方だけ増えたときに気づけない。
        """
        if isinstance(error, ToolError):
            payload = {
                "code": error.code,
                "message": error.message,
                "tool": tool,
                "exception": type(error).__name__,
            }
            payload.update(error.extra)
            return payload

        message = _exception_message(error)
        base = {
            "message": message,
            "tool": tool,
            "exception": type(error).__name__,
        }
        if isinstance(error, (KeyError, FileNotFoundError)):
            return {**base, "code": "not_found"}
        # 提案のまま verb へ渡した拒否。文言ではなく型で判定する(決定3が禁じた潰し方を防ぐ)。
        if isinstance(error, selection.SelectionNotCommitted):
            return {
                **base,
                "code": "selection_not_committed",
                "hint": "call commit_selection() before applying a verb that consumes the selection",
                "selection": self._selection_summary(),
            }
        # 選択由来の拒否は**コアが型で言ってくる**(`duocad/selection.py` の `require_*`)。
        # MCPは名前を付けて、次の一手を決める材料(`required_kind` / `selection`)を足すだけ。
        # 「1つだけ要るのに複数」は `invalid_argument` のまま —— AIが打つ次の手は
        # 「選び直す」ではなく「減らす」で、`selection_empty` とは違う(tests/README「F」節)。
        #
        # **`SelectionKindMismatch` を先に見る**。`SelectionEmpty` の
        # 派生なので、順序を入れ替えると種別違いが `selection_empty` に吸われて7語彙目が
        # 死ぬ。「空でないのに empty と名乗らない」ためにコードを分けたので、ここが要。
        if isinstance(error, selection.SelectionKindMismatch):
            return {
                **base,
                "code": "selection_kind_mismatch",
                "required_kind": error.kind,
                # 実際に確定している種別と件数(`{"face": 3}`)。画面もモデルも数え直さない。
                "present_kinds": error.present,
                "hint": (
                    f"the confirmed selection is not empty but has no {error.kind}: "
                    f"select {error.kind}(s) and commit before calling this verb"
                ),
                "selection": self._selection_summary(),
            }
        if isinstance(error, selection.SelectionEmpty):
            return {
                **base,
                "code": "selection_empty",
                "required_kind": error.kind,
                "selection": self._selection_summary(),
            }
        if isinstance(error, selection.SelectionAmbiguous):
            return {
                **base,
                "code": "invalid_argument",
                "required_kind": error.kind,
                "selection": self._selection_summary(),
            }
        if isinstance(error, ValueError):
            return {**base, "code": "invalid_argument"}
        return {**base, "code": "internal"}

    # ==================================================================================
    # ツールの契約(選択・成功封筒・失敗封筒)
    # ==================================================================================
    # 1. **形は選択から、数値は引数から。** 面・辺・領域を引数で受けるツールは1つも無い
    #    (`fillet(radius)` であって `fillet(edges, radius)` ではない)。Bodyだけは `body_id`
    #    引数で受ける —— Bodyは選択集合の項目ではない。選択から対象を取る実装は
    #    `duocad/selection.py` の `require_confirmed` / `require_edges` の1組だけ。
    # 2. **成功は封筒に入れて返す。** ログに1行積むツールは
    #    `verb` / `seq` / `changed_bodies` / `bodies` / `can_undo` / `can_redo` を必ず持つ。
    #    **ログに載らない変更**(選択・`set_visible`)は `verb` / `seq` を持たず、代わりに
    #    `change`("selection" / "visibility")で見分ける —— 持たせると、undoで戻ると読める
    #    封筒をAIに配ることになる。
    #    **`changed_bodies` は `Session.body_revision` の呼び出し前後の差**から導くこと。
    #    verbごとに「これはbodyを変える」と数え上げると、verbが増えたときに静かに漏れる。
    # 3. **失敗も封筒に入れて返す。** `{"error": {"code","message","tool","exception", ...}}`
    #    を `json.dumps` した本文 + `isError: true`。コードは7語彙
    #    (`not_found` / `invalid_argument` / `selection_not_committed` / `selection_empty` /
    #    `selection_kind_mismatch` / `invalid_predicate` / `internal`)。**例外のテキストを
    #    素で返さない。** **`FileNotFoundError` を忘れない** —— パスの間違いは AI が直せる
    #    誤りなので、`internal` に落とすと諦める。
    #
    # ## stdout を汚さない
    #
    # **OCCTのSTEP書き出しはC++側から標準出力へ統計を吐く**("Statistics on Transfer (Write)")。
    # stdoutはJSON-RPC専用なので、これが混ざると接続が壊れる。Python層の
    # `contextlib.redirect_stdout` では止まらない(C++が実fdへ直接書く)ため、**起動時に fd 1 を
    # fd 2 へ振り替える**こと(`_claim_stdout`・`os.dup2`)。ツール実行の間だけ囲む形では、
    # 人がブラウザから書き出す経路を取り逃す。
    #
    # ## 拒否はコアがする。MCPは名前を付けるだけ
    #
    # `selection_not_committed` を返すために `origin` を**先に見て弾いてはいけない**。拒否する
    # のは `selection.confirmed_items` だけであり、MCPは**落ちたあとに**
    # `isinstance(..., SelectionNotCommitted)` でコードを選ぶ。先に弾くと同じ規則が2箇所に
    # 生まれ、片方だけ緩んだときに誰も気づかない。
    # ------------------------------------------------------------------ 選択(5 + 自己記述)
    def _as_selection_payload(self, selection: dict) -> dict:
        """選択5ツールの戻り値を `{"selection": {...}}` に揃える。"""
        return {"selection": selection}

    def get_selection(self) -> dict:
        """現在の選択を返す。**発火しない**(読むだけ)。

        戻り値は `{"selection": {"items", "origin", "count"}}` で、**変更する4つと同じ形**。
        """
        return self._as_selection_payload(self.session.get_selection())

    def propose_selection(self, refs: Optional[list] = None, predicate: Optional[dict] = None) -> dict:
        """**AIが書き込む経路**。`refs`(明示的な参照)か `predicate`(述語)のどちらか一方。

        §0のAI側の合格条件「引数を**述語**または明示的な参照で与えられ」がこの1つに載る。
        - 一致0件は**成功**(`count: 0`)。エラーにするとAIが述語の書式を疑い始める
        - 不正な述語は `invalid_predicate` で、**`valid_ops` に
          `predicates.describe_ops()` をそのまま添える**(AIが自力で直せるように)
        - 不正な参照は `invalid_argument`(形)/ `not_found`(Body)。いずれも選択を変えない
        """
        # 不正な述語はコアに渡す前に弾く —— 不可視Body等の ValueError を
        # `invalid_predicate` に潰さないため(`validate` が通るものだけセッションへ)。
        if predicate is not None and refs is None:
            try:
                predicates.validate(predicate)
            except ValueError as error:
                raise ToolError(
                    "invalid_predicate",
                    _exception_message(error),
                    valid_ops=predicates.describe_ops(),
                    valid_kinds=list(predicates.PREDICATE_KINDS),
                ) from error
        event = self.session.propose_selection(refs=refs, predicate=predicate)
        return self._as_selection_payload(event["selection"])

    def modify_selection(self, add: Optional[list] = None, remove: Optional[list] = None) -> dict:
        """**人が直す経路**(差分編集)。`origin` は "user" になる —— 人が触ること自体が承認。"""
        event = self.session.modify_selection(add=add, remove=remove)
        return self._as_selection_payload(event["selection"])

    def commit_selection(self) -> dict:
        """提案を確定する(中身は変えない)。AIが自分の提案を実行する前に踏む明示の1手。"""
        event = self.session.commit_selection()
        return self._as_selection_payload(event["selection"])

    def clear_selection(self) -> dict:
        """選択を空にする。"""
        event = self.session.clear_selection()
        return self._as_selection_payload(event["selection"])

    def describe_predicates(self) -> dict:
        """述語の語彙をAIへ自己記述する。**`predicates.describe_ops()` をそのまま出す**。

        戻り値: `{"kinds": [...], "ops": <describe_ops()>, "where_is": "and", "example": {...}}`。
        opの定義を書き直すと、スキーマとバリデータが二重管理になり、**片方だけ増えたopが
        黙って通る**(`predicates.describe_ops` の docstring がこれを禁じている)。
        `where_is: "and"` と **ORが無いこと**まで出すのは、AIが無い機能を試して失敗するのを
        防ぐため(ORが要るときは2回評価して `modify_selection(add=...)`)。
        `example` は `predicates.validate()` を通る本物であること(古くなって嘘にならない)。
        """
        example = {
            "kind": "edge",
            "where": [
                {"op": "parallel_to", "axis": "Z"},
                {"op": "length_gt", "value": 20},
            ],
        }
        return {
            "kinds": list(predicates.PREDICATE_KINDS),
            "ops": predicates.describe_ops(),
            "where_is": "and",
            "example": example,
        }

    def measure(self) -> dict:
        """選択を測る(§5 `measure(selection)`)。**verbではないので提案のままでも通る**。

        AIが自分の提案を確かめてから確定する往復がここで閉じる。`confirmed_items` 経由に
        すると、AIは中身を見ずに commit する動機を持ってしまう。

        値は**問い合わせ層のもの**をそのまま渡す(`query.describe_faces` /
        `describe_edges` / `region.regions`)。測り直すと述語が使う値と食い違いうる。
        頂点は `describe_vertices` の3D座標(`point`)を返す。
        """
        current = selection.get_selection(self.session.document)
        items: list[dict] = []
        total_area = 0.0
        total_length = 0.0
        for ref in current.items:
            kind = selection.ref_kind(ref)
            if kind == "face":
                body = self._find_body(ref["body"])
                described = next(
                    face for face in query.describe_faces(body.shape) if face["index"] == ref["index"]
                )
                items.append(
                    {
                        "kind": "face",
                        "body": ref["body"],
                        "index": ref["index"],
                        "area": described["area"],
                        "normal": described["normal"],
                        "center": described["center"],
                    }
                )
                total_area += described["area"]
            elif kind == "edge":
                body = self._find_body(ref["body"])
                described = next(
                    edge for edge in query.describe_edges(body.shape) if edge["index"] == ref["index"]
                )
                items.append(
                    {
                        "kind": "edge",
                        "body": ref["body"],
                        "index": ref["index"],
                        "length": described["length"],
                        "start": described["start"],
                        "end": described["end"],
                        "edge_kind": described["kind"],
                    }
                )
                total_length += described["length"]
            elif kind == "region":
                sketch_id = ref["sketch"]
                described = next(
                    reg for reg in region.regions(self.session.document, sketch_id)
                    if reg.index == ref["index"]
                )
                items.append(
                    {
                        "kind": "region",
                        "sketch": sketch_id,
                        "index": ref["index"],
                        "area": described.area,
                    }
                )
                total_area += described.area
            elif kind == "vertex":
                body = self._find_body(ref["body"])
                described = next(
                    vertex
                    for vertex in query.describe_vertices(body.shape)
                    if vertex["index"] == ref["index"]
                )
                items.append(
                    {
                        "kind": "vertex",
                        "body": ref["body"],
                        "index": ref["index"],
                        "point": described["point"],
                    }
                )
            elif kind == selection.BODY_KIND:
                # **ソリッドを丸ごと指す種別**(形状最適化の保持/除外)。面積にも長さにも
                # 足さない —— ボディの「大きさ」は体積であって、面の面積の合計ではない。
                # 値は `body_meta` から取る(体積はキャッシュ済み。測り直さない)。
                body = self._find_body(ref["body"])
                meta = self.session.body_meta(body)
                items.append(
                    {
                        "kind": selection.BODY_KIND,
                        "body": ref["body"],
                        "name": meta["name"],
                        "volume": meta["volume"],
                        "face_count": meta["face_count"],
                    }
                )
            elif kind == selection.SKETCH_ENTITY_KIND:
                # **スケッチの図形**。点なら (u, v) を、辺(図形全体)なら解決済みの
                # `params` をそのまま返す —— `move_sketch_entities` の dx, dy を
                # 決めるのに要るのは「いまどこにあるか」だけである。
                sketch_id = ref["sketch"]
                entity_id = ref["entity"]
                target = sketch.get_entity(
                    sketch.get_sketch(self.session.document, sketch_id), entity_id
                )
                resolved = sketch.entity_params(
                    self.session.document, sketch_id, entity_id
                )
                entry = {
                    "kind": selection.SKETCH_ENTITY_KIND,
                    "sketch": sketch_id,
                    "entity": entity_id,
                    "entity_kind": target.kind,
                    "point": ref.get("point"),
                }
                if ref.get("point") is None:
                    entry["params"] = resolved
                else:
                    x_key, y_key = sketch.entity_point_pair(
                        target.kind, str(ref["point"])
                    )
                    entry["position"] = [resolved[x_key], resolved[y_key]]
                items.append(entry)
            else:
                raise ValueError(f"unmeasurable selection kind: {kind!r}")
        return {
            "count": len(items),
            "origin": current.origin,
            "items": items,
            "total_area": total_area,
            "total_length": total_length,
        }

    def _apply_logged(self, before: dict[str, int], applied: dict, **extra: Any) -> dict:
        """`Session` が返した applied から封筒を作り、作ったもののID等を足す。"""
        envelope = self._verb_envelope(applied["verb"], int(applied["seq"]), before)
        envelope.update(extra)
        return envelope

    # ------------------------------------------------------------------ スケッチ
    def sketch_create(self, base_plane: Optional[str] = None) -> dict:
        """スケッチを作る。`base_plane` 省略時は**確定選択の面**(ちょうど1枚)に貼る。

        **解決は `Session.sketch_create` が持つ** —— 2箇所に同じ「省略時は確定選択の面」を
        持つと、片方だけ直したときにAIと人で貼る先が食い違う。
        """
        before = self._revision_snapshot()
        applied = self.session.sketch_create(
            None if base_plane is None else str(base_plane)
        )
        return self._apply_logged(before, applied, sketch_id=applied["sketch_id"])

    def sketch_create_offset(
        self, distance: float, base_plane: Optional[str] = None
    ) -> dict:
        """オフセット平面にスケッチを作る。`base_plane` 省略時は**確定選択の面**(ちょうど1枚)
        を基準に、その法線方向へ `distance` だけ離れた平面に貼る。

        **解決は `Session.sketch_create_offset` が持つ**(`sketch_create` と同じ理由)。
        """
        before = self._revision_snapshot()
        applied = self.session.sketch_create_offset(
            float(distance), None if base_plane is None else str(base_plane)
        )
        return self._apply_logged(before, applied, sketch_id=applied["sketch_id"])

    def add_line(self, sketch_id: str, p0: list, p1: list) -> dict:
        before = self._revision_snapshot()
        applied = self.session.add_line(str(sketch_id), p0, p1)
        return self._apply_logged(before, applied, entity_id=applied["entity_id"])

    def add_circle(self, sketch_id: str, center: list, radius: float) -> dict:
        before = self._revision_snapshot()
        applied = self.session.add_circle(str(sketch_id), center, float(radius))
        return self._apply_logged(before, applied, entity_id=applied["entity_id"])

    def add_rectangle(
        self, sketch_id: str, corner: list, width: float, height: float, angle: float = 0.0
    ) -> dict:
        before = self._revision_snapshot()
        applied = self.session.add_rectangle(
            str(sketch_id), corner, float(width), float(height), float(angle)
        )
        return self._apply_logged(before, applied, entity_id=applied["entity_id"])

    def add_arc(
        self, sketch_id: str, center: list, radius: float, start_deg: float, end_deg: float
    ) -> dict:
        before = self._revision_snapshot()
        applied = self.session.add_arc(
            str(sketch_id), center, float(radius), float(start_deg), float(end_deg)
        )
        return self._apply_logged(before, applied, entity_id=applied["entity_id"])

    # ------------------------------------------------------------------ フィーチャ
    def extrude(self, distance: float, mode: str = "join", body_id: Optional[str] = None) -> dict:
        """**確定選択の領域**を押し出す(選択から取るのは `Session.extrude`)。

        `body_id` だけは引数のまま —— 「どのBodyへ足す/から引くか」は対象集合ではない。
        `mode="new"` は行き先を持たないので `body_id` を受け付けない。
        """
        before = self._revision_snapshot()
        applied = self.session.extrude(
            float(distance), mode=str(mode), body_id=body_id
        )
        return self._apply_logged(before, applied)

    def extrude_face(self, distance: float) -> dict:
        """**確定選択の面**(ちょうど1枚)を押し出す。対象Bodyは参照が持っている。

        2枚以上選ばれていたら黙ってどれかを選ばない —— AIから見て指示と結果がずれる
        唯一の経路になる(拒否は `selection.require_confirmed` が行う)。
        """
        before = self._revision_snapshot()
        applied = self.session.extrude_face(float(distance))
        return self._apply_logged(before, applied)

    def fillet(self, radius) -> dict:
        """**確定選択の辺**を丸める。`radius` は数値か辺ごとのリスト。

        辺と対象Bodyの取り出しは `Session.fillet`(`selection.require_edges`)に任せる。
        WebSocket側も同じラッパを通るので、選択→辺の変換はコアに1つしかない。
        """
        before = self._revision_snapshot()
        applied = self.session.fillet(radius)
        return self._apply_logged(before, applied)

    def chamfer(self, distance) -> dict:
        """**確定選択の辺**を面取りする(`fillet` と同じ規律)。"""
        before = self._revision_snapshot()
        applied = self.session.chamfer(distance)
        return self._apply_logged(before, applied)

    def boolean_body(self, target_ref, mode) -> dict:
        """**ターゲット(面参照)に確定選択のツールボディ群**を作用させる。"""
        before = self._revision_snapshot()
        applied = self.session.boolean_body(target_ref, mode)
        return self._apply_logged(before, applied)

    def pattern_rectangular(
        self, sketch_id: str, entities: list, dx: float, dy: float, nx: int, ny: int
    ) -> dict:
        """図形の矩形反復。**スケッチの図形は選択集合の項目ではない**ので引数で受ける。"""
        before = self._revision_snapshot()
        applied = self.session.pattern_rectangular(
            str(sketch_id), entities, float(dx), float(dy), int(nx), int(ny)
        )
        return self._apply_logged(before, applied, pattern_id=applied["pattern_id"])

    def pattern_circular(
        self,
        sketch_id: str,
        entities: list,
        center: list,
        count: int,
        total_angle: float = 360.0,
        rotate_copies: bool = True,
    ) -> dict:
        before = self._revision_snapshot()
        applied = self.session.pattern_circular(
            str(sketch_id),
            entities,
            center,
            int(count),
            total_angle=float(total_angle),
            rotate_copies=bool(rotate_copies),
        )
        return self._apply_logged(before, applied, pattern_id=applied["pattern_id"])

    def move_sketch_entities(
        self,
        dx: Optional[float] = None,
        dy: Optional[float] = None,
        copy: bool = False,
        mode: str = "translate",
        angle: Optional[float] = None,
        center: Any = None,
        center_entity: Any = None,
    ) -> dict:
        """**確定選択のスケッチの点・辺**を動かす / 複製する(選択から取るのは `Session`)。

        **数値へ潰さずそのまま渡す** —— `mode="rotate"` では dx/dy が省かれるので、
        ここで `float(None)` にすると「モードが取らない引数を断る」検査より先に落ちる。
        """
        before = self._revision_snapshot()
        applied = self.session.move_sketch_entities(
            dx,
            dy,
            copy=bool(copy),
            mode=str(mode),
            angle=angle,
            center=center,
            center_entity=center_entity,
        )
        return self._apply_logged(
            before, applied, copy=applied["copy"], created=applied["created"]
        )

    def move_body(
        self,
        dx: Optional[float] = None,
        dy: Optional[float] = None,
        dz: Optional[float] = None,
        copy: bool = False,
        mode: str = "translate",
        angle: Optional[float] = None,
        axis_edge: Any = None,
        axis_point: Any = None,
        axis_dir: Any = None,
        center_vertex: Any = None,
    ) -> dict:
        """**確定選択の面/辺/頂点が属するボディ**を動かす / 複製する(逆引きは `Session`)。

        **数値へ潰さずそのまま渡す**(`move_sketch_entities` と同じ理由)。
        """
        before = self._revision_snapshot()
        applied = self.session.move_body(
            dx,
            dy,
            dz,
            copy=bool(copy),
            mode=str(mode),
            angle=angle,
            axis_edge=axis_edge,
            axis_point=axis_point,
            axis_dir=axis_dir,
            center_vertex=center_vertex,
        )
        return self._apply_logged(
            before, applied, copy=applied["copy"], created=applied["created"]
        )

    def mirror_sketch_entities(self, axis: Any) -> dict:
        """**確定選択のスケッチ図形**を、既存の線分1本(`axis`)を軸に鏡映複製する。

        常に複製する(対象・軸の解決は `Session.mirror_sketch_entities`)。
        """
        before = self._revision_snapshot()
        applied = self.session.mirror_sketch_entities(axis)
        return self._apply_logged(before, applied, created=applied["created"])

    def mirror_body(self, base_plane: Optional[str] = None, plane_face: Any = None) -> dict:
        """**確定選択の面/辺/頂点が属するボディ**を、鏡映面に対して鏡映複製する。

        常に複製する(逆引き・鏡映面の検査は `Session.mirror_body`)。
        """
        before = self._revision_snapshot()
        applied = self.session.mirror_body(base_plane=base_plane, plane_face=plane_face)
        return self._apply_logged(before, applied, created=applied["created"])

    def delete(self) -> dict:
        """**確定選択のボディとスケッチ図形**をまとめて消す(逆引きと畳み込みは `Session`)。

        混ざった選択でも1回で処理し、undoは1回で全部戻る。領域が混ざっていると
        `selection_kind_mismatch`(黙って残りだけ消さない)。
        """
        before = self._revision_snapshot()
        applied = self.session.delete()
        return self._apply_logged(
            before,
            applied,
            bodies=applied["bodies"],
            entities=applied["entities"],
        )

    # ------------------------------------------------------------------ 形状最適化の条件
    #
    # **4つとも集合代入**で、対象は確定選択から取る(引数はhowだけ)。**確定選択が空でも
    # 通る** —— 空の代入が「その役割を全部外す」を意味するので、解除専用のツールが要らない。
    # 排他は先勝ちで、衝突すると `invalid_argument`(`optimize.RoleConflict` は ValueError の
    # 派生)。どのボディ・面が衝突したかは例外の文面が名指しする。
    def set_preserve_bodies(self) -> dict:
        """**確定選択のボディ**を保持ソリッドの集合にする(集合代入は `Session`)。"""
        before = self._revision_snapshot()
        applied = self.session.set_preserve_bodies()
        return self._apply_logged(before, applied, bodies=applied["bodies"])

    def set_exclude_bodies(self) -> dict:
        """**確定選択のボディ**を除外ソリッドの集合にする(集合代入は `Session`)。"""
        before = self._revision_snapshot()
        applied = self.session.set_exclude_bodies()
        return self._apply_logged(before, applied, bodies=applied["bodies"])

    def set_fixtures(self) -> dict:
        """**確定選択の面**を拘束面の集合にする(集合代入は `Session`)。"""
        before = self._revision_snapshot()
        applied = self.session.set_fixtures()
        return self._apply_logged(before, applied, face_count=applied["face_count"])

    def set_loads(self, magnitude: float, direction: Any) -> dict:
        """**確定選択の面だけ**に大きさ[N]と向きを焼く(上書き代入。選択外の荷重面は残る)。

        向きは `"normal"` / `"-normal"` / `[x, y, z]`。**ベクトルは単位化して控える**ので、
        返る `direction_vector` は打った値と長さが違うことがある(向きだけを使う)。

        `face_count` は**確定後の荷重面の総数**、`applied_count` はこの呼び出しで焼いた枚数。
        """
        before = self._revision_snapshot()
        applied = self.session.set_loads(magnitude, direction)
        return self._apply_logged(
            before,
            applied,
            face_count=applied["face_count"],
            applied_count=applied["applied_count"],
            magnitude=applied["magnitude"],
            direction=applied["direction"],
            direction_vector=applied["direction_vector"],
        )

    def clear_loads(self) -> dict:
        """**確定選択の面**を荷重から外す(選択が空なら全部外す)。"""
        before = self._revision_snapshot()
        applied = self.session.clear_loads()
        return self._apply_logged(before, applied, face_count=applied["face_count"])

    # ------------------------------------------------------------------ ボディ・パラメータ
    def set_parameter(self, name: str, value: float) -> dict:
        """名前つきの寸法を書き換える(形が追随する)。

        **どの名前がどの寸法か**は `describe_document` の
        `sketches[].entities[].parameters` から辿れること —— 辿れないとAIは
        `d1` `d2` … を総当たりするしかない。
        """
        before = self._revision_snapshot()
        applied = self.session.set_parameter(str(name), float(value))
        return self._apply_logged(
            before,
            applied,
            name=applied["name"],
            value=applied["value"],
            previous=applied["previous"],
        )

    # ------------------------------------------------------------------ 入出力
    def add_mesh_body(
        self, positions: list, indices: list, name: Optional[str] = None
    ) -> dict:
        """三角形メッシュのボディを1つ足す(verb `add_mesh_body`)。

        **足したメッシュは編集できない**(押し出し・フィレット・面取りは断られる)。
        できるのは 表示 / STLへの書き出し / 削除 / 表示の切り替え だけである
        —— 理由は `duocad/meshbody.py` のモジュールdocstring。

        **三角形をそのまま操作ログへ積む**ので、数万三角形を渡すとモデルのJSONが数MBになる。
        AIが手で三角形を並べるための口ではなく、**計算した結果を入れる**ための口である。
        """
        before = self._revision_snapshot()
        applied = self.session.add_mesh_body(positions, indices, name)
        return self._apply_logged(before, applied, body_id=applied["body_id"])

    def import_step(self, path: str) -> dict:
        """STEPを読み、**編集可能なボディまで作る**(verb `import_step`)。

        読み込んだらそのまま編集できる —— **STEPの読み込みはこの1本だけ**で、読んだ後に
        編集可能化を部品ごとに打つ手は無い(PCケースなら27回になっていた)。何部品あっても
        ログ1行・undo1回・MCP1呼び出しである。

        **ツール名は verb 名と同じ綴りにしてある。** `describe_document` のログに出る
        verb 名と呼べるツール名が違うと、AIに対応表を推測させることになる ——
        したがって**1つのツールを引数で複数のverbに打ち分けてはならない**。verbが1本に
        なった今もこの規律は同じで、読み込みのverbを増やすならツールも同じ綴りで増やす。
        """
        before = self._revision_snapshot()
        applied = self.session.import_step(path)
        return self._apply_logged(before, applied, body_ids=list(applied["body_ids"]))

    def import_svg(
        self,
        sketch_id: str,
        path: str,
        scale: Optional[float] = None,
    ) -> dict:
        """SVGの図形を**`sketch_id` が指す既存のスケッチへ**取り込む(verb `import_svg`)。

        **新しいスケッチは作らない** —— 貼る先は `sketch_id` が指すスケッチの
        平面で決まる。スケッチが無ければ先に `sketch_create` で作ること。

        取り込んだ図形はその時点で領域として使えるので、続けて `propose_selection` →
        `commit_selection` → `extrude` を打てる。**`hint` で領域の数を知らせる** ——
        SVGは1枚で複数の輪郭を持つのが普通で(文字の内側の穴、離れた図形)、
        黙って結果だけ返すと、AIは領域が1つだけのつもりで押し出しに進む。
        """
        before = self._revision_snapshot()
        applied = self.session.import_svg(
            str(sketch_id),
            path,
            None if scale is None else float(scale),
        )
        entity_ids = applied["entity_ids"]
        region_count = len(region.regions(self.session.document, str(sketch_id)))
        return self._apply_logged(
            before,
            applied,
            sketch_id=str(sketch_id),
            entity_ids=list(entity_ids),
            source_name=applied["source_name"],
            entity_count=applied["entity_count"],
            region_count=region_count,
            hint=(
                f"領域が {region_count} 個ある。押し出す前に describe_document の "
                "include_regions で面積を見て、対象を propose_selection で選ぶこと"
                "(文字の内側の穴も1つの領域として数える)"
            ),
        )

    def export_step(self, path: str) -> dict:
        """STEPを書き出す。**ログに載らない**(文書を変えないので verb ではない)。

        対象は**いま表示中のBodyだけ**(引数で選べない)。そのぶん
        `{"path", "bytes", "body_ids", "skipped_hidden"}` を返す —— AIは自分でファイルを
        確かめられないので、戻り値が唯一の確認手段である。
        """
        target = Path(path)
        resolved = io_step.export_body_ids(self.session.document)
        skipped = io_step.export_skipped_hidden(self.session.document)
        io_step.export_step(self.session.document, target)
        return {
            "path": str(target),
            "bytes": target.stat().st_size,
            "body_ids": list(resolved),
            "skipped_hidden": list(skipped),
        }

    def export_stl(self, path: str, deviation: Optional[float] = None) -> dict:
        """STLを書き出す(`deviation` 省略時は `Session.deviation`)。対象は export_step と同じ
        (いま表示中のBodyだけ・引数で選べない)。"""
        target = Path(path)
        resolved = io_step.export_body_ids(self.session.document)
        skipped = io_step.export_skipped_hidden(self.session.document)
        used = float(self.session.deviation if deviation is None else deviation)
        io_step.export_stl(self.session.document, target, used)
        return {
            "path": str(target),
            "bytes": target.stat().st_size,
            "body_ids": list(resolved),
            "skipped_hidden": list(skipped),
        }

    # ------------------------------------------------------------------ 履歴
    def undo(self) -> dict:
        """**UIから見た履歴と同じもの**を戻す(AI用の別履歴を作らない)。

        取り消せるかの判定は `Session.can_undo` が唯一の出どころ ——
        `describe_document` の答えと実際に通るかが一致する。
        """
        before = self._revision_snapshot()
        applied = self.session.undo()
        # `Session.undo` が返す seq は取り消した操作の通し番号(`_selection_hints`経由)。
        envelope = self._verb_envelope("undo", int(applied["seq"]), before)
        envelope["undone"] = applied["undone"]
        return envelope

    def redo(self) -> dict:
        before = self._revision_snapshot()
        applied = self.session.redo()
        envelope = self._verb_envelope("redo", int(applied["seq"]), before)
        envelope["redone"] = applied["redone"]
        return envelope


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
            # **アプリ内エージェント(`duocad/agent/`)と同じ文**。手順を2箇所に書くと
            # 片方だけ直った状態に気づけないので、宣言側(catalog.py)の1本から読む。
            "instructions": catalog.agent_instructions(),
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

        descriptor = self.tools.descriptor(name)
        if descriptor is None:
            # 「そんなツールは無い」はプロトコル側の誤り(仕様の例も -32602)。
            raise _RpcError(INVALID_PARAMS, f"unknown tool: {name!r}")
        _check_arguments(descriptor["inputSchema"], arguments)

        try:
            # stdout は起動時に `_claim_stdout` が fd ごと stderr へ退避済みなので、
            # ここで囲み直さない(囲んでも stderr を stderr に振り替えるだけになる)。
            payload = self.tools.call(name, arguments)
        except Exception as error:
            # **ツールの実行時エラーはプロトコルエラーにしない**(仕様)。決定3の封筒へ畳む。
            # 例外テキストを素で返すと `json.loads` できず、AIが機械可読に直せない。
            envelope = {"error": self.tools.error_envelope(name, error)}
            return {
                "content": [
                    {"type": "text", "text": json.dumps(envelope, ensure_ascii=False)}
                ],
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

    JSON Schemaの完全な検証はしない(依存を増やさないため)。見るのは必須・未知キーと
    `boolean` / `string` / `number` / `integer` / `array` / `object`、および型のリスト
    (例: `fillet.radius` の `["number","array"]`)。いずれも**間違ったまま実行すると
    静かに別のことをする**類の誤りである(例: `visible` に "false" → 真、`True` を
    数値として通すと 1.0)。エラーメッセージには**引数名を必ず含める**。
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
        if expected is None:
            continue
        if isinstance(expected, list):
            if not any(_value_matches_json_type(value, item) for item in expected):
                raise _RpcError(
                    INVALID_PARAMS,
                    f"argument {key!r} must be one of {expected}, got {value!r}",
                )
            continue
        if not _value_matches_json_type(value, expected):
            raise _RpcError(
                INVALID_PARAMS,
                f"argument {key!r} must be a {expected}, got {value!r}",
            )


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


def _exception_message(error: Exception) -> str:
    """例外の本文。`KeyError` の `str()` が付ける余分な引用符を避ける。"""
    if isinstance(error, KeyError) and error.args:
        return str(error.args[0])
    return str(error)


def _claim_stdout() -> Any:
    """JSON-RPC専用の書き口を確保し、**プロセスの fd 1 を fd 2 へ振り替える**。

    ツール実行の間だけ囲む形では足りない —— このプロセスは
    **MCPとWebSocketの両方**を持っており(`_run`)、**人がブラウザからSTEPを書き出した
    ときにも**OCCTのC++が実fdへ統計を吐く。その経路はツール実行ではないので囲みの外に
    あり、JSON-RPCの行の途中に `****** Statistics on Transfer (Write) ******` が
    割り込んで接続が壊れる(交互操作は人とAIが同じプロセスを同時に触る形なので、
    普通に起きる)。

    確保したdupを返し、fd 1 は stderr を指すようにする。これ以降、**誰がどの経路で
    標準出力へ書いても**それはstderrへ流れ、JSON-RPCは返した書き口だけを通る。
    """
    sys.stdout.flush()
    saved = os.dup(1)
    os.dup2(2, 1)
    return os.fdopen(saved, "wb", buffering=0)


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
    writer = writer if writer is not None else _claim_stdout()

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


#: `npm run dev` が使うポート(README §1.3)。dev serverは `web/src` を直に配るので
#: 静的配信(`--http-port`)とは別オリジンになる。
VITE_DEV_PORT = 5173


def allowed_origins(http_port: int) -> list:
    """WebSocketを開いてよい `Origin` の許可リスト。

    **なぜ要るか**: WebSocketは**同一オリジンポリシーの対象外**で、任意のWebページが
    `ws://127.0.0.1:<port>` へ繋いで読み書きできる。`127.0.0.1` へのbindは防御にならない
    —— 攻撃側のコードは利用者のブラウザの中で動くため。検査が無い間は、duocadを立てたまま
    別のサイトを開いているだけで、そのページが文書を読み・壊し・`export_step` で任意のパスへ
    書き・`ai_chat` で課金を消費できた。

    **`None`(Originヘッダ無し)を許可する**のは意図的である。ブラウザは必ず `Origin` を送り、
    JavaScriptから偽装できないので、**ブラウザ経由の攻撃はこれで塞がる**。一方 `smoke*.mjs` や
    素のWSクライアントはヘッダを送らないので、拒否すると自分の検査だけが壊れる。素のWSを
    立てられる相手は既にこのPCでコードを実行できており、この防御線の外側にいる。

    許可するのは「duocad自身が配るページ」だけ: 静的配信(`--http-port`)と Vite dev server。
    `localhost` と `127.0.0.1` は**別オリジン**なので両方を挙げる。
    """
    return [
        None,  # Originヘッダ無し(非ブラウザのクライアント)。上記の理由で許可する
        *(
            f"http://{host}:{port}"
            for port in (http_port, VITE_DEV_PORT)
            for host in ("127.0.0.1", "localhost")
        ),
    ]


async def _run(session: Session, args: argparse.Namespace) -> None:
    """WebSocket・静的配信・MCPを1つのイベントループで動かす。"""
    server = Server(session)
    server.bind_loop(asyncio.get_running_loop())
    # **同じ `DocumentTools`(= 同じ `Session`)をMCPとアプリ内AIの両方へ渡す**(G)。
    # 別々に作ると道具の顔ぶれは同じでも別インスタンスになり、`_handlers` の索引を
    # 2つ持つ理由が無い(状態は持たない薄いラッパなので共有して問題ない)。
    tools = DocumentTools(session)
    server.bind_agent_tools(tools)

    ws_url = f"ws://127.0.0.1:{args.port}"
    page_url = f"http://127.0.0.1:{args.http_port}/"
    mcp_url = f"http://127.0.0.1:{args.http_port}{MCP_HTTP_PATH}"
    # **HTTPトランスポート用のMCPは別インスタンスにする**。stdioと同じ
    # `McpServer` を共有すると `initialized` や `protocol_version` が混ざる —— 別の
    # トランスポートに繋いだ相手は別のクライアントなので、握手も別に持つのが正しい。
    # 道具は `ToolsOnLoop` で包む(HTTPはワーカースレッドで走るため。下記 `start_http_server`)。
    http_mcp = McpServer(ToolsOnLoop(tools, asyncio.get_running_loop()))
    try:
        start_http_server(args.web_dist, args.http_port, ws_url, mcp=http_mcp)
        served = True
    except (FileNotFoundError, OSError) as error:
        # ブラウザへ配れなくてもstdioのMCPは動く。**黙って落とさず**理由を残して続ける。
        # ただし `/mcp` も同じサーバに載っているので、**この経路ではHTTPのMCPも立たない**。
        _log(f"warning: 静的配信を立てられなかった(stdioのMCPだけで続行): {error}")
        served = False

    origins = allowed_origins(args.http_port)
    async with serve(
        server.handle,
        "127.0.0.1",
        args.port,
        max_size=8 * 1024 * 1024,
        origins=origins,
    ):
        _log(f"duocad MCP server: stdio / WebSocket {ws_url}")
        if served:
            _log(f"  ブラウザで開く: {page_url}")
            _log(f"  MCP(HTTP): {mcp_url}")
            if args.open:
                webbrowser.open(page_url)
        _log(f"  bodies={len(session.document.bodies)}")
        await serve_stdio(McpServer(tools))
    _log("duocad MCP server: stdin closed, bye")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="duocad MCPサーバ(stdio) + WebSocket + web/dist の静的配信"
    )
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--http-port", type=int, default=8766)
    parser.add_argument("--deviation", type=float, default=kernel.DEFAULT_DEVIATION)
    parser.add_argument("--web-dist", type=Path, default=DEFAULT_WEB_DIST)
    parser.add_argument(
        "--open",
        action="store_true",
        help="起動時にブラウザを開く(既定は開かない。MCPクライアントが起動する経路では邪魔になるため)",
    )
    parser.add_argument(
        "--models-dir",
        type=Path,
        default=None,
        help=(
            "保存/開く/一覧の置き場(既定は Session.DEFAULT_MODELS_DIR = リポジトリ直下の "
            "models/)。実ブラウザ検査が使い捨てのworkdirへ差し替えるための穴で、"
            "通常の起動では指定しない"
        ),
    )
    args = parser.parse_args(argv)

    # **常に空の文書で起動する**。
    # STEPはツール `import_step`(編集可能なボディまで作る)から読む。
    session = Session(None, args.deviation, models_dir=args.models_dir)
    try:
        asyncio.run(_run(session, args))
    except KeyboardInterrupt:
        _log("bye")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
