"""verb `move_sketch_entities` — スケッチの点・辺を平行移動する / 複製する。

**パラメータパネル(数値の打ち直し)の後継**である(2026-08-30本人判断
[[duocad-sketch-panel-replaced-by-direct-selection]])。位置の打ち直しは、右サイドバーの
入力欄ではなく「3Dで点/辺を選んで dx, dy を渡す」に一本化した。

**`mode` で並進と回転を切り替える**(`move_body` と同じ流儀。2026-09-05)。
`mode="translate"`(既定)は dx, dy、`mode="rotate"` は角度[度]と回転中心を取る。軸は常に
スケッチ平面の法線で、**パラメータにしない**(スケッチは2Dなので軸の自由度が無い。
本人判断)。角度の符号は `pattern_circular` の `total_angle` と同じ u->v の反時計回りが正。

対象は**確定選択のスケッチ図形**(`selection.SKETCH_ENTITY_KIND`)で、参照が持つ `point` が

    None       … その図形1つ全体(= 辺)。全部の点を同じ量だけ動かす **= 形は変えない**
    "x0" 等    … その1点だけ(線の端点・円/弧の中心・長方形の左下・ベジエの制御点)

を意味する。**寸法(半径・幅高さ・角度)は動かさない** —— 今回のスコープ外(本人判断
[[duocad-move-verb-split-sketch-and-body]])で、寸法を変えると既存ソリッドの再計算が絡む。

`copy` の意味:

- `copy=False` … 対象を動かす。
- `copy=True`  … **元を残し、対象が属する図形をまるごと複製**して平行移動した位置に置く。
  点だけを複製することはできない(点は図形の一部であって、それ自体が図形ではない)ので、
  複製の単位は常に図形1つである(本人明言「copyはentity単体が対象」)。複製は
  **反復(`Pattern`)には入らない** —— 反復の元を複製しても、複製は素の図形になる。

**長方形をまるごと回すことはできない**(`mode="rotate"` で `point=None` の長方形は
ValueError)。軸平行の `x, y, width, height` は回転を表せないので、回すと種別が4本の線分へ
変わる —— `pattern_circular` は**複製を展開するときだけ**その落とし方をしている
(`sketch._rectangle_as_rotated_lines`)のに対し、こちらは**既にある図形そのもの**を
書き換える verb なので、種別が変わると「undo = 逆向きに同じ角度」が成り立たなくなり、
戻すときに元の図形IDと名前つき寸法(`d3` 等)を復元できない。**黙って線分に化けさせない**。
長方形の1点(左下)だけを回すのは今までどおりできる(位置が動くだけで種別は変わらない)。

**同じ図形に「辺」と「点」が同時に選ばれていたら辺(全体)が勝つ**。両方を素直に適用すると
その点だけ2回動いて図形が歪む —— 選び方の違いで形が壊れるのは、この verb が
「形を変えない平行移動」であるという約束と食い違う。

**既存ソリッドは追従しない**(本人が許容済み [[duocad-sketch-body-desync-accepted-for-move]])。
`set_parameter` と違って `rebuild()` を呼ばない —— 押し出し済みのソリッドは動かず、
スケッチの線だけが動く。操作ログ上この verb は押し出しより**後ろ**に載るので、
`rebuild()` が走っても押し出しは元の座標で作り直され、見え方は変わらない。

**OCP / build123d を直接importしない**(幾何はここでは要らない。純粋な数値の書き換え)。
"""
from __future__ import annotations

import math
from typing import Any, Callable, Dict, List, Optional, Sequence
from uuid import uuid4

from .. import parameters
from ..document import Document
from ..oplog import Operation, register_verb
from ..sketch import (
    Entity,
    entity_kind_ops,
    entity_point_pair,
    entity_point_keys,
    get_entity,
    get_sketch,
    require_valid_params,
    rotate_point,
)

VERB_MOVE_SKETCH_ENTITIES = "move_sketch_entities"

#: 並進 / 回転の切り替え。**`move_body.MOVE_MODES` と同じ綴り**にしてある(AIとUIから見て
#: 「移動の2つのverbは同じ mode を取る」であること自体が契約)。値をここで別に持つのは、
#: verb 同士が互いをimportしない方針(1 verb 1モジュール)を崩さないためである。
MODE_TRANSLATE = "translate"
MODE_ROTATE = "rotate"
MOVE_MODES = (MODE_TRANSLATE, MODE_ROTATE)


