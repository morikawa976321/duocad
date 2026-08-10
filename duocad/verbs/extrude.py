"""verb `extrude` — スケッチの**領域**を押し出す(実装計画書§1の3Dのverb)。M2で追加。

`extrude_face`(M1・既存ソリッドの面を押す)とは別のverbである。こちらは
**領域の集合**を対象に取り、`mode="join"` で足し、`mode="cut"` で引く。
本人が日常的に使う「円を線で分けて片側だけ押し出す」がこのverbの存在理由。

**M2のスタブ段階(2026-08-11)**: 契約だけ置いて実体は `NotImplementedError`。

契約:
    extrude(doc, regions, distance, mode="join", body_id=None) -> Operation
        `regions` は `duocad.region.region_ref()` が返す `RegionRef` のリスト。
        `distance` はスケッチ平面の法線方向(正の向き)への押し出し量。0 は ValueError。
        `mode`:
            "join" — 対象ボディへ足す。**対象が無ければ新しいソリッドを1つ作る**
                     (最初の板はこれで生まれる)。作った場合も `args["body_id"]` に
                     そのIDを記録する(再生で同じIDが再現できないと選択が壊れるため)。
            "cut"  — 対象ボディから引く。**対象が無ければ ValueError**
                     (引く相手が無いのに成功したことにしない)。
        `body_id` の既定はアクティブボディ。編集できないボディ(kind="reference")は ValueError。
        ログに `verb="extrude"`,
        `args={"regions": [<RegionRefのdict>, ...], "distance":.., "mode":.., "body_id":..}`,
        `selection=[<RegionRefのdict>, ...]` を積んで `Operation` を返す。

**失敗を黙って通さない**(§6予防6・§7): 領域が空・添字が範囲外・`mode` が未知・距離0・
ブーリアン失敗のいずれでも例外を送出し、**documentにもログにも変更を残さない**。
とくに「切り取ったのに穴が1つも開いていない」を成功として通さないこと。

**OCP を直接importしない**。幾何は `duocad.kernel` 越しに呼ぶ。
"""
from __future__ import annotations

from typing import Optional, Sequence

from ..document import BodyId, Document
from ..oplog import Operation, register_verb
from ..region import RegionRef

VERB_EXTRUDE = "extrude"

#: 押し出しの向き(実装計画書§1「mode(join | cut)」)
MODES = ("join", "cut")


def extrude(
    doc: Document,
    regions: Sequence[RegionRef],
    distance: float,
    mode: str = "join",
    body_id: Optional[BodyId] = None,
) -> Operation:
    """領域の集合を押し出し、ログに積んで Operation を返す。"""
    raise NotImplementedError("M2で実装する: extrude")


# --------------------------------------------------------------------------------------
# verbの登録(oplog は verb名の分岐を持たない)
# --------------------------------------------------------------------------------------
def _do_extrude(document: Document, operation: Operation) -> None:
    """redo / replay 時の再適用。**ログには触らない**(触ると二重記録になる)。"""
    raise NotImplementedError("M2で実装する: extrude の redo/replay")


def _undo_extrude(document: Document, operation: Operation) -> None:
    """undo: 押し出す前の形状に戻す(新しく作ったボディなら取り除く)。"""
    raise NotImplementedError("M2で実装する: extrude の undo")


register_verb(VERB_EXTRUDE, do=_do_extrude, undo=_undo_extrude)
