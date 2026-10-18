"""verb `move_sketch_entities` — スケッチの点・辺を平行移動する / 複製する。

`mode="translate"`(既定)は dx, dy、`mode="rotate"` は角度[度]と回転中心を取る(`move_body` と同じ流儀)。
軸は常にスケッチ平面の法線でパラメータにしない。角度の符号は `pattern_circular` の `total_angle`
と同じ u->v の反時計回りが正。

対象は選択のスケッチ図形(`selection.SKETCH_ENTITY_KIND`)で、参照が持つ `point` が

    None       … その図形1つ全体(= 辺)。全部の点を同じ量だけ動かす = 形は変えない
    "x0" 等    … その1点だけ(線の端点・円/弧の中心・長方形の左下・ベジエの制御点)

を意味する。**寸法(半径・幅高さ・角度)は動かさない**(寸法を変えると既存ソリッドの再計算が絡む)。

`copy=True` は元を残し、**対象が属する図形をまるごと複製**して動かした位置に置く(点だけの複製は
できない)。複製は反復(`Pattern`)には入らず、素の図形になる。

同じ図形に「辺」と「点」が同時に選ばれていたら**辺(全体)が勝つ**(両方適用すると点が2回動いて歪む)。

**既存ソリッドは追従しない**。`set_parameter` と違って `rebuild()` を呼ばない。押し出し済みの
ソリッドは動かずスケッチの線だけが動く(このverbは操作ログ上、押し出しより後ろに載る)。
"""
from __future__ import annotations

import math
from typing import Any, Callable, Dict, List, Optional, Sequence

from .. import parameters
from ..document import Document
from ..oplog import Operation, register_verb
from .. import selection
from ..entry import entry
from ..sketch import (
    add_entities,
    entity_kind_ops,
    entity_point_pair,
    entity_point_keys,
    get_entity,
    get_sketch,
    new_entity_id,
    remove_entities,
    require_valid_params,
    rotate_point,
)
from ._common import (
    MODE_ROTATE,
    MODE_TRANSLATE,
    finite_numbers,
    inverse_move_args,
    is_rotation,
    overlaps_original,
    reject_foreign_args,
    require_move_mode,
    require_same_uv_plane,
    rotation_degrees,
)

VERB_MOVE_SKETCH_ENTITIES = "move_sketch_entities"
#: `mode` の宣言は `_common` が正(`move_body` と同じ値を取ることが契約)。


def _normalize_targets(doc: Document, targets: Sequence[dict]) -> List[dict]:
    """選択の参照を `{"sketch","entity","point"}` の**重複なし**の並びへ畳む。

    同じ (sketch, entity, point) は1つに畳み、図形全体(`point=None`)が入っていたらその図形の
    点の指定は落とす。存在しないスケッチ・図形は KeyError、その種別に無い点の名前は ValueError。
    """
    if not targets:
        raise ValueError("move_sketch_entities requires at least one target")

    whole: set = set()
    ordered: List[dict] = []
    seen: set = set()
    for ref in targets:
        sketch_id = ref["sketch"]
        entity_id = ref["entity"]
        point = ref.get("point")
        sketch = get_sketch(doc, sketch_id)
        entity = get_entity(sketch, entity_id)
        if point is not None:
            entity_point_pair(entity.kind, str(point))
        key = (sketch_id, entity_id, None if point is None else str(point))
        if point is None:
            whole.add((sketch_id, entity_id))
        if key in seen:
            continue
        seen.add(key)
        ordered.append(
            {"sketch": sketch_id, "entity": entity_id, "point": key[2]}
        )
    return [
        item
        for item in ordered
        if item["point"] is None or (item["sketch"], item["entity"]) not in whole
    ]


def _write_params(doc: Document, entity: Entity, values: Dict[str, float]) -> None:
    """解決済みの数値を図形へ書き戻す。

    名前つきの値なら文書のパラメータ表を書き換える(`d3` を 12.0 にする)。素の数値のまま持つ図形
    (取り込んだベジエ)は `Entity.params` を直接書き換える。まだ持っていないキーは名前つきの値として
    新しく登録する(`angle` を持たない時代のログから再生した長方形を回すとここへ来る。再生でも同じ
    順序で起きるので `d<n>` の割り当ては変わらない。undo は `_restore_previous` がそのキーと名前を外す)。
    """
    for key, value in values.items():
        ref = entity.params.get(key)
        if ref is None:
            entity.params[key] = parameters.register_value(doc, float(value))
        elif isinstance(ref, str):
            doc.parameters[ref] = float(value)
        else:
            entity.params[key] = float(value)


