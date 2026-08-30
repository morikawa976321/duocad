"""verb `move_body` — 選択中の面/辺/頂点が属するボディを平行移動する / 複製する。

**対象は「ボディ」だが、選ぶのは面・辺・頂点のまま**である(2026-08-30本人判断
[[duocad-move-verb-split-sketch-and-body]])。ShapeRef が既に `body` を持っているので、
「ボディ選択」という新しい選択の種別は作らない —— 逆引きは `selection.require_bodies`。
**選択が複数のボディにまたがっていれば全部を同じ量だけ動かす**(1呼び出し = 1オペレーション
= 1 undo。個々のボディに分解しない)。

`copy=False` は移動、`copy=True` は元を残して複製する。複製は独立した形状として作るので、
後から片方だけフィレットしても、もう片方は変わらない。

**Body に変換(transform)フィールドは足していない。** 形状は今までどおり `Body.shape` に
焼き込んだままで、平行移動は `kernel.translate_shape` が形状の位置(`TopLoc_Location`)を
差し替えて表す。フィールドを足すと、**形状を読む側全部**(テセレーション・体積・STEP書き出し・
領域検出・ピック)が「変換を掛け忘れていないか」を各自で気にすることになり、掛け忘れは
画面と書き出しが食い違う形で静かに出る。位置は形状が持つ、が既存の全経路と整合する。

**添字は変わらない**(`kernel.translate_shape` のdocstring)ので、この verb は確定選択を
リセットしない —— 少しずつ動かして位置を詰める使い方が主用途である。

**OCP / build123d を直接importしない**(幾何は `duocad.kernel` 越し)。
"""
from __future__ import annotations

import math
from typing import List, Optional, Sequence

from .. import kernel
from ..document import Body, BodyId, Document, new_body_id
from ..oplog import Operation, register_verb
from ._common import editable_body

VERB_MOVE_BODY = "move_body"


def _targets(doc: Document, body_ids: Sequence[BodyId]) -> List[Body]:
    """動かすボディを検査して返す。**編集できないもの・隠したものは例外**。

    `require_visible=True` にしてあるのは `fillet` / `chamfer` と同じ理由 —— 選んだ後・
    verbを呼ぶ前に人がそのボディを隠す順序が、AIと人が並行して動くと実際に起きる
    (`_common.editable_body` のdocstring)。
    """
    if not body_ids:
        raise ValueError("move_body requires at least one body")
    bodies: List[Body] = []
    for body_id in body_ids:
        bodies.append(editable_body(doc, body_id, what="move", require_visible=True))
    return bodies


def move_body(
    doc: Document,
    body_ids: Sequence[BodyId],
    dx: float,
    dy: float,
    dz: float,
    copy: bool = False,
    created_ids: Optional[Sequence[BodyId]] = None,
) -> Operation:
    """ボディを (dx, dy, dz) だけ動かす(`copy=True` なら複製する)。

    - 3つとも0は ValueError(`copy=False` では何も変えずに undo を1手消費し、
      `copy=True` では元と完全に重なるボディができる)。非有限値も ValueError。
    - `created_ids` は**再生用**。省略すると新しいIDを振る(新規呼び出し)。
    - 失敗したときは文書にもログにも何も残さない。
    """
    delta = (float(dx), float(dy), float(dz))
    if not all(math.isfinite(value) for value in delta):
        raise ValueError(f"dx/dy/dz must be finite numbers, got {delta!r}")
    if delta == (0.0, 0.0, 0.0):
        raise ValueError(
            "move_body needs a non-zero (dx, dy, dz) "
            "(0,0,0 は copy=False では何も変えず、copy=True では元と重なるボディを作る)"
        )
    bodies = _targets(doc, list(body_ids))
    ids = [body.id for body in bodies]

    if copy:
        new_ids = (
            [new_body_id() for _ in bodies]
            if created_ids is None
            else [str(value) for value in created_ids]
        )
        if len(new_ids) != len(bodies):
            raise ValueError(
                f"move_body: {len(bodies)} bodies but {len(new_ids)} ids recorded"
            )
        _do_copy(doc, bodies, delta, new_ids)
    else:
        new_ids = []
        _do_move(doc, bodies, delta)

    return doc.log.append(
        VERB_MOVE_BODY,
        {
            "bodies": ids,
            "dx": delta[0],
            "dy": delta[1],
            "dz": delta[2],
            "copy": bool(copy),
            # **作ったIDはログが持つ**(再生で同じIDになる。`extrude` の `body_id` と同じ流儀)
            "created": list(new_ids),
        },
    )


def _do_move(doc: Document, bodies: Sequence[Body], delta: tuple) -> None:
    """その場で動かす。**全部の形状を作ってから差し替える**(途中で落ちても半分動かない)。"""
    moved = [kernel.translate_shape(body.shape, *delta) for body in bodies]
    for body, shape in zip(bodies, moved):
        body.shape = shape


def _do_copy(
    doc: Document, bodies: Sequence[Body], delta: tuple, new_ids: Sequence[BodyId]
) -> None:
    """複製を足す(元は動かさない)。名前は元と同じ(`Body.name` は "solid" 固定の運用)。"""
    prepared = [
        Body(
            id=new_id,
            name=body.name,
            kind=body.kind,
            shape=kernel.translate_shape(body.shape, *delta, copy=True),
            visible=body.visible,
        )
        for body, new_id in zip(bodies, new_ids)
    ]
    for body in prepared:
        doc.add_body(body)  # 同じIDが既にあれば ValueError(黙って重複させない)


# --------------------------------------------------------------------------------------
# verbの登録
# --------------------------------------------------------------------------------------
def _do_move_body(document: Document, operation: Operation) -> None:
    """redo / replay: 記録されたボディ・量で動かし直す(ログには触らない)。"""
    args = operation.args
    delta = (float(args["dx"]), float(args["dy"]), float(args["dz"]))
    bodies = [document.get_body(body_id) for body_id in args["bodies"]]
    if args.get("copy"):
        _do_copy(document, bodies, delta, list(args.get("created") or []))
    else:
        _do_move(document, bodies, delta)


def _undo_move_body(document: Document, operation: Operation) -> None:
    """undo: 複製なら作ったボディを消し、移動なら**逆向きに同じ量**を動かす。

    形状のスナップショット(`_common.SNAPSHOTS`)を使わないのは、平行移動の逆変換が
    厳密に元へ戻るからである(`kernel.translate_shape` のdocstring)。スナップショットは
    「元に戻す式が無い」操作(フィレット・ブーリアン)のためのもので、戻す式があるなら
    **保存したログを再生せずに undo しても通る**ほうが素直である。
    """
    args = operation.args
    if args.get("copy"):
        for body_id in args.get("created") or []:
            try:
                document.remove_body(body_id)
            except KeyError:
                # 既に消えている(別のundoが先に消した)場合は空振りでよい
                pass
        return
    delta = (-float(args["dx"]), -float(args["dy"]), -float(args["dz"]))
    bodies = [document.get_body(body_id) for body_id in args["bodies"]]
    _do_move(document, bodies, delta)


register_verb(VERB_MOVE_BODY, do=_do_move_body, undo=_undo_move_body)
