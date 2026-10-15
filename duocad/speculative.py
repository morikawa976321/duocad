"""投機的実行 — verbを試しに実行して**文書を元へ戻す**(プレビューの土台)。

    with speculative(doc):
        fillet(doc, [3], 1.0)
        payload = kernel.tessellate(body.shape, 0.2)
    # 抜けた時点で文書は with に入る前と完全に同じ

**形状(`shape`)は差し替えであって書き換えではない**(TopoDS_Shapeを破壊的に編集する経路は
kernelに無い)。参照を控えて戻すだけで巻き戻せ、深いコピーは要らない。

戻さないもの(意図的):

- `duocad.verbs._common.SNAPSHOTS` の undo用スナップショット。同一seqの再適用しか起こさない
  verb(`fillet` / `chamfer` / `extrude`)なら確定時に正しい値で上書きされる。
  `set_parameter` のように `parameters.rebuild` が操作ログを頭から再生するverbは、過去のseqの
  スナップショットを別の値で作り直すため、中断したプレビューの形状が後の実undoで復元されうる。
  **`PREVIEWABLE_VERBS` に再生系のverbを足すときは、ここも一緒に戻すこと。**
- `Session` 側のキャッシュ(版・テセレーション・`_applied_count`)。`Session.preview` の責務。
"""
from __future__ import annotations

import copy
import dataclasses
from contextlib import contextmanager
from typing import Any, Callable, Iterator, List, Tuple

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
    }


def _restore(doc: Document, snapshot: dict) -> None:
    """控えた状態へ戻す。**リストは中身を入れ替える**(`[:] = ...`)。
    新しいリストにすると、`doc.bodies` を掴んでいる呼び出し側が古いリストを見続ける。
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


def fork(doc: Document, copy_shape: Callable[[Any], Any]) -> Tuple[Document, List[Tuple[Any, Any]]]:
    """投機的実行を**別スレッドで**行うための文書の写し。戻り値は `(写し, [(元の形, 写した形), ...])`。

    `speculative` が文書そのものを書き換えて戻すのに対し、こちらは元の文書に一切触らない(イベントループは写しの計算中も
    元の文書で要求を捌き続ける。issue #168)。ボディの形は `copy_shape` で深く写す: 同じ `TShape` を2つのスレッドが
    使うと、三角形分割の書き換え(`kernel.tessellate`)が競合する。巻き戻しの対象と同じもの(`_snapshot`)を写す
    (スケッチ・パラメータ・選択・操作ログ・アクティブなボディ)。形状最適化の条件は写すだけ(投機的に変える verb は無い)。
    """
    twin = Document()
    pairs: List[Tuple[Any, Any]] = []
    for body in doc.bodies:
        shape = body.shape
        if shape is not None:
            shape = copy_shape(body.shape)
            pairs.append((body.shape, shape))
        twin.bodies.append(dataclasses.replace(body, shape=shape))
    twin._active_body_id = doc._active_body_id
    twin.sketches[:] = copy.deepcopy(doc.sketches)
    twin.parameters.update(copy.deepcopy(doc.parameters))
    twin.log._restore(doc.log._snapshot())
    twin.selection.items = list(doc.selection.items)
    twin.optimize = copy.deepcopy(doc.optimize)
    return twin, pairs
