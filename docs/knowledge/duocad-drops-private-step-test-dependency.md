---
name: duocad-drops-private-step-test-dependency
description: 判断(2026-08-22) — duocadのテストがリポジトリ外の私物STEP(mf\3d-models)へ依存している件は、依存を直すのではなくテストごと削除してよい。実測で622件中151件(24%)が該当
created: 2026-08-22
updated: 2026-08-22
---

# 私物STEP依存のテストは削除してよい

**選択**: `tests/models.py` 経由で `C:\Users\tm976\mf\3d-models` の私物STEPを読むテストは、
合成形状への差し替えではなく**削除**する。

**実測（2026-08-22、MODELSのパスを存在しない場所へ向けてスイート全体を走らせた結果）**:
**622件中151件(24%)** が私物STEPに依存している。内訳は、無いと落ちるもの91件
(test_import_split 23 / test_extrude_face 20 / test_incremental_update 19 / test_visibility 14 /
test_io_step 8 / test_oplog 4 / test_m3_regressions 2 / test_m2_regressions 1)、
無いとskipするもの60件(test_mcp 27 / test_sketch_ui 13 / test_preview 11 / test_mcp_verbs 5 /
fillet・chamfer・arc 各1、llama canary 1)。

**測り方が使える**: 依存範囲を数えるのに読解は要らない。`tests/models.py` の `MODELS` を
存在しないパスへ向けて1回走らせれば、failed+error+skippedがそのまま依存件数になる。

**失うもの**: 実物モデル(17ソリッドの棚取付具・2835面のPCケース)を使う検査が全部消える。
[[full-cylinder-is-not-always-a-hole]] や [[occt-unify-same-domain-corrupts-input]] のような
OCCTの癖は合成の直方体では踏まないので、この層の検出力は代替が無い。

[[duocad-tests-halved-for-dev-speed]] の一環。
