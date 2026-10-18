"""verb `pattern_rectangular` / `pattern_circular` — スケッチの図形を反復する。

**複製は実行時に独立した図形として作る**(本人判断 2026-10-18。docs/DESIGN_DECISIONS.md「見えるものはすべてさわれる」):
複製は1つずつ図形 ID を持ち、選択・移動・削除ができる。元の図形との繋がりは残らない(元を後から動かしても
複製は付いてこない。拘束ソルバが無いのと同じ流儀)。ログは1行で、作った ID を `created` に持つ(再生で同じ ID になる)。
複製の数値は `add_entities` が名前つきの値に登録する(`move_sketch_entities` の複製と同じ)。

`nx` / `ny` / `count` は元を含めた個数で、複製が1つもできない指定・元と重なる複製ができる指定は ValueError。

**旧形式(`pattern_id` を持つ行)は再生専用で残す**: 以前は `Pattern` 1個をスケッチに置き、描くたびに展開していた
(`region.expanded_entities`)。その行を新しい規則で再生すると、複製ぶんの名前つきの値が増えて以後の `d<n>` が
全部ずれる(記録済みの `set_parameter` が別の値を指す)ので、行の形(`created` の有無)で振り分ける。
番人は `tests/test_pattern_live_replay.py`。
"""
from __future__ import annotations

import math
from typing import Any, Callable, Dict, List, Sequence

from .. import kernel_sketch, parameters
from ..document import Document, unknown_id_message
from ..oplog import Operation, register_verb
from ..entry import entry
from ..sketch import (
    Entity,
    Pattern,
    add_entities,
    circular_pattern_step,
    entity_kind_ops,
    get_sketch,
    new_entity_id,
    point_2d,
    remove_entities,
    require_valid_params,
)

VERB_PATTERN_RECTANGULAR = "pattern_rectangular"
VERB_PATTERN_CIRCULAR = "pattern_circular"

#: 複製の params を作る関数: (種別, 元の解決済み params) -> 複製の params の並び(元の位置は含めない)
Placement = Callable[[str, Dict[str, float]], List[Dict[str, float]]]

#: 反復1回で増やせる図形の数(元を除く複製の総数)の上限。状態配信の領域検出は図形数に比例して重く
#: (2026-10-18 実測: 重ならない図形で1つあたり約 2 ms。100×100 で約 22 秒、nx=20000 で約 44 秒)、複製どうしが
#: 重なると図形数より速く重くなる(円周上に直径 0.1 mm の円を 4000 個並べて重ねると 35 秒でも戻らなかった)。
#: 桁の取り違えがイベントループを数十秒〜事実上無限に止める。50×50 の穴の格子(2499 複製)は通す。
#: 保存済みの文書の再生(`_do_pattern`)にはこの上限を掛けない。
MAX_PATTERN_COPIES = 2500


def _check_copy_count(sources: int, copies_per_source: int) -> None:
    total = sources * copies_per_source
    if total > MAX_PATTERN_COPIES:
        raise ValueError(
            f"反復で増える図形が多すぎる({sources}個 × {copies_per_source}複製 = {total} > {MAX_PATTERN_COPIES})。"
            "個数か反復する図形を減らす"
        )


def _source_entities(sketch, sketch_id: str, entity_ids: Sequence[str]) -> List[Entity]:
    """反復の元にする図形を並び順に引く。未知の図形は KeyError。"""
    by_id = {entity.id: entity for entity in sketch.entities}
    for entity_id in entity_ids:
        if entity_id not in by_id:
            raise KeyError(
                unknown_id_message("entity", entity_id, [entity.id for entity in sketch.entities], where=f"sketch {sketch_id!r}")
            )
    return [by_id[entity_id] for entity_id in entity_ids]


def _make_copies(
    document: Document,
    sketch_id: str,
    entity_ids: Sequence[str],
    place: Placement,
    new_ids: Sequence[str],
    *,
    verb: str,
) -> List[str]:
    """元の図形ごとに `place` の並びで複製を作り、ID の並びを返す。全部検査してから足す
    (足しながら検査すると、途中で元と重なる複製が分かったとき一部だけ増えた文書が残る)。
    `new_ids` は作る ID の並び(再生では記録したものを渡す)。"""
    sketch = get_sketch(document, sketch_id)
    planned: List[tuple] = []
    for entity in _source_entities(sketch, sketch_id, entity_ids):
        base = parameters.resolve_all(document, entity.params)
        for values in place(entity.kind, base):
            if values == base:
                raise ValueError(f"{verb}: 複製が元の図形 {entity.id!r} と完全に重なる(間隔か角度が0)")
            require_valid_params(entity.kind, values)
            planned.append((entity.kind, values))
    if len(new_ids) != len(planned):
        raise ValueError(f"{verb}: {len(planned)} copies planned but {len(new_ids)} ids recorded")
    return add_entities(
        document, [(sketch, new_id, kind, values) for new_id, (kind, values) in zip(new_ids, planned)]
    )


