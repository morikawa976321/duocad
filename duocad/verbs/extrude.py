"""verb `extrude` — スケッチの**領域**を押し出す(実装計画書§1の3Dのverb)。M2で追加。

`extrude_face`(M1・既存ソリッドの面を押す)とは別のverbである。こちらは
**領域の集合**を対象に取り、`mode="join"` で足し、`mode="cut"` で引く。
本人が日常的に使う「円を線で分けて片側だけ押し出す」がこのverbの存在理由。

契約:
    extrude(doc, regions, distance, mode="join", body_id=None) -> Operation
        `regions` は `duocad.region.region_ref()` が返す `RegionRef` のリスト。
        `distance` はスケッチ平面の法線方向(正の向き)への押し出し量。0 は ValueError。
        `mode`:
            "join" — 対象ボディへ足す。**対象が無ければ新しいソリッドを1つ作る**
                     (最初の板はこれで生まれる)。作った場合も `args["body_id"]` に
                     そのIDを記録する(再生で同じIDが再現できないと選択が壊れるため)。
            "cut"  — 対象ボディから引く。**対象が無ければ ValueError**
                     (引く相手が無いのに成功したことにしない)。
        `body_id` の既定はアクティブボディ。編集できないボディ(kind="reference")は ValueError。
        ログに `verb="extrude"`,
        `args={"regions": [<RegionRefのdict>, ...], "distance":.., "mode":.., "body_id":..}`,
        `selection=[<RegionRefのdict>, ...]` を積んで `Operation` を返す。

**失敗を黙って通さない**(§6予防6・§7): 領域が空・添字が範囲外・`mode` が未知・距離0・
ブーリアン失敗のいずれでも例外を送出し、**documentにもログにも変更を残さない**。
とくに「切り取ったのに穴が1つも開いていない」を成功として通さないこと。

**OCP を直接importしない**。幾何は `duocad.kernel` 越しに呼ぶ。
"""
from __future__ import annotations

from typing import Any, List, Optional, Sequence

from .. import kernel
from ..document import Body, BodyId, Document, new_body_id
from ..oplog import Operation, register_verb
from ._common import recall, remember
from ..region import RegionRef, _regions_detailed
from ..sketch import get_sketch

VERB_EXTRUDE = "extrude"

#: 押し出しの向き(実装計画書§1「mode(join | cut)」)
MODES = ("join", "cut")

#: (document) -> {(body_id, seq): 押し出し前の形状 | None(新規作成)}。


def extrude(
    doc: Document,
    regions: Sequence[RegionRef],
    distance: float,
    mode: str = "join",
    body_id: Optional[BodyId] = None,
) -> Operation:
    """領域の集合を押し出し、ログに積んで Operation を返す。"""
    distance = float(distance)
    if distance == 0.0:
        raise ValueError("distance must not be 0 (押し出し量0は形状を変えない)")
    if mode not in MODES:
        raise ValueError(f"unknown extrude mode: {mode!r} (one of {list(MODES)})")

    refs = list(regions)
    if not refs:
        raise ValueError("extrude requires at least one region")

    faces, normal = _resolve_faces_and_normal(doc, refs)
    target_body, resolved_body_id = _resolve_target(doc, mode, body_id)
    target_shape = None if target_body is None else target_body.shape

    result = kernel.extrude_regions(
        faces, distance, mode, normal, target_shape=target_shape
    )

    created_new = False
    previous_shape: Any = None
    if target_body is None:
        resolved_body_id = new_body_id()
        doc.add_body(
            Body(
                id=resolved_body_id,
                name="solid",
                kind="solid",
                shape=result["shape"],
            )
        )
        created_new = True
    else:
        previous_shape = target_body.shape
        target_body.shape = result["shape"]

    selection = [ref.to_dict() for ref in refs]
    operation = doc.log.append(
        VERB_EXTRUDE,
        {
            "regions": selection,
            "distance": distance,
            "mode": mode,
            "body_id": resolved_body_id,
        },
        selection=selection,
    )
    remember(doc, resolved_body_id, operation.seq, None if created_new else previous_shape)
    return operation


