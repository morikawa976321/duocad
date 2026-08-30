"""verb `extrude` — スケッチの**領域**を押し出す。

`extrude_face`(既存ソリッドの面を押す)とは別のverbである。こちらは**領域の集合**を対象に
取り、`mode="join"` で足し、`mode="cut"` で引き、`mode="new"` で独立したボディにする。

- `"join"` … 対象ボディへ足す。**対象が無ければ新しいソリッドを1つ作る**(最初の板はこれで
  生まれる)。作った場合も `args["body_id"]` にそのIDを記録する(再生で同じIDが再現できないと
  選択が壊れるため)。
- `"cut"` … 対象ボディから引く。**対象が無ければ ValueError**(引く相手が無いのに成功した
  ことにしない)。
- `"new"` … **対象が何であっても、常に新しいソリッドを1つ作る**。`join` の「対象が無ければ
  作る」という**落ち方**を、種別として言えるようにしたもの —— これが無いと、既存部品がいる
  文書では2つ目の板が作れず(`join` が黙って融合する)、最初の板は空の文書でしか生まれない。
  **`body_id` は受け付けない**(行き先の無い操作に行き先を渡されたら取り違えである)。

**失敗を黙って通さない**: 領域が空・添字が範囲外・`mode` が未知・距離0・ブーリアン失敗の
いずれでも例外を送出し、**documentにもログにも変更を残さない**。とくに「切り取ったのに穴が
1つも開いていない」を成功として通さないこと。

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

#: 押し出しの種別(実装計画書§1「mode(join | cut)」＋ new)
MODES = ("join", "cut", "new")

#: (document) -> {(body_id, seq): 押し出し前の形状 | None(新規作成)}。


def extrude(
    doc: Document,
    regions: Sequence[RegionRef],
    distance: float,
    mode: str = "join",
    body_id: Optional[BodyId] = None,
) -> Operation:
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

    operation = doc.log.append(
        VERB_EXTRUDE,
        {
            "regions": [ref.to_dict() for ref in refs],
            "distance": distance,
            "mode": mode,
            "body_id": resolved_body_id,
        },
    )
    remember(doc, resolved_body_id, operation.seq, None if created_new else previous_shape)
    return operation


def _resolve_target(
    doc: Document, mode: str, body_id: Optional[BodyId]
) -> tuple[Optional[Body], Optional[BodyId]]:
    """対象ボディを決める。cut で対象が無い・reference は ValueError。

    **`new` は対象を決めない**(常に新規作成)。`body_id` を渡されたら ValueError。

    `body_id` を省略したときのアクティブボディが**編集できない**(参照ボディ)なら、
    「対象が無い」として扱い、join でも最初の板を作れるようにする。
    **2026-08-27の統合以降、この分岐へ落ちる経路は残っていない** —— `import_step` は
    参照ボディの**後に**編集用ボディを足すので、読み込み直後のアクティブは常に編集可能で、
    参照ボディを作る verb は他に無い。それでも握り潰さずに残してあるのは、アクティブが
    参照ボディである状態を作る verb が将来増えたときに、ここが黙って ValueError を投げる
    (= join が最初の板を作れない)側へ倒れないようにするためである。
    一方、**明示的に参照ボディを指定された場合は ValueError**(取り違えを黙認しない)。
    """
    if mode == "new":
        if body_id is not None:
            raise ValueError(
                f"new does not take a body_id (got {body_id!r}): "
                "新規ボディに行き先は無い。足すなら mode='join' を使うこと"
            )
        return None, None

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
            "参照ボディは読み込みの原本なので触れない —— import_step が同じソリッドに"
            "作る編集用ボディ(kind='solid')の body_id を使うこと"
        )
    body = doc.get_body(body_id)
    if body.shape is None:
        raise ValueError(f"body {body_id!r} has no shape to extrude")
    if not body.visible:
        # H・2026-08-20: 領域は選択から取るが、足す/引く**対象ボディ**(既定はアクティブ)は
        # 選択の外から来る。人がこのボディを隠した後にAIが押し出すと、見えなくなった
        # ボディが黙って変わる(`fillet`/`chamfer` と同じ穴。`_common.editable_body` の
        # docstring参照)。ここは `editable_body()` を経由しない専用の解決関数なので、
        # 同じ検査をここにも書く。
        raise ValueError(
            f"body {body_id!r} is hidden: 見えないものは指せない(set_visible で表示すること)"
        )
    return body, body_id


def _resolve_faces_and_normal(
    doc: Document, refs: Sequence[RegionRef]
) -> tuple[List[Any], tuple]:
    """領域参照から面と、スケッチ平面の法線を取り出す。範囲外は ValueError。

    **1回の押し出しが対象にできるのは1つのスケッチの領域だけ**。押し出しの向きは
    スケッチ平面の法線で決まるので、別平面の領域を混ぜると「最初のスケッチの法線」で
    全部押すことになり、方向がその面に寝た領域は**体積0の退化ソリッド**になる。
    それは `IsDone()` も `BRepCheck_Analyzer` も通ってしまう(体積が期待の半分でも
    例外は出ない)ので、ここで弾くのが唯一の関門である。
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
    # 面に貼ったスケッチでも**ライブの面は引かない**。押し出しの向きはスケッチ平面の法線で
    # 決まり、その法線は作成時に `plane["frame"]` へ焼き付けてある(`kernel.face_frame`)。
    # 添字で引き直していた頃は、貼った先のボディを後から面取りしただけで押し出し方向が
    # 別の面の法線へ静かに移りえた。
    normal = kernel.sketch_plane_normal(sketch.plane)
    return faces, normal



# --------------------------------------------------------------------------------------
# verbの登録
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

    if mode == "new":
        # **常に作る**(`join` のように既存へ合流しない)。同じIDが既にいるなら再生の
        # 前提が壊れているので、黙って上書き・融合させずに落とす。
        if existing is not None:
            raise ValueError(
                f"cannot replay new: body {body_id!r} already exists"
            )
        result = kernel.extrude_regions(faces, distance, mode, normal, target_shape=None)
        document.add_body(
            Body(id=body_id, name="solid", kind="solid", shape=result["shape"])
        )
        remember(document, body_id, operation.seq, None)
        return

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
    if not existing.visible:
        raise ValueError(
            f"body {body_id!r} is hidden: 見えないものは指せない(set_visible で表示すること)"
        )
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
