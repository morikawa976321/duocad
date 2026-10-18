"""verb `pattern_rectangular` / `pattern_circular` — スケッチの図形を反復する。

**反復は1つのverbとして持つ**。N個の独立した図形へ展開すると個数や間隔を後から変えられなくなる。
スケッチが持つのは `Pattern` 1個で、nx×ny の図形は幾何を作るときに展開される導出物。ログも1行だけ。

`nx` / `ny` / `count` は元を含めた個数。数値はすべて `parameters.register_value` で名前つきの値になり、
後から `set_parameter` で変えられる。

失敗は例外を送出し、`doc` にもログにも変更を残さない: `nx` / `ny` は1以上の整数 / `entities` が空、
または同じスケッチに無いIDを含むと ValueError・KeyError / 既に別の反復に使われている図形は不可。

**元の図形は反復に吸収される**: 格子は (i, j) = (0, 0) を含み、そこが元の位置になる。領域検出のとき、
反復に参照されている図形は単独では描かない((0,0) の辺が二重になる)。
"""
from __future__ import annotations

import math
from typing import Any, Dict, Optional, Sequence
from uuid import uuid4

from .. import parameters
from ..document import Document, unknown_id_message
from ..oplog import Operation, register_verb
from ..entry import entry
from ..sketch import Entity, Pattern, entity_kind_ops, get_sketch, point_2d

VERB_PATTERN_RECTANGULAR = "pattern_rectangular"
VERB_PATTERN_CIRCULAR = "pattern_circular"


#: 反復1回で増やせる図形の数(元を除く複製の総数)の上限。状態配信の領域検出は図形数にほぼ比例して重く
#: (2026-10-18 実測: 1図形あたり約 2 ms。100×100 で約 22 秒、nx=20000 で約 44 秒)、桁の取り違えが
#: イベントループを数十秒〜事実上無限に止める。保存済みの文書の再生(`_do_*`)にはこの上限を掛けない。
MAX_PATTERN_COPIES = 5000


def _check_copy_count(sources: int, copies_per_source: int) -> None:
    total = sources * copies_per_source
    if total > MAX_PATTERN_COPIES:
        raise ValueError(
            f"反復で増える図形が多すぎる({sources}個 × {copies_per_source}複製 = {total} > {MAX_PATTERN_COPIES})。"
            "個数か反復する図形を減らす"
        )


def _check_sources(sketch, sketch_id: str, entity_ids: Sequence[str]) -> Dict[str, Entity]:
    """反復の元にする図形の検査(同じスケッチにあるか → 既に別の反復に使われていないか)。
    未知の図形は KeyError、使用済みは ValueError。戻りは ID → 図形(円形反復が中心の一致検査に使う)。"""
    by_id = {entity.id: entity for entity in sketch.entities}
    for entity_id in entity_ids:
        if entity_id not in by_id:
            raise KeyError(
                unknown_id_message("entity", entity_id, [entity.id for entity in sketch.entities], where=f"sketch {sketch_id!r}")
            )
    already_used = {source for pattern in sketch.patterns for source in pattern.sources}
    for entity_id in entity_ids:
        if entity_id in already_used:
            raise ValueError(f"entity {entity_id!r} is already used by another pattern")
    return by_id


def _append_pattern(
    document: Document,
    sketch_id: str,
    pattern_id: str,
    entities: Sequence[str],
    values: Dict[str, float],
    *,
    kind: str = "rectangular",
    options: Optional[dict] = None,
) -> None:
    """Pattern をスケッチへ足し、`values` を名前つきの値として登録する(**キーの並び**が `d<n>` の振り順。
    矩形は `dx, dy, nx, ny`、円形は `cx, cy, count, total_angle`)。ログには触らない。"""
    sketch = get_sketch(document, sketch_id)
    if any(pattern.id == pattern_id for pattern in sketch.patterns):
        raise ValueError(f"pattern id already exists: {pattern_id!r}")
    refs = parameters.register_all(document, {key: float(value) for key, value in values.items()})
    extra = {} if kind == "rectangular" else {"kind": kind, "options": dict(options or {})}
    sketch.patterns.append(Pattern(id=pattern_id, sources=list(entities), params=refs, **extra))