# --------------------------------------------------------------------------------------
# 回転の引数の解決 —— ログに載せるのは解決した数値だけ
# --------------------------------------------------------------------------------------
#
# `center_entity` の点は、中心に使った図形自身が対象に入っていれば回した後には別の場所にいる。
# 参照をログに残して undo で引き直すと別の中心で戻すので、入口で [u, v] の数値へ潰す。
def _point_of(doc: Document, sketch_id: str, entity_id: str, point: Any) -> tuple:
    """図形の点1つの座標 (u, v)。`point=None` なら種別の最初の点(`sketch.entity_point_keys` の先頭)。"""
    entity = get_entity(get_sketch(doc, sketch_id), entity_id)
    values = parameters.resolve_all(doc, entity.params)
    if point is None:
        pairs = entity_point_keys(entity.kind)
        if not pairs:
            raise ValueError(f"{entity.kind} has no movable point to use as a center")
        x_key, y_key = pairs[0]
    else:
        x_key, y_key = entity_point_pair(entity.kind, str(point))
    return (float(values[x_key]), float(values[y_key]))


def _center_of_targets(doc: Document, targets: Sequence[dict]) -> tuple:
    """対象の代表点の平均(回転中心を省略したときの既定。境界箱の中心ではない)。

    図形まるごと(`point=None`)なら種別ごとの中心(`EntityKindOps.center_2d`)、点ならその点。
    """
    points = []
    for item in targets:
        entity = get_entity(get_sketch(doc, item["sketch"]), item["entity"])
        values = parameters.resolve_all(doc, entity.params)
        if item["point"] is None:
            points.append(entity_kind_ops(entity.kind).center_2d(values))
        else:
            x_key, y_key = entity_point_pair(entity.kind, str(item["point"]))
            points.append((float(values[x_key]), float(values[y_key])))
    return (
        sum(point[0] for point in points) / len(points),
        sum(point[1] for point in points) / len(points),
    )


def _rotation_args(
    doc: Document,
    targets: Sequence[dict],
    angle: Any,
    center: Any,
    center_entity: Any,
) -> dict:
    """回転の引数を検査して、ログに載せる数値 `{angle, center}` にする。

    中心は `center_entity`(既存の点を指す) -> `center`([u, v] の数値) -> 対象の代表点の平均
    (`_center_of_targets`)の順で決まる。前2つの同時指定は ValueError。
    """
    degrees = rotation_degrees(angle, verb="move_sketch_entities", noun="図形")

    if center is not None and center_entity is not None:
        raise ValueError("center と center_entity は同時に渡せない")
    if center_entity is not None:
        if not isinstance(center_entity, dict):
            raise ValueError(
                "center_entity must be a reference like "
                '{"sketch": "<sketch_id>", "entity": "<entity_id>", "point": "x0"}, '
                + f"got {center_entity!r}"
            )
        if "sketch" not in center_entity or "entity" not in center_entity:
            raise ValueError(
                'center_entity needs both "sketch" and "entity" '
                f"(got {center_entity!r})"
            )
        resolved = _point_of(
            doc,
            str(center_entity["sketch"]),
            str(center_entity["entity"]),
            center_entity.get("point"),
        )
        for target_sketch in {item["sketch"] for item in targets}:
            require_same_uv_plane(doc, str(center_entity["sketch"]), target_sketch, what="回転中心")
    elif center is not None:
        numbers = finite_numbers(center, 2, what="center", form="[u, v]")
        resolved = (numbers[0], numbers[1])
    else:
        resolved = _center_of_targets(doc, targets)

    return {"angle": degrees, "center": [float(resolved[0]), float(resolved[1])]}


