"""選択機構 — 何が選択の対象になりうるかを決める層(M3.5・2026-08-11)。

**可視性は選択機構の一部**である。実機でPCケース(27部品・2835面)を触ったときの
所見が「パーツごとの表示・非表示が実装されればすべての面を選択できるようになるはず」
であり、ピック失敗12回/押し出し7回(失敗率63%)の原因は**手前の部品が奥の面を隠していた**
ことにあった。したがって「見えないものは指せない」はUIの都合ではなくコアの契約にする —
アプリ層だけがこれを守る設計にすると、MCP(M4)から不可視のBodyを押し出せてしまい、
「コアはUIが無くても機能的に完全」(実装計画書§2)が崩れる。

M3.5の契約:
    selectable_bodies(doc) -> list[BodyId]
        選択の対象になりうるBodyのIDを `doc.bodies` の順で返す。
        **`visible=False` のBodyは含めない**。
    select_face(doc, body_id, face_index) -> ShapeRef(dict)
        面を1つ指す参照を作る。形は実装計画書§3.5の ShapeRef:
            {"body": BodyId, "kind": "face", "index": int, "provenance": None}
        失敗の契約(黙って通さない。§6予防6):
            存在しないbody_id            -> KeyError
            visible=False のBody         -> ValueError(見えないものは指せない)
            範囲外のface_index(1始まり)  -> ValueError

`provenance` は day1 の空スロット(§6予防2)。面の添字は形状が変わると壊れる(§7)ので、
**保存はしてよいが再生時に信用してはならない**(§3.5)。

**OCP / build123d を直接importしない**(面の数え上げは `kernel.face_count` 越しに呼ぶ)。
"""
from __future__ import annotations

from typing import List

from . import kernel  # noqa: F401  face_count を使う(OCPは kernel の中だけ)
from .document import BodyId, Document

#: ShapeRef の kind(M3.5で作れるのは面だけ。辺・頂点はM4以降)
SHAPE_KINDS = ("face", "edge", "vertex")


def selectable_bodies(doc: Document) -> List[BodyId]:
    """選択の対象になりうるBodyのIDを `doc.bodies` の順で返す(不可視Bodyは含めない)。"""
    return [body.id for body in doc.bodies if body.visible]


def select_face(doc: Document, body_id: BodyId, face_index: int) -> dict:
    """面を指す ShapeRef を作る。不可視Body・存在しないBody・範囲外の添字は例外。"""
    body = next((b for b in doc.bodies if b.id == body_id), None)
    if body is None:
        raise KeyError(f"no such body: {body_id!r}")
    if not body.visible:
        raise ValueError(f"body {body_id!r} is hidden: 見えないものは指せない")
    count = kernel.face_count(body.shape)
    index = int(face_index)
    if not 1 <= index <= count:
        raise ValueError(f"face index out of range: {index} (1..{count})")
    return {"body": body_id, "kind": "face", "index": index, "provenance": None}