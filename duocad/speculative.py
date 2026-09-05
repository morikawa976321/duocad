"""投機的実行 — verbを試しに実行して**文書を元へ戻す**(プレビューの土台)。

    with speculative(doc):
        extrude_face(doc, face_index=3, distance=5.0)
        payload = kernel.tessellate(body.shape, 0.2)
    # 抜けた時点で文書は with に入る前と完全に同じ

**形状(`shape`)は差し替えであって書き換えではない**(OCCTのTopoDS_Shapeを破壊的に編集する
経路はkernelに無い)。だから参照を控えて戻すだけで完全な巻き戻しになり、深いコピーは要らない。

戻さないもの(意図的):

- `duocad.verbs._common.SNAPSHOTS` の undo用スナップショット。**同一seqの再適用しか
  起こさないverb**(`extrude_face` / `fillet` / `chamfer` / 領域の `extrude`)なら、確定時に
  正しい値で上書きされるので誤ったundoにはならない。`set_parameter` のように
  `parameters.rebuild` が**操作ログを頭から再生する**verbは、過去のseqのスナップショットを
  別の値で作り直すため、中断したプレビューの形状が後の実undoで復元されうる。
  **`PREVIEWABLE_VERBS` に再生系のverbを足すときは、ここも一緒に戻すこと**(1行足すだけでは壊れる)。
- `Session` 側のキャッシュ(版・テセレーション・`_applied_count`)。`Session.preview` の責務。
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
from typing import Iterator

from .document import Document


@contextmanager
def speculative(doc: Document) -> Iterator[None]:
    """`with` の中で文書へ加えた変更を、抜けるときに**すべて元へ戻す**(例外時も)。

    **入れ子にしない**(巻き戻しの単位が曖昧になる)。
    """
    snapshot = _snapshot(doc)
    try:
        yield
    finally:
        _restore(doc, snapshot)


def _snapshot(doc: Document) -> dict:
    """巻き戻しに要るものを控える。"""
    return {
        # **`mesh` も控える**。いまメッシュを差し替えるverbは無いが、控えから
        # 落ちているフィールドは「戻したはずなのに戻っていない」という形で後から効く。
        "bodies": [
            (body, body.shape, body.visible, body.name, body.kind, body.mesh)
            for body in doc.bodies
        ],
        # `active_body()` はBodyを返すのでIDに落として控える(Bodyごと消えていても戻せる)
        "active_body_id": doc._active_body_id,
        "sketches": copy.deepcopy(doc.sketches),
        "parameters": copy.deepcopy(doc.parameters),
        "log": doc.log._snapshot(),
        "selection_items": list(doc.selection.items),
        "selection_origin": doc.selection.origin,
    }


def _restore(doc: Document, snapshot: dict) -> None:
    """控えた状態へ戻す。**リストは中身を入れ替える**(`[:] = ...`)。

    新しいリストで置き換えると、`doc.bodies` を掴んでいる呼び出し側が古いリストを見続ける。
    """
    doc.bodies[:] = [entry[0] for entry in snapshot["bodies"]]
    for body, shape, visible, name, kind, mesh in snapshot["bodies"]:
        body.shape = shape
        body.visible = visible
        body.name = name
        body.kind = kind
        body.mesh = mesh
    # `set_active_body` は None を受け付けない(存在しないIDとして KeyError)ので直接戻す。
    doc._active_body_id = snapshot["active_body_id"]
    doc.sketches[:] = snapshot["sketches"]
    doc.parameters.clear()
    doc.parameters.update(snapshot["parameters"])
    doc.log._restore(snapshot["log"])
    doc.selection.items = snapshot["selection_items"]
    doc.selection.origin = snapshot["selection_origin"]
