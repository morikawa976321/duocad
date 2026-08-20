"""図形種別(kind)の3つの表が、キー集合として一致することを機械で検査する。

## なぜ要るか

`Entity.kind分岐のテーブル化`(2026-08-20)で、処理ごとのkind分岐(switch)を
図形種別ごとの表へ反転した。表は import境界のため3つに分かれている:

1. `duocad/sketch.py` の `_ENTITY_KIND_TABLE`(純幾何 — 検査・平行移動・回転・中心)。
   `sketch.ENTITY_KINDS` はここのキーから導出される**唯一の正**。
2. `duocad/kernel.py` の `_KERNEL_ENTITY_KIND_TABLE`(OCP依存 — 辺生成・2D bbox)。
3. `web/src/entityKinds.ts` の `ENTITY_KIND_TABLE`(TS — 描画用折れ線・幽霊生成)。

**新しい図形種別を1つ足すとき、3つのうち1つにだけ行を足しても、これまでは
テストでも型でも止まらなかった**(表を分けたこと自体が、同期漏れという新しい
静かなバグの入口を開いていた)。ここはその穴を塞ぐ —— `tests/test_boundaries.py` /
`tests/test_ui_command_wiring.py` と同じ「ソースを静的に読む」やり方で、
サーバもブラウザも要らない。

TS側は `tsc` を通しても素の文字列なので型では守れない
(`tests/test_ui_command_wiring.py` の `CommandDef.id` と同じ事情)。
"""
from __future__ import annotations

import re
from pathlib import Path

from duocad import kernel, sketch

REPO_ROOT = Path(__file__).resolve().parent.parent
ENTITY_KINDS_TS = REPO_ROOT / "web" / "src" / "entityKinds.ts"


def _ts_entity_kind_table_keys() -> set[str]:
    """`web/src/entityKinds.ts` の `ENTITY_KIND_TABLE` の**トップレベルキー**をソースから拾う。

    `const ENTITY_KIND_TABLE` の宣言から `export const ENTITY_KINDS`(その次の文)の
    手前までに区間を絞ってから、2スペース下げの `<name>: {` 行だけを拾う。
    内側のメソッド定義(`fromArgs(args) {` / `toPoints(params) {`)は名前の後ろが
    `(` であって `:` ではないので混ざらない。区間を絞るのは、この正規表現パターン
    (`  word: {`)が偶然どこかに現れても拾わないようにするため。
    """
    text = ENTITY_KINDS_TS.read_text(encoding="utf-8")
    start = text.index("const ENTITY_KIND_TABLE")
    end = text.index("export const ENTITY_KINDS")
    block = text[start:end]
    return set(re.findall(r"^  (\w+): \{$", block, re.M))


def test_ts_entity_kind_table_is_readable():
    """先に**走査そのものが効いている**ことを見る(0件なら下の検査は常に緑になる)。

    拾えなかったときに「一致」へ静かに化けるのが一番危ないので、0件はここで
    名指しで落とす(`tests/test_ui_command_wiring.py` の
    `test_server_command_tables_are_readable` と同じ考え方)。
    """
    keys = _ts_entity_kind_table_keys()
    assert len(keys) >= 4, (
        f"web/src/entityKinds.ts の ENTITY_KIND_TABLE を読めていない(0件かもしれない): {sorted(keys)}"
    )


def test_python_kernel_entity_kind_table_is_readable():
    """先に**走査そのものが効いている**ことを見る(TS側と対にする)。"""
    keys = set(kernel._KERNEL_ENTITY_KIND_TABLE.keys())
    assert len(keys) >= 4, (
        f"duocad/kernel.py の _KERNEL_ENTITY_KIND_TABLE を読めていない: {sorted(keys)}"
    )


def test_the_three_entity_kind_tables_agree_on_their_key_set():
    """3つの表のキー集合が一致する。**新種別を1つの表にだけ足すとここが落ちる。**

    `sketch.ENTITY_KINDS`(`duocad/sketch.py` の `_ENTITY_KIND_TABLE` のキーから
    導出される唯一の正)を基準に、kernel側・TS側の表と突き合わせる。
    """
    canonical = set(sketch.ENTITY_KINDS)
    assert canonical, "sketch.ENTITY_KINDS が空(表そのものが読めていない)"

    kernel_keys = set(kernel._KERNEL_ENTITY_KIND_TABLE.keys())
    assert kernel_keys == canonical, (
        "duocad/kernel.py の _KERNEL_ENTITY_KIND_TABLE が sketch.ENTITY_KINDS と食い違う"
        f"(kernelのみ={sorted(kernel_keys - canonical)} / "
        f"sketchのみ={sorted(canonical - kernel_keys)})。"
        "新しい図形種別は kernel.py にも1行(辺生成・2D bbox)足すこと。"
    )

    ts_keys = _ts_entity_kind_table_keys()
    assert ts_keys == canonical, (
        "web/src/entityKinds.ts の ENTITY_KIND_TABLE が sketch.ENTITY_KINDS と食い違う"
        f"(TSのみ={sorted(ts_keys - canonical)} / "
        f"sketchのみ={sorted(canonical - ts_keys)})。"
        "新しい図形種別は entityKinds.ts にも1行(fromArgs・toPoints)足すこと。"
    )