def _require_entities(entity_ids: Sequence[str], verb: str) -> List[str]:
    ids = [str(entity_id) for entity_id in entity_ids]
    if not ids:
        raise ValueError(f"{verb} requires at least one entity")
    return ids


def _count_copies(document: Document, sketch_id: str, entity_ids: Sequence[str], place: Placement) -> int:
    sketch = get_sketch(document, sketch_id)
    return sum(
        len(place(entity.kind, parameters.resolve_all(document, entity.params)))
        for entity in _source_entities(sketch, sketch_id, entity_ids)
    )


def _bake(document: Document, verb: str, sketch_id: str, entity_ids: List[str], place: Placement, shaped: dict) -> List[str]:
    """新しい ID で複製を作り、ログに1行足す。"""
    new_ids = [new_entity_id() for _ in range(_count_copies(document, sketch_id, entity_ids, place))]
    created = _make_copies(document, sketch_id, entity_ids, place, new_ids, verb=verb)
    document.log.append(verb, {"sketch_id": sketch_id, "entities": list(entity_ids), **shaped, "created": list(created)})
    return created


# --------------------------------------------------------------------------------------
# 矩形反復
# --------------------------------------------------------------------------------------
def _rectangular_placement(dx: float, dy: float, nx: int, ny: int) -> Placement:
    """格子 (i, j) の (0, 0) が元の位置。並びは j の外側・i の内側(旧形式の展開と同じ)。"""

    def place(kind: str, base: Dict[str, float]) -> List[Dict[str, float]]:
        ops = entity_kind_ops(kind)
        copies = []
        for j in range(ny):
            for i in range(nx):
                if i == 0 and j == 0:
                    continue
                values = dict(base)
                values.update(ops.translate(base, i * dx, j * dy))
                copies.append(values)
        return copies

    return place


def pattern_rectangular(
    doc: Document,
    sketch_id: str,
    entities: Sequence[str],
    dx: float,
    dy: float,
    nx: int,
    ny: int,
) -> List[str]:
    """図形群を nx × ny の矩形状に反復し、作った複製の EntityId を返す。"""
    if not parameters.is_positive_int(nx) or not parameters.is_positive_int(ny):
        raise ValueError(f"nx/ny must be integers >= 1, got nx={nx!r}, ny={ny!r}")
    nx_i, ny_i = int(nx), int(ny)
    if nx_i * ny_i < 2:
        raise ValueError(f"pattern_rectangular makes no copies with nx={nx_i}, ny={ny_i}")
    delta_x, delta_y = float(dx), float(dy)
    if not math.isfinite(delta_x) or not math.isfinite(delta_y):
        raise ValueError(f"dx/dy must be finite numbers, got dx={dx!r}, dy={dy!r}")
    entity_ids = _require_entities(entities, VERB_PATTERN_RECTANGULAR)
    _check_copy_count(len(entity_ids), nx_i * ny_i - 1)
    return _bake(
        doc,
        VERB_PATTERN_RECTANGULAR,
        sketch_id,
        entity_ids,
        _rectangular_placement(delta_x, delta_y, nx_i, ny_i),
        {"dx": delta_x, "dy": delta_y, "nx": nx_i, "ny": ny_i},
    )


# --------------------------------------------------------------------------------------
# 円形反復
# --------------------------------------------------------------------------------------
def _circular_placement(cx: float, cy: float, count: int, total_angle: float, rotate_copies: bool) -> Placement:
    step = circular_pattern_step(count, total_angle)

    def place(kind: str, base: Dict[str, float]) -> List[Dict[str, float]]:
        ops = entity_kind_ops(kind)
        copies = []
        for i in range(1, count):
            values = dict(base)
            values.update(ops.rotate(base, cx, cy, i * step, rotate_copies))
            copies.append(values)
        return copies

    return place


