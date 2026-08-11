"""verb `fillet` — 辺を丸める(v1.1/v1.2から前倒し・2026-08-11)。**このタスクではスタブ**。

**なぜ前倒したのか**: 本人の実際の設計は、どれも現行の7verbでは作れない(2026-08-11の実測)。
積層プレート(74面)にはフィレットとトーラスが要り、PCケース(2835面)は部分円筒646・
トーラス124を持つ。この3verb(フィレット・面取り・円弧)が入って初めて**受入テストを実物に
戻せる**。

契約(`tests/test_fillet.py` が正):

    fillet(doc, edges, radius, body_id=None) -> Operation
        `edges` は**1始まりの辺の添字**のリスト(面の添字と同じ流儀。`query.describe_edges`
        で選ぶ)。`radius` は float(全辺同じ)か、辺ごとの float のリスト(長さは `edges` と同じ)。
        `body_id` の既定はアクティブボディ。編集できないボディ(kind="reference")は ValueError。
        ログに `verb="fillet"`,
        `args={"edges": [int,...], "radius": float|[float,...], "body_id": ..}` を積む。

**OCCTのフィレットは例外を投げずに失敗し、しかも半径に対して単調でない**(2026-08-07実測。
20mm立方体の全辺: r=10.0 は `IsDone()=False` / r=10.5 は成功 / r=30.0 は `IsDone()=False`)。
したがって `IsDone()` を必ず確認し、**失敗したら操作ごと捨てて文書を変更しない・ログにも
積まない**(§6予防6・§7)。「黙って何も起きない」を成功として通さないこと。

**由来(provenance)は正規化アダプタ経由で回収する**。フィレット系(`BRepFilletAPI_*`)は
`History()` を持たず `Modified()` / `Generated()` / `IsDeleted()` だけを持つ(2026-08-11に
`BRepFilletAPI_MakeFillet` で実測: History 無し・他3つ有り)。ブーリアンは `History()` を
持つので取り出し方が違う — **この差の吸収が `kernel.normalize_provenance` の存在理由**
であり、verb側に分岐を書かない。

**OCP を直接importしない**。幾何は `duocad.kernel` 越しに呼ぶ。
"""
from __future__ import annotations

from typing import Any, Optional, Sequence, Union

from ..document import BodyId, Document
from ..oplog import Operation, register_verb

VERB_FILLET = "fillet"


def fillet(
    doc: Document,
    edges: Sequence[int],
    radius: Union[float, Sequence[float]],
    body_id: Optional[BodyId] = None,
) -> Operation:
    """辺を丸めてログに積み、Operation を返す。**スタブ(実装は次のタスク)**。"""
    raise NotImplementedError("fillet は未実装(テスト先行。次のタスクで実装する)")


def _do_fillet(document: Document, operation: Operation) -> None:
    """redo / replay: 記録された辺と半径で丸め直す(ログには触らない)。"""
    raise NotImplementedError("fillet の redo/replay は未実装")


def _undo_fillet(document: Document, operation: Operation) -> None:
    """undo: 丸める前の形状へ戻す。"""
    raise NotImplementedError("fillet の undo は未実装")


register_verb(VERB_FILLET, do=_do_fillet, undo=_undo_fillet)
