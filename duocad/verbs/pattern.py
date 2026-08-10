"""verb `pattern_rectangular` — スケッチの図形を矩形状に反復する(実装計画書§1)。M3で追加。

**反復は1つのverbとして持つ**(§2「verbの粒度」)。N個の独立した図形へ展開すると、
個数や間隔を後から変えられなくなり、**反復の存在理由そのものが消える**。
スケッチが持つのは `Pattern` 1個で、nx×ny の図形は**幾何を作るときに展開される**導出物。

契約:
    pattern_rectangular(doc, sketch_id, entities, dx, dy, nx, ny) -> PatternId
        `entities` は反復する図形のIDのリスト(同じスケッチのもの)。
        `dx`/`dy` は間隔、`nx`/`ny` は**元を含めた個数**(Fusion360と同じ流儀)。
        4つの数値はすべて `parameters.register_value` で名前つきの値になる
        (後から `set_parameter` で個数も間隔も変えられる。§6予防5)。
        `verb="pattern_rectangular"`,
        `args={"sketch_id":.., "pattern_id":.., "entities":[..], "dx":.., "dy":.., "nx":.., "ny":..}`
        を**1行だけ**積んで PatternId を返す(nx×ny 行にならない)。

**失敗を黙って通さない**(§6予防6):

- `nx` / `ny` は**1以上の整数**。0・負数・2.5 のような非整数は ValueError
- `entities` が空、または同じスケッチに無いIDを含むなら ValueError / KeyError
- 既に別の反復に使われている図形を二重に反復させない(ValueError)
- 失敗時は `doc` にもログにも変更を残さない

**元の図形は反復に吸収される**: 反復の格子は (i, j) = (0, 0) を含み、そこが元の図形の位置に
なる。したがって領域検出のときは、**反復に参照されている図形を単独では描かない**
(描くと (0,0) の辺が二重になる)。

**OCP を直接importしない**。幾何は `duocad.kernel` 越し。
"""
from __future__ import annotations

from typing import Any, Sequence
from uuid import uuid4

from .. import parameters
from ..document import Document
from ..oplog import Operation, register_verb
from ..sketch import Pattern, get_sketch

VERB_PATTERN_RECTANGULAR = "pattern_rectangular"


#: 個数の検査は `set_parameter` からも使うので parameters 側に置いてある
#: (作成時だけ検査していると、後から書き込む経路が素通りする)
_is_positive_int = parameters.is_positive_int


def _release_param_refs(document: Document, params: dict) -> None:
    for ref in params.values():
        if isinstance(ref, str):
            document.parameters.pop(ref, None)


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
    """Pattern をスケッチへ足し、4つの数値を名前つきの値として登録する。ログには触らない。"""
    sketch = get_sketch(document, sketch_id)
    if any(pattern.id == pattern_id for pattern in sketch.patterns):
        raise ValueError(f"pattern id already exists: {pattern_id!r}")
    refs = {
        "dx": parameters.register_value(document, float(dx)),
        "dy": parameters.register_value(document, float(dy)),
        "nx": parameters.register_value(document, float(nx)),
        "ny": parameters.register_value(document, float(ny)),
    }
    sketch.patterns.append(
        Pattern(id=pattern_id, sources=list(entities), params=refs)
    )


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
    if not _is_positive_int(nx) or not _is_positive_int(ny):
        raise ValueError(
            f"nx/ny must be integers >= 1, got nx={nx!r}, ny={ny!r}"
        )
    nx_i = int(nx)
    ny_i = int(ny)
    entity_ids = list(entities)
    if not entity_ids:
        raise ValueError("pattern_rectangular requires at least one entity")

    sketch = get_sketch(doc, sketch_id)
    known = {entity.id for entity in sketch.entities}
    for entity_id in entity_ids:
        if entity_id not in known:
            raise KeyError(
                f"no such entity: {entity_id!r} in sketch {sketch_id!r}"
            )

    already_used = {
        source
        for pattern in sketch.patterns
        for source in pattern.sources
    }
    for entity_id in entity_ids:
        if entity_id in already_used:
            raise ValueError(
                f"entity {entity_id!r} is already used by another pattern"
            )

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


def _undo_pattern_rectangular(document: Document, operation: Operation) -> None:
    """undo: パターンを取り除く(元の図形は残る)。その4つのパラメータも文書から消す。"""
    args = operation.args
    sketch = get_sketch(document, args["sketch_id"])
    pattern_id = args["pattern_id"]
    remaining = []
    for pattern in sketch.patterns:
        if pattern.id == pattern_id:
            _release_param_refs(document, pattern.params)
        else:
            remaining.append(pattern)
    sketch.patterns = remaining


register_verb(
    VERB_PATTERN_RECTANGULAR,
    do=_do_pattern_rectangular,
    undo=_undo_pattern_rectangular,
)