def _geometry_signature(kind: str, params: Dict[str, float]) -> tuple:
    """図形の幾何を、表し方の違い(線分の端点の順、長方形の基準の角と向き、楕円の長軸の取り方、円弧の角度の巻き)に
    よらず同じ値にする署名。回した複製が元と**重なるか**を比べるのに使う(丸めは小数6桁)。"""

    def r(value: float) -> float:
        return round(float(value), 6) + 0.0  # -0.0 を 0.0 にする

    if kind == "circle":
        return ("circle", r(params["cx"]), r(params["cy"]), r(params["diameter"]))
    if kind == "line":
        ends = sorted([(r(params["x0"]), r(params["y0"])), (r(params["x1"]), r(params["y1"]))])
        return ("line", *ends)
    if kind == "rectangle":
        return ("rectangle", *sorted((r(x), r(y)) for x, y in kernel_sketch.rectangle_corners(params)))
    if kind == "arc":
        sweep = (float(params["end_deg"]) - float(params["start_deg"])) % 360.0
        if sweep < 1e-9 or sweep > 360.0 - 1e-9:  # 始終の角が同じ(0〜360 度など)円弧は全周で、円と同じ
            return ("circle", r(params["cx"]), r(params["cy"]), r(2.0 * float(params["radius"])))
        return (
            "arc", r(params["cx"]), r(params["cy"]), r(params["radius"]),
            r(params["start_deg"] % 360.0) % 360.0, r(params["end_deg"] % 360.0) % 360.0,
        )
    if kind == "ellipse":
        width, height, angle = float(params["width"]), float(params["height"]), float(params["angle"])
        if abs(width - height) <= 1e-9:
            return ("circle", r(params["cx"]), r(params["cy"]), r(width))
        if width < height:  # 長軸と短軸を入れ替えても同じ楕円(向きは 90 度ずれる)
            width, height, angle = height, width, angle + 90.0
        return ("ellipse", r(params["cx"]), r(params["cy"]), r(width), r(height), r(angle % 180.0) % 180.0)
    if kind == "bezier":
        points = [(r(params[kx]), r(params[ky])) for kx, ky in kernel_sketch.BEZIER_POINT_KEYS]
        return ("bezier", *min(points, points[::-1]))  # 始点と終点を入れ替えても同じ曲線
    return (kind, *sorted((key, r(value)) for key, value in params.items()))


def pattern_circular(
    doc: Document,
    sketch_id: str,
    entities: Sequence[str],
    center: Any,
    count: int,
    total_angle: float = 360.0,
    rotate_copies: bool = True,
) -> List[str]:
    """図形群をスケッチ平面の法線まわりに円形反復し、作った複製の EntityId を返す。

    - `count` は**元を含めた総数**(矩形反復の `nx`/`ny` と同じ流儀)。2未満は複製ができないので ValueError。
    - `total_angle` は `0 < angle <= 360`。360超は複製が必ず重なるので受け取らない。
    - **回転中心と中心が一致する図形は、元と幾何として重なる複製があるときだけ ValueError**(重なる複製は
      「反復したのに増えない」が成功として通り、領域検出が同じ辺を2本見る)。
    """
    if not parameters.is_positive_int(count):
        raise ValueError(f"count must be an integer >= 1, got {count!r}")
    count_i = int(count)
    if count_i < 2:
        raise ValueError("pattern_circular makes no copies with count=1")
    angle = float(total_angle)
    if not math.isfinite(angle) or not (0.0 < angle <= 360.0):
        raise ValueError(f"total_angle must satisfy 0 < angle <= 360, got {total_angle!r}")
    cx, cy = point_2d(center, "center")
    entity_ids = _require_entities(entities, VERB_PATTERN_CIRCULAR)
    _check_copy_count(len(entity_ids), count_i - 1)

    sketch = get_sketch(doc, sketch_id)
    step = circular_pattern_step(count_i, angle)
    for entity in _source_entities(sketch, sketch_id, entity_ids):
        resolved = parameters.resolve_all(doc, entity.params)
        ops = entity_kind_ops(entity.kind)
        ecx, ecy = ops.center_2d(resolved)
        if abs(ecx - cx) > 1e-9 or abs(ecy - cy) > 1e-9:
            continue  # 中心が回転中心から離れていれば、複製は元と別の場所に出る
        # 中心が回転中心と一致する図形は、複製が元と**幾何として**重なることがある(円は常に、円弧・線分・長方形・楕円は
        # 対称な角度で。rotate_copies=False は全種別で位置が動かない)。重なる複製が1つでもあれば断る。
        original = _geometry_signature(entity.kind, resolved)
        for index in range(1, count_i):
            copy = ops.rotate(resolved, cx, cy, index * step, bool(rotate_copies))
            if _geometry_signature(entity.kind, copy) == original:
                raise ValueError(
                    f"entity {entity.id!r} is centred on the pattern pivot ({cx}, {cy}); "
                    f"copy {index} (rotated {index * step:g} deg) would coincide with the original "
                    "(change the count / total_angle, or move the pivot or the entity off-centre)"
                )

    return _bake(
        doc,
        VERB_PATTERN_CIRCULAR,
        sketch_id,
        entity_ids,
        _circular_placement(cx, cy, count_i, angle, bool(rotate_copies)),
        {"center": [cx, cy], "count": count_i, "total_angle": angle, "rotate_copies": bool(rotate_copies)},
    )


