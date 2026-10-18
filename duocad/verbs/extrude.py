"""verb `extrude` — スケッチの**領域**、または**同一平面・同一ボディのソリッドの面群**を押し出す。

領域は `extrude(doc, regions, ...)`、面は `extrude_faces(doc, faces, ...)`。どちらも同じ verb `extrude`
として記録し、ログの args が `regions` か `faces` かで再生を分ける。

- `"join"` … 対象ボディへ足す。対象が無ければ新しいソリッドを作る(最初の板)。作った場合も
  `args["body_id"]` にそのIDを記録する(再生で同じIDが再現できないと選択が壊れる)。
- `"cut"` … 対象ボディから引く。対象が無ければ ValueError。面のとき、行き先の既定はその面を持つ
  ボディ。距離の正は面の外向き法線側なので、面から削るには負の距離で `cut`。
- `"new"` … 対象が何であっても常に新しいソリッドを作る(これが無いと既存部品がいる文書で2つ目の板が
  作れない)。`body_id` は受け付けない。

失敗(領域が空・添字が範囲外・`mode` が未知・距離0・ブーリアン失敗)は例外を送出し、documentにも
ログにも変更を残さない。「切り取ったのに穴が1つも開いていない」を成功として通さないこと。
"""
from __future__ import annotations

from typing import Any, List, Optional, Sequence

from .. import kernel_extrude, kernel_sketch, naming
from ..document import Body, BodyId, Document, new_body_id
from ..oplog import Operation, register_verb
from .. import selection
from ..entry import entry
from ._common import editable_body, recall, remember, replayed_body, resolve_shape_ref
from ..region import RegionRef, _regions_detailed
from ..sketch import get_sketch

VERB_EXTRUDE = "extrude"

#: 押し出しの種別(join | cut ＋ new)
MODES = ("join", "cut", "new")


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

    result = kernel_extrude.extrude_regions(
        faces, distance, mode, normal, target_shape=target_shape
    )

    created_new = False
    previous_shape: Any = None
    numbered: dict = {}
    if target_body is None:
        resolved_body_id = new_body_id()
        numbered = _add_numbered_body(doc, resolved_body_id, result["shape"])
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
            **numbered,
        },
    )
    operation.provenance = result["provenance"]
    remember(doc, resolved_body_id, operation.seq, None if created_new else previous_shape)
    return operation


def extrude_faces(
    doc: Document,
    faces: Sequence[dict],
    distance: float,
    mode: str = "join",
    body_id: Optional[BodyId] = None,
) -> Operation:
    """同一ボディの平面の面群(`{"body", "index"}` の並び)を押し出す。

    失敗時は例外を送出し、**documentにもログにも変更を残さない**(幾何計算をログの append より先に済ませる)。
    """
    distance = float(distance)
    if distance == 0.0:
        raise ValueError("distance must not be 0 (押し出し量0は形状を変えない)")
    if mode not in MODES:
        raise ValueError(f"unknown extrude mode: {mode!r} (one of {list(MODES)})")

    source, indices = _resolve_face_source(doc, faces, require_visible=True)
    if mode != "new" and body_id is None:
        body_id = source.id
    target_body, resolved_body_id = _resolve_target(doc, mode, body_id)
    target_shape = None if target_body is None else target_body.shape

    result = kernel_extrude.extrude_faces(
        source.shape, indices, distance, mode, target_shape=target_shape
    )

    previous_shape: Any = None
    numbered: dict = {}
    if target_body is None:
        resolved_body_id = new_body_id()
        numbered = _add_numbered_body(doc, resolved_body_id, result["shape"])
    else:
        previous_shape = target_body.shape
        target_body.shape = result["shape"]

    operation = doc.log.append(
        VERB_EXTRUDE,
        {
            "faces": [{"body": source.id, "index": index} for index in indices],
            "distance": distance,
            "mode": mode,
            "body_id": resolved_body_id,
            **numbered,
        },
    )
    operation.provenance = result["provenance"]
    remember(doc, resolved_body_id, operation.seq, previous_shape)
    return operation


def _resolve_face_source(
    doc: Document, faces: Sequence[Any], *, require_visible: bool
) -> tuple[Body, List[int]]:
    """面の参照から (面を持つボディ, 添字の並び) を取る。**別のボディにまたがる選択は ValueError**。
    新しい押し出しは面の持ち主に「見えないものは指せない」を課す(`editable_body` と同じ理由)。redo / 再生は課さない
    (`replayed_body`)。"""
    refs = list(faces)
    if not refs:
        raise ValueError("extrude requires at least one face")
    source: Optional[Body] = None
    indices: List[int] = []
    for position, ref in enumerate(refs, start=1):
        body, index = resolve_shape_ref(
            doc, ref, kind="face", what="extrude face", mesh_hint="(メッシュボディの面は押し出せない)"
        )
        if source is None:
            source = body
        elif body.id != source.id:
            raise ValueError(
                f"selected face #{position} belongs to body {body.id!r} but #1 belongs to "
                f"{source.id!r}: 別のボディにまたがる面は押し出せない(同一ボディの面だけ)"
            )
        indices.append(index)
    assert source is not None
    if require_visible and not source.visible:
        raise ValueError(
            f"body {source.id!r} is hidden: 見えないものは指せない(set_visible で表示すること)"
        )
    return source, indices


