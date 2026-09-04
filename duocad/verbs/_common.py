"""verb層が共有する道具 —— **1 verb 1モジュールを保ったまま、同型の部品だけを1つにする**。

- undo用スナップショット表(`fillet` / `chamfer` / `extrude` / `extrude_face` が使う)
- `editable_body()`(`fillet` / `chamfer` / `extrude_face` が使う)

**スナップショット表を1つにしていることが、この統合の主な利得**である。プレビューは文書を
投機的に変えて戻すので(`duocad/speculative.py`)、**戻し忘れたテーブルが1つでもあると、
中断したプレビューの形状が後の実undoで復元されうる**。表が1つなら1箇所で確かめれば済む。
"""
from __future__ import annotations

import math
from typing import Any, List, Optional, Sequence, Union
from weakref import WeakKeyDictionary

from .. import meshbody
from ..document import MESH_KIND, Body, BodyId, Document

#: undo用のスナップショット。`(body_id, seq) -> 直前の形状`。
#:
#: **文書ごとに持ち、文書が消えれば一緒に消える**(`WeakKeyDictionary`)。値は
#: `None` でもよい —— `extrude` の join が新しいボディを作った場合、「前の形状は無い」が
#: 正しい記録である。
SNAPSHOTS: "WeakKeyDictionary[Document, dict]" = WeakKeyDictionary()


def remember(doc: Document, body_id: BodyId, seq: int, shape: Any) -> None:
    """操作前の形状を覚える(同じseqで再適用されたら上書きする)。"""
    SNAPSHOTS.setdefault(doc, {})[(body_id, seq)] = shape


def recall(doc: Document, body_id: BodyId, seq: int) -> Any:
    """覚えていた形状を取り出して忘れる。

    無ければ **KeyError**(黙ってundoを空振りさせない)。保存したログを読んだだけで
    再生していない文書は、スナップショットを持たないのでここへ来る。
    """
    snapshots = SNAPSHOTS.get(doc)
    if snapshots is None or (body_id, seq) not in snapshots:
        raise KeyError(
            f"no snapshot for undo of seq={seq} on body {body_id!r}: "
            "保存したログを再生せずにundoしようとしている。"
            "OpLog.replay(document) してから undo すること"
        )
    return snapshots.pop((body_id, seq))


def editable_body(
    doc: Document,
    body_id: Optional[BodyId],
    *,
    what: str,
    require_visible: bool = False,
) -> Body:
    """操作対象のボディ。**編集できないもの・形状の無いものは例外**(黙って無視しない)。

    `what` は失敗のメッセージに入る動詞(`"fillet"` / `"extrude"` 等)。
    `require_visible` は「見えないものは指せない」を課すかどうか。

    **選ぶ時点の門番(`selection._visible_body`)だけでは足りない**。アプリ内AIが人と本当に
    並行して動くと、**確定選択した後・verbを呼ぶ前に人がそのボディを隠す**という順序が
    起きうる —— 選択に入った時点では可視でも、使う時点では不可視というずれが生じ、選択側の
    門番はもう働かない。実測(`tests/test_agent_concurrency.py`)で `fillet`/`chamfer` が
    この順序で隠したボディを黙って丸めることを確認したため、**面・辺どちらを取るverbも
    True にする**(`fillet` / `chamfer` / `extrude_face`)。
    """
    if body_id is None:
        active = doc.active_body()
        if active is None:
            raise ValueError(f"no active body to {what} (document is empty)")
        body_id = active.id
    if not doc.is_editable(body_id):  # 存在しないIDは KeyError(document側の契約)
        # **編集verbがメッシュボディを断る唯一の入口**(2026-09-05)。verbごとに
        # `kind` を見て分岐させると、verbが増えるたびに断り漏れが1本ずつできる。
        body = doc.get_body(body_id)
        if body.kind == MESH_KIND:
            meshbody.reject(body, what)
        raise ValueError(
            f"body {body_id!r} is not editable (kind={body.kind!r})"
        )
    body = doc.get_body(body_id)
    if require_visible and not body.visible:
        raise ValueError(
            f"body {body_id!r} is hidden: 見えないものは指せない(set_visible で表示すること)"
        )
    if body.shape is None:
        raise ValueError(f"body {body_id!r} has no shape to {what}")
    return body


def deletable_body(doc: Document, body_id: BodyId) -> Body:
    """削除の対象になるボディ。**編集できるかは問わない**(メッシュボディも消せる)。

    `editable_body` と分けてあるのは、**削除だけは種別に関係なく通す**から ——
    「編集できないから消せもしない」ボディがあると、間違えて入れた形を取り除けなくなる。
    可視性の門番(`require_visible` 相当)は `editable_body` と同じ理由で課す:
    確定選択した後・verbを呼ぶ前に人がそのボディを隠す順序が実際に起きる。
    """
    body = doc.get_body(body_id)  # 存在しないIDは KeyError(document側の契約)
    if not body.visible:
        raise ValueError(
            f"body {body_id!r} is hidden: 見えないものは指せない(set_visible で表示すること)"
        )
    if body.shape is None and body.mesh is None:
        raise ValueError(f"body {body_id!r} has no shape to delete")
    return body


def normalize_values(
    edges: Sequence[int],
    value: Union[float, Sequence[float]],
    *,
    label: str,
) -> tuple[List[float], Union[float, List[float]]]:
    """辺ごとの値をそろえる(スカラーなら全辺へ配る)。**0以下と非有限は例外**。

    戻り値は `(適用する値のリスト, ログへ積む形)`。ログには**打った形のまま**
    (スカラーならスカラー)を積む —— 再生でリストへ化けると、ログの diff が読めなくなる。
    """
    if isinstance(value, (int, float)):
        values = [float(value)] * len(edges)
        stored: Union[float, List[float]] = float(value)
    else:
        values = [float(item) for item in value]
        if len(values) != len(edges):
            raise ValueError(
                f"{label} list length {len(values)} != edges length {len(edges)}"
            )
        stored = values
    for item in values:
        if not math.isfinite(item) or item <= 0.0:
            raise ValueError(f"{label} must be finite and > 0, got {item!r}")
    return values, stored
