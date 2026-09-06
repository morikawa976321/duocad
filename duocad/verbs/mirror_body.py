"""verb `mirror_body` — 確定選択の面/辺/頂点が属するボディを、鏡映面に対して鏡映複製する。

**常に複製を作る**(`move_body` と違い in-place反転の口は無い —— 本人の依頼は「鏡像反転
コピーを作る」の1点だけ)。対象は `move_body._targets` と同じ解決(選択中の面/辺/頂点が
属するボディ。複数のボディにまたがる選択なら全部を同じ鏡映面で複製する)。

鏡映面は次のどちらか一方(**両方渡す/どちらも渡さないのは ValueError**):

- `base_plane` … XY/XZ/YZ(原点通過)。**`sketch_create` と同じ座標定義を流用する**
  (`sketch.base_plane` + `kernel.sketch_plane_frame`)。別々に定義すると同じ"XY"の意味が
  食い違う。
- `plane_face` … 既存ソリッドの平面な面への参照 `{"body": body_id, "index": 面の1始まりの
  添字}`。**対象の確定選択とは別引数で受ける**(`move_body.axis_edge` と同じ理由 ——
  対象集合に混ぜると、それが鏡映される側か基準かをコアが区別できない)。この面自身が
  対象ボディに属していてもよい。非平面の面は `kernel.face_frame`(内部で
  `require_planar_face` を通す)が ValueError にする。

ログには解決済みの数値 `{"plane_point", "plane_normal"}` だけを載せる(`move_body` の軸と
同じ理由 —— 鏡映対象にその面自身が含まれる場合、複製後に参照を引き直すと別の面になる)。

形状の鏡映は `kernel.mirror_shape`(OCCTの `gp_Trsf.SetMirror` 相当)。

**鏡映は向きを反転する変換**(行列式が負)。複製したボディの面の法線は元と逆向きになりうる
—— `query.face_fingerprint`(面積・法線で照合)は**同じボディの同じ添字**を時点をまたいで
照合するためのものであり、鏡映が作る新しいボディは元とは別の body_id を持つので既存の指紋
とは独立している(衝突しない)。

**元と完全に重なる複製を作らないことは検査しない**(`move_body` の回転と同じ判断 ——
境界箱の一致では見抜けない形状があり、ブーリアンの共通部分で測るのは大きなモデルで重い。
不健全な判定を入れるより、断らずに通すほうを選んでいる)。

**OCP / build123d を直接importしない**(幾何は `duocad.kernel` 越し)。
"""
from __future__ import annotations

from typing import Any, List, Optional, Sequence

from .. import kernel
from .. import sketch
from ..document import Body, BodyId, Document, new_body_id
from ..oplog import Operation, register_verb
from ._common import editable_body

VERB_MIRROR_BODY = "mirror_body"


def _targets(doc: Document, body_ids: Sequence[BodyId]) -> List[Body]:
    """鏡映するボディを検査して返す(`move_body._targets` と同じ規律)。"""
    if not body_ids:
        raise ValueError("mirror_body requires at least one body")
    return [
        editable_body(doc, body_id, what="mirror", require_visible=True)
        for body_id in body_ids
    ]


def _shape_ref(doc: Document, ref: Any, *, kind: str, what: str) -> tuple:
    """`{"body": BodyId, "index": int}` を (Body, 添字) にする(`move_body._shape_ref` と同じ形)。

    **`commit_selection` を経由しない**(モジュールdocstring)。
    """
    if not isinstance(ref, dict):
        raise ValueError(
            f"{what} must be a reference like "
            '{"body": "<body_id>", "index": 1}, got ' + repr(ref)
        )
    if "body" not in ref or "index" not in ref:
        raise ValueError(
            f'{what} needs both "body" and "index" '
            '(例: {"body": "<body_id>", "index": 1}), got ' + repr(ref)
        )
    declared = ref.get("kind")
    if declared is not None and str(declared) != kind:
        raise ValueError(f"{what} must be a {kind!r} reference, got kind={declared!r}")
    body = doc.get_body(str(ref["body"]))  # 存在しないIDは KeyError(document側の契約)
    if body.shape is None:
        raise ValueError(
            f"{what}: body {body.id!r} has no B-rep shape (メッシュボディには面が無い)"
        )
    try:
        index = int(ref["index"])
    except (TypeError, ValueError):
        raise ValueError(f"{what}: index must be an integer, got {ref['index']!r}") from None
    return body, index