def _rotated_params(
    kind: str,
    current: Dict[str, float],
    point: Any,
    cx: float,
    cy: float,
    angle_deg: float,
) -> Dict[str, float]:
    """`current` の上から1つの対象を回した `params` を返す(書き込みはしない。積み上げ方は `_moved_params`)。

    - `point is None` … 図形まるごと。種別の表の `rotate(..., rotate_copies=True)` を使う
      (`False` は円形反復用の「複製の向きを変えず位置だけ回す」挙動で、ここでは誤り)。
    - `point` あり … その点だけを `sketch.rotate_point` で回す。
    """
    if point is None:
        result = dict(current)
        result.update(entity_kind_ops(kind).rotate(current, cx, cy, angle_deg, True))
        return result
    x_key, y_key = entity_point_pair(kind, str(point))
    new_x, new_y = rotate_point(current[x_key], current[y_key], cx, cy, angle_deg)
    result = dict(current)
    result[x_key] = new_x
    result[y_key] = new_y
    return result


def _params_transform(args: dict) -> Callable:
    """ログの `args` から「(種別, params, 点) -> 変換後の params」を作る。do も undo も再生もここを通る。"""
    if is_rotation(args):
        cx, cy = float(args["center"][0]), float(args["center"][1])
        degrees = float(args["angle"])
        return lambda kind, current, point: _rotated_params(
            kind, current, point, cx, cy, degrees
        )
    dx = float(args["dx"])
    dy = float(args["dy"])
    return lambda kind, current, point: _moved_params(kind, current, point, dx, dy)


def _moved_params(
    kind: str, current: Dict[str, float], point: Any, dx: float, dy: float
) -> Dict[str, float]:
    """`current` の上から1つの対象を動かした `params` を返す(書き込みはしない)。

    **渡された `current` を土台にする**のが要点。同じ図形の複数の点を毎回モデルから読み直すと、
    順に書き戻したとき最後の1つ以外が元の値で上書きされる(線分の両端を選ぶと片方しか動かなかった)。
    呼び出し側は同じ図形の対象を積み上げてから1回だけ書く。
    """
    if point is None:
        # `translate` は種別により変える鍵だけ返すものと全部返すものが混在するので、元の値に重ねる。
        result = dict(current)
        result.update(entity_kind_ops(kind).translate(current, dx, dy))
        return result
    x_key, y_key = entity_point_pair(kind, str(point))
    result = dict(current)
    result[x_key] = current[x_key] + dx
    result[y_key] = current[y_key] + dy
    return result


def _apply(doc: Document, targets: Sequence[dict], transform: Callable) -> None:
    """`copy=False` の本体。同じ図形の対象を1つの `params` に積み上げ(`_moved_params`)、
    全部検査してから書く(書きながら検査すると、途中で長さ0の線分が分かったとき一部だけ動いた文書が残る)。"""
    merged: dict = {}   # (sketch_id, entity_id) -> [entity, 積み上げ中の params]
    order: List[tuple] = []
    for item in targets:
        key = (item["sketch"], item["entity"])
        if key not in merged:
            sketch = get_sketch(doc, item["sketch"])
            entity = get_entity(sketch, item["entity"])
            merged[key] = [entity, parameters.resolve_all(doc, entity.params)]
            order.append(key)
        entry = merged[key]
        entry[1] = transform(entry[0].kind, entry[1], item["point"])

    planned: List[tuple] = []
    for key in order:
        entity, values = merged[key]
        require_valid_params(entity.kind, values)
        planned.append((entity, values))
    for entity, values in planned:
        _write_params(doc, entity, values)


