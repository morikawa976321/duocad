"""verb `mirror_body` — 選択の面/辺/頂点が属するボディを、鏡映面に対して鏡映複製する。

**常に複製を作る**(`move_body` と違い in-place反転の口は無い —— 本人の依頼は「鏡像反転
コピーを作る」の1点だけ)。対象は `move_body` と同じ解決(`_common.editable_bodies`。選択中の
面/辺/頂点が属するボディ。複数のボディにまたがる選択なら全部を同じ鏡映面で複製する)。

鏡映面は次のどちらか一方(**両方渡す/どちらも渡さないのは ValueError**):

- `base_plane` … XY/XZ/YZ(原点通過)。**`sketch_create` と同じ座標定義を流用する**
  (`sketch.base_plane` + `kernel_sketch.sketch_plane_frame`)。別々に定義すると同じ"XY"の意味が
  食い違う。
- `plane_face` … 既存ソリッドの平面な面への参照 `{"body": body_id, "index": 面の1始まりの
  添字}`。**対象の選択とは別引数で受ける**(`move_body.axis_edge` と同じ理由 ——
  対象集合に混ぜると、それが鏡映される側か基準かをコアが区別できない)。この面自身が
  対象ボディに属していてもよい。非平面の面は `kernel_sketch.face_frame`(内部で
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

from typing import Any, Optional, Sequence

from .. import kernel, kernel_sketch
from .. import sketch
from ..document import BodyId, Document
from ..oplog import Operation, register_verb
from .. import selection
from ..entry import entry
from ._common import (
    add_transformed_copies,
    copy_ids,
    editable_bodies,
    remove_created_bodies,
    resolve_shape_ref,
)

VERB_MIRROR_BODY = "mirror_body"
_MESH_HINT = "(メッシュボディには面が無い)"


def _mirror_plane(doc: Document, base_plane: Any, plane_face: Any) -> dict:
    """鏡映面の引数を検査して、ログに載せる数値 `{plane_point, plane_normal}` にする。"""
    if base_plane is not None and plane_face is not None:
        raise ValueError(
            "base_plane と plane_face は同時に渡せない(どちらが勝つかを暗黙にしない)"
        )
    if base_plane is not None:
        plane_ref = sketch.base_plane(str(base_plane))
        frame = kernel_sketch.sketch_plane_frame(plane_ref)
    elif plane_face is not None:
        body, index = resolve_shape_ref(doc, plane_face, kind="face", what="plane_face", mesh_hint=_MESH_HINT)
        frame = kernel_sketch.face_frame(body.shape, index)
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
    """選択の面/辺/頂点が属するボディを、鏡映面に対して鏡映複製する(常に複製)。

    `created_ids` は**再生用**(省略すると新しいIDを振る)。失敗したときは文書にもログにも
    何も残さない。
    """
    bodies = editable_bodies(doc, list(body_ids), verb="mirror_body", what="mirror")
    plane_args = _mirror_plane(doc, base_plane, plane_face)
    point = plane_args["plane_point"]
    normal = plane_args["plane_normal"]

    new_ids = copy_ids(len(bodies), created_ids, verb="mirror_body")
    numbers = add_transformed_copies(
        doc,
        bodies,
        lambda shape: kernel.mirror_shape(shape, point, normal, copy=True),
        new_ids,
        allocate=True,
    )

    return doc.log.append(
        VERB_MIRROR_BODY,
        {
            "bodies": [body.id for body in bodies],
            **plane_args,
            "created": list(new_ids),
            "numbers": numbers,
        },
    )


def _do_mirror_body(document: Document, operation: Operation) -> None:
    """redo / replay: 記録されたボディ・鏡映面で鏡映し直す(ログには触らない)。"""
    args = operation.args
    bodies = [document.get_body(body_id) for body_id in args["bodies"]]
    point = [float(value) for value in args["plane_point"]]
    normal = [float(value) for value in args["plane_normal"]]
    new_ids = copy_ids(len(bodies), list(args.get("created") or []), verb="mirror_body")
    add_transformed_copies(
        document,
        bodies,
        lambda shape: kernel.mirror_shape(shape, point, normal, copy=True),
        new_ids,
        args.get("numbers"),
    )


def _undo_mirror_body(document: Document, operation: Operation) -> None:
    """undo: 作った複製を消す(元のボディは触らない)。"""
    remove_created_bodies(document, operation.args.get("created") or [])


register_verb(VERB_MIRROR_BODY, do=_do_mirror_body, undo=_undo_mirror_body)


# --------------------------------------------------------------------------------------
# 入口(`duocad/entry.py`) —— 対象を選択から取り、上の verb を掛け、応答に載せるものを返す
# --------------------------------------------------------------------------------------
@entry(VERB_MIRROR_BODY)
def _enter_mirror_body(
    document: Document, base_plane: Optional[str] = None, plane_face: Any = None
) -> dict:
    """**元は動かないので `changed_bodies` は複製だけ**(`move_body` と違い、動いたボディを
    取り直す必要がない)。"""
    body_ids = selection.require_bodies(document)
    args = mirror_body(document, body_ids, base_plane=base_plane, plane_face=plane_face).args
    created = list(args["created"])
    return {
        "plane_point": args["plane_point"],
        "plane_normal": args["plane_normal"],
        "created": created,
        "changed_bodies": created,
    }
