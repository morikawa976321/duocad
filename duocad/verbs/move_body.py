"""verb `move_body` — 選択中の面/辺/頂点が属するボディを動かす(平行移動 / 回転) / 複製する。

対象は「ボディ」だが選ぶのは面・辺・頂点のまま(ShapeRef が `body` を持つので「ボディ選択」の
種別は作らない。逆引きは `selection.require_bodies`)。**選択が複数のボディにまたがれば全部を
同じ量だけ動かす**(1呼び出し = 1オペレーション = 1 undo)。

`mode="translate"`(既定)は dx, dy, dz を、`mode="rotate"` は軸(点+向き、または直線の辺)と
角度[度]を取る。もう片方のモードの引数を渡すと ValueError。`copy=True` は元を残して独立した
形状として複製する。

**Body に変換フィールドは足さない。** 平行移動も回転も `kernel.translate_shape` /
`kernel.rotate_shape` が形状の位置(`TopLoc_Location`)を差し替えて表す。フィールドを足すと
形状を読む側全部(テセレーション・体積・STEP書き出し・領域検出・ピック)に掛け忘れの危険が出る。

添字は変わらないが、選択は他の形状変更verbと同じくリセットする(`catalog.py` の
`resets_selection=True`。添字が失効しないことを理由に例外にする案は却下)。
"""
from __future__ import annotations

import math
from typing import Any, Callable, List, Optional, Sequence

from .. import kernel
from ..document import Body, BodyId, Document
from ..oplog import Operation, register_verb
from .. import selection
from ..entry import entry
from ._common import (
    MODE_ROTATE,
    MODE_TRANSLATE,
    add_transformed_copies,
    copy_ids,
    editable_bodies,
    finite_numbers,
    inverse_move_args,
    is_rotation,
    reject_foreign_args,
    remove_created_bodies,
    require_move_mode,
    resolve_shape_ref,
    rotation_degrees,
)

VERB_MOVE_BODY = "move_body"
#: `mode` の宣言(`MODE_TRANSLATE` / `MODE_ROTATE` / `MOVE_MODES`)は `_common` が正。
_MESH_HINT = "(メッシュボディには軸に使える辺も頂点も無い)"


# --------------------------------------------------------------------------------------
# 引数の解決 —— ログに載せるのは解決した数値だけ
# --------------------------------------------------------------------------------------
#
# 辺・頂点への参照(`axis_edge` / `center_vertex`)をログに書かない。undo は逆向きに同じ角度を
# 回すが、そのとき軸に使った辺は既に回った後にいる(軸が対象のボディに属していれば必ず)ので、
# 参照を引き直すと別の軸になり形が戻らない。入口で数値へ潰し、`_do_*` / `_undo_*` は数値しか見ない。
def _vertex_point(doc: Document, ref: Any) -> List[float]:
    """頂点への参照を [x, y, z] にする。範囲外の添字は ValueError。"""
    body, index = resolve_shape_ref(doc, ref, kind="vertex", what="center_vertex", mesh_hint=_MESH_HINT)
    positions = kernel.vertex_positions(body.shape)
    if not 1 <= index <= len(positions):
        raise ValueError(
            f"center_vertex: vertex index out of range: {index} (1..{len(positions)})"
        )
    return [float(value) for value in positions[index - 1]["point"]]


def _composite_centroid(bodies: Sequence[Body]) -> List[float]:
    """対象**全部**をひとかたまりと見たときの体積重心(境界箱の中心は使わない)。

    対象ごとの重心で回すと、1オペレーションなのに各ボディがばらばらの剛体変換を受ける。
    """
    total = 0.0
    weighted = [0.0, 0.0, 0.0]
    for body in bodies:
        mass = kernel.volume(body.shape)
        center = kernel.centroid(body.shape)
        total += mass
        for axis in range(3):
            weighted[axis] += mass * center[axis]
    if not total > 0.0:
        raise ValueError(
            "move_body(mode='rotate'): 対象の体積が0で重心が決まらない "
            "(axis_point か center_vertex で回転中心を指定すること)"
        )
    return [value / total for value in weighted]


