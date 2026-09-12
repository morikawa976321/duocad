---
name: mma-volume-target-is-an-upper-bound-not-an-equality
description: 実測(2026-09-10) — SIMPの更新則をOCからMMAへ替えると体積目標が等式から上限に変わり、届かない目標でも例外が上がらず「いちばん軽い配置」で走り切る。OCの VolumeTargetError(above_capacity) が出なくなるので、それを too_much の分類に使っている安全率探索(safety_loop._evaluate)が黙って別の道を通る。超過側は原理的に起きない
created: 2026-09-10
updated: 2026-09-11
---

# MMAでは体積目標が上限になる(2026-09-10実測)

duocadの `simp.optimize(update="mma")` を実装してOCと並べた実測(duocad `d4ebedf`)。

**OCは体積目標を等式として解き、MMAは上限として解く。** 同じ入力で出方が変わる:

| | OC | MMA |
|---|---|---|
| 目標に届く場合 | 相対1e-11で目標に乗る | 目標**以下**に着く(相対 -4.3e-8〜-4.7e-5) |
| 目標に届かない場合 | `VolumeTargetError(side="above_capacity")` | **例外なし。**いちばん軽い配置で走り切る |

届かない題材(塊の容量2160mm^3の98%=2116.8mm^3を要求・40反復)で、OCは不足24.87mm^3を名指しして落ち、**MMAは落ちずに2091.93mm^3(目標の98.83%)を返した。**

**超過側は原理的に起きない。** 体積は設計変数の線形関数で、MMAの分離可能な凸近似はその接線より上に来る。近似した制約を0にすれば真の体積は必ず目標以下になる。

**効くのは最適化器の外側。** duocadの安全率探索(`safety_loop._evaluate`)は `VolumeTargetError` の `side` で `too_much` / `too_little` を分類しているので、**MMAを既定にすると `too_much` の作られ方だけが消える**(`too_little` は体積目標の口 `_resolve_volume_target` が更新則より手前で見ているので残る)。

関連: [[mma-matches-oc-at-equal-or-lower-cost]] / [[safety-factor-goes-outside-the-optimizer]]
