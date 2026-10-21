"""verb `split_body` — 選択の面/辺/頂点が属するボディを、平面で切って複数のボディに分ける。

切る平面は次のどれか1つ(0個・2個以上は ValueError):

- `base_plane` … XY/XZ/YZ(原点通過)
- `plane_face` … 既存ソリッドの平らな面への参照 `{"body": body_id, "index": 面の1始まりの添字}`。法線はその面の外向き
- `sketch_id` … スケッチの平面(面に貼ったもの・オフセット平面を含む)。法線はスケッチの法線

ログには解決済みの数値 `{plane_point, plane_normal}` だけを載せる(`mirror_body` と同じ。面の番号は後で変わる)。

`keep` は残す側: `"both"`(既定)/ `"positive"`(法線が向く側)/ `"negative"`(反対側)。

**元のボディの番号と名前は、残す塊のうち体積が最大のものが引き継ぐ**(法線の向きで引き継ぎ先が変わらない。
体積が相対 1e-9 以内で同じなら正側、それでも決まらなければ重心の x, y, z の小さい方)。ほかの塊は新しいボディで、
新しい番号の名前を持ち、表示の属性は元のボディの写し。片側が離れた複数の塊に分かれるときは、塊ごとに別のボディ。

**全ボディの切断と検査が終わるまで文書にも番号にも触れない**。どれか1つでも平面と交わらなければ全体を断る。
"""
from __future__ import annotations

import dataclasses
from typing import Any, List, Optional, Sequence

from .. import kernel_sketch, kernel_split, naming, selection, sketch
from ..document import Body, BodyId, Document, new_body_id
from ..entry import entry
from ..oplog import Operation, register_verb
from ._common import (
    copy_ids,
    editable_bodies,
    recall,
    remember,
    remove_created_bodies,
    replayed_body,
    resolve_shape_ref,
)

VERB_SPLIT_BODY = "split_body"
KEEP_BOTH = "both"
KEEP_CHOICES = (KEEP_BOTH, *kernel_split.SIDES)
_MESH_HINT = "(メッシュボディには面が無い)"


def _cut_plane(doc: Document, base_plane: Any, plane_face: Any, sketch_id: Any) -> dict:
    """切る平面の引数を検査して、ログに載せる数値 `{plane_point, plane_normal}` にする。"""
    given = [name for name, value in (("base_plane", base_plane), ("plane_face", plane_face), ("sketch_id", sketch_id)) if value is not None]
    if len(given) > 1:
        raise ValueError(f"{' と '.join(given)} は同時に渡せない(どれで切るかを暗黙にしない)")
    if base_plane is not None:
        frame = kernel_sketch.sketch_plane_frame(sketch.base_plane(str(base_plane)))
    elif plane_face is not None:
        body, index = resolve_shape_ref(doc, plane_face, kind="face", what="plane_face", mesh_hint=_MESH_HINT)
        frame = kernel_sketch.face_frame(body.shape, index)
    elif sketch_id is not None:
        frame = kernel_sketch.sketch_plane_frame(sketch.get_sketch(doc, str(sketch_id)).plane)
    else:
        raise ValueError(
            "split_body needs a cutting plane: base_plane(XY/XZ/YZ)・plane_face(既存の平らな面への参照)・"
            "sketch_id(スケッチの平面)のどれか1つ"
        )
    return {
        "plane_point": [float(value) for value in frame["origin"]],
        "plane_normal": [float(value) for value in frame["normal"]],
    }


def _require_keep(keep: Any) -> str:
    if keep not in KEEP_CHOICES:
        raise ValueError(f"keep must be one of {list(KEEP_CHOICES)}, got {keep!r}")
    return str(keep)


def _plan(bodies: Sequence[Body], point: Sequence[float], normal: Sequence[float], keep: str) -> List[dict]:
    """ボディごとに切って、引き継ぐ塊と新しいボディになる塊に分ける(文書は変えない)。

    戻りは `[{"body": Body, "heir": 塊, "others": [塊, ...]}]`。`others` の並びは `kernel_split` の決定的な並び。
    """
    plans = []
    for body in bodies:
        pieces = kernel_split.split_by_plane(body.shape, point, normal)["pieces"]
        kept = [piece for piece in pieces if keep == KEEP_BOTH or piece["side"] == keep]
        largest = max(piece["volume"] for piece in kept)
        # `pieces` は正側が先・重心の昇順なので、体積が同じ塊の先頭がそのまま決め方の規則になる。
        heir = next(piece for piece in kept if piece["volume"] >= largest * (1.0 - kernel_split.VOLUME_TIE_RTOL))
        plans.append({"body": body, "heir": heir, "others": [piece for piece in kept if piece is not heir]})
    return plans


def _provenance(plans: Sequence[dict], created: Sequence[Sequence[BodyId]]) -> dict:
    """切り口の面は各塊の `new_faces`(元の面の後継は `face_map`。添字は塊ごと)。"""
    return {
        "verb": VERB_SPLIT_BODY,
        "bodies": [
            {
                "body": plan["body"].id,
                "pieces": [
                    {"body": body_id, "side": piece["side"], **piece["provenance"]}
                    for body_id, piece in zip([plan["body"].id, *ids], [plan["heir"], *plan["others"]])
                ],
            }
            for plan, ids in zip(plans, created)
        ],
    }


