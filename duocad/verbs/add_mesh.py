"""verb `add_mesh_body` — 三角形メッシュのボディを1つ足す。

**形はログの中にある**。`args["positions"]` / `args["indices"]` に三角形群をそのまま
持つので、再生(redo / `open_document`)は外部ファイルを一切読まない —— `import_step` が
STEP本文を `args["step"]` へ生で持つのと同じ規律である。

**元になったB-repを args に持たない**(「このボディをテッセレーションした」とは記録しない)。
持つと、元のボディをその後に押し出した文書を再生したときに**別の形のメッシュが復元される**
(再生は頭からログを流すので、元の形はもう当時のものではない)。形状最適化の結果のように
そもそも元のB-repが存在しないメッシュも同じ経路で入る。

**IDを `args["body_id"]` に記録する**(`import_step` と同じ理由)。記録が無いと再生で別の
IDが振られ、保存した選択集合や配信の差分が指す先を失う。

**このverbはログが太る**。三角形1枚あたり数値が9個(頂点は共有されるので実際はもっと少ない)
なので、数万三角形のメッシュを足したモデルのJSONは数MBになる。それでも埋め込むのは、
**外部ファイルへの参照にすると保存したモデルが単体で開けなくなる**ため。
"""
from __future__ import annotations

from typing import Optional, Sequence

from ..document import Body, BodyId, Document, MESH_KIND, make_mesh, new_body_id
from .. import naming
from ..oplog import Operation, register_verb
from ..entry import entry

VERB_ADD_MESH_BODY = "add_mesh_body"


def add_mesh_body(
    document: Document,
    positions: Sequence[float],
    indices: Sequence[int],
    name: Optional[str] = None,
) -> BodyId:
    """三角形メッシュのボディを足して、そのIDを返す。`name` を省くと通し番号の名前(`duocad/naming.py`)。

    `positions` は x,y,z を平らに並べた頂点座標[mm]、`indices` は**0始まり**の頂点添字を
    3つずつ並べた三角形。検査(3の倍数・添字の範囲・三角形0枚)は `Mesh.__post_init__`。

    **検査に落ちたら文書もログも変えない** —— メッシュを先に組み立ててから `add_body` する。
    """
    mesh = make_mesh(positions, indices)
    body_id = new_body_id()
    numbered: dict = {}
    if name is None:
        number = naming.allocate(document, naming.BODY)
        body_name = naming.label(naming.BODY, number)
        numbered = {"number": number}
    else:
        body_name = str(name)
    _apply(document, body_id, body_name, mesh)
    document.log.append(
        VERB_ADD_MESH_BODY,
        {
            "body_id": body_id,
            "name": body_name,
            **numbered,
            # **タプルではなくリストで積む**(JSONに載る形をログの時点でそろえる)。
            "positions": list(mesh.positions),
            "indices": list(mesh.indices),
        },
    )
    return body_id


def _apply(document: Document, body_id: BodyId, name: str, mesh) -> None:
    document.add_body(Body(id=body_id, name=name, kind=MESH_KIND, mesh=mesh))


def _do_add_mesh_body(document: Document, operation: Operation) -> None:
    """redo / replay: 記録したIDと三角形からそのまま復元する。

    公開APIの `add_mesh_body` を呼び直してはいけない(ログに二重記録される)。
    """
    args = operation.args
    if args.get("number") is not None:
        document.log.note_number(naming.BODY, int(args["number"]))
    _apply(
        document,
        str(args["body_id"]),
        str(args["name"]),
        make_mesh(args["positions"], args["indices"]),
    )


def _undo_add_mesh_body(document: Document, operation: Operation) -> None:
    document.remove_body(str(operation.args["body_id"]))


register_verb(
    VERB_ADD_MESH_BODY,
    do=_do_add_mesh_body,
    undo=_undo_add_mesh_body,
)


# --------------------------------------------------------------------------------------
# 入口(`duocad/entry.py`) —— 上の verb を掛け、応答に載せるものを返す
# --------------------------------------------------------------------------------------
@entry(VERB_ADD_MESH_BODY)
def _enter_add_mesh_body(
    document: Document, positions: Sequence[float], indices: Sequence[int], name: Optional[str] = None
) -> dict:
    body_id = add_mesh_body(document, positions, indices, None if name is None else str(name))
    return {"body_id": body_id}