def _resolve_target(
    doc: Document, mode: str, body_id: Optional[BodyId], what: str = VERB_EXTRUDE
) -> tuple[Optional[Body], Optional[BodyId]]:
    """対象ボディを決める。cut で対象が無い・reference は ValueError。

    `new` は対象を決めない(常に新規作成)。`body_id` を渡されたら ValueError。

    `body_id` を省略したときのアクティブボディが編集できない(参照ボディ)なら「対象が無い」として扱い、
    join でも最初の板を作れるようにする。この分岐へ落ちる経路は現在は無い(`import_step` は参照ボディの
    後に編集用ボディを足す)が、アクティブが参照ボディになる verb が増えても join が最初の板を作れなく
    ならないよう残してある。明示的に参照ボディを指定された場合は ValueError。
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

    # 対象ボディ(既定はアクティブ)は選択の外から来る。人が隠した後にAIが押し出すと見えないボディが
    # 黙って変わる(`editable_body` と同じ穴)ので、`editable_body` が見えないボディを断る。
    return _editable_target(doc, body_id, what), body_id


def _editable_target(doc: Document, body_id: BodyId, what: str = VERB_EXTRUDE) -> Body:
    """押し出し・回転体の対象にできるボディ(編集可・可視・形状あり。メッシュは「足す/引く対象にする」ことを断る)。"""
    return editable_body(doc, body_id, what=what, reject_as=_reject_as(what))


def _replayed_target(doc: Document, body_id: BodyId, what: str = VERB_EXTRUDE) -> Body:
    """redo / 再生で足す・引く対象。`_editable_target` から可視性の門だけを外したもの(`_common.replayed_body`)。"""
    return replayed_body(doc, body_id, what=what, reject_as=_reject_as(what))


def _reject_as(what: str) -> str:
    return "押し出しの対象にする" if what == VERB_EXTRUDE else "足す/引く対象にする"


def _add_numbered_body(doc: Document, body_id: BodyId, shape: Any) -> dict:
    """押し出した形を新しいソリッドとして、通し番号の名前で足す。戻り値はログの args へ足すもの。"""
    number = naming.allocate(doc, naming.BODY)
    _add_new_body(doc, body_id, shape, naming.label(naming.BODY, number))
    return {"number": number}


def _add_new_body(doc: Document, body_id: BodyId, shape: Any, name: str) -> None:
    """押し出した形を新しいソリッドとして足す(`join` で対象が無いとき・`new`・その再生)。"""
    doc.add_body(Body(id=body_id, name=name, kind="solid", shape=shape))


def _recorded_name(document: Document, args: dict) -> str:
    """redo / 再生で作り直すボディの名前(番号の記録が無い古いログは "solid")。"""
    return naming.recorded(document, naming.BODY, args.get("number"), "solid")


def _resolve_faces_and_normal(
    doc: Document, refs: Sequence[RegionRef], what: str = VERB_EXTRUDE
) -> tuple[List[Any], tuple]:
    """領域参照から面と、スケッチ平面の法線を取り出す。範囲外は ValueError。

    **1回の押し出しが対象にできるのは1つのスケッチの領域だけ**。向きはスケッチ平面の法線で決まり、
    別平面の領域を混ぜると最初のスケッチの法線で全部押すことになって、方向がその面に寝た領域は
    体積0の退化ソリッドになる。`IsDone()` も `BRepCheck_Analyzer` も通ってしまうので、ここで弾くのが
    唯一の関門。回転体(`revolve`)も軸がそのスケッチの平面上にあるので同じ制約を持つ。
    """
    sketch_ids = {ref.sketch for ref in refs}
    if len(sketch_ids) > 1:
        raise ValueError(
            f"{what} takes regions of a single sketch, got {sorted(sketch_ids)!r} "
            "(向きと軸はスケッチ平面で決まるため)"
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
    # 面に貼ったスケッチでもライブの面は引かない。法線は作成時に `plane["frame"]` へ焼き付けてある
    # (`kernel_sketch.face_frame`)。添字で引き直すと、貼った先のボディを後から面取りしただけで
    # 押し出し方向が別の面の法線へ移りうる。
    normal = kernel_sketch.sketch_plane_normal(sketch.plane)
    return faces, normal



# --------------------------------------------------------------------------------------
# verbの登録
# --------------------------------------------------------------------------------------
def _do_extrude(document: Document, operation: Operation) -> None:
    """redo / replay 時の再適用。**ログには触らない**(触ると二重記録になる)。"""
    args = operation.args
    body_id = args["body_id"]
    mode = args["mode"]
    distance = float(args["distance"])
    if "faces" in args:
        _redo_faces(document, operation, args["faces"], distance, mode, body_id)
        return
    refs = [RegionRef.from_dict(item) for item in args["regions"]]
    faces, normal = _resolve_faces_and_normal(document, refs)

    # 存在検査(無いことは「新しく作る」への分岐条件で、失敗ではない)。`get_body` は KeyError なので使わない。
    existing = next(
        (candidate for candidate in document.bodies if candidate.id == body_id), None
    )

    # `new` で同じIDが既にいるなら再生の前提が壊れているので、上書き・融合させず落とす。
    if mode == "new" and existing is not None:
        raise ValueError(f"cannot replay new: body {body_id!r} already exists")
    if existing is None and mode == "cut":
        raise ValueError(f"cannot replay {mode} without target body {body_id!r}")
    if mode == "new" or existing is None:
        result = kernel_extrude.extrude_regions(faces, distance, mode, normal, target_shape=None)
        _add_new_body(document, body_id, result["shape"], _recorded_name(document, args))
        remember(document, body_id, operation.seq, None)
    else:
        target = _replayed_target(document, body_id)
        previous_shape = target.shape
        result = kernel_extrude.extrude_regions(faces, distance, mode, normal, target_shape=target.shape)
        target.shape = result["shape"]
        remember(document, body_id, operation.seq, previous_shape)
    if operation.provenance is None:
        operation.provenance = result["provenance"]


def _redo_faces(
    document: Document, operation: Operation, faces: Sequence[Any], distance: float, mode: str, body_id: BodyId
) -> None:
    """面のログの再適用。記録した添字で面を引き直す(形が決定的なので同じ面を指す)。"""
    source, indices = _resolve_face_source(document, faces, require_visible=False)
    existing = next((body for body in document.bodies if body.id == body_id), None)
    if mode == "new":
        if existing is not None:
            raise ValueError(f"cannot replay new: body {body_id!r} already exists")
        result = kernel_extrude.extrude_faces(source.shape, indices, distance, mode)
        _add_new_body(document, body_id, result["shape"], _recorded_name(document, operation.args))
        remember(document, body_id, operation.seq, None)
    else:
        if existing is None:
            raise ValueError(f"cannot replay {mode} without target body {body_id!r}")
        target = _replayed_target(document, body_id)
        previous_shape = target.shape
        result = kernel_extrude.extrude_faces(
            source.shape, indices, distance, mode, target_shape=previous_shape
        )
        target.shape = result["shape"]
        remember(document, body_id, operation.seq, previous_shape)
    if operation.provenance is None:
        operation.provenance = result["provenance"]


def _undo_extrude(document: Document, operation: Operation) -> None:
    """undo: 押し出す前の形状に戻す(新しく作ったボディなら取り除く)。"""
    body_id = operation.args["body_id"]
    previous = recall(document, body_id, operation.seq)
    if previous is None:
        document.remove_added_body(body_id)
        return
    document.get_body(body_id).shape = previous


register_verb(VERB_EXTRUDE, do=_do_extrude, undo=_undo_extrude)


# --------------------------------------------------------------------------------------
# 入口(`duocad/entry.py`) —— 対象を選択から取り、上の verb を掛け、応答に載せるものを返す
# --------------------------------------------------------------------------------------
@entry(VERB_EXTRUDE)
def _enter_extrude(
    document: Document, distance: float, mode: str = "join", body_id: Optional[BodyId] = None
) -> dict:
    """選択の領域、または同一平面・同一ボディの面を押し出す(混在は ValueError)。

    `body_id` だけは引数のまま(どの Body へ足す/から引くかであって、対象集合ではない)。
    """
    kind, items = selection.require_extrude_targets(document)
    if kind == "face":
        operation = extrude_faces(document, items, float(distance), mode=str(mode), body_id=body_id)
    else:
        refs = [RegionRef.from_dict(item) for item in items]
        operation = extrude(document, refs, float(distance), mode=str(mode), body_id=body_id)
    changed = operation.args["body_id"]
    return {
        "body_id": changed,
        "distance": float(distance),
        "mode": str(mode),
        "changed_bodies": [changed],
    }
