"""verb `extrude_face` — 既存ソリッドの面を選んで押し出す(実装計画書§1.1・7つ目のverb)。

**このverbが M1 の本体**。本人の速度ベンチマーク(選択→押し出し→数値→Enterの4step)が
そのままこれであり、これが無いと「手動のほうが速い」という企画の前提を測れない。

契約:
    extrude_face(doc, face_index, distance, body_id=None) -> Operation
        `body_id`(既定はアクティブボディ)の面 `face_index` を `distance` 押し出す。
        `distance > 0` は面の外向き法線方向へ足す、`distance < 0` は削る。
        ログに `verb="extrude_face"`,
        `args={"body_id":.., "face_index":.., "distance":..}` を積み、
        `Operation.provenance` に**JSON化できる由来の射影**を入れて Operation を返す。

verbの粒度(実装計画書§2「verbの粒度」): undo1回で消える単位＝操作ログ1行＝MCP1呼び出しが
一致する。UIの「面を選ぶ→数値を打つ→Enter」は1つのverbを組み立てる漏斗であって3つのverbでない。

**編集できるのは `kind="solid"` のボディだけ**(`Document.is_editable`)。読み込んだSTEPは
`kind="reference"`(読み取り専用)なので、そのままでは押し出せない — 先に
`make_editable_copy` で編集用のソリッドを作る(同パッケージ)。

**undoは押し出し前の形状のスナップショット**で戻す(操作ログを逆算しない)。スナップショットは
プロセス内の弱参照テーブルに `(body_id, seq)` で持ち、`do` のたびに取り直すので
redo→undo も再生後のundoも効く。**保存したログを読み込んで再生せずにundoすることはできない**
(その場合はスナップショットが無いので明示的に例外にする。黙って何もしないより良い)。
これはM1の制限であり、由来による永続的な面の命名(実装計画書§6予防2のL4化)が入るまで続く。
"""
from __future__ import annotations

from typing import Any, Optional
from weakref import WeakKeyDictionary

from .. import kernel
from ..document import Body, BodyId, Document
from ..oplog import Operation, register_verb

VERB_EXTRUDE_FACE = "extrude_face"

#: (document) -> {(body_id, seq): 押し出し前の形状}。documentが消えたら一緒に消える。
_SNAPSHOTS: "WeakKeyDictionary[Document, dict]" = WeakKeyDictionary()


def extrude_face(
    doc: Document,
    face_index: int,
    distance: float,
    body_id: Optional[BodyId] = None,
) -> Operation:
    """面を押し出し、ログに積んで Operation を返す。

    失敗時(面の添字が範囲外・距離0・ブーリアン失敗・形状が変わらない)は例外を送出し、
    **document にもログにも変更を残さない** — 失敗しうる幾何計算を append より先に済ませる
    (io_step.import_step と同じ順序。§6予防6)。
    """
    body = _editable_body(doc, body_id)
    result = kernel.extrude_face_on_shape(body.shape, face_index, distance)

    previous_shape = body.shape
    body.shape = result["shape"]
    operation = doc.log.append(
        VERB_EXTRUDE_FACE,
        {"body_id": body.id, "face_index": int(face_index), "distance": float(distance)},
        selection=[{"body": body.id, "kind": "face", "index": int(face_index)}],
    )
    operation.provenance = result["provenance"]
    _remember(doc, body.id, operation.seq, previous_shape)
    return operation


def _editable_body(doc: Document, body_id: Optional[BodyId]) -> Body:
    """押し出す対象のボディ。編集できないもの・形状の無いものは例外(黙って無視しない)。"""
    if body_id is None:
        body = doc.active_body()
        if body is None:
            raise ValueError("no active body to extrude (document is empty)")
        body_id = body.id
    if not doc.is_editable(body_id):  # 存在しないIDは KeyError(document側の契約)
        raise ValueError(
            f"body {body_id!r} is not editable (kind='reference'). "
            "make_editable_copy() で編集用のソリッドを作ってから押し出すこと"
        )
    body = next(candidate for candidate in doc.bodies if candidate.id == body_id)
    if not body.visible:
        raise ValueError(
            f"body {body_id!r} is hidden: 見えないものは指せない(set_visible で表示すること)"
        )
    if body.shape is None:
        raise ValueError(f"body {body_id!r} has no shape to extrude")
    return body


# --------------------------------------------------------------------------------------
# undo用のスナップショット
# --------------------------------------------------------------------------------------
def _remember(doc: Document, body_id: BodyId, seq: int, shape: Any) -> None:
    """押し出し前の形状を覚える(同じseqで再適用されたら上書きする)。"""
    _SNAPSHOTS.setdefault(doc, {})[(body_id, seq)] = shape


def _recall(doc: Document, body_id: BodyId, seq: int) -> Any:
    """覚えていた形状を取り出して忘れる。無ければ KeyError(黙ってundoを空振りさせない)。"""
    snapshots = _SNAPSHOTS.get(doc)
    if snapshots is None or (body_id, seq) not in snapshots:
        raise KeyError(
            f"no snapshot for undo of seq={seq} on body {body_id!r}: "
            "保存したログを再生せずにundoしようとしている(M1の制限)。"
            "OpLog.replay(document) してから undo すること"
        )
    return snapshots.pop((body_id, seq))


# --------------------------------------------------------------------------------------
# verbの登録(oplog は verb名の分岐を持たない。登録は verb を定義したこちら側)
# --------------------------------------------------------------------------------------
def _do_extrude_face(document: Document, operation: Operation) -> None:
    """redo / replay 時の再適用。記録された添字と距離で押し出し直す。

    **ログには触らない**(触ると redo/replay で二重記録になる)。`provenance` は初回適用で
    入っているのが正なので、空のときだけ埋める(再生が履歴を書き換えないようにする)。
    """
    args = operation.args
    body = _editable_body(document, args["body_id"])
    result = kernel.extrude_face_on_shape(body.shape, args["face_index"], args["distance"])
    previous_shape = body.shape
    body.shape = result["shape"]
    if operation.provenance is None:
        operation.provenance = result["provenance"]
    _remember(document, body.id, operation.seq, previous_shape)


def _undo_extrude_face(document: Document, operation: Operation) -> None:
    """undo 時の取り消し: 押し出す前の形状に戻す。"""
    args = operation.args
    body_id = args["body_id"]
    body = next((candidate for candidate in document.bodies if candidate.id == body_id), None)
    if body is None:
        raise KeyError(f"no such body: {body_id!r}")
    body.shape = _recall(document, body_id, operation.seq)


register_verb(VERB_EXTRUDE_FACE, do=_do_extrude_face, undo=_undo_extrude_face)