def _normalize_targets(doc: Document, targets: Sequence[dict]) -> List[dict]:
    """選択の参照を `{"sketch","entity","point"}` の**重複なし**の並びへ畳む。

    - 同じ (sketch, entity, point) は1つに畳む(同じ点を2回動かさない)。
    - **図形全体(`point=None`)が入っていたら、その図形の点の指定は落とす**
      (モジュールdocstringの「辺が勝つ」)。
    - 存在しないスケッチ・図形は KeyError、その種別に無い点の名前は ValueError。
      **検査が全部通ってから**しか呼び出し側は文書を触らない。
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


def _resolved_params(doc: Document, entity: Entity) -> Dict[str, float]:
    """図形の `params` を解決済みの数値で返す(`Entity.params` は名前つきの値への参照)。"""
    return {key: parameters.resolve(doc, ref) for key, ref in entity.params.items()}


def _write_params(doc: Document, entity: Entity, values: Dict[str, float]) -> None:
    """解決済みの数値を図形へ書き戻す。

    **名前つきの値なら文書のパラメータ表を書き換える**(`d3` を 12.0 にする)ので、その名前を
    `set_parameter` で後から打ち直す道は残る。素の数値のまま持っている図形(取り込んだベジエ)は
    `Entity.params` の側を直接書き換える。
    """
    for key, value in values.items():
        ref = entity.params[key]
        if isinstance(ref, str):
            doc.parameters[ref] = float(value)
        else:
            entity.params[key] = float(value)


# --------------------------------------------------------------------------------------
# 回転の引数の解決 —— **ログに載せるのは解決した数値だけ**
# --------------------------------------------------------------------------------------
#
# 回転中心を図形の点で指したとき(`center_entity`)、その点は**回した後には別の場所にいる**
# ことがある(中心に使った図形自身が対象に入っていれば必ずそうなる)。参照をログに残して
# undo で引き直すと別の中心で戻すことになるので、`move_sketch_entities()` の入口で [u, v] の
# 数値へ潰す(`move_body` の軸と同じ規律)。
def _point_of(doc: Document, sketch_id: str, entity_id: str, point: Any) -> tuple:
    """図形の点1つの座標 (u, v)。`point=None` なら**その種別の最初の点**を使う。

    最初の点は「線の始点・円/弧の中心・長方形の左下・ベジエの始点」で、`EntityKindOps.points`
    の並びが唯一の正である(`sketch.entity_point_keys`)。
    """
    entity = get_entity(get_sketch(doc, sketch_id), entity_id)
    values = _resolved_params(doc, entity)
    if point is None:
        pairs = entity_point_keys(entity.kind)
        if not pairs:
            raise ValueError(f"{entity.kind} has no movable point to use as a center")
        x_key, y_key = pairs[0]
    else:
        x_key, y_key = entity_point_pair(entity.kind, str(point))
    return (float(values[x_key]), float(values[y_key]))


def _center_of_targets(doc: Document, targets: Sequence[dict]) -> tuple:
    """**対象の代表点の平均**(回転中心を省略したときの既定)。

    図形まるごと(`point=None`)なら種別ごとの中心(`EntityKindOps.center_2d` —— 線分は中点、
    円/弧は中心、長方形は中央、ベジエは制御点の重心)、点を選んでいるならその点そのもの。
    **境界箱の中心ではない**(`move_body` の重心と同じ判断。2026-09-05)。
    """
    points = []
    for item in targets:
        entity = get_entity(get_sketch(doc, item["sketch"]), item["entity"])
        values = _resolved_params(doc, entity)
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

    中心は次の順で決まる: `center_entity`(既存の点を指す) -> `center`([u, v] の数値) ->
    **対象の代表点の平均**(`_center_of_targets`)。前2つを同時に渡すのは ValueError
    (どちらが勝つかを暗黙にしない)。
    """
    if angle is None:
        raise ValueError("move_sketch_entities(mode='rotate') needs `angle` (度)")
    degrees = float(angle)
    if not math.isfinite(degrees):
        raise ValueError(f"angle must be a finite number of degrees, got {angle!r}")
    if degrees % 360.0 == 0.0:
        raise ValueError(
            "move_sketch_entities(mode='rotate') needs an angle that is not a multiple "
            "of 360 (0度・360度は copy=False では何も変えず、copy=True では元と重なる図形を作る)"
        )

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
    elif center is not None:
        try:
            numbers = [float(value) for value in center]
        except (TypeError, ValueError):
            raise ValueError(
                f"center must be two numbers [u, v], got {center!r}"
            ) from None
        if len(numbers) != 2 or not all(math.isfinite(value) for value in numbers):
            raise ValueError(
                f"center must be two finite numbers [u, v], got {center!r}"
            )
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
    """`current` の上から1つの対象を回した `params` を返す(**書き込みはしない**)。

    `_moved_params` の回転版で、積み上げ方(同じ図形の複数の点を順に重ねる)の理由も同じ。

    - `point is None` … 図形まるごと。種別の表の `rotate(..., rotate_copies=True)` を使う
      —— **`True` でなければならない**。`False` は「複製の向きを変えず位置だけ回す」ための
      円形反復用の挙動で、こちらは対象そのものを回すからである。
    - `point` あり … その点だけを `sketch.rotate_point` で回す(種別は変わらない)。

    種別が変わる結果(長方形まるごと -> 線分4本)は **ValueError** にする。理由は
    モジュールdocstring(undoが「逆向きに同じ角度」で成り立たなくなる)。
    """
    if point is None:
        results = entity_kind_ops(kind).rotate(current, cx, cy, angle_deg, True)
        kinds = [str(item["kind"]) for item in results]
        if len(results) != 1 or kinds[0] != kind:
            raise ValueError(
                f"move_sketch_entities: {kind} はまるごと回せない"
                f"(回すと種別が {kinds} に変わる)。"
                "点を1つだけ選んで回すか、線分で描き直してから回すこと"
            )
        result = dict(current)
        result.update(results[0]["params"])
        return result
    x_key, y_key = entity_point_pair(kind, str(point))
    new_x, new_y = rotate_point(current[x_key], current[y_key], cx, cy, angle_deg)
    result = dict(current)
    result[x_key] = new_x
    result[y_key] = new_y
    return result


