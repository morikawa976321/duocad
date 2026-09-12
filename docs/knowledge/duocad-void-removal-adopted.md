---
name: duocad-void-removal-adopted
description: 判断(2026-09-09) — duocadの形状最適化で、剛性行列の組み立てから void 要素を外す(比較の条件2)を main へ入れると決めた
created: 2026-09-09
updated: 2026-09-09
---

**「void除去は確定でいれるべき」**(2026-09-09)。4条件の比較
(`mf/reports/topopt-assembly-strategies-comparison.md`)を受けての決定。

## 決めたこと

- **条件2(void除去)を main へ入れる。** `void_mask` のパッシブ要素を組み立てから外し、
  `_iK`/`_jK` も active 要素ぶんしか確保しない。
- **切り替えフラグは残さない**(比較用の `ASSEMBLY_STRATEGY` は持ち込まない)。
- **条件3(パターン固定)と条件4(matrix-free)は入れない。** 実装はブランチ
  `topopt-assembly-comparison` にだけ残す。

## 採らなかった選択肢

比較の途中で AI が「条件3が条件2を支配するので条件2を落としてよい」と2度提案したが、
**どちらも成立しなかった**。条件2と条件3は支配関係に無く、**メモリなら条件2・大きい問題の
速度なら条件3**である(組立時間が約6.5秒＝全体の10%強を超えると条件3が勝つ)。
本人は最も安く・最も確実に効く条件2だけを採った。

## 実行(2026-09-09・taskbox `20260909-0518`)

main へ入れた後の実測は [[void-removal-measured-on-main]](全体 75.9% / ピークRSS 49.7%、
ブランチ実装と ρ がビット一致)。同時に見つかった目的値の総和範囲のずれと直し方は
[[objective-must-belong-to-the-system-that-was-solved]]、除去後のピークメモリが箱ではなく
塊に比例する件は [[topopt-peak-memory-scales-with-active-elements-not-boxes]]。
hanger の形が初めて出たのも同じタスク([[hanger-first-shapes-produced]])。

関連: [[hanger-working-resolution-2400um]] /
[[assembly-peak-memory-ratio-does-not-carry-to-run-peak]] /
[[void-removal-changes-cg-iteration-count]] /
[[matrix-free-matvec-needs-element-blocks-and-dof-bands]](入れなかった条件4の実測)
