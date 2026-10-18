"""MCPサーバ — AIがコアを直接叩くための口。

**MCPサーバはアプリ層ではなく、コアのもう一人のクライアント**である。ブラウザ(web/)とMCPは
同じ `Session`(=コア側の文書)を叩く対等なクライアントで、MCPからブラウザを操作したり
ブラウザの状態を読んだりしない。したがって `set_visible` はアプリ層を通らずに文書を変える
(ただし**操作ログには載らず、undoで戻らない** —— 理由は `duocad.selection.set_visible`)。
コアが変更を発火し(`Session.add_listener`)、`Server`(WebSocket)が購読して全接続へ配信するので、
MCPが可視性を変えると**開いているブラウザの表示も変わる**。

**MCPとWebSocketを同じプロセスで動かす**。別プロセスだとMCP側が自分の `Document` を持ち、
「AIが変えた文書」と「人が見ている文書」が別物になる:

    HTTP /mcp ─┐
    stdio(MCP) ┼→ Session(文書・操作ログ) → WebSocket → ブラウザ
    ブラウザ ──┘

**トランスポートは2本**。`handle()` はどちらも知らない(1メッセージの辞書を受け取って返すだけ):

- **HTTP** —— 静的配信と同じ8766の `/mcp`。**アプリを先に立て、MCPクライアントが後から繋ぎに来る**
  のが通常の道で、`claude mcp add --transport http duocad http://127.0.0.1:8766/mcp` を
  一度登録すれば繋がる。アプリの寿命がクライアントのセッションに縛られない
- **stdio** —— クライアントがこのプロセスを所有する古典的な形。stdioしか話せないクライアント
  (Claude Desktop)のために残してある

接続方法は `MCP.md` が正。

**依存を増やさない**: MCPの公式Python SDK(`mcp`)は入れず、JSON-RPC 2.0 over stdio / HTTP を
素で実装する(`mcp` は pydantic/anyio/httpx/starlette/uvicorn を連れてくる。この venv には
`pip` すら無い)。使うのは `initialize` / `tools/list` / `tools/call` の3メソッドだけ。

**stdoutに出してよいのはJSON-RPCの行だけ**(1メッセージ1行・改行区切り)。起動メッセージや
警告は必ず stderr へ — stdoutを1文字でも汚すとMCPクライアントが接続に失敗する。

**既知の割り切り(意図的に据え置き)**:

- `notifications/initialized` の受信を `tools/*` の前提条件に**しない**。厳格に弾いても拒否
  できるのは行儀の悪いクライアントだけで、握手の順序が違う実装で**繋がらない**失敗を作る。
- ツールの実行はイベントループの中で同期に走る(重い `describe_*` の間はWebSocketの配信も
  止まる)。実行器スレッドへ逃がすと `Server` 側のスレッド前提を作り直すことになる。
  stdioは `serve_stdio` 自体がループの上にいるのでこれで済む。HTTPだけは
  `ThreadingHTTPServer` のワーカースレッドから入るので、`ToolsOnLoop`(`duocad/server.py`)で
  包んでループへ投げ返している。
- `/mcp` は**リクエストを直列化する**(`WebHandler.mcp_lock`)。MCPクライアントは1つしか
  想定しておらず、並行で捌く価値より握手の状態が混ざらないことを取った。

**OCPを直接importしない**(境界は kernel.py だけ)。
"""
from __future__ import annotations

import argparse
import asyncio
import functools
import json
import os
import sys
import webbrowser
from pathlib import Path
from typing import Any, Optional

from websockets.asyncio.server import serve

from . import (
    catalog,
    kernel,
    meshbody,
    predicates,
    query,
    region,
    selection,
    sketch,
)
from .channel import CHANNELS, StableUpdater, describe, hidden_features
from .agent.loop import VersionChanged
from .entry import entry_for
from .session import DocumentBusy
from .workspace import NoCommand
from .server import (
    DEFAULT_WEB_DIST,
    MCP_HTTP_PATH,
    Server,
    Session,
    ToolsOnLoop,
    start_http_server,
)

SERVER_NAME = "duocad"
SERVER_VERSION = "0.0.1"

#: 対応するMCPプロトコル版。**新しい順**(先頭が既定)。要求された版がこの中にあればそれを、
#: 無ければ先頭を返す(仕様どおり。クライアントが返った版を見て続けるか切るかを決める)。
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

JSONRPC_VERSION = "2.0"

#: verb の成功封筒のうち、入口の返したもので上書きしないキー。`changed_bodies` は版の前後差から導き
#: (`DocumentTools._changed_bodies`)、`client_op` は画面への確認応答で AI には意味が無い。
_ENVELOPE_OWNED_KEYS = frozenset({"verb", "seq", "changed_bodies", "client_op"})

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


#: `selection_empty` に添える「その種別の選び方」。実測で、領域は述語で選べないと分からず数往復を使った。
_HOW_TO_SELECT = {
    "region": 'select the sketch region first: set_selection(refs=[{"sketch": sketch_id, "index": 1}])',
    "edge": 'select edges first: set_selection(predicate={"kind": "edge", "body": body_id, "where": [...]})',
    "face": 'select faces first: set_selection(predicate={"kind": "face", "body": body_id, "where": [...]})',
}


