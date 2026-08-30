"""verb `move_sketch_entities` — スケッチの点・辺を平行移動する / 複製する。

**パラメータパネル(数値の打ち直し)の後継**である(2026-08-30本人判断
[[duocad-sketch-panel-replaced-by-direct-selection]])。位置の打ち直しは、右サイドバーの
入力欄ではなく「3Dで点/辺を選んで dx, dy を渡す」に一本化した。

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
from typing import Any, Dict, List, Sequence
from uuid import uuid4

from .. import parameters
from ..document import Document
from ..oplog import Operation, register_verb
from ..sketch import (
    Entity,
    entity_kind_ops,
    entity_point_pair,
    get_entity,
    get_sketch,
    require_valid_params,
)

VERB_MOVE_SKETCH_ENTITIES = "move_sketch_entities"


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


def _moved_params(
    doc: Document, entity: Entity, point: Any, dx: float, dy: float
) -> Dict[str, float]:
    """1つの対象を動かした後の解決済み `params`。**書き込みはしない**(先に全部検査するため)。"""
    current = _resolved_params(doc, entity)
    if point is None:
        moved = entity_kind_ops(entity.kind).translate(current, dx, dy)
        # `translate` は種別ごとに「変える鍵だけ」を返すものと全部返すものが混在するので、
        # 元の値の上へ重ねて全鍵を揃える(半径・角度はそのまま残る)。
        result = dict(current)
        result.update(moved)
        return result
    x_key, y_key = entity_point_pair(entity.kind, str(point))
    result = dict(current)
    result[x_key] = current[x_key] + dx
    result[y_key] = current[y_key] + dy
    return result


def _apply(doc: Document, targets: Sequence[dict], dx: float, dy: float) -> None:
    """`copy=False` の本体。**全部の結果を先に作って検査してから**書き込む。

    途中で書きながら検査すると、3つ目の点で「線分の長さが0になる」と分かったときに
    1つ目・2つ目だけが動いた文書が残る(この verb を取り消す手はまだログに無い)。
    """
    planned: List[tuple] = []
    for item in targets:
        sketch = get_sketch(doc, item["sketch"])
        entity = get_entity(sketch, item["entity"])
        values = _moved_params(doc, entity, item["point"], dx, dy)
        require_valid_params(entity.kind, values)
        planned.append((entity, values))
    for entity, values in planned:
        _write_params(doc, entity, values)


def _copy(
    doc: Document, targets: Sequence[dict], dx: float, dy: float, new_ids: Sequence[str]
) -> List[str]:
    """`copy=True` の本体。対象が属する**図形をまるごと**複製して EntityId を返す。

    `new_ids` は作るIDの並び(再生で同じIDになるよう、ログに記録したものを渡す)。
    **同じ図形を指す対象が複数あっても複製は1つ**(`_entities_to_copy` が畳む)。
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
        values = dict(current)
        values.update(entity_kind_ops(entity.kind).translate(current, dx, dy))
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


def move_sketch_entities(
    doc: Document,
    targets: Sequence[dict],
    dx: float,
    dy: float,
    copy: bool = False,
) -> Operation:
    """確定選択のスケッチの点・辺を (dx, dy) だけ動かす(`copy=True` なら複製する)。

    - `dx` と `dy` が**両方0は ValueError**。`copy=False` では何も起きない操作が
      undo を1手消費し、`copy=True` では元と完全に重なる図形ができて領域検出が壊れる。
    - 数値が非有限なら ValueError。動かした結果が使えない図形(長さ0の線分など)になっても
      ValueError で、**文書にもログにも何も残さない**。
    """
    delta_x = float(dx)
    delta_y = float(dy)
    if not math.isfinite(delta_x) or not math.isfinite(delta_y):
        raise ValueError(f"dx/dy must be finite numbers, got dx={dx!r}, dy={dy!r}")
    if delta_x == 0.0 and delta_y == 0.0:
        raise ValueError(
            "move_sketch_entities needs a non-zero (dx, dy) "
            "(0,0 は copy=False では何も変えず、copy=True では元と重なる図形を作る)"
        )
    normalized = _normalize_targets(doc, targets)

    if copy:
        new_ids = [f"entity-{uuid4().hex[:12]}" for _ in _entities_to_copy(doc, normalized)]
        created = _copy(doc, normalized, delta_x, delta_y, new_ids)
    else:
        new_ids = []
        created = []
        _apply(doc, normalized, delta_x, delta_y)

    return doc.log.append(
        VERB_MOVE_SKETCH_ENTITIES,
        {
            "targets": [dict(item) for item in normalized],
            "dx": delta_x,
            "dy": delta_y,
            "copy": bool(copy),
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
    dx = float(args["dx"])
    dy = float(args["dy"])
    if args.get("copy"):
        _copy(document, targets, dx, dy, list(args.get("created") or []))
    else:
        _apply(document, targets, dx, dy)


def _undo_move_sketch_entities(document: Document, operation: Operation) -> None:
    """undo: 複製なら作った図形を消し、移動なら**逆向きに同じ量**を動かす。

    逆向きの平行移動にしてあるのは、`do` と同じ1本の経路を通るからである(移動前の値を
    args へ控えて書き戻す形にすると、`rebuild()` で土台の値が変わったときに**控えた値が
    古いまま**復元されうる)。浮動小数の丸めで最下位が1ulpずれることはあるが、
    幾何の許容差(mmの1e-9未満)よりはるかに小さい。
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
        -float(args["dx"]),
        -float(args["dy"]),
    )


register_verb(
    VERB_MOVE_SKETCH_ENTITIES,
    do=_do_move_sketch_entities,
    undo=_undo_move_sketch_entities,
)