def _params_transform(args: dict) -> Callable:
    """ログの `args` から「(種別, params, 点) -> 変換後の params」を作る。

    **do も undo も再生もここを通る**。**`mode` キーが無いログは並進**として読む ——
    2026-09-05より前に保存した文書には `mode` が無く、それらは全部平行移動だからである。
    """
    if args.get("mode", MODE_TRANSLATE) == MODE_ROTATE:
        cx, cy = float(args["center"][0]), float(args["center"][1])
        degrees = float(args["angle"])
        return lambda kind, current, point: _rotated_params(
            kind, current, point, cx, cy, degrees
        )
    dx = float(args["dx"])
    dy = float(args["dy"])
    return lambda kind, current, point: _moved_params(kind, current, point, dx, dy)


def _inverse_args(args: dict) -> dict:
    """undo 用に**逆向きの変換**の args を作る(並進は逆ベクトル、回転は逆角度・同じ中心)。"""
    if args.get("mode", MODE_TRANSLATE) == MODE_ROTATE:
        return {**args, "angle": -float(args["angle"])}
    return {**args, "dx": -float(args["dx"]), "dy": -float(args["dy"])}


def _moved_params(
    kind: str, current: Dict[str, float], point: Any, dx: float, dy: float
) -> Dict[str, float]:
    """`current` の上から1つの対象を動かした `params` を返す(**書き込みはしない**)。

    **渡された `current` を土台にする**のが要点である。「同じ図形の複数の点」を動かすとき、
    毎回モデルから読み直すと**各対象が「自分だけを動かした完全な辞書」を作る**ことになり、
    それを順に書き戻すと**最後の1つ以外が元の値で上書きされて消える**
    (2026-08-30に実際に踏んだ: 線分の両端を選んで動かすと片方しか動かなかった)。
    呼び出し側は同じ図形の対象をここで**積み上げてから**1回だけ書く。
    """
    if point is None:
        # `translate` は種別ごとに「変える鍵だけ」を返すものと全部返すものが混在するので、
        # 元の値の上へ重ねて全鍵を揃える(半径・角度はそのまま残る)。
        result = dict(current)
        result.update(entity_kind_ops(kind).translate(current, dx, dy))
        return result
    x_key, y_key = entity_point_pair(kind, str(point))
    result = dict(current)
    result[x_key] = current[x_key] + dx
    result[y_key] = current[y_key] + dy
    return result