def _copy(
    doc: Document, targets: Sequence[dict], transform: Callable, new_ids: Sequence[str], *, fresh: bool
) -> List[str]:
    """`copy=True` の本体。対象が属する**図形をまるごと**複製して EntityId を返す。

    `new_ids` は作るIDの並び(再生では記録したものを渡す)。同じ図形を指す対象が複数あっても複製は1つ。

    **元と完全に重なる複製は作らない**(ValueError。円をその中心まわりに回すと起きる。領域検出が
    同じ辺を2本見る)。重なりの判定は `overlaps_original`(`fresh` は新しく作る複製)。
    """
    plan = _entities_to_copy(targets)
    if len(new_ids) != len(plan):
        raise ValueError(
            f"move_sketch_entities: {len(plan)} copies planned but "
            f"{len(new_ids)} ids recorded"
        )
    prepared: List[tuple] = []
    for (sketch_id, entity_id), new_id in zip(plan, new_ids):
        sketch = get_sketch(doc, sketch_id)
        entity = get_entity(sketch, entity_id)
        current = parameters.resolve_all(doc, entity.params)
        values = transform(entity.kind, current, None)
        if overlaps_original(values, current, fresh=fresh):
            raise ValueError(
                f"move_sketch_entities: 複製 {entity_id!r} が元と完全に重なる"
                "(回転中心が図形の中心と一致している)"
            )
        require_valid_params(entity.kind, values)
        prepared.append((sketch, new_id, entity.kind, values))
    # ID の重複は `add_entities` が全部検査してから足す
    return add_entities(doc, prepared)


def _entities_to_copy(targets: Sequence[dict]) -> List[tuple]:
    """複製する図形の `(sketch_id, entity_id)` を**出現順・重複なし**で返す。"""
    plan: List[tuple] = []
    for item in targets:
        key = (item["sketch"], item["entity"])
        if key not in plan:
            plan.append(key)
    return plan


def _translation_args(dx: Any, dy: Any) -> dict:
    """並進の引数を検査して、ログに載せる数値 `{dx, dy}` にする。"""
    if dx is None or dy is None:
        raise ValueError(
            f"move_sketch_entities(mode='translate') needs dx, dy "
            f"(got dx={dx!r}, dy={dy!r})"
        )
    delta_x = float(dx)
    delta_y = float(dy)
    if not math.isfinite(delta_x) or not math.isfinite(delta_y):
        raise ValueError(f"dx/dy must be finite numbers, got dx={dx!r}, dy={dy!r}")
    if delta_x == 0.0 and delta_y == 0.0:
        raise ValueError(
            "move_sketch_entities needs a non-zero (dx, dy) "
            "(0,0 は copy=False では何も変えず、copy=True では元と重なる図形を作る)"
        )
    return {"dx": delta_x, "dy": delta_y}


def move_sketch_entities(
    doc: Document,
    targets: Sequence[dict],
    dx: Optional[float] = None,
    dy: Optional[float] = None,
    copy: bool = False,
    *,
    mode: str = MODE_TRANSLATE,
    angle: Optional[float] = None,
    center: Any = None,
    center_entity: Any = None,
) -> Operation:
    """選択のスケッチの点・辺を動かす(`copy=True` なら複製する)。

    - `mode="translate"`(既定) … (dx, dy)[mm] だけ平行移動する。両方0は ValueError。
    - `mode="rotate"` … スケッチ平面の法線まわりに `angle` 度だけ回す(中心の決め方は
      `_rotation_args`)。360の倍数は ValueError。長方形をまるごと回すと `angle` も同じだけ回る。
    - モードが取らない引数・非有限の数値は ValueError。動かした結果が使えない図形(長さ0の
      線分など)になっても ValueError で、文書にもログにも何も残さない。
    """
    require_move_mode(mode, verb="move_sketch_entities")
    normalized = _normalize_targets(doc, targets)

    if mode == MODE_ROTATE:
        reject_foreign_args(mode, {"dx": dx, "dy": dy}, verb="move_sketch_entities")
        shaped = _rotation_args(doc, normalized, angle, center, center_entity)
    else:
        reject_foreign_args(
            mode, {"angle": angle, "center": center, "center_entity": center_entity}, verb="move_sketch_entities"
        )
        shaped = _translation_args(dx, dy)

    args = {
        "targets": [dict(item) for item in normalized],
        "mode": mode,
        **shaped,
        "copy": bool(copy),
    }
    transform = _params_transform(args)

    logged = {**args}
    if copy:
        new_ids = [new_entity_id() for _ in _entities_to_copy(normalized)]
        created = _copy(doc, normalized, transform, new_ids, fresh=True)
    else:
        created = []
        previous = _previous_params(doc, normalized)
        _apply(doc, normalized, transform)
        logged["previous"] = previous

    # 複製したIDはログが持つ(再生で同じIDになる)。
    return doc.log.append(VERB_MOVE_SKETCH_ENTITIES, {**logged, "created": list(created)})


