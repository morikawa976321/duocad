"""自動で付ける名前 —— ボディとスケッチに**種類ごとに独立した通し番号**を振る。

**振った番号は作成の操作の args に記録する**(1つなら `number`、複製のように複数なら `numbers`)。
redo・再生はそれを読んで同じ名前を付ける —— その場で振り直すと、undo → redo で名前が変わる。

**一度振った番号は再利用しない**。最大値は `OpLog` が持ち(undo しても下げない・保存に載る)、
再生した操作の番号もそこへ合わせる(`OpLog.note_number`)。

番号の記録が無い操作(この仕組みより前に保存された文書)は、当時の名前のまま再生する
(押し出しは "solid"、スケッチは名前なし)。

ボディの複製は通し番号を使わず、「元の名前 + 連番」(`copy_name`)を作成の操作の args の `names` に記録する。
複製の `numbers` を持つ操作(それ以前に保存された文書)は、当時の通し番号の名前のまま再生する。

人や AI が名前を渡した作成(`add_mesh_body(name=...)`・STEP の取り込み・形状最適化の結果)は
番号を振らない。番号は「名前が与えられなかったときの名前」である。
"""
from __future__ import annotations

import re
from typing import Collection, Optional

from .document import Document

BODY = "body"
SKETCH = "sketch"

_LABELS = {BODY: "ボディ", SKETCH: "スケッチ"}


def label(kind: str, number: int) -> str:
    return f"{_LABELS[kind]}{int(number)}"


_COPY_SUFFIX = re.compile(r" \d+$")


def copy_name(source: str, taken: Collection[str]) -> str:
    """複製の名前: 「元の名前 + 空白 + 連番」で、`taken` に無い最小の番号(2から)。

    複製の複製(`ブラケット 2`)は、番号を外した名前(`ブラケット`)が `taken` にあるときだけ外して数え直す
    (人が付けた `M8 30` のような名前の末尾の数字は削らない)。
    """
    base = source
    stripped = _COPY_SUFFIX.sub("", source)
    if stripped != source and stripped in taken:
        base = stripped
    number = 2
    while f"{base} {number}" in taken:
        number += 1
    return f"{base} {number}"


def allocate(doc: Document, kind: str) -> int:
    """新しい番号を振る(作成の verb が、ログへ積む前に呼ぶ)。"""
    return doc.log.allocate_number(kind)


def recorded(doc: Document, kind: str, number: Optional[int], legacy: Optional[str]) -> Optional[str]:
    """redo / 再生: ログに記録された番号の名前。記録が無ければ `legacy`(当時の名前)。"""
    if number is None:
        return legacy
    doc.log.note_number(kind, int(number))
    return label(kind, int(number))