def _apply(doc: Document, targets: Sequence[dict], transform: Callable) -> None:
    """`copy=False` の本体。**図形ごとに1つの計画へ併合してから**、全部検査して書き込む。

    2段階に分かれている:

    1. **併合** —— 同じ図形に属する対象(線分の両端など)を1つの `params` へ積み上げる。
       対象ごとに別々の完全な辞書を作って順に書くと、後の書き込みが前の書き込みを
       丸ごと打ち消す(`_moved_params` のdocstring)。
    2. **検査してから書く** —— 途中で書きながら検査すると、3つ目の点で「線分の長さが0に
       なる」と分かったときに1つ目・2つ目だけが動いた文書が残る(この verb を取り消す手は
       まだログに無い)。

    `_normalize_targets` が「図形全体と、その図形の点」を既に畳んでいるので、1つの図形に
    `point=None` と点が混ざって来ることはない。
    """
    merged: dict = {}   # (sketch_id, entity_id) -> [entity, 積み上げ中の params]
    order: List[tuple] = []
    for item in targets:
        key = (item["sketch"], item["entity"])
        if key not in merged:
            sketch = get_sketch(doc, item["sketch"])
            entity = get_entity(sketch, item["entity"])
            merged[key] = [entity, _resolved_params(doc, entity)]
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
    doc: Document, targets: Sequence[dict], transform: Callable, new_ids: Sequence[str]
) -> List[str]:
    """`copy=True` の本体。対象が属する**図形をまるごと**複製して EntityId を返す。

    `new_ids` は作るIDの並び(再生で同じIDになるよう、ログに記録したものを渡す)。
    **同じ図形を指す対象が複数あっても複製は1つ**(`_entities_to_copy` が畳む)。

    **元と完全に重なる複製は作らない**(ValueError)。回転で起きる —— 円をその中心まわりに
    回すと座標が1つも変わらないので、`pattern_circular` が「回転中心と図形の中心が一致」を
    断るのと同じ事故(領域検出が同じ辺を2本見る)になる。並進は (0,0) を先に断っているので
    ここへは来ない。
    """
    plan = _entities_to_copy(doc, targets)
    if len(new_ids) != len(plan):
        raise ValueError(
            f"move_sketch_entities: {len(plan)} copies planned but "
            f"{len(new_ids)} ids recorded"
        )
    created: List[str] = []
    prepared: List[tuple] = []
    for (sketch_id, entity_id), new_id in zip(plan, new_ids):
        sketch = get_sketch(doc, sketch_id)
        if any(existing.id == new_id for existing in sketch.entities):
            raise ValueError(f"entity id already exists: {new_id!r}")
        entity = get_entity(sketch, entity_id)
        current = _resolved_params(doc, entity)
        values = transform(entity.kind, current, None)
        if values == current:
            raise ValueError(
                f"move_sketch_entities: 複製 {entity_id!r} が元と完全に重なる"
                "(回転中心が図形の中心と一致している)"
            )
        require_valid_params(entity.kind, values)
        prepared.append((sketch, new_id, entity.kind, values))
    for sketch, new_id, kind, values in prepared:
        refs = {
            key: parameters.register_value(doc, value) for key, value in values.items()
        }
        sketch.entities.append(Entity(id=new_id, kind=kind, params=refs))
        created.append(new_id)
    return created


def _entities_to_copy(doc: Document, targets: Sequence[dict]) -> List[tuple]:
    """複製する図形の `(sketch_id, entity_id)` を**出現順・重複なし**で返す。"""
    plan: List[tuple] = []
    for item in targets:
        key = (item["sketch"], item["entity"])
        if key not in plan:
            plan.append(key)
    return plan


def _translation_args(dx: Any, dy: Any) -> dict:
    """並進の引数を検査して、ログに載せる数値 `{dx, dy}` にする(既存のふるまい)。"""
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


