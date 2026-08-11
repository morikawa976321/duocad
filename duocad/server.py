"""WebSocketサーバ + `web/dist` の静的配信(実装計画書§2「コマンドは下り、イベントは上り」)。

アプリ層(ブラウザ)とコアの唯一の接点。**コアはこのサーバが無くても機能的に完全**であり、
ここには「文書を持つ」以外の機能を置かない(アプリ層にしか無い機能を作った瞬間AIから使えなくなる。
§2「外してはいけない3つ」の1)。

起動(1コマンド):
    .venv\\Scripts\\python.exe -m duocad.server

    --step PATH        起動時に読み込むSTEP(既定: 3d-models/shelf_attachment_GD.step)
    --port 8765        WebSocketのポート
    --http-port 8766   静的配信のポート(ブラウザで開くのはこちら)
    --deviation 0.2    テセレーションの線形偏差(mm・絶対)
    --measure-log PATH 計測ログの追記先(既定: <リポジトリ直下>/measure_log.jsonl)
    --no-open          既定ではブラウザを開く。開かせたくないとき(自動テスト)に付ける

プロトコル(JSON文字列):

    下り(client -> server)
        {"cmd": "get_state"}
        {"cmd": "extrude_face", "face_index": int, "distance": float, "client_op": int, "body_id": str}
        {"cmd": "set_visible", "body_id": str, "visible": bool, "client_op": int}
        {"cmd": "undo"} / {"cmd": "redo"}
        {"cmd": "measure_record", "record": {...}}    計測1行の追記(下記)

    上り(server -> client)
        {"type": "state", "state": {...}, "delta": {"bodies": [...]}, "applied": {...}|None}
        {"type": "error", "message": str, "where": str}
        {"type": "warning", "message": str}

    **M3.5パーツ一覧UI(2026-08-11)で `state` は軽量化した** — `state.bodies` は
    `geometry` を含まない(name/kind/visible/face_count/volume/revisionのみ、27部品でも数KB)。
    形状は `delta.bodies`(接続ごとに版が変わったBodyだけ、`geometry` 付き)で届く。
    毎回27部品ぶんのテセレーション結果(実測6MB超)を送り直すと、パーツ分解でサーバ側を
    速くした意味がクライアント側の描画で消える(§2「外してはいけない3つ」)。
    接続直後は「知っている版」が空なので `delta.bodies` に全Bodyが入り、初期描画が揃う。

**状態変更は接続している全クライアントへ配信する**(上り方向。これが無いとM4の交互操作が
成立しない。今は人間1人だが、経路は今作っておく)。操作を出したクライアントにだけ `applied`
を付け、それ以外には同じ `state` だけを送る。

**M3.5(2026-08-11)で `state` は部品ごとの配列になった**。契約は
`tests/test_incremental_update.py` と tests/README.md の「M3.5」節が正:

    state() -> {
      "units", "active_body_id",
      "bodies": [{"body_id","name","kind","visible","revision",
                  "face_count","volume","geometry": <tessellate結果|None>}, ...],
      "log", "can_undo", "can_redo", "applied_count", "measure_log",
      "selection": {"items": [...], "origin": "user"|"proposed", "count": int},   # A(下記)
    }
    delta(known_revisions) -> {"bodies": [<上と同じ形・版が変わった分だけ>],
                               "unchanged": [BodyId, ...]}

**A(2026-08-11)で `state` に `selection` が入る**(契約は `tests/test_selection.py` と
tests/README.md の「A」節が正。**A1の時点ではまだ載っていない** —— A2が入れる)。
`_on_core_change` はMCP起点の変更から `applied` を落として配るので、**選択が `state` に
載っていないとAIが書き換えた選択はブラウザへ届かない**(`set_visible` の効果が
`bodies[].visible` で届いているのと同じ場所)。選択の変更イベントは
`{"change": "selection", "selection": {...}, "client_op": ...}` の形で、verbの `applied`
とはキーで区別する(選択はverbではなく、操作ログにも載らない)。

**計測ログ(measure_log.jsonl)はサーバが書く**。時刻はブラウザの単調時計
(`performance.now()`)が正で、区間のms換算はここで一括して行う(クライアントとサーバで
区間の定義が食い違わないようにするため)。5つの時刻のどれかが欠けた行は**受け付けない**
(黙って壊れた計測を貯めない。§6予防6)。時刻の定義は MEASURE.md が正。

**OCPを直接importしない**(境界はkernel.pyだけ。tests/test_boundaries.py が検査する)。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import threading
import time
import webbrowser
from datetime import datetime
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

from websockets.asyncio.server import serve

from . import io_step, kernel, query
from .document import Document
from .verbs import extrude_face, make_editable_copy
from .verbs import set_visible as set_visible_verb

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STEP = Path(r"C:\Users\tm976\mf\3d-models\mATX_PC_case_20251122\PC_case_20251122.step")
DEFAULT_WEB_DIST = REPO_ROOT / "web" / "dist"
DEFAULT_MEASURE_LOG = REPO_ROOT / "measure_log.jsonl"

#: 計測1行に必須の時刻(欠けたら受け付けない)。意味は MEASURE.md が正。
MEASURE_TIMESTAMPS = (
    "t_command_start",
    "t_face_picked",
    "t_value_entered",
    "t_committed",
    "t_geometry_updated",
)


class Session:
    """1つの文書を持つサーバ側の状態。WebSocketの接続とは独立(接続が切れても文書は残る)。"""

    def __init__(
        self,
        step_path: Optional[Path],
        deviation: float,
        measure_log_path: Path,
    ) -> None:
        self.document = Document()
        self.deviation = float(deviation)
        self.measure_log_path = Path(measure_log_path)
        self.warnings: list[str] = []
        self._server_ms: dict[int, float] = {}
        self._applied_count = 0
        self._revision_state: dict = {}    # body_id -> (id(shape), visible) 直近の状態
        self._revision_counter: dict = {}  # body_id -> 現在の版(int)
        self._geometry_cache: dict = {}    # body_id -> (shape, tessellate結果)
        self._setup_floor_seq = 0  # load_step が積んだ最後のseq(これ以下はundoの対象外)
        self._listeners: list = []  # 文書が変わったときに呼ぶ購読者(下記 add_listener)
        if step_path is not None:
            self.load_step(step_path)

    # ---------------------------------------------------------------- 変更イベント(上り方向)
    def add_listener(self, callback: Any) -> None:
        """文書が変わったら `callback(applied: dict)` で知らせる(M4・2026-08-11)。

        **コアが変更を発火し、アプリ層が購読して描き直す**(実装計画書§2「外しては
        いけない3つ」の2番目)。これが無いと、**WebSocket経由以外の主体**——MCPサーバ
        (`duocad/mcp_server.py`)からAIが文書を変えたとき——にブラウザが何も知らないまま
        古い絵を出し続け、交互操作が原理的に成立しない。

        購読者は `Server`(WebSocket)。1つの操作につき1回だけ呼ばれる。
        """
        self._listeners.append(callback)

    def remove_listener(self, callback: Any) -> None:
        """購読をやめる。登録されていなければ ValueError(黙って空振りさせない)。

        文書は接続より長生きするので、`Server` を作り直す運用(テスト・再起動)で
        古い購読者が残ると、既に閉じたループへ配信しようとして警告が溜まる。
        """
        self._listeners.remove(callback)

    @property
    def can_undo(self) -> bool:
        """次のundoが通るか。**土台(部品の読み込み・編集用ボディ)は取り消せない**。

        `state_meta()` と MCP の `describe_document` が同じ答えを返すための唯一の出どころ
        (2026-08-11のQA指摘: MCP側が `document.log.can_undo` を素で見ていたため、
        土台の直後に「undoできる」と答えてしまい、実際に呼ぶと拒否される食い違いがあった)。
        """
        return self.document.log.can_undo and not self._at_setup_floor()

    def _emit(self, applied: dict) -> dict:
        """購読者へ変更を知らせて `applied` をそのまま返す(呼び出し側は `return self._emit(...)`)。

        **1人の購読者が落ちても他へ配り、操作自体は成功のままにする** — 通知の失敗で
        既に確定した文書の変更を巻き戻すことはできないため。落ちたことは警告として残す
        (黙って握りつぶさない。§6予防6)。
        """
        for callback in list(self._listeners):
            try:
                callback(applied)
            except Exception as error:
                self.warnings.append(f"変更イベントの配信に失敗: {type(error).__name__}: {error}")
        return applied

    # ---------------------------------------------------------------- 文書の組み立て
    def load_step(self, path: Path) -> list:
        """STEPを読み、ソリッドごとに参照ボディ+編集用コピーを作る。

        参照ボディは編集用コピーを作った直後に `set_visible(..., False)` で隠す。
        **verb経由にしている**(直接 `body.visible = False` を代入しない) — そうしないと、
        保存したログ(`document.log.to_json()`)を再生したときに「参照を隠した」という
        操作が失われ、参照・編集用の両方が可視のまま復元されてしまう(2026-08-11のQA指摘)。
        同じ形が二重にテセレーション・二重に送信されるのを避けるため
        (1ソリッドにつき同時に見えるのは1つ、が契約)。

        **土台(undoの対象外にする範囲)はverb名ではなくseqの下限で区切る**
        (`_setup_floor_seq`。`load_step` が積んだ最後のseq)。verb名で「末尾が
        `import_step`/`make_editable_copy`/`set_visible` のどれか」と判定する実装だと、
        `set_visible` はこの後(将来のMCP等から)ユーザー操作としても呼ばれうるため、
        文書に対して1回でも呼ぶと末尾が `set_visible` になって以後の `undo` が誤って
        拒否される(2026-08-11のQA指摘)。seqの下限なら、土台の後に積まれた操作だけが
        正しくundoできる。
        """
        reference_ids = io_step.import_step(self.document, str(path))
        editable_ids = []
        for reference_id in reference_ids:
            editable_id = make_editable_copy(self.document, reference_id)
            set_visible_verb(self.document, reference_id, False)
            editable_ids.append(editable_id)
        entries = self.document.log.entries
        self._setup_floor_seq = entries[-1].seq if entries else 0
        return editable_ids

    def _at_setup_floor(self) -> bool:
        """次のundoが文書の土台(部品の読み込み・編集用ボディ)に当たるか。"""
        entries = self.document.log.entries
        return bool(entries) and entries[-1].seq <= self._setup_floor_seq

    def body(self):
        """編集対象のボディ(アクティブ)。無ければ RuntimeError。"""
        body = self.document.active_body()
        if body is None:
            raise RuntimeError("文書にボディが無い(--step を指定して起動する)")
        return body

    # ---------------------------------------------------------------- 状態
    def state(self) -> dict:
        """アプリ層へ渡す全状態。bodiesは配列(部品ごと)。**geometryを含む(重い)**。

        WebSocket配信(`Server._push`)はこれを毎回は使わない — `state_meta()` + `delta()`
        を組み合わせて、版が変わったBodyの `geometry` だけを送る(モジュールdocstring参照)。
        `state()` はテストと `get_state` の全量確認用に残す。
        """
        meta = self.state_meta()
        meta["bodies"] = [self._body_state(body) for body in self.document.bodies]
        return meta

    def state_meta(self) -> dict:
        """`state()` の軽量版。**bodiesの各要素にgeometryを含まない**(テセレーションを呼ばない)。

        パーツ一覧(名前・面数・可視性)のように形状そのものが要らない用途、および
        WebSocket配信のたびに送る「土台」の情報に使う。27部品でも数KB(state()は6MB超)。
        """
        active = self.document.active_body()
        return {
            "units": self.document.units,
            "active_body_id": active.id if active is not None else None,
            "bodies": [self._body_meta(body) for body in self.document.bodies],
            "log": [
                {"seq": operation.seq, "verb": operation.verb, "args": operation.args}
                for operation in self.document.log.entries
            ],
            "can_undo": self.can_undo,
            "can_redo": self.document.log.can_redo,
            "applied_count": self._applied_count,
            "measure_log": str(self.measure_log_path),
        }

    # ---------------------------------------------------------------- M3.5: 差分送信
    def body_revision(self, body_id: str) -> int:
        """そのBodyの版。**版は「経路」ではなく「状態」から導く** — 形状の同一性(id)とvisible
        のタプルが前回の記録と違ったら+1する。押し出し・undo・redo・可視性変更のどれでも
        自動的に効く(verbごとに数え上げない)。
        """
        body = self._find_body(body_id)
        key = (id(body.shape), body.visible)
        if self._revision_state.get(body_id) != key:
            self._revision_state[body_id] = key
            self._revision_counter[body_id] = self._revision_counter.get(body_id, 0) + 1
        return self._revision_counter[body_id]

    def geometry(self, body_id: str) -> dict:
        """1Bodyのテセレーション結果(キャッシュ付き)。不可視Bodyは ValueError。

        キャッシュは**形状の同一性**で判定する(版ではない)。可視性だけを変えたときに
        測り直すと、表示を切り替えるたびに高コストな再測定が走ってしまう。
        """
        body = self._find_body(body_id)
        if not body.visible:
            raise ValueError(f"body {body_id!r} is not visible: 不可視Bodyは測らない")
        cached = self._geometry_cache.get(body_id)
        if cached is not None and cached[0] is body.shape:
            return cached[1]
        result = kernel.tessellate(body.shape, self.deviation)
        if result["untriangulated"]:
            self.warnings.append(
                f"三角形分割が取れなかった面({body.name}): {result['untriangulated']}(画面に出ません)"
            )
        self._geometry_cache[body_id] = (body.shape, result)
        return result

    def delta(self, known_revisions: dict) -> dict:
        """クライアントが持っている版との差分だけを返す(未知のIDは必ずbodiesへ)。

        戻り値:
            {
              "bodies":    [<state()の bodies と同じ形。ただし版が変わったBodyだけ>],
              "unchanged": [BodyId, ...],   # 版が一致したBody
            }

        `known_revisions` に無いBodyId(＝クライアントが知らないBody)は必ず `bodies` に入る。
        逆に、クライアントが知っているのに `bodies` にも `unchanged` にも現れないIDは
        **文書から消えた**ことを意味する(undoでの消滅がこれ。専用の "removed" は持たない)。
        """
        bodies_out = []
        unchanged = []
        for body in self.document.bodies:
            current = self.body_revision(body.id)
            if known_revisions.get(body.id) == current:
                unchanged.append(body.id)
            else:
                bodies_out.append(self._body_state(body))
        return {"bodies": bodies_out, "unchanged": unchanged}

    def _find_body(self, body_id: str):
        body = next((b for b in self.document.bodies if b.id == body_id), None)
        if body is None:
            raise KeyError(f"no such body: {body_id!r}")
        return body

    def _body_meta(self, body) -> dict:
        """1Bodyぶんの軽量な状態(geometryを含まない。state_metaとの共有部分)。"""
        return {
            "body_id": body.id,
            "name": body.name,
            "kind": body.kind,
            "visible": body.visible,
            "revision": self.body_revision(body.id),
            "face_count": kernel.face_count(body.shape),
            "volume": query.volume(body.shape),
        }

    def _body_state(self, body) -> dict:
        """1Bodyぶんの状態(stateとdeltaが共有する)。`_body_meta` にgeometryを足したもの。"""
        meta = self._body_meta(body)
        meta["geometry"] = self.geometry(body.id) if body.visible else None
        return meta

    # ---------------------------------------------------------------- 操作
    def extrude(
        self,
        face_index: int,
        distance: float,
        client_op: Optional[int] = None,
        body_id: Optional[str] = None,
    ) -> dict:
        """面を押し出す。サーバ側の所要msを控えて `applied` を返す。

        M3.5で `body_id` を足した(STEPが部品ごとに分解されるので、どのBodyの面かを
        指定できないと27部品のうち1つを選べない)。既定はアクティブボディ。
        `applied` は `"changed_bodies": [BodyId, ...]` を持つ(差分送信の対象)。
        """
        started = time.perf_counter()
        operation = extrude_face(self.document, int(face_index), float(distance), body_id=body_id)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        self._applied_count += 1
        if client_op is not None:
            self._server_ms[int(client_op)] = round(elapsed_ms, 3)
        provenance = operation.provenance or {}
        changed_body_id = operation.args["body_id"]
        return self._emit({
            "verb": operation.verb,
            "seq": operation.seq,
            "client_op": client_op,
            "body_id": changed_body_id,
            "face_index": int(face_index),
            "distance": float(distance),
            "after_face_index": provenance.get("after_face_index"),
            "server_ms": round(elapsed_ms, 3),
            "changed_bodies": [changed_body_id],
        })

    def set_visible(self, body_id: str, visible: bool, client_op: Optional[int] = None) -> dict:
        """パーツ一覧からの表示・非表示切り替え。**コアの `set_visible` verbを呼ぶだけ**
        (アプリ層に可視性の状態を持たせない。実装計画書§2)。

        呼ぶたびに操作ログへ1行積まれるので、undoで戻る。存在しない `body_id` は
        `KeyError`(verb側の契約どおり。document・ログとも変更しない)。
        """
        operation = set_visible_verb(self.document, body_id, bool(visible))
        self._applied_count += 1
        return self._emit({
            "verb": operation.verb,
            "seq": operation.seq,
            "client_op": client_op,
            "body_id": body_id,
            "visible": bool(visible),
            "changed_bodies": [body_id],
        })

    # ---------------------------------------------------------------- A1: 共有される選択集合
    #
    # **`set_visible` と同じ上り方向に乗せる**(別系統を新設しない)。
    # 変更 → `Session._emit` → `add_listener` の購読者(`Server._on_core_change`) → 全接続へ配信。
    #
    # ただし `set_visible` の効果がブラウザへ届くのは `applied` ではなく
    # **`state_meta()["bodies"][].visible`** 経由である(MCP起点の変更では `applied` を
    # 落として配るため。`Server._on_core_change` のdocstring)。選択も同じで、
    # **`state_meta()` に `"selection"` を載せない限りAIが書き換えた選択は画面へ届かない**。
    # A2はイベントと状態の両方を通すこと(片方だけだと交互操作が片道になる)。
    #
    # 発火するイベントの形(verbの `applied` とはキーで区別する。選択はverbではない):
    #     {"change": "selection",
    #      "selection": {"items": [...], "origin": "user"|"proposed", "count": int},
    #      "client_op": int | None}
    # `selection` を丸ごと載せるのは、**UIが描き直すのに足る情報**(何が選択されたか・
    # 提案か確定か)をイベント単体で持たせるため(項番13)。
    #
    # **戻り値**: 変更する4つは `set_visible` と同じく `self._emit(...)` の戻り —— つまり
    # **発火したイベントそのもの**を返す(`{"change","selection","client_op"}`)。
    # `get_selection` だけは発火しないので `Selection.to_dict()`
    # (`{"items","origin","count"}`)を返す。**この2つは形が違う**ので、
    # 呼び出し側は `["selection"]` を挟むかどうかを取り違えないこと。
    def get_selection(self) -> dict:
        """現在の選択集合を素の値で返す(`Selection.to_dict()`)。発火はしない。"""
        raise NotImplementedError("A2で実装する(スタブ)")

    def propose_selection(
        self,
        refs: Optional[list] = None,
        predicate: Optional[dict] = None,
        client_op: Optional[int] = None,
    ) -> dict:
        """AIの提案として選択を置き換え、変更イベントを発火して**そのイベント**を返す。"""
        raise NotImplementedError("A2で実装する(スタブ)")

    def modify_selection(
        self,
        add: Optional[list] = None,
        remove: Optional[list] = None,
        client_op: Optional[int] = None,
    ) -> dict:
        """人の差分編集。足し引きして確定にし、変更イベントを発火して**そのイベント**を返す。"""
        raise NotImplementedError("A2で実装する(スタブ)")

    def commit_selection(self, client_op: Optional[int] = None) -> dict:
        """提案を確定する(中身は変えない)。変更イベントを発火して**そのイベント**を返す。"""
        raise NotImplementedError("A2で実装する(スタブ)")

    def clear_selection(self, client_op: Optional[int] = None) -> dict:
        """選択を空にする。変更イベントを発火して**そのイベント**を返す。"""
        raise NotImplementedError("A2で実装する(スタブ)")

    def undo(self) -> dict:
        """直前の操作を取り消す。**文書の土台を作った操作は取り消させない**。

        計測の手順は「10回押して10回戻す」なので、1回多く押すと編集用ボディを作った操作まで
        取り消され、`active_body` が消えて以後の画面更新が全部落ちる(2026-08-10のQA指摘)。
        土台はundoの対象外にして、理由を画面へ返す。
        """
        if not self.document.log.entries:
            raise ValueError("取り消せる操作が無い")
        if self._at_setup_floor():
            raise ValueError(
                f"{self.document.log.entries[-1].verb} は取り消せない"
                "(部品の読み込みと編集用ボディは計測の土台)"
            )
        operation = self.document.log.undo(self.document)
        if operation is None:
            raise ValueError("取り消せる操作が無い")
        return self._emit({"verb": "undo", "undone": operation.verb, **_selection_hints(operation)})

    def redo(self) -> dict:
        operation = self.document.log.redo(self.document)
        if operation is None:
            raise ValueError("やり直せる操作が無い")
        return self._emit({"verb": "redo", "redone": operation.verb, **_selection_hints(operation)})

    # ---------------------------------------------------------------- 計測
    def append_measure_record(self, record: dict) -> dict:
        """計測1行を `measure_log.jsonl` へ追記する(区間のms換算はここで行う)。

        `kind="extrude"` の行は5つの時刻が全部あることを要求する。欠けていれば例外にして
        **書かない**(壊れた計測データは無いより悪い)。`kind="pick_miss"` は時刻1つでよい。
        """
        if not isinstance(record, dict):
            raise ValueError("record must be a JSON object")
        line: dict[str, Any] = dict(record)
        line["ts"] = datetime.now().isoformat(timespec="milliseconds")
        kind = line.get("kind", "extrude")
        line["kind"] = kind

        if kind == "extrude":
            missing = [
                name
                for name in MEASURE_TIMESTAMPS
                if not isinstance(record.get(name), (int, float))
            ]
            if missing:
                raise ValueError(f"計測の時刻が欠けている: {missing}")
            line.update(_intervals(record))
            server_ms = self._server_ms.pop(int(record.get("op", -1)), None)
            if server_ms is not None:
                line["server_extrude_ms"] = server_ms

        self.measure_log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.measure_log_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(line, ensure_ascii=False) + "\n")
        return line

    def take_warnings(self) -> list[str]:
        """溜まった警告を取り出して空にする(出したら消す)。"""
        warnings, self.warnings = self.warnings, []
        return warnings


def _selection_hints(operation: Any) -> dict:
    """undo/redo のあとにアプリ層がどの面を選び直せばよいかを返す。

    面の添字は形状が変わると壊れる(実装計画書§7)ので、**取り消したら押し出す前の添字へ、
    やり直したら後継の添字へ**戻す。これを渡さないと、undo後も後継の添字を選んだままになり、
    次の押し出しが別の面に当たる(2026-08-10のQA指摘)。
    """
    provenance = operation.provenance or {}
    return {
        "seq": operation.seq,
        "body_id": operation.args.get("body_id"),
        "face_index": operation.args.get("face_index"),
        "after_face_index": provenance.get("after_face_index"),
    }


def _intervals(record: dict) -> dict:
    """各区間の所要ms。**`ms_total` が Fusion360 との比較対象**(MEASURE.md 参照)。"""
    command_start = float(record["t_command_start"])
    face_picked = float(record["t_face_picked"])
    value_entered = float(record["t_value_entered"])
    committed = float(record["t_committed"])
    geometry_updated = float(record["t_geometry_updated"])
    return {
        "ms_to_pick": round(face_picked - command_start, 3),
        "ms_to_value": round(value_entered - face_picked, 3),
        "ms_to_commit": round(committed - value_entered, 3),
        "ms_to_geometry": round(geometry_updated - committed, 3),
        "ms_total": round(geometry_updated - command_start, 3),
    }


# --------------------------------------------------------------------------------------
# 静的配信(web/dist)
# --------------------------------------------------------------------------------------
class WebHandler(SimpleHTTPRequestHandler):
    """`web/dist` を配る。加えて `/duocad-config.json` でWebSocketのURLを教える。

    ポートを別にしているのは、1つのポートでHTTPとWebSocketを同時に喋る実装が
    websocketsのバージョンに依存するため(壊れやすい側を避けた)。
    """

    config_payload: bytes = b"{}"

    def do_GET(self) -> None:  # noqa: N802  (BaseHTTPRequestHandlerの命名に従う)
        path = self.path.split("?")[0]
        if path == "/favicon.ico":
            # ブラウザが必ず取りに来る。404を返すとコンソールがエラーで汚れ、
            # 計測中に本物のエラーを見落とす(自動検査もこれで落ちた)。中身は要らない。
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/duocad-config.json":
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(self.config_payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(self.config_payload)
            return
        super().do_GET()

    def log_message(self, *args: Any) -> None:
        """アクセスログを出さない(計測中のコンソールを静かに保つ)。"""


def start_http_server(dist_dir: Path, port: int, ws_url: str) -> ThreadingHTTPServer:
    """静的配信をデーモンスレッドで立てる。`web/dist` が無ければ何も立てずに例外。"""
    if not (dist_dir / "index.html").exists():
        raise FileNotFoundError(
            f"{dist_dir} に index.html が無い。先に web をビルドすること:\n"
            f"    cd {dist_dir.parent}\n    npm install\n    npm run build"
        )
    WebHandler.config_payload = json.dumps({"ws_url": ws_url}).encode("utf-8")
    handler = partial(WebHandler, directory=str(dist_dir))
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=httpd.serve_forever, name="duocad-http", daemon=True)
    thread.start()
    return httpd


# --------------------------------------------------------------------------------------
# WebSocket
# --------------------------------------------------------------------------------------
async def _send(connection: Any, payload: dict) -> None:
    await connection.send(json.dumps(payload, ensure_ascii=False))


class Server:
    """WebSocketの接続集合とコマンドの実行。状態変更は全接続へ配信する。

    **接続ごとに「知っている版」を覚える**(`_known_revisions`)。M3.5で複数Bodyになった
    ぶん、接続によって最後に送った版が違いうる(例: 遅れて `get_state` した接続は
    まだ何も知らない)。差分(`Session.delta`)はこの接続ごとの版を基準に計算する。
    """

    def __init__(self, session: Session) -> None:
        self.session = session
        self.clients: set = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # WebSocket経由の操作を処理している最中か(`_on_core_change` の二重配信よけ)。
        # `Session` の操作メソッドは同期なので、この印は「その1回の呼び出しの間」だけ立つ。
        self._dispatching = 0
        # 進行中の配信タスク(捨てるとGCされうる。`_spawn_push` 参照)
        self._pending_pushes: set = set()
        # **コアの変更を購読する**(M4・2026-08-11)。WebSocket以外の主体(MCPサーバ)が
        # 文書を変えたとき、ここが全接続へ配信する。これが上り方向の実体。
        session.add_listener(self._on_core_change)
        self._known_revisions: dict = {}  # connection -> {body_id: revision}
        # 接続ごとの送信ロック。**2つの `_push` 呼び出し(例: 2クライアントがほぼ同時に
        # 操作したとき)が同じ接続への配信で競合するのを防ぐ**(2026-08-11のQA指摘: ロック無しだと
        # 「known_revisionsの更新→await送信」の間に別の`_push`が割り込み、
        # (a) 古い版のペイロードが後から届く、(b) 実際には送っていない版を「知っている」ことに
        # されてしまい以後の差分が欠落する、の2通りの事故が起きうる)。
        self._send_locks: dict = {}  # connection -> asyncio.Lock

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """配信に使うイベントループを覚える(`run()` と `mcp_server` が起動時に呼ぶ)。

        コアの変更イベントは**同期の呼び出しの中**で飛んでくる(MCPのツールは
        `Session.set_visible` を素で呼ぶ)ので、そこから非同期の配信を始めるために
        ループの参照が要る。
        """
        self._loop = loop

    def _on_core_change(self, applied: Optional[dict] = None) -> None:
        """コアが変わった。**WebSocket以外の主体による変更だけ**を全接続へ配信する。

        WebSocket経由の操作は `_dispatch` が自分で `_push` するので、ここで配ると
        同じ状態を2回送ることになる(帯域だけでなく、`applied` の付いた配信と
        付かない配信が交錯して画面側の相関が壊れる)。`_dispatching` の印で区別する。

        `applied` を**載せずに**配るのは、MCPからの変更は「誰の操作でもない外からの
        変化」であり、`applied` は操作を出したクライアントへの確認応答だからである
        (ブラウザは `applied.client_op` で自分の計測と突き合わせる。他人の操作に
        付けて送ると、知らない `client_op` を自分のものとして拾う経路ができる)。
        """
        if self._dispatching:
            return
        loop = self._loop
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                # 配信できないことを**黙って飲み込まない**(次の配信で画面へ出る)。
                self.session.warnings.append(
                    "文書が変わったがイベントループが無いため配信できなかった"
                    "(Server.bind_loop を呼ぶこと)"
                )
                return
        loop.call_soon_threadsafe(lambda: self._spawn_push())

    def _spawn_push(self) -> None:
        """配信タスクを起こし、**参照を持ち続ける**(2026-08-11のQA指摘)。

        `create_task` の戻りを捨てるとタスクは弱参照でしか繋がらず、途中でGCされうる
        (公式ドキュメントの警告)。加えて、配信中の例外を誰も回収しないと
        「Task exception was never retrieved」が出るだけで**ブラウザは古いまま・
        警告も出ない**という、完了条件3が静かに壊れる形になる。集合で保持し、
        終わったら外し、落ちていたら警告として残す。
        """
        task = asyncio.get_running_loop().create_task(self._push(None, applied=None))
        self._pending_pushes.add(task)

        def finished(done: "asyncio.Task") -> None:
            self._pending_pushes.discard(done)
            if done.cancelled():
                return
            error = done.exception()
            if error is not None:
                self.session.warnings.append(
                    f"コア変更の配信に失敗: {type(error).__name__}: {error}"
                )

        task.add_done_callback(finished)

    async def handle(self, connection: Any) -> None:
        self._loop = self._loop or asyncio.get_running_loop()
        self.clients.add(connection)
        try:
            try:
                await self._push(connection, applied=None, only=connection)
            except Exception as error:
                # 文書が空(--no-step)等で状態が作れなくても、接続ごと落とさず理由を画面へ返す
                await _send(
                    connection,
                    {
                        "type": "error",
                        "where": "initial_state",
                        "message": f"{type(error).__name__}: {error}",
                    },
                )
            async for raw in connection:
                await self._dispatch(connection, raw)
        finally:
            self.clients.discard(connection)
            self._known_revisions.pop(connection, None)
            self._send_locks.pop(connection, None)

    async def _dispatch(self, connection: Any, raw: Any) -> None:
        try:
            message = json.loads(raw)
            command = message.get("cmd")
        except (TypeError, ValueError) as error:
            await _send(connection, {"type": "error", "where": "parse", "message": str(error)})
            return

        try:
            if command == "get_state":
                await self._push(connection, applied=None, only=connection)
            elif command == "extrude_face":
                applied = self._apply(
                    self.session.extrude,
                    message["face_index"],
                    message["distance"],
                    message.get("client_op"),
                    body_id=message.get("body_id"),
                )
                await self._push(connection, applied=applied)
            elif command in ("undo", "redo"):
                applied = self._apply(
                    self.session.undo if command == "undo" else self.session.redo
                )
                applied["client_op"] = message.get("client_op")
                await self._push(connection, applied=applied)
            elif command == "set_visible":
                applied = self._apply(
                    self.session.set_visible,
                    message["body_id"],
                    bool(message["visible"]),
                    message.get("client_op"),
                )
                await self._push(connection, applied=applied)
            elif command == "measure_record":
                line = self.session.append_measure_record(message.get("record") or {})
                await _send(connection, {"type": "recorded", "record": line})
            else:
                raise ValueError(f"unknown command: {command!r}")
        except Exception as error:  # 1つの失敗でサーバを落とさない。必ず画面へ返す
            await _send(
                connection,
                {
                    "type": "error",
                    "where": str(command),
                    "message": f"{type(error).__name__}: {error}",
                },
            )

    def _apply(self, call: Any, *args: Any, **kwargs: Any) -> dict:
        """WebSocket経由の変更を実行する。この間だけ `_dispatching` を立てる。

        **同期の呼び出しだけを囲む**(`await` を挟まない)。印を配信(`_push`)の間まで
        立てたままにすると、その待ち時間に届いたMCPからの変更まで
        「WebSocket経由だから配信不要」と誤判定して黙って捨ててしまう。
        """
        self._dispatching += 1
        try:
            return call(*args, **kwargs)
        finally:
            self._dispatching -= 1

    async def _push(self, origin: Any, applied: Optional[dict], only: Any = None) -> None:
        """状態を配信する。`only` を指定するとその接続にだけ送る。

        `state`(軽量・geometryなし)は全接続共通で1回だけ作る。`delta`(geometry付き、
        版が変わったBodyだけ)は**接続ごとに知っている版が違いうる**ので、接続ごとに計算する
        (モジュールdocstring参照。ここが差分送信の出口)。

        **接続ごとの「delta計算 → 送信 → 版の記録」は `_send_locks` で直列化する**
        (2026-08-11のQA指摘)。2つの操作(2クライアントがほぼ同時に操作した場合や、
        1クライアントが応答を待たず連続でコマンドを送った場合)がそれぞれ別の `_push` を
        起こすと、ロック無しでは「新しい版の送信が先に終わり、後から古い版が上書きで届く」
        「送信前に版を記録してしまい、実際には届いていない版を`_known_revisions`が
        知っている扱いになる」の2通りの事故が起きる。ロックの範囲を1接続ぶんに絞って
        あるので、**別々の接続への配信は並行のまま**(1接続の遅延が他接続を止めない)。

        **`state` はロックの中で作る(M4・2026-08-11に修正)**。M3.5ではこの関数の先頭で
        1回だけ作って全ターゲットへ使い回していた。そのため、後発の `_push` が先にロックを
        取って新しい `state` を送った後、ロック待ちだった先発の `_push` が**古い** `state` で
        上書きする経路があった(`_send_locks` は「同じ接続への配信」を直列化するだけで、
        `_push` 呼び出しをまたいだ新旧の順序は保証しない)。M3.5では
        「単一ユーザーなので顕在化しない・M4で再訪する」として見送っていたが、
        **MCPが入って「人とAIが同時に操作する」が実在の経路になった**ので閉じた
        (2026-08-11のM4のQA指摘)。送る直前に読み直せば、送る値は常にその瞬間の状態であり、
        古い状態が後から上書きすることが原理的に起きない。`delta` と同じロックの中で作るので
        `state` と `delta` の食い違いも同時に消える(以前は `state` の方が古くなりえた)。
        代償は接続数ぶん `state_meta()` を計算すること(27部品で数KB・ローカル1ユーザー)。
        """
        targets = [only] if only is not None else list(self.clients)

        async def deliver(target: Any) -> None:
            lock = self._send_locks.setdefault(target, asyncio.Lock())
            async with lock:
                # **送る直前に**状態を作る(上記docstring: 古い状態が後から上書きするのを防ぐ)
                meta = self.session.state_meta()
                known = self._known_revisions.get(target, {})
                delta = self.session.delta(known)
                payload = {
                    "type": "state",
                    "state": meta,
                    "delta": {"bodies": delta["bodies"]},
                }
                if applied is not None and target is origin:
                    payload["applied"] = applied
                try:
                    await _send(target, payload)
                    # **送信が成功してから**版を記録する(送信前に記録すると、失敗時や
                    # 割り込み時に「送っていない版を知っている」ことになってしまう)。
                    self._known_revisions[target] = {
                        body["body_id"]: body["revision"] for body in meta["bodies"]
                    }
                except Exception:  # 落ちた接続のために他へ配れないことを避ける
                    self.clients.discard(target)
                    self._known_revisions.pop(target, None)
                    self._send_locks.pop(target, None)

        # 警告は delta() が呼ぶ geometry() の中で積まれる(deliver() の内側)。
        # 全ターゲットぶんの配信(delta計算を含む)を終えてから取り出せば、
        # どのターゲットの分も取りこぼさない(1回のpushで複数回積まれても束ねて1回出す)。
        await asyncio.gather(*(deliver(target) for target in targets))
        # 警告の宛先は操作を出したクライアント。**MCPからの変更には出し手がいない**
        # (`origin is None`)ので、その場合は配信先全員へ出す — 出し手がいないことを理由に
        # 警告を捨てると、AIが操作したときだけ「三角形分割が取れなかった」等が消える。
        for warning in self.session.take_warnings():
            payload = {"type": "warning", "message": warning}
            for target in [origin] if origin is not None else targets:
                try:
                    await _send(target, payload)
                except Exception:
                    self.clients.discard(target)


async def run(session: Session, host: str, port: int) -> None:
    server = Server(session)
    server.bind_loop(asyncio.get_running_loop())
    async with serve(server.handle, host, port, max_size=8 * 1024 * 1024):
        await asyncio.get_running_loop().create_future()  # Ctrl+C まで動かし続ける


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="duocad M1 サーバ(WebSocket + 静的配信)")
    parser.add_argument("--step", type=Path, default=DEFAULT_STEP)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--http-port", type=int, default=8766)
    parser.add_argument("--deviation", type=float, default=kernel.DEFAULT_DEVIATION)
    parser.add_argument("--measure-log", type=Path, default=DEFAULT_MEASURE_LOG)
    parser.add_argument("--web-dist", type=Path, default=DEFAULT_WEB_DIST)
    parser.add_argument("--no-open", action="store_true", help="ブラウザを自動で開かない")
    parser.add_argument("--no-step", action="store_true", help="STEPを読まずに空で起動する")
    args = parser.parse_args(argv)

    step_path: Optional[Path] = None if args.no_step else args.step
    if step_path is not None and not step_path.exists():
        print(f"STEPが見つからない: {step_path}", file=sys.stderr)
        return 2

    session = Session(step_path, args.deviation, args.measure_log)
    ws_url = f"ws://127.0.0.1:{args.port}"
    try:
        start_http_server(args.web_dist, args.http_port, ws_url)
    except FileNotFoundError as error:
        print(error, file=sys.stderr)
        return 2

    page_url = f"http://127.0.0.1:{args.http_port}/"
    body = session.document.active_body()
    print(f"duocad server: {page_url}  (WebSocket {ws_url})")
    if body is not None:
        print(
            f"  body={body.name!r} faces={kernel.face_count(body.shape)} "
            f"volume={query.volume(body.shape):.4f} mm^3"
        )
    print(f"  measure log: {session.measure_log_path}")
    print("  Ctrl+C で終了")
    if not args.no_open:
        webbrowser.open(page_url)

    try:
        asyncio.run(run(session, "127.0.0.1", args.port))
    except KeyboardInterrupt:
        print("\nbye")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
