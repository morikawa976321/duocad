---
name: safety-search-dies-on-residual-failure-at-low-material
description: 実測(2026-09-09) — 安全率探索は材料量が小さい側で SolveResidualError を分類せず探索ごと落ちていた(hanger 2.4mm・10回目の評価で5.7時間ぶんを捨てた)。材料が少ないほどρが下限に張り付いて残った系が悪条件になるので too_little に分類するのが正しい(duocad 4f81f6b)。低密度の悪条件は要素数が多いほど出るので、小さい題材の受入検査では再現しない
created: 2026-09-09
updated: 2026-09-11
---

# 安全率探索は低材料側の残差失敗で落ちていた(2026-09-09実測)

duocadのhangerを2.4mm(案1の箱 1,096,894要素)で端から端まで回したところ、**10回目の評価で `simp.SolveResidualError` が上まで抜け、5.7時間ぶんの評価を捨てて落ちた。**

```
材料 8.704e4mm^3 → 安全率 3.183      (通る)
材料 5.423e4mm^3 → SolveResidualError
    自由度 3,623,730 のうち残ったのは 43,161 / 残差 2.93e-5 > 1e-6 / max|u| 8.24e7
```

機序: **材料量が小さいほどρが下限に張り付き、`_floating_dofs` がほとんどの節点を拘束側へ回す。** 残った系は小さいが悪条件で、CG/多重格子が `SOLVE_RESIDUAL_TOL`(1e-6)に届かない。

`safety_loop._evaluate` は `VolumeTargetError` と設計空間の例外しか分類しておらず、残差検算の失敗は素通しだった(`SolveResidualError` を型として分けた2026-09-09より前は素の `ValueError`)。**`too_little` に分類するのが正しい** — 材料が足りない側の現れなので、`lo` を立てて探索を続ければよい(duocad `4f81f6b`)。

**拘束不足の握りつぶしにはならない**: 本当に拘束が足りなければ上端の評価が同じ例外で落ち、`SafetyTargetError(side="invalid_bound")` として原因を連鎖したまま上がる。

## 受入検査では見つからなかった

`check_safety_loop.py` はbracket 20要素(案1 3,336要素)で探索を最後まで回すが、**低密度の悪条件は要素数が多いほど出る**ので再現しない。同じ理由で、参照runが14要素の格子で残差検算を通らなかった件も本番では起きない([[reference-run-needs-only-10-iterations]]の検査設定の議論)。**本番解像度で1回通すまで見つからない種類の失敗がある。**

関連: [[hanger-2400um-measured-cost-and-safety-curve]] / [[safety-factor-goes-outside-the-optimizer]]