class ToolError(Exception):
    """ツール実行の構造化失敗(`isError` の封筒へ畳む)。プロトコル層の誤りには使わない。"""

    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


class DocumentTools:
    """`Session`(コア)を叩くツール群。**ここに新しい幾何ロジックを書かない**。

    戻り値は**JSON化できる素の値だけ**。失敗は例外のまま上げ、呼び出し側(`McpServer`)が
    `isError` の結果へ変換する(黙って空の成功を返さない)。
    """

    def __init__(self, session: Session, *, hidden_features: frozenset = frozenset()) -> None:
        self.session = session
        #: この版で隠す機能(`channel.hidden_features`)。属する道具は一覧にも実体にも入れない。
        self.hidden_features = frozenset(hidden_features)
        #: ツールの顔ぶれは起動中変わらない(`listChanged` を宣言していない)ので**1度だけ組む**。
        self._descriptors = self._build_descriptors()
        self._descriptor_by_name = {d["name"]: d for d in self._descriptors}
        self._handlers = self._build_handlers()

    def descriptors(self) -> list[dict]:
        """`tools/list` が返すツール定義。`__init__` で1度だけ組んだ**同じリストを返す**ので、
        返ったリスト・辞書を書き換えないこと。
        """
        return self._descriptors

    def descriptor(self, name: str) -> Optional[dict]:
        return self._descriptor_by_name.get(name)

    def _build_descriptors(self) -> list[dict]:
        """中身は `duocad/catalog.py` の宣言そのもの(説明文の書き方は `Command.description` の注記)。"""
        return catalog.tool_descriptors(self.hidden_features)

    def _build_handlers(self) -> dict:
        """ツール名 -> 実体。同じ綴りのメソッドがあればそれ、無ければ verb の入口
        (`duocad/entry.py`)へ渡す `_apply_verb`。

        `__init__` で組むので、宣言に足したのに入口もメソッドも無いツールは構築時に KeyError になる
        (AIがそのツールを呼ぶまで気づかない、という失敗の仕方をしない)。
        """
        handlers = {}
        for name in catalog.tool_names(self.hidden_features):
            own = getattr(self, name, None)
            if own is None:
                entry_for(name)
                own = functools.partial(self._apply_verb, name)
            handlers[name] = own
        return handlers

    def call(self, name: str, arguments: dict, expected_generation: Optional[int] = None) -> dict:
        """ツールを実行して結果の辞書を返す。未知の名前は KeyError。

        **引数はスキーマと突き合わせてから渡す**(必須・未知キー・型)。MCP は `McpServer._call_tool`
        が同じ検査を -32602 で先に弾くが、アプリ内AIの経路はここが唯一の門 —— 通すと
        `radius: "12"` が1文字ずつ読まれて辺2本に半径1と2が付くように、黙って別の値になる。
        違反は `invalid_argument` の封筒(引数名つき)で返す。

        `expected_generation` を渡すと、文書の世代が違えば実行せず `document_changed` で返す。照合をここ(実行と
        同じ1手)で行うのは、`ToolsOnLoop` で照合と実行を別の投げ返しにすると、その間に人の切り替えが入るため。
        """
        try:
            handler = self._handlers[name]
        except KeyError:
            raise KeyError(
                f"unknown tool: {name!r} (available: {sorted(self._handlers)})"
            ) from None
        if expected_generation is not None and expected_generation != self.session.generation:
            raise ToolError(
                "document_changed",
                f"人が画面で文書を切り替えた(今は {self._document_label()})。前の文書のつもりの操作は実行しなかった。"
                "describe_document で今の文書を確かめてから続けること。",
                current_name=self.session.current_name,
                generation=self.session.generation,
            )
        problem = catalog.check_arguments(self._descriptor_by_name[name]["inputSchema"], arguments)
        if problem is not None:
            raise ToolError("invalid_argument", problem)
        return handler(**arguments)

    def _document_label(self) -> str:
        if not self.session.document_open:
            return "文書が閉じられている"
        return self.session.current_name or "保存前の新規文書"

    def call_and_seq(self, name: str, arguments: dict, expected_seq: Any = None) -> tuple:
        """`call` と**直後の** `current_seq` を返す: `(結果, 例外, 版)`。例外は上げずに返す。
        `ToolsOnLoop` はこれを1回の投げ返しで走らせる —— 2回に分けると、間に入った人の操作が
        版に吸われて検出できない(issue #48)。

        `expected_seq` と今の版が違えば走らせず、例外の位置に `VersionChanged` を返す。照合をここに入れるのは、
        呼び手の照合とこの実行の間に人が文書を切り替えると、古い前提の verb が新しい文書に効くため(issue #102)。
        """
        if expected_seq is not None:
            seq = self.current_seq()
            if seq != expected_seq:
                return None, VersionChanged(seq), seq
        try:
            return self.call(name, arguments), None, self.current_seq()
        except Exception as error:  # noqa: BLE001 — 封筒にするのは呼び手(`error_envelope`)
            return None, error, self.current_seq()

    def current_seq(self) -> tuple[int, int, int, str, str, int]:
        """文書+選択+作業空間の版。**モデルには見せない** —— `AgentLoop` だけが読む `ToolSurface` の4つ目のメンバー。

        `AgentLoop.run()` はモデルへの1往復・ツール1実行のたびにこれを控え、値が変わっていたら
        残りのツール呼び出しを構造化エラーへ差し替える(楽観ロック)。

        戻り値は `(Session.generation, len(document.log.entries), Session.applied_count, selection.fingerprint(document),
        selection.visibility_fingerprint(document), Session.workspace.revision)`。**6つとも要る**:

        - 世代が無いと文書の切り替えを見逃す。新規→新規は残り3つが `(0, 空, 空)` のまま一致する(実測)。
        - ログの本数は undo / redo で動く。無いと undo / redo 単独を見逃す。
        - 適用回数(`applied_count`)は undo / redo で動かず、verb を適用するたびに進む。ログの本数だけでは、undo してから
          同じ本数になる別の verb を打つと版が戻って見逃す(面・辺の添字が差し替わった形へ、古い前提の `set_selection` が効く)。
          逆に適用回数だけでは undo / redo 単独が出ないので、両方持つ。
        - ログと適用回数だけでは足りない。選択の変更は操作ログに乗らないので、辺Aを外して辺Bを
          選び直す**入れ替え**を検出できず、`fillet(radius)`(選択から対象を取る)は辺Aの
          つもりのAIに辺Bを黙って丸めさせる。
        - 選択の指紋だけでも足りない(形状が変わっても選択の中身が同じままのことがある)。
        - 可視性の指紋も要る。`set_visible` はログに載らないので、抜けると「考えている間に人が
          隠したボディ」をAIが黙って表示に戻す(逆も同じ)。
        - 作業空間の版も要る(issue #153)。起動中のコマンドと引数はログに載らないので、抜けると「考えている間に
          人が半径を打ち直した / Esc で畳んだ」を見ずに AI が古い引数で `apply_command` する。

        **塞ぎきれない範囲**: 選択を経由せず body_id 等で直接効くverbは、版がABAで偶然一致しても
        別の対象へ効くことはない(IDは不変)。残るのは「人が既に同じ変更をしていたら上書きが
        起こりうる」程度の意図の陳腐化である。
        """
        return (
            self.session.generation,
            len(self.session.document.log.entries),
            self.session.applied_count,
            selection.fingerprint(self.session.document),
            selection.visibility_fingerprint(self.session.document),
            self.session.workspace.revision,
        )

    def describe_document(
        self,
        include_bbox: bool = False,
        include_regions: bool = False,
    ) -> dict:
        """ボディ一覧・スケッチ一覧とパラメータ一覧。

        各図形は `params`(解決済みの数値)と `parameters`(その数値を持つパラメータ名)の
        **両方**を返す(名前だけでは今の値が分からず、数値だけでは名前つきの寸法を指せない)。
        `include_regions` は領域の数え上げに計算コストがあるので任意。
        """
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
                "name": sk.name,
                "plane": dict(sk.plane),
                "visible": sk.visible,
                "entities": entities_out,
            }
            if include_regions:
                sketch_entry["region_count"] = len(region.regions(document, sk.id))
            sketches_out.append(sketch_entry)

        active = document.active_body()
        return {
            # AI は文書を開けない(`open_document` / `new_document` は `mcp=False`)ので、
            # False なら空の文書を相手に操作せず、人に開いてもらうよう伝える。
            "document_open": self.session.document_open,
            # 人は画面で文書を切り替えられる(issue #102)。名前は人に伝える用、世代は同一性の判定用
            # (名前は新規→新規や開き直しで一致する)。
            "current_name": self.session.current_name,
            "generation": self.session.generation,
            "units": document.units,
            "active_body_id": active.id if active is not None else None,
            "body_count": len(bodies),
            "visible_count": sum(1 for body in bodies if body["visible"]),
            "bodies": bodies,
            "sketches": sketches_out,
            "parameters": dict(document.parameters),
            "can_undo": self.session.can_undo,
            "can_redo": document.log.can_redo,
            # **`Session.applied_count` を使う**(`len(log.entries)` ではない)。ログの長さは
            # undoで減り、AIが見る数とブラウザが見る数がundoの後でずれる。
            "applied_count": self.session.applied_count,
            # 作業空間(issue #153)。人が画面で起動した verb と途中の引数、編集中のスケッチ。AI が「人と一緒に進める」ときの出発点。
            **self._workspace_payload(),
        }

    def set_visible(self, body_id: str, visible: bool) -> dict:
        """可視性を切り替える。**コアを経由する**(`Session.set_visible`)ので、文書の `Body.visible`
        が変わり、変更イベントが発火して開いているブラウザが描き直す。

        **verbではないので封筒に `verb` / `seq` を載せない**(契約2)。操作ログに積まれず
        undo / redo でも戻らない(理由は `duocad.selection.set_visible`)。隠したのを戻すには
        `set_visible(visible=True)`。存在しない `body_id` は KeyError(文書は変更しない)。
        """
        body_id = str(body_id)
        before = self.session.document.get_body(body_id)
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

    def set_sketch_visible(self, sketch_id: str, visible: bool) -> dict:
        """スケッチの可視性を切り替える(`set_visible` のスケッチ版。契約も封筒の形も同じ)。
        形は変わらないので `changed_bodies` は常に空。存在しない `sketch_id` は KeyError。
        """
        sketch_id = str(sketch_id)
        if not isinstance(visible, bool):
            raise ValueError(f"visible must be a boolean, got {type(visible).__name__}")
        applied = self.session.set_sketch_visible(sketch_id, visible)
        return {
            "change": "visibility",
            "sketch_id": sketch_id,
            "visible": applied["visible"],
            "previous": applied["previous"],
            "changed": applied["changed"],
            "changed_bodies": [],
        }

    def describe_shape(self, body_id: str) -> dict:
        body = self.session.document.get_body(str(body_id))
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

    def _revision_snapshot(self) -> dict[str, int]:
        return {
            body.id: self.session.body_revision(body.id)
            for body in self.session.document.bodies
        }

    def _body_summary(self, body) -> dict:
        """封筒の `bodies[]` 1要素(キーは `BODY_KEYS` の6つ)。`Session.body_meta` から、
        MCP封筒の契約に無い `revision` / `shape_revision`(画面の差分配信の印)を落とす。
        """
        meta = self.session.body_meta(body)
        del meta["revision"]
        del meta["shape_revision"]
        return meta

    def _verb_envelope(self, verb: str, seq: int, before: dict[str, int]) -> dict:
        """ログに1行積むツールの成功封筒。`changed_bodies` は `Session.body_revision` の前後差。"""
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

        verbごとに数え上げると、verbが増えたときに静かに漏れる(契約2)。`body_revision` は
        形状の同一性と可視性から版を導くので、押し出しでも `set_visible` でもこの1本で拾える。
        """
        after = self._revision_snapshot()
        return [
            body.id
            for body in self.session.document.bodies
            if before.get(body.id) != after.get(body.id)
        ]

    def _selection_summary(self) -> dict:
        """エラー封筒へ載せる選択の要約(`count` だけ)。"""
        current = selection.get_selection(self.session.document)
        return {"count": len(current.items)}

    def error_envelope(self, tool: str, error: Exception) -> dict:
        """例外を決定3の語彙へ畳む。MCPは先に弾かず、落ちたあとに名前を付ける。

        **公開API**: MCPとアプリ内エージェント(`duocad/agent/loop.py`)がどちらもこれを通るので、
        AIが見る失敗の形は経路によらず同じ(エージェント側で語彙を書き写すと片方だけ増えても気づけない)。
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
        # 形状最適化の実行中に文書を変えようとした。**引数を直しても通らない**ので `invalid_argument` に
        # 落とさない(AIが値を変えて打ち直し続ける)。待つか、人に中断を頼むしかない。
        if isinstance(error, DocumentBusy):
            return {
                **base,
                "code": "busy",
                "hint": "a shape optimization is running; wait until it finishes or the user cancels it",
            }
        if isinstance(error, (KeyError, FileNotFoundError)):
            return {**base, "code": "not_found"}
        # 選択由来の拒否は**コアが型で言ってくる**(`duocad/selection.py` の `require_*`)。
        # MCPは先に弾かず(同じ規則が2箇所に生まれ、片方だけ緩んでも気づけない)、落ちたあとに
        # 名前を付け、次の一手の材料(`required_kind` / `selection`)を足すだけ。
        # 「1つだけ要るのに複数」は `invalid_argument` のまま —— AIが打つ次の手は
        # 「選び直す」ではなく「減らす」で、`selection_empty` とは違う。
        #
        # **`SelectionKindMismatch` を先に見る**。`SelectionEmpty` の派生なので、順序を
        # 入れ替えると種別違いが `selection_empty` に吸われて `selection_kind_mismatch` が死ぬ。
        if isinstance(error, selection.SelectionKindMismatch):
            return {
                **base,
                "code": "selection_kind_mismatch",
                "required_kind": error.kind,
                # 実際に選ばれている種別と件数(`{"face": 3}`)。画面もモデルも数え直さない。
                "present_kinds": error.present,
                "hint": (
                    f"the selection is not empty but has no {error.kind}: "
                    f"select {error.kind}(s) before calling this verb"
                ),
                "selection": self._selection_summary(),
            }
        if isinstance(error, selection.SelectionEmpty):
            payload = {
                **base,
                "code": "selection_empty",
                "required_kind": error.kind,
                "selection": self._selection_summary(),
            }
            if error.kind in _HOW_TO_SELECT:
                payload["hint"] = _HOW_TO_SELECT[error.kind]
            return payload
        if isinstance(error, selection.SelectionAmbiguous):
            return {
                **base,
                "code": "invalid_argument",
                "required_kind": error.kind,
                "selection": self._selection_summary(),
            }
        # 起動中のコマンドが無い(`ValueError` の派生なので先に見る)。引数を直しても通らない —— 先に `begin_command`。
        if isinstance(error, NoCommand):
            return {**base, "code": "no_command", "hint": "begin_command(verb) で起動してから set_command_args / apply_command を呼ぶ"}
        if isinstance(error, ValueError):
            return {**base, "code": "invalid_argument"}
        return {**base, "code": "internal"}

    # ==================================================================================
    # ツールの契約(選択・成功封筒・失敗封筒)
    # ==================================================================================
    # 1. **形は選択から、数値は引数から。** 面・辺・領域を引数で受けるツールは1つも無い
    #    (`fillet(radius)` であって `fillet(edges, radius)` ではない)。Bodyだけは `body_id`
    #    引数で受ける —— Bodyは選択集合の項目ではない。選択から対象を取る実装は
    #    `duocad/selection.py` の `require_selected` / `require_edges` の1組だけ。
    # 2. **成功は封筒に入れて返す。** ログに1行積むツールは
    #    `verb` / `seq` / `changed_bodies` / `bodies` / `can_undo` / `can_redo` を必ず持つ。
    #    **ログに載らない変更**(選択・`set_visible`)は `verb` / `seq` を持たず、代わりに
    #    `change`("selection" / "visibility")で見分ける —— 持たせると、undoで戻ると読める
    #    封筒をAIに配ることになる。
    # 3. **失敗も封筒に入れて返す。** `{"error": {"code","message","tool","exception", ...}}`
    #    を `json.dumps` した本文 + `isError: true`。コードは次の語彙に限る
    #    (`not_found` / `invalid_argument` / `selection_empty` /
    #    `selection_kind_mismatch` / `invalid_predicate` / `busy` / `internal`)。**例外のテキストを
    #    素で返さない。** **`FileNotFoundError` を忘れない** —— パスの間違いは AI が直せる
    #    誤りなので、`internal` に落とすと諦める。
    def _as_selection_payload(self, selection: dict) -> dict:
        """選択5ツールの戻り値を `{"selection": {...}}` に揃える。"""
        return {"selection": selection}

    def get_selection(self) -> dict:
        """現在の選択を返す(読むだけ。発火しない)。
        戻り値は変更する4つと同じ `{"selection": {"items", "count"}}`。
        """
        return self._as_selection_payload(self.session.get_selection())

    def set_selection(self, refs: Optional[list] = None, predicate: Optional[dict] = None) -> dict:
        """選択を置き換える。`refs`(明示的な参照)か `predicate`(述語)のどちらか一方。

        §0のAI側の合格条件「引数を**述語**または明示的な参照で与えられ」がこの1つに載る。
        - 一致0件は**成功**(`count: 0`)。エラーにするとAIが述語の書式を疑い始める
        - 不正な述語は `invalid_predicate` で、`valid_ops` に `predicates.describe_ops()` を
          そのまま添える(AIが自力で直せるように)
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
        event = self.session.set_selection(refs=refs, predicate=predicate)
        return self._as_selection_payload(event["selection"])

    def modify_selection(self, add: Optional[list] = None, remove: Optional[list] = None) -> dict:
        event = self.session.modify_selection(add=add, remove=remove)
        return self._as_selection_payload(event["selection"])

    def toggle_selection(self, refs: list) -> dict:
        """参照ごとに、あれば外し無ければ足す(画面のクリックと同じ操作)。"""
        event = self.session.toggle_selection(refs)
        return self._as_selection_payload(event["selection"])

    def clear_selection(self) -> dict:
        event = self.session.clear_selection()
        return self._as_selection_payload(event["selection"])

    # ---------------------------------------------------------------- 作業空間(起動中のコマンド・モード。issue #153)
    # 選択と同じく **コアを経由する**(`Session.begin_command` 等)ので、人の画面が追随する。応答は `{"command", "mode"}`。
    def _workspace_payload(self) -> dict:
        state = self.session.workspace.to_state()
        return {"command": state["command"], "mode": state["mode"]}

    def get_command(self) -> dict:
        return self._workspace_payload()

    def begin_command(self, verb: str, args: Optional[dict] = None) -> dict:
        if verb in catalog.feature_command_names(self.hidden_features):
            raise ToolError("not_found", f"{verb!r} はこの版では使えない(docs/STABLE.md)")
        self.session.begin_command(verb, args)
        return self._workspace_payload()

    def set_command_args(self, args: dict) -> dict:
        self.session.set_command_args(args)
        return self._workspace_payload()

    def end_command(self) -> dict:
        self.session.end_command()
        return self._workspace_payload()

    def apply_command(self, args: Optional[dict] = None) -> dict:
        """構えた verb の確定。封筒は `_apply_verb` と同じ(ログに1行積むツールの形)。"""
        before = self._revision_snapshot()
        applied = self.session.apply_command(args)
        envelope = self._verb_envelope(applied["verb"], int(applied["seq"]), before)
        envelope.update(
            {key: value for key, value in applied.items() if key not in _ENVELOPE_OWNED_KEYS}
        )
        return envelope

    def enter_sketch(self, sketch_id: str) -> dict:
        self.session.enter_sketch(sketch_id)
        return self._workspace_payload()

    def leave_sketch(self) -> dict:
        self.session.leave_sketch()
        return self._workspace_payload()

    def enter_optimize(self) -> dict:
        self.session.enter_optimize()
        return self._workspace_payload()

    def leave_optimize(self) -> dict:
        self.session.leave_optimize()
        return self._workspace_payload()

    def describe_predicates(self) -> dict:
        """述語の語彙をAIへ自己記述する。**`predicates.describe_ops()` をそのまま出す**
        (opの定義を書き直すと二重管理になり、片方だけ増えたopが黙って通る)。

        戻り値: `{"kinds": [...], "ops": <describe_ops()>, "where_is": "and", "example": {...}}`。
        `where_is: "and"` と**ORが無いこと**まで出すのは、AIが無い機能を試して失敗するのを防ぐため
        (ORが要るときは2回評価して `modify_selection(add=...)`)。`example` は
        `predicates.validate()` を通る本物であること。
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
            "body": "省略可。1つのボディだけを対象にするときの body_id(省略時は見えているボディ全部)",
            "ops": predicates.describe_ops(),
            "where_is": "and",
            "example": example,
        }

    def measure(self) -> dict:
        """選択を測る(§5 `measure(selection)`)。**verbではないので提案のままでも通る**。
        実体は `Session.measure`(= `duocad/measure.py`)で、WebSocket の `measure` と同じ計算。
        """
        return self.session.measure()

    # ------------------------------------------------------------------ verb(ログに1行積むツール)
    def _apply_verb(self, verb: str, **arguments: Any) -> dict:
        """verb を掛けて成功封筒を返す。**専用メソッドを持たない verb のツールは全部ここを通る。**

        実体は `Session.apply`(`duocad/entry.py`)で、ブラウザがWebSocketから掛けるのと同じ1本。
        封筒には入口が返したもの(作ったものの ID・解決後の数値など)をそのまま足すが、
        **`changed_bodies` だけは入口の申告を使わず**、`Session.body_revision` の前後差から導く(契約2)。
        """
        before = self._revision_snapshot()
        applied = self.session.apply(verb, arguments)
        envelope = self._verb_envelope(applied["verb"], int(applied["seq"]), before)
        envelope.update(
            {key: value for key, value in applied.items() if key not in _ENVELOPE_OWNED_KEYS}
        )
        return envelope

    def import_svg(self, sketch_id: str, path: str, scale: Optional[float] = None) -> dict:
        """SVG の取り込み。**`hint` で領域の数を知らせる** —— SVG は1枚で複数の輪郭を持つのが普通で
        (文字の内側の穴、離れた図形)、黙って結果だけ返すと、AI は領域が1つだけのつもりで押し出しに進む。
        """
        arguments = {"sketch_id": sketch_id, "path": path}
        if scale is not None:
            arguments["scale"] = scale
        envelope = self._apply_verb("import_svg", **arguments)
        region_count = len(region.regions(self.session.document, str(sketch_id)))
        envelope["region_count"] = region_count
        envelope["hint"] = (
            f"領域が {region_count} 個ある。押し出す前に describe_document の "
            "include_regions で面積を見て、対象を set_selection で選ぶこと"
            "(文字の内側の穴も1つの領域として数える)"
        )
        return envelope

    # ------------------------------------------------------------------ 書き出し
    def export_step(self, path: str) -> dict:
        """STEPを書き出す。**ログに載らない**(文書を変えないので verb ではない)。

        対象は**いま表示中のBodyだけ**(引数で選べない)。`{"path", "body_ids", "skipped_hidden"}` を
        返す —— AIは自分でファイルを確かめられないので、**書けなかったときは必ず例外になる**
        (半端なファイルも0バイトの成果物も残さない。`kernel.write_step` / `meshbody.write_stl`)。
        """
        return self._export_file_response(self.session.export_step_file(path))

    def export_stl(self, path: str) -> dict:
        """STLを書き出す。粗さは `io_step.STL_DEVIATION` 固定(人の画面にも入力欄は無い)。
        対象は export_step と同じ(いま表示中のBodyだけ・引数で選べない)。"""
        return self._export_file_response(self.session.export_stl_file(path))

    @staticmethod
    def _export_file_response(payload: dict) -> dict:
        """`Session.export_*_file` の応答から `action` だけ落とす(WebSocket専用の印)。"""
        return {key: value for key, value in payload.items() if key != "action"}

    # ------------------------------------------------------------------ 履歴
    def undo(self) -> dict:
        """**UIから見た履歴と同じもの**を戻す(AI用の別履歴を作らない)。取り消せるかの判定は
        `Session.can_undo` が唯一の出どころで、`describe_document` の答えと実際に通るかが一致する。
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

    **トランスポートを知らない**ので、stdioを立てなくてもテストから直接叩ける。`handle` が None を
    返したら「返事を書いてはいけない」(通知・レスポンスの受信)という意味。
    """

    def __init__(self, tools: DocumentTools) -> None:
        self.tools = tools
        #: このクライアントが最後に `describe_document` で見た文書の世代(issue #102)。None = まだ見ていない(照合しない)。
        #: **インスタンスに1つ**: HTTP の全クライアントがこれを共有するので、2つ繋ぐと片方の確認でもう片方も通る
        #: (`Mcp-Session-Id` 未実装。限界は `MCP.md`「実装のメモ」)。
        self._seen_generation: Optional[int] = None

    # ------------------------------------------------------------------ 入口
    def handle(self, message: Any) -> Optional[dict]:
        """1メッセージを処理して返すレスポンス(通知なら None)。

        **例外を外へ出さない** — プロトコル層で落ちるとMCPクライアントから見てサーバが黙って
        死んだのと同じになるため、想定外の例外も内部エラーのレスポンスへ畳む。
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
        # 何も控えない(`notifications/initialized` を含め、握手の状態を使う処理が無い)。
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
        return {
            "protocolVersion": version,
            # ツールしか持たず、**変更通知は出さない**(顔ぶれは起動中変わらない)ので
            # listChanged は宣言しない。
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            # **アプリ内エージェント(`duocad/agent/`)と同じ文**。手順を2箇所に書くと
            # 片方だけ直った状態に気づけないので、宣言側(catalog.py)の1本から読む。
            "instructions": catalog.agent_instructions(getattr(self.tools, "hidden_features", frozenset())),
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
        # **MCP はプロトコルエラー(-32602)で先に弾く**。`DocumentTools.call` にも同じ検査があるが、
        # そちらは `isError` の封筒になる(アプリ内AIの経路の門)。
        problem = catalog.check_arguments(descriptor["inputSchema"], arguments)
        if problem is not None:
            raise _RpcError(INVALID_PARAMS, problem)

        try:
            # stdout は起動時に `_claim_stdout` が fd ごと stderr へ退避済みなので、
            # ここで囲み直さない(囲んでも stderr を stderr に振り替えるだけになる)。
            # 切り替えの後は `describe_document` 以外を止める。解くのは `describe_document` だけ(止めた時点で解くと、
            # 確かめずに打ち直した verb が新しい文書に効く)。
            looks = name == "describe_document"
            payload = self.tools.call(
                name, arguments, expected_generation=None if looks else self._seen_generation
            )
            if looks:
                self._seen_generation = payload["generation"]
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


def _exception_message(error: Exception) -> str:
    """例外の本文。`KeyError` の `str()` が付ける余分な引用符を避ける。"""
    if isinstance(error, KeyError) and error.args:
        return str(error.args[0])
    return str(error)


def _claim_stdout() -> Any:
    """JSON-RPC専用の書き口を確保し、**プロセスの fd 1 を fd 2 へ振り替える**。

    OCCTのSTEP書き出しはC++側から実fdへ統計(`****** Statistics on Transfer (Write) ******`)を吐き、
    Python層の `contextlib.redirect_stdout` では止まらない。ツール実行の間だけ囲む形では、
    **人がブラウザからSTEPを書き出す経路**(ツール実行ではない)を取り逃し、JSON-RPCの行の途中に
    割り込んで接続が壊れる(人とAIが同じプロセスを同時に触る形なので普通に起きる)。

    確保したdupを返し、fd 1 は stderr を指すようにする。以後、誰がどの経路で標準出力へ
    書いてもstderrへ流れ、JSON-RPCは返した書き口だけを通る。
    """
    sys.stdout.flush()
    saved = os.dup(1)
    os.dup2(2, 1)
    return os.fdopen(saved, "wb", buffering=0)


async def serve_stdio(server: McpServer, reader: Any = None, writer: Any = None) -> None:
    """標準入出力でMCPを喋る(1メッセージ1行・UTF-8・改行区切り)。EOFで戻る。

    `readline` はブロックするので実行器スレッドへ逃がす(Windowsのイベントループはパイプの
    非同期読み取りに癖があるため、素直にスレッドで読むほうが壊れにくい)。
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