def _install_pattern(
    document: Document,
    sketch_id: str,
    pattern_id: str,
    entities: Sequence[str],
    dx: float,
    dy: float,
    nx: int,
    ny: int,
) -> None:
    _append_pattern(document, sketch_id, pattern_id, entities, {"dx": dx, "dy": dy, "nx": nx, "ny": ny})


def pattern_rectangular(
    doc: Document,
    sketch_id: str,
    entities: Sequence[str],
    dx: float,
    dy: float,
    nx: int,
    ny: int,
) -> str:
    """図形群を nx × ny の矩形状に反復し、PatternId を返す。"""
    if not parameters.is_positive_int(nx) or not parameters.is_positive_int(ny):
        raise ValueError(
            f"nx/ny must be integers >= 1, got nx={nx!r}, ny={ny!r}"
        )
    nx_i = int(nx)
    ny_i = int(ny)
    entity_ids = list(entities)
    if not entity_ids:
        raise ValueError("pattern_rectangular requires at least one entity")
    _check_copy_count(len(entity_ids), nx_i * ny_i - 1)

    _check_sources(get_sketch(doc, sketch_id), sketch_id, entity_ids)

    pattern_id = f"pattern-{uuid4().hex[:12]}"
    _install_pattern(
        doc, sketch_id, pattern_id, entity_ids, float(dx), float(dy), nx_i, ny_i
    )
    doc.log.append(
        VERB_PATTERN_RECTANGULAR,
        {
            "sketch_id": sketch_id,
            "pattern_id": pattern_id,
            "entities": list(entity_ids),
            "dx": float(dx),
            "dy": float(dy),
            "nx": nx_i,
            "ny": ny_i,
        },
    )
    return pattern_id


# --------------------------------------------------------------------------------------
# 円形反復
# --------------------------------------------------------------------------------------
def _install_pattern_circular(
    document: Document,
    sketch_id: str,
    pattern_id: str,
    entities: Sequence[str],
    cx: float,
    cy: float,
    count: int,
    total_angle: float,
    rotate_copies: bool,
) -> None:
    _append_pattern(
        document,
        sketch_id,
        pattern_id,
        entities,
        {"cx": cx, "cy": cy, "count": count, "total_angle": total_angle},
        kind="circular",
        options={"rotate_copies": bool(rotate_copies)},
    )


def pattern_circular(
    doc: Document,
    sketch_id: str,
    entities: Sequence[str],
    center: Any,
    count: int,
    total_angle: float = 360.0,
    rotate_copies: bool = True,
) -> str:
    """図形群をスケッチ平面の法線まわりに円形反復し、PatternId を返す。

    - `count` は**元を含めた総数**(矩形反復の `nx`/`ny` と同じ流儀)。
    - **刻み角の場合分け**: 全周(`total_angle == 360`)は `360/count`、全周未満は端点を含むので
      `total_angle/(count-1)`。全周を `count-1` で割ると最後の複製が最初と重なる。
    - `cx` / `cy` / `count` / `total_angle` は名前つきの値にする(後から編集できる)。
      **`rotate_copies` は `params` に入れない** — 寸法ではないので `Pattern.options` へ。
      混ぜると `set_parameter` で `0.5` を書き込める余地ができる。
    - `total_angle` は `0 < angle <= 360`。360超は複製が必ず重なるので受け取らない。
    - **回転中心と図形の中心が一致する場合は ValueError**(全複製が元と重なり、「反復したのに
      何も増えない」が成功として通ってしまう)。

    同じ `Pattern` を使い、`kind="circular"` で区別する。
    """
    if not parameters.is_positive_int(count):
        raise ValueError(f"count must be an integer >= 1, got {count!r}")
    count_i = int(count)
    angle = float(total_angle)
    if not math.isfinite(angle) or not (0.0 < angle <= 360.0):
        raise ValueError(
            f"total_angle must satisfy 0 < angle <= 360, got {total_angle!r}"
        )
    cx, cy = point_2d(center, "center")
    entity_ids = list(entities)
    if not entity_ids:
        raise ValueError("pattern_circular requires at least one entity")
    _check_copy_count(len(entity_ids), count_i - 1)

    by_id = _check_sources(get_sketch(doc, sketch_id), sketch_id, entity_ids)

    for entity_id in entity_ids:
        entity = by_id[entity_id]
        resolved = parameters.resolve_all(doc, entity.params)
        ecx, ecy = entity_kind_ops(entity.kind).center_2d(resolved)
        if abs(ecx - cx) <= 1e-9 and abs(ecy - cy) <= 1e-9:
            # 円と rotate_copies=False は全複製が元と重なる。それ以外(円弧・線分・長方形・楕円を回す反復)は重ならない角度もあるが、
            # 対称な形(正方形の90度刻み、線分の180度)で元と重なるかを種別ごとに判定しないので、中心が一致するものは断る。
            raise ValueError(
                f"entity {entity_id!r} is centred on the pattern pivot ({cx}, {cy}); "
                "copies would coincide with the original (move the pivot or the entity slightly off-centre)"
            )

    pattern_id = f"pattern-{uuid4().hex[:12]}"
    _install_pattern_circular(
        doc,
        sketch_id,
        pattern_id,
        entity_ids,
        cx,
        cy,
        count_i,
        angle,
        bool(rotate_copies),
    )
    doc.log.append(
        VERB_PATTERN_CIRCULAR,
        {
            "sketch_id": sketch_id,
            "pattern_id": pattern_id,
            "entities": list(entity_ids),
            "center": [cx, cy],
            "count": count_i,
            "total_angle": angle,
            "rotate_copies": bool(rotate_copies),
        },
    )
    return pattern_id


