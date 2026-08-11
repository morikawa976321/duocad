"""verb `chamfer` — 辺を面取りする(v1.1/v1.2から前倒し・2026-08-11)。**このタスクではスタブ**。

**このverbが入って初めて実物が1つ作れるようになる**: `PC_case_feet.step`(PCケースの脚)は
1ソリッド5面 = 平面3 + 全周円筒1 + **円錐1**(2026-08-11に実測して確認)で、現行の7verbでは
円錐面を作れない。

契約(`tests/test_chamfer.py` が正):

    chamfer(doc, edges, distance, body_id=None) -> Operation
        `edges` は1始まりの辺の添字のリスト、`distance` は float か辺ごとのリスト。
        ログに `verb="chamfer"`,
        `args={"edges": [int,...], "distance": float|[float,...], "body_id": ..}` を積む。

**どの辺を面取りすると何ができるか**(2026-08-11実測。ここを取り違えると契約が嘘になる):

    箱の**まっすぐな辺**を面取り  -> 平面が1枚増える(6面 -> 平面7・面7)。**円錐にはならない**
    円筒の**円い縁**を面取り      -> **円錐**が1枚できる(平面2+全周円筒1 -> +cone 1・面4)

タスクファイルの項番8は「箱のエッジに面取りをかけると円錐面ができること」と書いているが、
実測ではまっすぐな辺の面取りは平面である(円錐は円い辺から生まれる)。**測ってから書く**という
タスクファイル自身の指示に従い、テストは実測どおりに書いた(`tests/test_chamfer.py` の
モジュールdocstring参照)。

**フィレットと同じく黙って失敗する**(`BRepFilletAPI_MakeChamfer` は同じ系統)。`IsDone()` を
必ず確認し、失敗したら操作ごと捨てて文書もログも変更しない。由来も同じく
`kernel.normalize_provenance` 経由で回収する(`History()` を持たない側)。

**OCP を直接importしない**。幾何は `duocad.kernel` 越しに呼ぶ。
"""
from __future__ import annotations

from typing import Optional, Sequence, Union

from ..document import BodyId, Document
from ..oplog import Operation, register_verb

VERB_CHAMFER = "chamfer"


def chamfer(
    doc: Document,
    edges: Sequence[int],
    distance: Union[float, Sequence[float]],
    body_id: Optional[BodyId] = None,
) -> Operation:
    """辺を面取りしてログに積み、Operation を返す。**スタブ(実装は次のタスク)**。"""
    raise NotImplementedError("chamfer は未実装(テスト先行。次のタスクで実装する)")


def _do_chamfer(document: Document, operation: Operation) -> None:
    """redo / replay: 記録された辺と距離で面取りし直す(ログには触らない)。"""
    raise NotImplementedError("chamfer の redo/replay は未実装")


def _undo_chamfer(document: Document, operation: Operation) -> None:
    """undo: 面取り前の形状へ戻す。"""
    raise NotImplementedError("chamfer の undo は未実装")


register_verb(VERB_CHAMFER, do=_do_chamfer, undo=_undo_chamfer)
