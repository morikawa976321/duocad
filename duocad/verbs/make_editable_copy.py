"""verb `make_editable_copy` — 読み込んだ参照ボディから編集用のソリッドを作る。

**なぜ実装計画書の7 verbに無いものを足したのか(2026-08-10・M1で判明した穴)**:

M1の完了条件は「`shelf_attachment_GD.step` を表示し、その面を押し出せること」である。ところが
`import_step` が作るボディは `kind="reference"`(読み取り専用)で、`Document.is_editable` は
これを False にする(M0の契約)。したがって選択肢は3つしか無かった:

1. 参照ボディを直接押し出す → M0が明文化した「reference は編集操作の対象にならない」を破る
2. 読み込んだものを編集できないままにする → M1の完了条件3に到達できない
3. **編集用のソリッドを1つ作る操作を足す**(これを採った)

Fusion360で参照部品を挿入してから編集するのと同じ形であり、M2以降でも必ず必要になる
(スケッチも押し出しも `kind="solid"` のボディを対象にする)。**モデリングのverbではなく
文書を組み立てるための操作**なので、実装計画書§1のverb表(スケッチ5＋3D2)の数には数えない。

契約:
    make_editable_copy(doc, source_body_id=None, name=None) -> BodyId
        `source_body_id`(既定はアクティブボディ)の形状を持つ `kind="solid"` のボディを追加し、
        `verb="make_editable_copy"`,
        `args={"source_body_id":.., "body_id":.., "name":..}` をログに積んでBodyIdを返す。
        追加したボディは `Document.add_body` の契約によりアクティブになる。

形状は**共有する(深いコピーをしない)**。verbは常に新しい形状を作って `body.shape` を差し替える
実装(`extrude_face` を参照)なので、編集しても参照ボディ側は変わらない。
"""
from __future__ import annotations

from typing import Optional
from uuid import uuid4

from ..document import Body, BodyId, Document
from ..oplog import Operation, register_verb

VERB_MAKE_EDITABLE_COPY = "make_editable_copy"


def make_editable_copy(
    doc: Document,
    source_body_id: Optional[BodyId] = None,
    name: Optional[str] = None,
) -> BodyId:
    """参照ボディの形状を持つ編集可能なソリッドを追加し、そのBodyIdを返す。

    形状の無いボディ・存在しないIDは例外にして、documentにもログにも変更を残さない。
    """
    source = _source_body(doc, source_body_id)
    body_id = f"body-{uuid4().hex[:12]}"
    body_name = name if name else f"{source.name}-edit"
    _apply_to(doc, source.id, body_id, body_name)
    doc.log.append(
        VERB_MAKE_EDITABLE_COPY,
        {"source_body_id": source.id, "body_id": body_id, "name": body_name},
    )
    return body_id


def _source_body(doc: Document, source_body_id: Optional[BodyId]) -> Body:
    """複製元のボディ。無い・形状が無いなら例外(黙って空のソリッドを作らない)。"""
    if source_body_id is None:
        body = doc.active_body()
        if body is None:
            raise ValueError("no active body to copy (document is empty)")
    else:
        body = next(
            (candidate for candidate in doc.bodies if candidate.id == source_body_id), None
        )
        if body is None:
            raise KeyError(f"no such body: {source_body_id!r}")
    if body.shape is None:
        raise ValueError(f"body {body.id!r} has no shape to copy")
    return body


def _apply_to(doc: Document, source_body_id: BodyId, body_id: BodyId, name: str) -> Body:
    """ログに触らずにコピーを追加する(公開APIと redo/replay が共有する非公開ヘルパ)。"""
    source = next(
        (candidate for candidate in doc.bodies if candidate.id == source_body_id), None
    )
    if source is None:
        raise KeyError(f"no such body: {source_body_id!r}")
    body = Body(id=body_id, name=name, kind="solid", shape=source.shape)
    doc.add_body(body)
    return body


# --------------------------------------------------------------------------------------
# verbの登録
# --------------------------------------------------------------------------------------
def _do_make_editable_copy(document: Document, operation: Operation) -> None:
    """redo / replay: 記録された複製元・BodyId・名前でソリッドを作り直す。"""
    args = operation.args
    _apply_to(document, args["source_body_id"], args["body_id"], args["name"])


def _undo_make_editable_copy(document: Document, operation: Operation) -> None:
    """undo: 追加したソリッドを取り除く。"""
    document.remove_body(operation.args["body_id"])


register_verb(
    VERB_MAKE_EDITABLE_COPY, do=_do_make_editable_copy, undo=_undo_make_editable_copy
)