def _apply(
    doc: Document,
    plans: Sequence[dict],
    created: Sequence[Sequence[BodyId]],
    numbers: Optional[Sequence[Sequence[int]]],
) -> tuple:
    """形を差し替え、新しいボディを足す。戻りは `(undo 用の元の形(`plans` と同じ並び), 名前に使った通し番号)`。

    `numbers` が None なら新しく振る(作成時)。ID の衝突は何かを変える前に調べる。
    """
    taken = {body.id for body in doc.bodies}
    for ids in created:
        for new_id in ids:
            if new_id in taken:
                raise ValueError(f"body id already exists: {new_id!r}")
            taken.add(new_id)
    if numbers is None:
        numbers = [[naming.allocate(doc, naming.BODY) for _ in ids] for ids in created]
    previous = []
    for plan, ids, group in zip(plans, created, numbers):
        body = plan["body"]
        previous.append(body.shape)
        body.shape = plan["heir"]["shape"]
        for new_id, number, piece in zip(ids, group, plan["others"]):
            name = naming.recorded(doc, naming.BODY, number, None)
            doc.add_body(dataclasses.replace(body, id=new_id, name=name, shape=piece["shape"]))
    return previous, [list(group) for group in numbers]


def _remember(doc: Document, plans: Sequence[dict], previous: Sequence[Any], seq: int) -> None:
    for plan, shape in zip(plans, previous):
        remember(doc, plan["body"].id, seq, shape)


def split_body(
    doc: Document,
    body_ids: Sequence[BodyId],
    base_plane: Any = None,
    plane_face: Any = None,
    sketch_id: Any = None,
    keep: str = KEEP_BOTH,
) -> Operation:
    """ボディを平面で切って分ける。失敗したときは文書にもログにも番号にも何も残さない。"""
    ids = [str(body_id) for body_id in body_ids]
    if len(set(ids)) != len(ids):
        raise ValueError(f"split_body got duplicate body ids: {ids!r}")
    bodies = editable_bodies(doc, ids, verb="split_body", what="split")
    keep = _require_keep(keep)
    plane_args = _cut_plane(doc, base_plane, plane_face, sketch_id)
    plans = _plan(bodies, plane_args["plane_point"], plane_args["plane_normal"], keep)

    created = [[new_body_id() for _ in plan["others"]] for plan in plans]
    previous, numbers = _apply(doc, plans, created, None)
    operation = doc.log.append(
        VERB_SPLIT_BODY,
        {
            "bodies": [body.id for body in bodies],
            **plane_args,
            "keep": keep,
            "created": [list(group) for group in created],
            "numbers": numbers,
        },
    )
    operation.provenance = _provenance(plans, created)
    _remember(doc, plans, previous, operation.seq)
    return operation


def _do_split_body(document: Document, operation: Operation) -> None:
    """redo / replay: 記録された平面・残し方・ID・番号で切り直す(ログには触らない)。"""
    args = operation.args
    bodies = [replayed_body(document, body_id, what="split") for body_id in args["bodies"]]
    plans = _plan(bodies, args["plane_point"], args["plane_normal"], _require_keep(args.get("keep", KEEP_BOTH)))
    if len(args["created"]) != len(plans):
        raise ValueError(f"split_body: {len(plans)} bodies but {len(args['created'])} id groups recorded")
    created = [copy_ids(len(plan["others"]), group, verb="split_body") for plan, group in zip(plans, args["created"])]
    previous, _ = _apply(document, plans, created, args["numbers"])
    if operation.provenance is None:
        operation.provenance = _provenance(plans, created)
    _remember(document, plans, previous, operation.seq)


def _undo_split_body(document: Document, operation: Operation) -> None:
    """undo: 作ったボディを消し、元のボディの形を戻す。"""
    args = operation.args
    shapes = [recall(document, body_id, operation.seq) for body_id in args["bodies"]]
    remove_created_bodies(document, [new_id for group in args["created"] for new_id in group])
    for body_id, shape in zip(args["bodies"], shapes):
        document.get_body(body_id).shape = shape


register_verb(VERB_SPLIT_BODY, do=_do_split_body, undo=_undo_split_body)


# --------------------------------------------------------------------------------------
# 入口(`duocad/entry.py`) —— 対象を選択から取り、上の verb を掛け、応答に載せるものを返す
# --------------------------------------------------------------------------------------
@entry(VERB_SPLIT_BODY)
def _enter_split_body(
    document: Document,
    base_plane: Optional[str] = None,
    plane_face: Any = None,
    sketch_id: Optional[str] = None,
    keep: str = KEEP_BOTH,
) -> dict:
    body_ids = selection.require_bodies(document)
    args = split_body(
        document, body_ids, base_plane=base_plane, plane_face=plane_face, sketch_id=sketch_id, keep=keep
    ).args
    created = [new_id for group in args["created"] for new_id in group]
    return {
        "plane_point": args["plane_point"],
        "plane_normal": args["plane_normal"],
        "keep": args["keep"],
        "created": created,
        "changed_bodies": [*args["bodies"], *created],
    }
