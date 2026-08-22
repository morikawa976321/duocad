"""C契約(サーバ側): ブラウザから**手でスケッチを描く**ための口と、その配信。

UI(スケッチモード・図形・領域の表示)そのものは**`pytest` では完了判定できない**
(実機確認は `web/scripts/smoke.mjs`)。このファイルが固定するのはサーバ側の2つ——

1. **スケッチのverbをWebSocketにも開ける**(`sketch_create` / `add_line` / `add_circle` /
   `add_rectangle` / `add_arc` ＋ 値の打ち直しの `set_parameter`)。**`sketch_create` だけ
   表に載らず個別に捌かれる** —— `base_plane` が省略できる引数だからである
2. **スケッチを描ける形で配信する**(`state.sketches`: 平面の3D枠・図形・領域の三角形)

——は機械で判定できる。ここが緑でないとUIの検証は意味を持たない(画面に線が出ることの
実体は、平面の枠と図形がコアから届いていることだから)。

**中心命題との関係**: スケッチのverbはコアとMCPにあり、AIは描ける。**人が描くための
口**が `SKETCH_COMMANDS` であり、これが無いと「手動操作が一級の操作モード」
(実装計画書§8b C)がスケッチで成立しない。**コアには何も足していない**
(選択の3コマンドをブラウザに開けているのと同じ形)。

固定する契約:

- 基準平面3面(`base_plane`)と**確定選択の面**(引数を省いたとき)の両方にスケッチを
  作れる。平面でない面・2枚選択・提案のままは黙って通さない
- 4種の図形(線分・円・長方形・円弧)をWebSocketから足せ、`state.sketches` に出る
- 図形は**パラメータ名を持って届く**(クリックで置いた後に値を打ち直せる=§3.4)
- 円を線分で分けると**領域が2つ**になり、各領域は**画面に描ける三角形**を持つ
- 領域は共有選択集合に入れられる(Dの押し出しがこの選択を引数に取る)
- undoは図形を1つずつ戻す
- 引数が欠けたコマンドは**黙って既定値で埋めない**
"""
from __future__ import annotations

import asyncio
import json
import math
import pytest

from duocad import query
from duocad.server import SKETCH_COMMANDS, Server, Session

DEVIATION = 0.2
RADIUS = 20.0
class FakeConnection:
    """`Server` が使うのは `send` だけ(配信の出口)。かつて test_preview.py が使っていたのと同じ形。"""

    def __init__(self) -> None:
        self.payloads: list = []

    async def send(self, raw: str) -> None:
        self.payloads.append(json.loads(raw))

    def last(self, kind: str) -> dict:
        for payload in reversed(self.payloads):
            if payload.get("type") == kind:
                return payload
        raise AssertionError(f"{kind!r} の応答が無い: {[p.get('type') for p in self.payloads]}")


def test_only_the_registered_commands_are_reachable() -> None:
    """表から呼べるのは登録した5つだけ(文字列で任意のメソッドを呼ばせない)。

    **`sketch_create` はこの表に居ない**(2026-08-22)—— `base_plane` が省略できる引数で、
    表は「載せた引数は全部必須」だからである。宣言側は `ws="special"` で、実体は
    `Server._dispatch` の個別の分岐(`tests/test_ui_command_wiring.py` が対応を見る)。
    """
    assert set(SKETCH_COMMANDS) == {
        "add_line",
        "add_circle",
        "add_rectangle",
        "add_arc",
        "set_parameter",
    }


