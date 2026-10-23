"""verb `thread`(画面の名前「ねじ」)— 選んだ全周の円筒面1つにねじ山を実体で刻む。

外向きの面(軸)なら雄ねじ、内向きの面(穴)なら雌ねじ。面の直径を呼び径とみなす。山形は半円・30°台形・45°台形の3つから選ぶ
(本人の決定 2026-10-23。評価は docs/THREAD_SIM.md 6.8〜6.10)。隙間は空欄なら山形ごとの既定(穴は推奨の真ん中、軸は 0)、
上限を超えれば断り、推奨の範囲の外なら確定はして警告を出す。ピッチは径から自動(`kernel_thread.default_pitch`)。

**ログには解決後の数値(隙間・ピッチ)を残す**(既定値の表を後で直しても、古い文書の再生が変わらない。docs/THREAD.md「verb の形」)。
"""
from __future__ import annotations

from typing import Optional

from .. import kernel_thread, selection
from ..document import BodyId, Document
from ..entry import entry
from ..oplog import Operation, register_verb
from ._common import editable_body, reapply_shape, remember, replayed_body, undo_shape

VERB_THREAD = "thread"


def thread(
    doc: Document,
    face: int,
    profile: str,
    clearance: Optional[float] = None,
    body_id: Optional[BodyId] = None,
) -> Operation:
    body = editable_body(doc, body_id, what="thread")
    result = kernel_thread.thread_face(body.shape, int(face), profile, clearance)

    previous_shape = body.shape
    body.shape = result["shape"]
    operation = doc.log.append(
        VERB_THREAD,
        {
            "face": int(face),
            "profile": profile,
            "clearance": result["clearance"],
            "pitch": result["pitch"],
            "body_id": body.id,
        },
    )
    operation.provenance = result["provenance"]
    remember(doc, body.id, operation.seq, previous_shape)
    return operation


def _do_thread(document: Document, operation: Operation) -> None:
    """redo / replay: 記録された面・山形・隙間・ピッチで刻み直す(ログには触らない)。"""
    args = operation.args
    body = replayed_body(document, args["body_id"], what="thread")
    result = kernel_thread.thread_face(
        body.shape, int(args["face"]), args["profile"], float(args["clearance"]), float(args["pitch"])
    )
    reapply_shape(document, body, result, operation)


register_verb(VERB_THREAD, do=_do_thread, undo=undo_shape)


@entry(VERB_THREAD)
def _enter_thread(document: Document, profile: str, clearance: Optional[float] = None) -> dict:
    item = selection.require_selected(document, "face", exactly_one=True)[0]
    operation = thread(document, int(item["index"]), profile, clearance, body_id=item["body"])
    changed = operation.args["body_id"]
    out = {"body_id": changed, "changed_bodies": [changed]}
    warning = kernel_thread.clearance_warning(profile, operation.provenance["female"], operation.args["clearance"])
    if warning:
        out["warning"] = warning  # `Session._after_logged_verb` が確定のときだけ警告のトーストへ積む
    return out
