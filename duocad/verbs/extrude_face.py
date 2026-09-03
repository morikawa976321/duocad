"""verb `extrude_face` — 既存ソリッドの面を選んで押し出す。

`distance > 0` は面の外向き法線方向へ足し、`distance < 0` は削る。

**編集できるのは `kind="solid"` のボディだけ**(`Document.is_editable`)。

**undoは押し出し前の形状のスナップショット**で戻す(操作ログを逆算しない)。スナップショットは
プロセス内の弱参照テーブルに `(body_id, seq)` で持ち、`do` のたびに取り直すので redo→undo も
再生後のundoも効く。**保存したログを読み込んで再生せずにundoすることはできない**
(その場合はスナップショットが無いので明示的に例外にする)。
"""
from __future__ import annotations

from typing import Optional

from .. import kernel
from ..document import BodyId, Document
from ..oplog import Operation, register_verb
from ._common import editable_body, recall, remember

VERB_EXTRUDE_FACE = "extrude_face"


def extrude_face(
    doc: Document,
    face_index: int,
    distance: float,
    body_id: Optional[BodyId] = None,
) -> Operation:
    """面を押し出し、ログに積んで Operation を返す。

    失敗時(面の添字が範囲外・距離0・ブーリアン失敗・形状が変わらない)は例外を送出し、
    **document にもログにも変更を残さない** — 失敗しうる幾何計算を append より先に済ませる。
    """
    body = editable_body(doc, body_id, what="extrude", require_visible=True)
    result = kernel.extrude_face_on_shape(body.shape, face_index, distance)

    previous_shape = body.shape
    body.shape = result["shape"]
    operation = doc.log.append(
        VERB_EXTRUDE_FACE,
        {"body_id": body.id, "face_index": int(face_index), "distance": float(distance)},
    )
    operation.provenance = result["provenance"]
    remember(doc, body.id, operation.seq, previous_shape)
    return operation


def _do_extrude_face(document: Document, operation: Operation) -> None:
    """redo / replay: 記録された添字と距離で押し出し直す。**ログには触らない**(二重記録になる)。

    `provenance` は初回適用で入っているのが正なので、空のときだけ埋める。
    """
    args = operation.args
    body = editable_body(document, args["body_id"], what="extrude", require_visible=True)
    result = kernel.extrude_face_on_shape(body.shape, args["face_index"], args["distance"])
    previous_shape = body.shape
    body.shape = result["shape"]
    if operation.provenance is None:
        operation.provenance = result["provenance"]
    remember(document, body.id, operation.seq, previous_shape)


def _undo_extrude_face(document: Document, operation: Operation) -> None:
    body_id = operation.args["body_id"]
    document.get_body(body_id).shape = recall(document, body_id, operation.seq)


register_verb(VERB_EXTRUDE_FACE, do=_do_extrude_face, undo=_undo_extrude_face)