def _mirror_plane(doc: Document, base_plane: Any, plane_face: Any) -> dict:
    """鏡映面の引数を検査して、ログに載せる数値 `{plane_point, plane_normal}` にする。"""
    if base_plane is not None and plane_face is not None:
        raise ValueError(
            "base_plane と plane_face は同時に渡せない(どちらが勝つかを暗黙にしない)"
        )
    if base_plane is not None:
        plane_ref = sketch.base_plane(str(base_plane))
        frame = kernel.sketch_plane_frame(plane_ref)
    elif plane_face is not None:
        body, index = _shape_ref(doc, plane_face, kind="face", what="plane_face")
        frame = kernel.face_frame(body.shape, index)
    else:
        raise ValueError(
            "mirror_body needs a mirror plane: base_plane(XY/XZ/YZ) か "
            "plane_face(既存の平面な面への参照)のどちらか"
        )
    return {
        "plane_point": [float(value) for value in frame["origin"]],
        "plane_normal": [float(value) for value in frame["normal"]],
    }


def mirror_body(
    doc: Document,
    body_ids: Sequence[BodyId],
    base_plane: Any = None,
    plane_face: Any = None,
    created_ids: Optional[Sequence[BodyId]] = None,
) -> Operation:
    """確定選択の面/辺/頂点が属するボディを、鏡映面に対して鏡映複製する(常に複製)。

    `created_ids` は**再生用**(省略すると新しいIDを振る)。失敗したときは文書にもログにも
    何も残さない。
    """
    bodies = _targets(doc, list(body_ids))
    plane_args = _mirror_plane(doc, base_plane, plane_face)
    point = plane_args["plane_point"]
    normal = plane_args["plane_normal"]

    new_ids = (
        [new_body_id() for _ in bodies] if created_ids is None else [str(v) for v in created_ids]
    )
    if len(new_ids) != len(bodies):
        raise ValueError(f"mirror_body: {len(bodies)} bodies but {len(new_ids)} ids recorded")

    prepared = [
        Body(
            id=new_id,
            name=body.name,
            kind=body.kind,
            shape=kernel.mirror_shape(body.shape, point, normal, copy=True),
            visible=body.visible,
        )
        for body, new_id in zip(bodies, new_ids)
    ]
    for body in prepared:
        doc.add_body(body)  # 同じIDが既にあれば ValueError(黙って重複させない)

    return doc.log.append(
        VERB_MIRROR_BODY,
        {
            "bodies": [body.id for body in bodies],
            **plane_args,
            "created": list(new_ids),
        },
    )


def _do_mirror_body(document: Document, operation: Operation) -> None:
    """redo / replay: 記録されたボディ・鏡映面で鏡映し直す(ログには触らない)。"""
    args = operation.args
    bodies = [document.get_body(body_id) for body_id in args["bodies"]]
    point = [float(value) for value in args["plane_point"]]
    normal = [float(value) for value in args["plane_normal"]]
    new_ids = list(args.get("created") or [])
    if len(new_ids) != len(bodies):
        raise ValueError(f"mirror_body: {len(bodies)} bodies but {len(new_ids)} ids recorded")
    for body, new_id in zip(bodies, new_ids):
        document.add_body(
            Body(
                id=new_id,
                name=body.name,
                kind=body.kind,
                shape=kernel.mirror_shape(body.shape, point, normal, copy=True),
                visible=body.visible,
            )
        )


def _undo_mirror_body(document: Document, operation: Operation) -> None:
    """undo: 作った複製を消す(元のボディは触らない)。"""
    for body_id in operation.args.get("created") or []:
        try:
            document.remove_body(body_id)
        except KeyError:
            # 既に消えている(別のundoが先に消した)場合は空振りでよい(`move_body` と同じ)。
            pass


register_verb(VERB_MIRROR_BODY, do=_do_mirror_body, undo=_undo_mirror_body)