# --------------------------------------------------------------------------------------
# 再生・undo
# --------------------------------------------------------------------------------------
def _placement_of(operation: Operation) -> Placement:
    args = operation.args
    if operation.verb == VERB_PATTERN_CIRCULAR:
        center = args["center"]
        return _circular_placement(
            float(center[0]), float(center[1]), int(args["count"]), float(args["total_angle"]), bool(args["rotate_copies"])
        )
    return _rectangular_placement(float(args["dx"]), float(args["dy"]), int(args["nx"]), int(args["ny"]))


def _is_live_form(operation: Operation) -> bool:
    """旧形式(スケッチに `Pattern` を置く)の行か。新形式は `created` を持つ。"""
    return "created" not in operation.args


def _do_pattern(document: Document, operation: Operation) -> None:
    """redo / replay: 記録した ID で複製を作り直す(旧形式の行は `Pattern` を置き直す)。ログには触らない。"""
    if _is_live_form(operation):
        _install_live_pattern(document, operation)
        return
    args = operation.args
    _make_copies(
        document,
        args["sketch_id"],
        list(args["entities"]),
        _placement_of(operation),
        list(args["created"]),
        verb=operation.verb,
    )


def _undo_pattern(document: Document, operation: Operation) -> None:
    """undo: 作った複製を消す(元の図形は残る。旧形式の行は `Pattern` とそのパラメータを消す)。"""
    args = operation.args
    if _is_live_form(operation):
        _remove_live_pattern(document, args["sketch_id"], args["pattern_id"])
        return
    remove_entities(document, args["created"], sketch_id=args["sketch_id"])


# 旧形式の行の再生。名前つきの値の振り順(矩形は dx, dy, nx, ny、円形は cx, cy, count, total_angle)が
# 保存済みの `set_parameter` の名前を決めているので、並びを変えない。
def _install_live_pattern(document: Document, operation: Operation) -> None:
    args = operation.args
    sketch = get_sketch(document, args["sketch_id"])
    if operation.verb == VERB_PATTERN_CIRCULAR:
        center = args["center"]
        values = {
            "cx": float(center[0]),
            "cy": float(center[1]),
            "count": int(args["count"]),
            "total_angle": float(args["total_angle"]),
        }
        extra = {"kind": "circular", "options": {"rotate_copies": bool(args["rotate_copies"])}}
    else:
        values = {"dx": float(args["dx"]), "dy": float(args["dy"]), "nx": int(args["nx"]), "ny": int(args["ny"])}
        extra = {}
    if any(pattern.id == args["pattern_id"] for pattern in sketch.patterns):
        raise ValueError(f"pattern id already exists: {args['pattern_id']!r}")
    refs = parameters.register_all(document, {key: float(value) for key, value in values.items()})
    sketch.patterns.append(Pattern(id=args["pattern_id"], sources=list(args["entities"]), params=refs, **extra))


def _remove_live_pattern(document: Document, sketch_id: str, pattern_id: str) -> None:
    sketch = get_sketch(document, sketch_id)
    remaining = []
    for pattern in sketch.patterns:
        if pattern.id == pattern_id:
            parameters.release_param_refs(document, pattern.params)
        else:
            remaining.append(pattern)
    sketch.patterns = remaining


register_verb(VERB_PATTERN_RECTANGULAR, do=_do_pattern, undo=_undo_pattern)
register_verb(VERB_PATTERN_CIRCULAR, do=_do_pattern, undo=_undo_pattern)


# --------------------------------------------------------------------------------------
# 入口(`duocad/entry.py`) —— 上の verb を掛け、応答に載せるものを返す
# --------------------------------------------------------------------------------------
@entry(VERB_PATTERN_RECTANGULAR)
def _enter_pattern_rectangular(
    document: Document, sketch_id: str, entities: Any, dx: float, dy: float, nx: int, ny: int
) -> dict:
    return {"created": pattern_rectangular(document, sketch_id, entities, dx, dy, nx, ny)}


@entry(VERB_PATTERN_CIRCULAR)
def _enter_pattern_circular(
    document: Document,
    sketch_id: str,
    entities: Any,
    center: Any,
    count: int,
    total_angle: float = 360.0,
    rotate_copies: bool = True,
) -> dict:
    created = pattern_circular(
        document, sketch_id, entities, center, count, total_angle=total_angle, rotate_copies=rotate_copies
    )
    return {"created": created}
