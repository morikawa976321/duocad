"""verb `extrude_face` — 既存ソリッドの面を選んで押し出す(実装計画書§1.1・7つ目のverb)。

**本人の速度ベンチマーク(選択→押し出し→数値→Enterの4step)がこのverbそのもの**であり、
これが無いと「手動のほうが速い」という企画の前提を測れない。

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
これは現在の制限であり、由来による永続的な面の命名(実装計画書§6予防2のL4化)が入るまで続く。
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
    **document にもログにも変更を残さない** — 失敗しうる幾何計算を append より先に済ませる
    (io_step.import_step と同じ順序。§6予防6)。
    """
    body = editable_body(doc, body_id, what="extrude", require_visible=True)
    result = kernel.extrude_face_on_shape(body.shape, face_index, distance)

    previous_shape = body.shape
    body.shape = result["shape"]
    operation = doc.log.append(
        VERB_EXTRUDE_FACE,
        {"body_id": body.id, "face_index": int(face_index), "distance": float(distance)},
        selection=[{"body": body.id, "kind": "face", "index": int(face_index)}],
    )
    operation.provenance = result["provenance"]
    remember(doc, body.id, operation.seq, previous_shape)
    return operation


# --------------------------------------------------------------------------------------
# verbの登録(oplog は verb名の分岐を持たない。登録は verb を定義したこちら側)
# --------------------------------------------------------------------------------------
def _do_extrude_face(document: Document, operation: Operation) -> None:
    """redo / replay 時の再適用。記録された添字と距離で押し出し直す。

    **ログには触らない**(触ると redo/replay で二重記録になる)。`provenance` は初回適用で
    入っているのが正なので、空のときだけ埋める(再生が履歴を書き換えないようにする)。
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
    """undo 時の取り消し: 押し出す前の形状に戻す。"""
    body_id = operation.args["body_id"]
    document.get_body(body_id).shape = recall(document, body_id, operation.seq)


register_verb(VERB_EXTRUDE_FACE, do=_do_extrude_face, undo=_undo_extrude_face)
