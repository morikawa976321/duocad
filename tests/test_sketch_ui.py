"""サーバ側の契約: ブラウザから**手でスケッチを描く**ための口と、その配信。

UI(スケッチモード・図形・領域の表示)そのものは **`pytest` では完了判定できない**
(実機確認は `web/scripts/smoke.mjs`)。ここが固定するのはサーバ側の2つ ——
スケッチのverbをWebSocketにも開けていること(**`sketch_create` だけ表に載らず個別に捌かれる**
—— `base_plane` が省略できる引数だから)と、スケッチを描ける形で配信すること
(`state.sketches`: 平面の3D枠・図形・領域の三角形)。

固定する契約:

- 基準平面3面(`base_plane`)と**確定選択の面**(引数を省いたとき)の両方にスケッチを
  作れる。平面でない面・2枚選択・提案のままは黙って通さない
- 4種の図形(線分・円・長方形・円弧)をWebSocketから足せ、`state.sketches` に出る
- 図形は**パラメータ名を持って届く**(クリックで置いた後に値を打ち直せる)
- 円を線分で分けると**領域が2つ**になり、各領域は**画面に描ける三角形**を持つ
- 領域は共有選択集合に入れられる
- undoは図形を1つずつ戻す
- 引数が欠けたコマンドは**黙って既定値で埋めない**
"""
from __future__ import annotations

import json

from duocad.server import SKETCH_COMMANDS

DEVIATION = 0.2
RADIUS = 20.0
class FakeConnection:
    """`Server` が使うのは `send` だけ(配信の出口)。"""

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
    """表から呼べるのは登録した7つだけ(文字列で任意のメソッドを呼ばせない)。

    **表に居ないスケッチのverbが2つある**(どちらも宣言側は `ws="special"`)——
    `sketch_create` は `base_plane` が、`move_sketch_entities` は回転の中心
    (`center` / `center_entity`)が**省略できる引数**で、表は「載せた引数は全部必須」
    だからである。`import_svg` は `sketch_id` を、`mirror_sketch_entities` は `axis` を
    必須で取る形なので表に載る。
    """
    assert set(SKETCH_COMMANDS) == {
        "add_line",
        "add_circle",
        "add_rectangle",
        "add_arc",
        "set_parameter",
        "import_svg",
        "mirror_sketch_entities",
    }


