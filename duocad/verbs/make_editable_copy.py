"""verb `make_editable_copy` — 読み込んだ参照ボディから編集用のソリッドを作る。

`import_step` が作るボディは `kind="reference"`(読み取り専用)で `Document.is_editable` が
False を返す。スケッチも押し出しも `kind="solid"` のボディを対象にするので、編集用のソリッドを
1つ作る操作が要る。追加したボディは `Document.add_body` の契約によりアクティブになる。

形状は**共有する(深いコピーをしない)**。verbは常に新しい形状を作って `body.shape` を差し替える
ので、編集しても参照ボディ側は変わらない。
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
    args = operation.args
    _apply_to(document, args["source_body_id"], args["body_id"], args["name"])


def _undo_make_editable_copy(document: Document, operation: Operation) -> None:
    document.remove_body(operation.args["body_id"])


register_verb(
    VERB_MAKE_EDITABLE_COPY, do=_do_make_editable_copy, undo=_undo_make_editable_copy
)
