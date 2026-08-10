"""verb `pattern_rectangular` — スケッチの図形を矩形状に反復する(実装計画書§1)。M3で追加。

**反復は1つのverbとして持つ**(§2「verbの粒度」)。N個の独立した図形へ展開すると、
個数や間隔を後から変えられなくなり、**反復の存在理由そのものが消える**。
スケッチが持つのは `Pattern` 1個で、nx×ny の図形は**幾何を作るときに展開される**導出物。

**M3のスタブ段階(2026-08-11)**: 契約だけ置いて実体は `NotImplementedError`。

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

from typing import Sequence

from ..document import Document
from ..oplog import Operation, register_verb

VERB_PATTERN_RECTANGULAR = "pattern_rectangular"


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
    raise NotImplementedError("M3で実装する: pattern_rectangular")


# --------------------------------------------------------------------------------------
# verbの登録
# --------------------------------------------------------------------------------------
def _do_pattern_rectangular(document: Document, operation: Operation) -> None:
    """redo / replay: 記録された PatternId と参照でパターンを作り直す。ログには触らない。"""
    raise NotImplementedError("M3で実装する: pattern_rectangular の redo/replay")


def _undo_pattern_rectangular(document: Document, operation: Operation) -> None:
    """undo: パターンを取り除く(元の図形は残る)。"""
    raise NotImplementedError("M3で実装する: pattern_rectangular の undo")


register_verb(
    VERB_PATTERN_RECTANGULAR,
    do=_do_pattern_rectangular,
    undo=_undo_pattern_rectangular,
)
