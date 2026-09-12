---
name: pick-ratio-cost-quality-tradeoff-on-hanger-test
description: hanger_testでPICK_RATIO(0.08/0.16/0.60)を振った実測 — 安全率は単調に下がり秒/反復はほぼ線形に増える。3点とも反力ゼロ・保持消失は起きない(hanger_bar_attachmentとは別題材)
created: 2026-09-12
updated: 2026-09-12
---

duocad のトポロジー最適化で `design_space.PICK_RATIO`(塊の大きさを決める摘み比率)を
`hanger_test` 2.412mm・材料70,634.5mm^3・40反復で 0.08/0.16/0.60 の3点振った実測
(`taskbox/workspace/20260911-1811-topopt-design-space-width-and-size/
repro-hanger_test-2.4mm/sweep_pick.json`)。

| p | 塊要素数 | 秒/評価 | 秒/反復 | 安全率 | compliance | 反力ゼロ面 | 死んだ保持 |
|---|---|---|---|---|---|---|---|
| 0.08 | 25,791 | 501.3秒 | 12.53 | 2.605 | 26.46 | なし | なし |
| 0.16 | 51,581 | 593.7秒 | 14.84 | 2.357 | 29.28 | なし | なし |
| 0.60 | 193,430 | 1841.4秒 | 46.03 | 2.156 | 28.74 | なし | なし |

- **安全率は p が大きいほど単調に下がり、秒/反復はほぼ線形に増える。** 塊を大きくしても
  安全側にも速い側にも振れない。
- **`hanger_test` では p=0.60 でも反力ゼロ・保持消失は起きない。** これは
  `hanger_bar_attachment`(箱が3.4倍大きい題材)で p=0.60 が `SolveResidualError` で落ちる
  こと([[initial-volfrac-falls-with-design-space-size]])と矛盾しない —— 別題材・別現象。
  `hanger_test` は構造上この病理を再現できない
  ([[hanger-test-cannot-reproduce-the-stranded-fixture-pathology]])。
- p の既定値決定に使う材料として、[[design-space-width-guarantee-does-not-keep-fixtures-alive]]
  (`hanger_bar_attachment` での受入3点。p=0.08 のみ PASS)と合わせて読む。
