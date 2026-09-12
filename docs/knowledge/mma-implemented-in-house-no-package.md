---
name: mma-implemented-in-house-no-package
description: 本人決定(2026-09-10) — duocadの形状最適化にMMA(Method of Moving Asymptotes)を入れるにあたり、既存パッケージを使わず全て自前実装する(GPLのコードを見て書き写さない・論文から独立に実装)。出回っているPython実装(mmapy・GCMMA-MMA-Python・Svanberg原典)はすべてGPLv3。GPL系譜の外はNLoptのLD_MMA(MIT)だけだが、最適化ループをNLopt側が持つ形になりduocadの simp.optimize の構造と合わない
created: 2026-09-10
updated: 2026-09-11
---

# MMAは自前実装、パッケージは使わない(2026-09-10本人決定)

duocadのSIMPの更新則をOCからMMAへ広げる件(反力制約[[reaction-constraint-forces-a-load-path-at-any-epsilon]]を入れる前提)。**既存パッケージを使わず、全て自前実装する。** GPLのコードを見て書き写さないこと、論文から独立に実装すること、を含む(taskbox `20260910-1312-topopt-mma-alongside-oc`)。実測の結果は[[mma-matches-oc-at-equal-or-lower-cost]]。

## 出回っている実装のライセンス地図(2026-09-10調査)

| 実装 | 形 | ライセンス |
|---|---|---|
| `mmapy`(PyPI・aadityacs) | SvanbergのMATLABを翻訳 | **GPLv3** |
| `GCMMA-MMA-Python`(GitHub・arjendeetman) | 同上。TO界隈の事実上の標準 | **GPLv3**。Svanbergへの一報と引用を求めている |
| 原典 `mmasub.m` / `subsolv.m`(smoptit.se) | MATLAB | **GPLv3** |
| **NLoptの `LD_MMA`** | 汎用NLPソルバの1アルゴリズム | **実装自体はMIT**(ライブラリ全体はLGPL) |

NLoptのMMAはSteven Johnsonが論文から独立に実装したもので、Svanbergのコードを見ていないと明記されている — GPL系譜の外にある唯一の実装。

## 形の違いが効く(ライセンス以前の問題)

- **`mmasub()` は「部分問題ソルバ」**。反復ごとに1回呼ぶと新しい設計変数が返る。既存の更新則をそのまま差し替えられる(呼び出し側がループを持ったまま)。
- **NLoptは「最適化器」**。目的関数を渡すとNLoptが呼び返す。ループをNLoptが持つので、反復ごとの進捗配信・中断・パッシブ要素の上書き・履歴を全部その流儀へ作り直すことになる。

duocadでは `simp.optimize` がループを持ち、外側(`safety_loop` / `optimize_run`)がそこへ進捗と中断を刺しているので、**NLoptは形が合わない**。

著作権が守るのはコードという表現であってアルゴリズムそのものではない。論文から独立に実装すればGPLの制約は及ばない(NLoptが取った道)が、GPLのコードを見て書き写すのは派生物になる。MMAの特許状況は未確認。