def _reject_foreign_args(mode: str, given: dict) -> None:
    """**別のモードの引数が来たら断る**(黙って無視も、黙って両方適用もしない)。"""
    foreign = sorted(name for name, value in given.items() if value is not None)
    if foreign:
        raise ValueError(
            f"move_sketch_entities(mode={mode!r}) does not take {foreign} "
            "(mode を切り替えるか、その引数を外すこと)"
        )


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
    """確定選択のスケッチの点・辺を動かす(`copy=True` なら複製する)。

    - `mode="translate"`(既定) … (dx, dy) だけ平行移動する。`dx` と `dy` が**両方0は
      ValueError**(`copy=False` では何も起きない操作が undo を1手消費し、`copy=True` では
      元と完全に重なる図形ができて領域検出が壊れる)。
    - `mode="rotate"` … スケッチ平面の法線まわりに `angle` 度だけ回す(中心の決め方は
      `_rotation_args`)。**360の倍数は ValueError**(0度と同じ理由)。
      **長方形をまるごと回すのは ValueError**(モジュールdocstring)。
    - **モードが取らない引数を渡すと ValueError**(黙って片方だけ効くことは無い)。
    - 数値が非有限なら ValueError。動かした結果が使えない図形(長さ0の線分など)になっても
      ValueError で、**文書にもログにも何も残さない**。
    """
    if mode not in MOVE_MODES:
        raise ValueError(
            f"unknown move_sketch_entities mode: {mode!r} (one of {list(MOVE_MODES)})"
        )
    normalized = _normalize_targets(doc, targets)

    if mode == MODE_ROTATE:
        _reject_foreign_args(mode, {"dx": dx, "dy": dy})
        shaped = _rotation_args(doc, normalized, angle, center, center_entity)
    else:
        _reject_foreign_args(
            mode, {"angle": angle, "center": center, "center_entity": center_entity}
        )
        shaped = _translation_args(dx, dy)

    args = {
        "targets": [dict(item) for item in normalized],
        "mode": mode,
        **shaped,
        "copy": bool(copy),
    }
    transform = _params_transform(args)

    if copy:
        new_ids = [f"entity-{uuid4().hex[:12]}" for _ in _entities_to_copy(doc, normalized)]
        created = _copy(doc, normalized, transform, new_ids)
    else:
        created = []
        _apply(doc, normalized, transform)

    return doc.log.append(
        VERB_MOVE_SKETCH_ENTITIES,
        {
            **args,
            # **複製したIDはログが持つ**(再生で同じIDになる。`pattern_*` の `pattern_id` と同じ流儀)
            "created": list(created),
        },
    )


# --------------------------------------------------------------------------------------
# verbの登録
# --------------------------------------------------------------------------------------
def _do_move_sketch_entities(document: Document, operation: Operation) -> None:
    """redo / replay: 記録された対象・量で動かし直す(ログには触らない)。"""
    args = operation.args
    targets = [dict(item) for item in args["targets"]]
    transform = _params_transform(args)
    if args.get("copy"):
        _copy(document, targets, transform, list(args.get("created") or []))
    else:
        _apply(document, targets, transform)


def _undo_move_sketch_entities(document: Document, operation: Operation) -> None:
    """undo: 複製なら作った図形を消し、移動なら**逆向きに同じ量**を動かす(回転は逆角度)。

    逆向きの変換にしてあるのは、`do` と同じ1本の経路を通るからである(移動前の値を
    args へ控えて書き戻す形にすると、`rebuild()` で土台の値が変わったときに**控えた値が
    古いまま**復元されうる)。浮動小数の丸めで最下位が1ulpずれることはあるが、
    幾何の許容差(mmの1e-9未満)よりはるかに小さい。

    **回転の中心は数値でログに入っている**ので、undo が図形から引き直すことはない ——
    引き直すと、中心に使った点が既に回った後にいて別の中心になる(`_rotation_args` の注記)。
    """
    args = operation.args
    if args.get("copy"):
        created = set(args.get("created") or [])
        for sketch in document.sketches:
            remaining = []
            for entity in sketch.entities:
                if entity.id in created:
                    parameters.release_param_refs(document, entity.params)
                else:
                    remaining.append(entity)
            sketch.entities = remaining
        return
    _apply(
        document,
        [dict(item) for item in args["targets"]],
        _params_transform(_inverse_args(args)),
    )


register_verb(
    VERB_MOVE_SKETCH_ENTITIES,
    do=_do_move_sketch_entities,
    undo=_undo_move_sketch_entities,
)
