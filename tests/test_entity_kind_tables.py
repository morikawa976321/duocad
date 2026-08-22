"""図形種別(kind)の3つの表が、キー集合として一致することを機械で検査する。

表は import境界のため3つに分かれている:

1. `duocad/sketch.py` の `_ENTITY_KIND_TABLE`(純幾何 — 検査・平行移動・回転・中心)。
   `sketch.ENTITY_KINDS` はここのキーから導出される**唯一の正**。
2. `duocad/kernel.py` の `_KERNEL_ENTITY_KIND_TABLE`(OCP依存 — 辺生成・2D bbox)。
3. `web/src/entityKinds.ts` の `ENTITY_KIND_TABLE`(TS — 描画用折れ線・幽霊生成)。

**新しい図形種別を1つ足すとき、3つのうち1つにだけ行を足しても、テストでも型でも止まらない**
—— ここがその穴を塞ぐ。TS側は `tsc` を通しても素の文字列なので型では守れないため、
ソースを静的に読む(サーバもブラウザも要らない)。
"""
from __future__ import annotations

import re
from pathlib import Path

from duocad import kernel, sketch

REPO_ROOT = Path(__file__).resolve().parent.parent
ENTITY_KINDS_TS = REPO_ROOT / "web" / "src" / "entityKinds.ts"


def _ts_entity_kind_table_keys() -> set[str]:
    """`web/src/entityKinds.ts` の `ENTITY_KIND_TABLE` の**トップレベルキー**をソースから拾う。

    `const ENTITY_KIND_TABLE` の宣言から `export const ENTITY_KINDS` の手前までに区間を
    絞ってから、2スペース下げの `<name>: {` 行だけを拾う(内側のメソッド定義は名前の後ろが
    `(` なので混ざらない)。
    """
    text = ENTITY_KINDS_TS.read_text(encoding="utf-8")
    start = text.index("const ENTITY_KIND_TABLE")
    end = text.index("export const ENTITY_KINDS")
    block = text[start:end]
    return set(re.findall(r"^  (\w+): \{$", block, re.M))


def test_ts_entity_kind_table_is_readable():
    """先に**走査そのものが効いている**ことを見る(0件なら下の検査は常に緑になる)。"""
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
    """3つの表のキー集合が一致する。**新種別を1つの表にだけ足すとここが落ちる。**"""
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