def _rotation_args(
    doc: Document,
    bodies: Sequence[Body],
    axis_edge: Any,
    axis_point: Any,
    axis_dir: Any,
    center_vertex: Any,
    angle: Any,
) -> dict:
    """回転の引数を検査して、ログに載せる数値 `{axis_point, axis_dir, angle}` にする。

    軸の**向き**は `axis_edge`(直線の辺)か `axis_dir`(数値)のどちらか一方から取る。
    軸の**位置**は次のどれか1つで決まる。2つ以上の同時指定は ValueError:

    1. `center_vertex` … 軸はその頂点を通る(`axis_edge` と組める: 辺が向き、頂点が位置)
    2. `axis_point`    … 軸上の点(`axis_dir` と組。`axis_edge` / `center_vertex` とは不可)
    3. `axis_edge` だけ … その辺が乗っている直線
    4. 何も指定しない  … 対象の合成重心(`_composite_centroid`)

    3Dの回転は軸線上のどの点を基準にしても同じ変換なので、位置が結果を変えるのは 1 と 4 だけ。
    """
    degrees = rotation_degrees(angle, verb="move_body", noun="ボディ")

    if axis_edge is not None and (axis_point is not None or axis_dir is not None):
        raise ValueError(
            "axis_edge と axis_point/axis_dir は同時に渡せない "
            "(辺が軸の位置も向きも決めるので、どちらが勝つかを暗黙にしない)"
        )
    if axis_point is not None and center_vertex is not None:
        raise ValueError(
            "axis_point と center_vertex は同時に渡せない "
            "(どちらも軸の位置を決めるので、片方が黙って無視される形にしない)"
        )
    if axis_edge is not None:
        axis_body, index = resolve_shape_ref(doc, axis_edge, kind="edge", what="axis_edge", mesh_hint=_MESH_HINT)
        point, direction = kernel.edge_axis(axis_body.shape, index)
    else:
        if axis_dir is None:
            raise ValueError(
                "move_body(mode='rotate') needs an axis: "
                "axis_edge(直線の辺)か axis_dir(向きのベクトル)のどちらか"
            )
        direction = finite_numbers(axis_dir, 3, what="axis_dir", form="[x, y, z]")
        if not math.sqrt(sum(value * value for value in direction)) > 0.0:
            raise ValueError("axis_dir must not be the zero vector (軸の向きが決まらない)")
        point = None if axis_point is None else finite_numbers(axis_point, 3, what="axis_point", form="[x, y, z]")

    if center_vertex is not None:
        point = _vertex_point(doc, center_vertex)
    if point is None:
        point = _composite_centroid(bodies)

    return {"axis_point": point, "axis_dir": direction, "angle": degrees}


def _translation_args(dx: Any, dy: Any, dz: Any) -> dict:
    """並進の引数を検査して、ログに載せる数値 `{dx, dy, dz}` にする。"""
    if dx is None or dy is None or dz is None:
        raise ValueError(
            f"move_body(mode='translate') needs dx, dy, dz "
            f"(got dx={dx!r}, dy={dy!r}, dz={dz!r})"
        )
    delta = (float(dx), float(dy), float(dz))
    if not all(math.isfinite(value) for value in delta):
        raise ValueError(f"dx/dy/dz must be finite numbers, got {delta!r}")
    if delta == (0.0, 0.0, 0.0):
        raise ValueError(
            "move_body needs a non-zero (dx, dy, dz) "
            "(0,0,0 は copy=False では何も変えず、copy=True では元と重なるボディを作る)"
        )
    return {"dx": delta[0], "dy": delta[1], "dz": delta[2]}


def move_body(
    doc: Document,
    body_ids: Sequence[BodyId],
    dx: Optional[float] = None,
    dy: Optional[float] = None,
    dz: Optional[float] = None,
    copy: bool = False,
    created_ids: Optional[Sequence[BodyId]] = None,
    *,
    mode: str = MODE_TRANSLATE,
    axis_edge: Any = None,
    axis_point: Any = None,
    axis_dir: Any = None,
    center_vertex: Any = None,
    angle: Optional[float] = None,
) -> Operation:
    """ボディを動かす(`copy=True` なら複製する)。

    - `mode="translate"`(既定) … (dx, dy, dz)[mm] だけ平行移動する。3つとも0・非有限値は ValueError。
    - `mode="rotate"` … 軸まわりに `angle` 度だけ回す(軸と中心の決め方は `_rotation_args`)。
      360の倍数は ValueError。

      **既知の穴**: `copy=True` で、対象がその軸まわりの回転対称を持つ角度(立方体を中心軸で
      90度など)を指すと、元と完全に重なる複製ができる。境界箱の一致では L字を180度回した場合まで
      誤って弾き、ブーリアンの共通部分は大きなモデルで重いので、不健全な判定を入れず通す。
    - モードが取らない引数を渡すと ValueError。
    - `created_ids` は再生用。省略すると新しいIDを振る。
    - 失敗したときは文書にもログにも何も残さない。
    """
    require_move_mode(mode, verb="move_body")

    if mode == MODE_ROTATE:
        reject_foreign_args(mode, {"dx": dx, "dy": dy, "dz": dz}, verb="move_body")
        # 既定の回転中心が対象の合成重心なので、対象を先に解決する。
        bodies = editable_bodies(doc, list(body_ids), verb="move_body", what="move")
        shaped = _rotation_args(
            doc, bodies, axis_edge, axis_point, axis_dir, center_vertex, angle
        )
    else:
        reject_foreign_args(
            mode,
            {
                "angle": angle,
                "axis_dir": axis_dir,
                "axis_edge": axis_edge,
                "axis_point": axis_point,
                "center_vertex": center_vertex,
            },
            verb="move_body",
        )
        shaped = _translation_args(dx, dy, dz)
        bodies = editable_bodies(doc, list(body_ids), verb="move_body", what="move")

    args = {"bodies": [body.id for body in bodies], "mode": mode, **shaped, "copy": bool(copy)}
    transform = _shape_transform(args)

    numbered: dict = {}
    if copy:
        new_ids = copy_ids(len(bodies), created_ids, verb="move_body")
        numbers = add_transformed_copies(
            doc, bodies, lambda shape: transform(shape, True), new_ids, allocate=True
        )
        numbered = {"numbers": numbers}
    else:
        new_ids = []
        _do_move(bodies, transform)

    return doc.log.append(
        VERB_MOVE_BODY,
        {
            **args,
            # 作ったIDはログが持つ(再生で同じIDになる)。
            "created": list(new_ids),
            **numbered,
        },
    )