def _resolve_target(
    doc: Document, mode: str, body_id: Optional[BodyId]
) -> tuple[Optional[Body], Optional[BodyId]]:
    """対象ボディを決める。cut で対象が無い・reference は ValueError。

    `body_id` を省略したときのアクティブボディが**編集できない**(読み込んだ参照STEP)なら、
    「対象が無い」として扱う — `import_step` はボディを常にアクティブにする(M0の契約)ので、
    参照部品を読んだ直後の文書では join が最初の板を作れなくなる。受入対象(棚取付具を
    参照として読み、その隣に板を作る)がまさにこの経路である(2026-08-11のQAで発見)。
    **明示的に参照ボディを指定された場合は今までどおり ValueError**(取り違えを黙認しない)。
    """
    if body_id is None:
        active = doc.active_body()
        if active is not None and doc.is_editable(active.id):
            body_id = active.id

    if body_id is None:
        if mode == "cut":
            raise ValueError("cut requires a target body")
        return None, None

    if not doc.is_editable(body_id):
        raise ValueError(
            f"body {body_id!r} is not editable (kind='reference'). "
            "make_editable_copy() で編集用のソリッドを作ってから押し出すこと"
        )
    body = doc.get_body(body_id)
    if body.shape is None:
        raise ValueError(f"body {body_id!r} has no shape to extrude")
    return body, body_id


def _resolve_faces_and_normal(
    doc: Document, refs: Sequence[RegionRef]
) -> tuple[List[Any], tuple]:
    """領域参照から面と、スケッチ平面の法線を取り出す。範囲外は ValueError。

    **1回の押し出しが対象にできるのは1つのスケッチの領域だけ**。押し出しの向きは
    スケッチ平面の法線で決まるので、別平面の領域を混ぜると「最初のスケッチの法線」で
    全部押すことになり、方向がその面に寝た領域は**体積0の退化ソリッド**になる。
    それは `IsDone()` も `BRepCheck_Analyzer` も通ってしまう(2026-08-11のQAが実測。
    体積は期待の半分なのに例外は出なかった)ので、ここで弾くのが唯一の関門である。
    """
    sketch_ids = {ref.sketch for ref in refs}
    if len(sketch_ids) > 1:
        raise ValueError(
            f"extrude takes regions of a single sketch, got {sorted(sketch_ids)!r} "
            "(押し出しの向きはスケッチ平面の法線で決まるため)"
        )

    sketch_id = refs[0].sketch
    detailed = _regions_detailed(doc, sketch_id)  # Splitterは1スケッチにつき1回で足りる
    faces: List[Any] = []
    for ref in refs:
        match = next((item for item in detailed if item["index"] == ref.index), None)
        if match is None:
            raise ValueError(
                f"region index out of range: {ref.index} (sketch {ref.sketch!r})"
            )
        faces.append(match["face"])

    sketch = get_sketch(doc, sketch_id)
    support = None
    if sketch.plane.get("kind") == "face":
        # 貼った先のボディが消えていれば KeyError(以前は StopIteration で理由が読めなかった)
        body = doc.get_body(sketch.plane["body"])
        support = kernel.face_of(body.shape, int(sketch.plane["index"]))
    normal = kernel.sketch_plane_normal(sketch.plane, face=support)
    return faces, normal



# --------------------------------------------------------------------------------------
# verbの登録(oplog は verb名の分岐を持たない)
# --------------------------------------------------------------------------------------
def _do_extrude(document: Document, operation: Operation) -> None:
    """redo / replay 時の再適用。**ログには触らない**(触ると二重記録になる)。"""
    args = operation.args
    refs = [RegionRef.from_dict(item) for item in args["regions"]]
    faces, normal = _resolve_faces_and_normal(document, refs)
    body_id = args["body_id"]
    mode = args["mode"]
    distance = float(args["distance"])

    # **ここは存在検査**(無いことが「新しく作る」への分岐条件で、失敗ではない)。
    # `get_body` は無ければ KeyError なので使わない。
    existing = next(
        (candidate for candidate in document.bodies if candidate.id == body_id), None
    )
    if existing is None:
        if mode != "join":
            raise ValueError(f"cannot replay {mode} without target body {body_id!r}")
        result = kernel.extrude_regions(
            faces, distance, mode, normal, target_shape=None
        )
        document.add_body(
            Body(id=body_id, name="solid", kind="solid", shape=result["shape"])
        )
        remember(document, body_id, operation.seq, None)
        return

    if not document.is_editable(body_id):
        raise ValueError(f"body {body_id!r} is not editable")
    previous_shape = existing.shape
    result = kernel.extrude_regions(
        faces, distance, mode, normal, target_shape=existing.shape
    )
    existing.shape = result["shape"]
    remember(document, body_id, operation.seq, previous_shape)


def _undo_extrude(document: Document, operation: Operation) -> None:
    """undo: 押し出す前の形状に戻す(新しく作ったボディなら取り除く)。"""
    body_id = operation.args["body_id"]
    previous = recall(document, body_id, operation.seq)
    if previous is None:
        document.remove_body(body_id)
        return
    document.get_body(body_id).shape = previous


register_verb(VERB_EXTRUDE, do=_do_extrude, undo=_undo_extrude)