def _do_pattern_circular(document: Document, operation: Operation) -> None:
    """redo / replay: 記録された PatternId と参照で反復を作り直す。ログには触らない。"""
    args = operation.args
    center = args["center"]
    _install_pattern_circular(
        document,
        args["sketch_id"],
        args["pattern_id"],
        args["entities"],
        float(center[0]),
        float(center[1]),
        int(args["count"]),
        float(args["total_angle"]),
        bool(args["rotate_copies"]),
    )


def _undo_pattern(document: Document, operation: Operation) -> None:
    """undo(矩形・円形の両方): 反復を取り除く(元の図形は残る)。そのパラメータも文書から消す。"""
    args = operation.args
    sketch = get_sketch(document, args["sketch_id"])
    pattern_id = args["pattern_id"]
    remaining = []
    for pattern in sketch.patterns:
        if pattern.id == pattern_id:
            parameters.release_param_refs(document, pattern.params)
        else:
            remaining.append(pattern)
    sketch.patterns = remaining


register_verb(
    VERB_PATTERN_CIRCULAR,
    do=_do_pattern_circular,
    undo=_undo_pattern,
)


# --------------------------------------------------------------------------------------
# verbの登録
# --------------------------------------------------------------------------------------
def _do_pattern_rectangular(document: Document, operation: Operation) -> None:
    """redo / replay: 記録された PatternId と参照でパターンを作り直す。ログには触らない。"""
    args = operation.args
    _install_pattern(
        document,
        args["sketch_id"],
        args["pattern_id"],
        args["entities"],
        float(args["dx"]),
        float(args["dy"]),
        int(args["nx"]),
        int(args["ny"]),
    )


register_verb(
    VERB_PATTERN_RECTANGULAR,
    do=_do_pattern_rectangular,
    undo=_undo_pattern,
)


# --------------------------------------------------------------------------------------
# 入口(`duocad/entry.py`) —— 上の verb を掛け、応答に載せるものを返す
# --------------------------------------------------------------------------------------
@entry(VERB_PATTERN_RECTANGULAR)
def _enter_pattern_rectangular(
    document: Document, sketch_id: str, entities: Any, dx: float, dy: float, nx: int, ny: int
) -> dict:
    return {"pattern_id": pattern_rectangular(document, sketch_id, entities, dx, dy, nx, ny)}


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
    pattern_id = pattern_circular(
        document, sketch_id, entities, center, count, total_angle=total_angle, rotate_copies=rotate_copies
    )
    return {"pattern_id": pattern_id}