def _shape_transform(args: dict) -> Callable:
    """ログの `args` から「形状 -> 変換後の形状」を作る。do も undo も再生もここを通る。"""
    if is_rotation(args):
        point = [float(value) for value in args["axis_point"]]
        direction = [float(value) for value in args["axis_dir"]]
        degrees = float(args["angle"])
        return lambda shape, copy: kernel.rotate_shape(
            shape, point, direction, degrees, copy=copy
        )
    delta = (float(args["dx"]), float(args["dy"]), float(args["dz"]))
    return lambda shape, copy: kernel.translate_shape(shape, *delta, copy=copy)


def _do_move(bodies: Sequence[Body], transform: Callable) -> None:
    """その場で動かす。**全部の形状を作ってから差し替える**(途中で落ちても半分動かない)。"""
    moved = [transform(body.shape, False) for body in bodies]
    for body, shape in zip(bodies, moved):
        body.shape = shape


# --------------------------------------------------------------------------------------
# verbの登録
# --------------------------------------------------------------------------------------
def _do_move_body(document: Document, operation: Operation) -> None:
    """redo / replay: 記録されたボディ・量で動かし直す(ログには触らない)。"""
    args = operation.args
    bodies = [document.get_body(body_id) for body_id in args["bodies"]]
    transform = _shape_transform(args)
    if args.get("copy"):
        new_ids = copy_ids(len(bodies), list(args.get("created") or []), verb="move_body")
        add_transformed_copies(
            document, bodies, lambda shape: transform(shape, True), new_ids, args.get("numbers")
        )
    else:
        _do_move(bodies, transform)


def _undo_move_body(document: Document, operation: Operation) -> None:
    """undo: 複製なら作ったボディを消し、移動なら逆向きに同じ量を動かす。

    スナップショットを使わないのは逆変換で戻るから(回転の丸め誤差は相対1e-16程度で、幾何の
    許容差より桁違いに小さい)。保存したログを再生せずに undo しても通る。
    """
    args = operation.args
    if args.get("copy"):
        remove_created_bodies(document, args.get("created") or [])
        return
    bodies = [document.get_body(body_id) for body_id in args["bodies"]]
    _do_move(bodies, _shape_transform(inverse_move_args(args, ("dx", "dy", "dz"))))


register_verb(VERB_MOVE_BODY, do=_do_move_body, undo=_undo_move_body)


# --------------------------------------------------------------------------------------
# 入口(`duocad/entry.py`) —— 対象を選択から取り、上の verb を掛け、応答に載せるものを返す
# --------------------------------------------------------------------------------------
@entry(VERB_MOVE_BODY)
def _enter_move_body(
    document: Document,
    dx: Optional[float] = None,
    dy: Optional[float] = None,
    dz: Optional[float] = None,
    copy: bool = False,
    mode: str = "translate",
    angle: Optional[float] = None,
    axis_edge: Any = None,
    axis_point: Any = None,
    axis_dir: Any = None,
    center_vertex: Any = None,
) -> dict:
    """`changed_bodies` には動いたボディと複製で増えたボディの両方を載せる(画面はここを見て形を取り直す)。

    戻り値のキーはモードによらず同じで、使わない側は `None`。`axis_point` / `axis_dir` は解決後の数値。
    """
    body_ids = selection.require_bodies(document)
    operation = move_body(
        document,
        body_ids,
        dx,
        dy,
        dz,
        copy=bool(copy),
        mode=str(mode),
        angle=angle,
        axis_edge=axis_edge,
        axis_point=axis_point,
        axis_dir=axis_dir,
        center_vertex=center_vertex,
    )
    args = operation.args
    created = list(args["created"])
    return {
        "mode": args["mode"],
        "dx": args.get("dx"),
        "dy": args.get("dy"),
        "dz": args.get("dz"),
        "angle": args.get("angle"),
        "axis_point": args.get("axis_point"),
        "axis_dir": args.get("axis_dir"),
        "copy": args["copy"],
        "created": created,
        "changed_bodies": list(args["bodies"]) + created,
    }
