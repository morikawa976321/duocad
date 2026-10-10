"""図形種別(kind)の3つの表が、キー集合として一致することを機械で検査する。"""
from __future__ import annotations

import re

from duocad import kernel_sketch, sketch

from duocad.paths import REPO_ROOT
ENTITY_KINDS_TS = REPO_ROOT / "web" / "src" / "entityKinds.ts"


def _ts_entity_kind_table_keys() -> set[str]:
    """`web/src/entityKinds.ts` の `ENTITY_KIND_TABLE` の**トップレベルキー**をソースから拾う。"""
    return set(re.findall(r"^  (\w+): \{$", _entity_kind_table_block(), re.M))


def _entity_kind_table_block() -> str:
    """`const ENTITY_KIND_TABLE` の宣言から `const VERB_TO_KIND` の手前までの区間。"""
    text = ENTITY_KINDS_TS.read_text(encoding="utf-8")
    return text[text.index("const ENTITY_KIND_TABLE"):text.index("const VERB_TO_KIND")]


def test_ts_entity_kind_table_is_readable():
    """先に**走査そのものが効いている**ことを見る(0件なら下の検査は常に緑になる)。"""
    keys = _ts_entity_kind_table_keys()
    assert len(keys) >= 4, (
        f"web/src/entityKinds.ts の ENTITY_KIND_TABLE を読めていない(0件かもしれない): {sorted(keys)}"
    )


def test_python_kernel_entity_kind_table_is_readable():
    """先に**走査そのものが効いている**ことを見る(TS側と対にする)。"""
    keys = set(kernel_sketch._KERNEL_ENTITY_KIND_TABLE.keys())
    assert len(keys) >= 4, (
        f"duocad/kernel_sketch.py の _KERNEL_ENTITY_KIND_TABLE を読めていない: {sorted(keys)}"
    )


def test_the_three_entity_kind_tables_agree_on_their_key_set():
    """3つの表のキー集合が一致する。**新種別を1つの表にだけ足すとここが落ちる。**"""
    canonical = set(sketch.ENTITY_KINDS)
    assert canonical, "sketch.ENTITY_KINDS が空(表そのものが読めていない)"

    kernel_keys = set(kernel_sketch._KERNEL_ENTITY_KIND_TABLE.keys())
    assert kernel_keys == canonical, (
        "duocad/kernel_sketch.py の _KERNEL_ENTITY_KIND_TABLE が sketch.ENTITY_KINDS と食い違う"
        f"(kernelのみ={sorted(kernel_keys - canonical)} / "
        f"sketchのみ={sorted(canonical - kernel_keys)})。"
        "新しい図形種別は kernel_sketch.py にも1行(辺生成・2D bbox)足すこと。"
    )

    ts_keys = _ts_entity_kind_table_keys()
    assert ts_keys == canonical, (
        "web/src/entityKinds.ts の ENTITY_KIND_TABLE が sketch.ENTITY_KINDS と食い違う"
        f"(TSのみ={sorted(ts_keys - canonical)} / "
        f"sketchのみ={sorted(canonical - ts_keys)})。"
        "新しい図形種別は entityKinds.ts にも1行(fromArgs・toPoints)足すこと。"
    )


def _ts_point_keys() -> dict:
    """`web/src/entityKinds.ts` の各種別の `pointKeys` をソースから拾う。

    `<name>: {` の直後の行が `pointKeys: [...]` である前提で読む(表の書き方の規約)。
    読めなかった種別は入らないので、下の検査が「片方にしか無い」で落ちる。
    """
    block = _entity_kind_table_block()
    found: dict = {}
    pattern = r"^  (\w+): \{\n    pointKeys: (\[.*\]),$"
    for kind, body in re.findall(pattern, block, re.M):
        pairs = re.findall(r"\['(\w+)', '(\w+)'\]", body)
        found[kind] = tuple(pairs)
    return found


def test_the_movable_point_tables_agree():
    """**動かせる点の定義がPythonとTSで一致する。**

    参照の `point` は「xキー」そのもの(`duocad/selection.py` の `SKETCH_ENTITY_KIND`)なので、
    片方だけ直すと**画面が送った点の名前をコアが知らない**という形で落ちる ——
    落ちるだけましだが、順序だけがずれた場合は「別の点が動く」という静かな事故になる。
    """
    ts_table = _ts_point_keys()
    assert ts_table, "web/src/entityKinds.ts の pointKeys を読めていない"
    python_table = {kind: tuple(sketch.entity_point_keys(kind)) for kind in sketch.ENTITY_KINDS}
    assert ts_table == python_table, (
        "動かせる点の表が食い違う"
        f"(TS={ts_table} / Python={python_table})。"
        "duocad/sketch.py の EntityKindOps.points と web/src/entityKinds.ts の pointKeys は"
        "**順序も含めて**同じでなければならない"
    )