# --------------------------------------------------------------------------------------
# verbの登録
# --------------------------------------------------------------------------------------
def _do_move_sketch_entities(document: Document, operation: Operation) -> None:
    """redo / replay: 記録された対象・量で動かし直す(ログには触らない)。"""
    args = operation.args
    targets = [dict(item) for item in args["targets"]]
    transform = _params_transform(args)
    if args.get("copy"):
        _copy(document, targets, transform, list(args.get("created") or []), fresh=False)
    else:
        _apply(document, targets, transform)


def _previous_params(doc: Document, targets: Sequence[dict]) -> List[dict]:
    """動かす前の、対象の図形ごとの解決済みの `params`(undo が書き戻す。図形ごとに1件、出現順)。"""
    previous: List[dict] = []
    seen: set = set()
    for item in targets:
        key = (item["sketch"], item["entity"])
        if key in seen:
            continue
        seen.add(key)
        entity = get_entity(get_sketch(doc, item["sketch"]), item["entity"])
        previous.append(
            {"sketch": item["sketch"], "entity": item["entity"], "params": parameters.resolve_all(doc, entity.params)}
        )
    return previous


def _restore_previous(doc: Document, previous: Sequence[dict]) -> None:
    """動かす前の値をそのまま書き戻す。動かしたときに新しく足したキー(`angle` を持たない旧い長方形の回転)は、
    名前つきの値ごと外す。"""
    entities = [
        (get_entity(get_sketch(doc, item["sketch"]), item["entity"]), item["params"]) for item in previous
    ]
    for entity, saved in entities:
        _write_params(doc, entity, saved)
        for key in [key for key in entity.params if key not in saved]:
            parameters.release_param_refs(doc, {key: entity.params.pop(key)})


def _undo_move_sketch_entities(document: Document, operation: Operation) -> None:
    """undo: 複製なら作った図形を消し、移動なら動かす前の値(`previous`)を書き戻す。

    逆向きに同じ量を動かすと `x + dx - dx != x` で1ulpずれ、領域の並び(同面積の領域の添字)が再生した
    文書と食い違う。`previous` を持たない旧ログだけ逆向きに動かす(回転は逆角度。この経路は書き戻しが
    ずれうるうえ、足したキーを外せない)。
    """
    args = operation.args
    if args.get("copy"):
        remove_entities(document, args.get("created") or [])
        return
    if args.get("previous") is not None:
        _restore_previous(document, args["previous"])
        return
    _apply(
        document,
        [dict(item) for item in args["targets"]],
        _params_transform(inverse_move_args(args, ("dx", "dy"))),
    )


register_verb(
    VERB_MOVE_SKETCH_ENTITIES,
    do=_do_move_sketch_entities,
    undo=_undo_move_sketch_entities,
)


# --------------------------------------------------------------------------------------
# 入口(`duocad/entry.py`) —— 対象を選択から取り、上の verb を掛け、応答に載せるものを返す
# --------------------------------------------------------------------------------------
@entry(VERB_MOVE_SKETCH_ENTITIES)
def _enter_move_sketch_entities(
    document: Document,
    dx: Optional[float] = None,
    dy: Optional[float] = None,
    copy: bool = False,
    mode: str = "translate",
    angle: Optional[float] = None,
    center: Any = None,
    center_entity: Any = None,
) -> dict:
    """戻り値のキーはモードによらず同じで、使わない側は `None`(`web/src/session.ts` の `Applied` が
    形の変わる封筒を読めないため)。"""
    targets = selection.require_sketch_entities(document)
    sketch_ids = sorted({str(item["sketch"]) for item in targets})
    operation = move_sketch_entities(
        document,
        targets,
        dx,
        dy,
        copy=bool(copy),
        mode=str(mode),
        angle=angle,
        center=center,
        center_entity=center_entity,
    )
    args = operation.args
    return {
        "mode": args["mode"],
        "dx": args.get("dx"),
        "dy": args.get("dy"),
        "angle": args.get("angle"),
        "center": args.get("center"),
        "copy": args["copy"],
        "created": list(args["created"]),
        "sketch_ids": sketch_ids,
    }