def _log(message: str) -> None:
    """**stderrへ**出す(stdoutはJSON-RPC専用)。"""
    print(message, file=sys.stderr, flush=True)


#: `npm run dev` が使うポート(README §1.3)。dev serverは `web/src` を直に配るので
#: 静的配信(`--http-port`)とは別オリジンになる。
VITE_DEV_PORT = 5173


def allowed_origins(http_port: int) -> list:
    """WebSocketを開いてよい `Origin` の許可リスト。

    WebSocketは**同一オリジンポリシーの対象外**で、任意のWebページが `ws://127.0.0.1:<port>` へ
    繋いで読み書きできる。`127.0.0.1` へのbindは防御にならない(攻撃側のコードは利用者の
    ブラウザの中で動く)。検査が無い間は、duocadを立てたまま別のサイトを開くだけで、そのページが
    文書を読み・壊し・`export_step` で任意のパスへ書き・`ai_chat` で課金を消費できた。

    **`None`(Originヘッダ無し)を許可する**のは意図的。ブラウザは必ず `Origin` を送り
    JavaScriptから偽装できないので、ブラウザ経由の攻撃はこれで塞がる。一方 `smoke*.mjs` や素のWS
    クライアントはヘッダを送らず、拒否すると自分の検査だけが壊れる。素のWSを立てられる相手は
    既にこのPCでコードを実行できており、この防御線の外側にいる。

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
    hidden = hidden_features(args.channel)
    server = Server(session, hidden_features=hidden)
    server.bind_loop(asyncio.get_running_loop())
    # **同じ `DocumentTools`(= 同じ `Session`)をMCPとアプリ内AIの両方へ渡す**。
    # 状態を持たない薄いラッパなので共有して問題なく、別々に作る理由が無い。
    tools = DocumentTools(session, hidden_features=hidden)
    server.bind_agent_tools(tools)

    ws_url = f"ws://127.0.0.1:{args.port}"
    page_url = f"http://127.0.0.1:{args.http_port}/"
    mcp_url = f"http://127.0.0.1:{args.http_port}{MCP_HTTP_PATH}"
    # **HTTPトランスポート用のMCPは別インスタンスにする**。別のトランスポートに繋いだ相手は
    # 別のクライアントなので握手も別に持つ(`McpServer` は握手の状態を控えないが、
    # 控えるようになっても混ざらない形にしておく)。道具は `ToolsOnLoop` で包む
    # (HTTPはワーカースレッドで走るため。下記 `start_http_server`)。
    http_mcp = McpServer(ToolsOnLoop(tools, asyncio.get_running_loop()))
    loop = asyncio.get_running_loop()
    #: 安定版の更新が始まったら立つ。`--no-stdio` の待ちがこれで解けてプロセスが終わる。
    stop = asyncio.Event()
    updater = _stable_updater(session, server, args, loop, stop)
    try:
        start_http_server(
            args.web_dist,
            args.http_port,
            ws_url,
            mcp=http_mcp,
            config=describe(args.channel, updater),
            updater=updater,
        )
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
        if args.no_stdio:
            await stop.wait()
            _log("duocad MCP server: stopping for update, bye")
            return
        await serve_stdio(McpServer(tools))
    _log("duocad MCP server: stdin closed, bye")


def _stable_updater(
    session: Session,
    server: Server,
    args: argparse.Namespace,
    loop: asyncio.AbstractEventLoop,
    stop: asyncio.Event,
) -> Optional[StableUpdater]:
    """安定版(`--channel stable --no-stdio`)だけ更新の口を開く(`duocad/channel.py`)。

    **`--no-stdio` を条件にする**のは、stdioで動いている間は終わり方が無いため ——
    `serve_stdio` の `readline` は実行器スレッドで塞がっており、`asyncio.run` の後始末が
    そのスレッドを待って止まる。安定版は `duocad.launcher` が必ず `--no-stdio` で立てる。
    """
    if args.channel != "stable" or not args.no_stdio or args.data_dir is None:
        return None

    async def blocker() -> Optional[str]:
        # 形状最適化とAIの実行は既定の実行器で走る。`asyncio.run` の後始末はそれを時間制限なしで
        # 待つので、走っている間に止めるとプロセスが終わらず、update.ps1 が強制終了して結果を捨てる。
        if session.optimizing:
            return "形状最適化が走っている。終わるか中断してから更新すること"
        if getattr(server, "_ai_running", False):
            return "AIが実行中。終わるか止めてから更新すること"
        if not session.saved:
            return "自動保存が書けていない文書がある。Ctrl+S で保存してから更新すること"
        return None

    def update_blocker() -> Optional[str]:
        # `Session` を触るのはイベントループのスレッドだけ(`ToolsOnLoop` と同じ不変条件)。
        return asyncio.run_coroutine_threadsafe(blocker(), loop).result(timeout=5)

    def on_started() -> None:
        # HTTPの応答(202)を書き終える猶予を置いてから止める。
        loop.call_soon_threadsafe(loop.call_later, 0.5, stop.set)

    return StableUpdater(
        log_dir=args.data_dir / "logs",
        relaunch={
            "channel": args.channel,
            "port": args.port,
            "http_port": args.http_port,
            "data_dir": args.data_dir,
        },
        update_blocker=update_blocker,
        on_started=on_started,
    )


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
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
    parser.add_argument(
        "--exports-dir",
        type=Path,
        default=None,
        help="STEP/STL書き出しの置き場(既定はリポジトリ直下の export/)",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help=(
            "制作データの置き場。models/ と export/ をこの下に取る(個別の --models-dir / "
            "--exports-dir が優先)。安定版はリポジトリの外を指す(docs/STABLE.md)"
        ),
    )
    parser.add_argument(
        "--channel",
        choices=CHANNELS,
        default="dev",
        help=(
            "dev(既定) / stable。画面のヘッダーに「開発版」の札が出るかと、安定版の更新ボタンが"
            "出るかを決める。**既定を dev にしてある** —— 付け忘れたときに札が出ない側へ倒れると、"
            "開発版を安定版と取り違えたまま制作してしまう"
        ),
    )
    parser.add_argument(
        "--no-stdio",
        action="store_true",
        help=(
            "標準入力でMCPを喋らない(窓の無い pythonw から立てる安定版用。stdinが無いと"
            "stdioのMCPは即EOFで終わり、サーバごと落ちる)"
        ),
    )
    args = parser.parse_args(argv)
    if args.data_dir is not None:
        args.data_dir = args.data_dir.expanduser().resolve()
        if args.models_dir is None:
            args.models_dir = args.data_dir / "models"
        if args.exports_dir is None:
            args.exports_dir = args.data_dir / "export"
    return args


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)

    # **何も開いていない状態(トップ画面)で起動する**。人が画面で「新規」か既存の文書を選ぶまで、
    # 文書を変える操作は断られる(`Session.NoDocument`)。
    session = Session(
        args.deviation,
        models_dir=args.models_dir,
        exports_dir=args.exports_dir,
        opened=False,
    )
    try:
        asyncio.run(_run(session, args))
    except KeyboardInterrupt:
        _log("bye")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
