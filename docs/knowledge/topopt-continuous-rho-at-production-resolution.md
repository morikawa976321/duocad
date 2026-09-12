---
name: topopt-continuous-rho-at-production-resolution
description: 判断(2026-09-09) — 案5の粗い格子の解像度を上げる案は採らず、連続値ρを保存して使い回す方式を実装する。解像度は本番と同じとする
created: 2026-09-09
updated: 2026-09-09
---

duocad の形状最適化で、設計空間(塊)の導出失敗をなくすための方式選択。

**採らなかった**: `design_space.COARSE_ELEMENTS_LONG_AXIS` を 14 から上げる(20 が
hanger で 22/22 成功する最小値だと実測済み。粗い段を細かくしても塊の質は変わらないことは
[[case5-dilate-12mm-does-not-scale-k-criterion]])。

**採る**: 粗い段の ρ を二値化せず**連続値のまま保存して使い回す**。二分探索の材料量 M ごとに
ρ の大きい順から k*M ぶん取って設計空間にする。**粗い段の解像度は本番と同じにする**
(=「粗い段」ではなくなり、本番解像度で1回だけ回して以後は使い回す形になる)。

M0(保存する ρ を作るときの材料量)と k(設計空間の体積 ÷ 材料量)の決め方は未決
(2026-09-09時点。以下の実測は出そろっているが、本人の決定は記録されていない):

- M0 は対象 M 以下でなければ被覆が壊れる(太い解の ρ の飽和) — [[rho-rank-selection-m0-must-not-exceed-target]]
- 順位の場はひずみエネルギーでは原理的に失敗。距離を主キー・ρ を副キーにすると細い M0 の弱点が消える — [[rho-rank-distance-tiebreak-beats-strain-energy]]
- 参照 run は10反復で40反復と同じ被覆。k=2.0 で壁接触率が案1の値へ戻る — [[reference-run-needs-only-10-iterations]]
- M0 を不要にする「詰まった箱の1回線形解析」は成立しない — [[solid-block-stress-cannot-rank-design-space]]
