---
name: anchor-centroid-gate-false-positive-on-curved-condition-faces
description: 実測(2026-09-09) — optimize_run._check_anchor_centroids は曲面(トーラス・円筒)の条件面で必ず誤検出する。許容が要素サイズ比例なので、細かくするほど厳しくなり、hanger は 4.27mm より細かいと製品経路を通らない
created: 2026-09-09
updated: 2026-09-09
---

`duocad/optimize_run.py` の `_check_anchor_centroids` は「条件面の節点集合が本当にその面の
ところにあるか」を測る2枚目の網で、**面アンカーの節点重心と `set_loads`/`set_fixtures` が
焼き込んだ面の重心の距離**を `ANCHOR_CENTROID_LIMIT_ELEMENTS(=3.0) × 要素サイズ` と比べる。

## 何が起きるか

**曲面の条件面では、この距離が解像度に依らないほぼ一定の値になる。** 面に焼き込まれる
`center` は OCCT が返す面の「中心」であって、面パッチの重心ではないためである。
一方で許容は要素サイズに比例するので、**解像度を上げるほど許容だけが縮み、いつか必ず
`FaceMappingError` になる**。離散化の誤差を測る門ではないのに、離散化の細かさで
閾値を決めているのが原因。

`models/hanger_bar_attachment.json` の実測(2026-09-09):

- 荷重面は **`kind="torus"`**(面積 2783.24mm^2)。焼き込まれた `center` は
  `[0, -212.862, 60.0]`、面アンカーの節点重心は **`[0, -200.055, 60.007]`**。
- 距離は **要素サイズ 2.4mm で 12.807mm / 2.8mm で 12.875 / 3.2mm で 12.954 /
  3.6mm で 13.034 / 4.0mm で 12.600 / 4.3mm で 12.951** ——**ほぼ動かない**。
- 許容は `3 × 実spacing` なので 2.4mm では 7.223mm。**分かれ目は約 4.27mm** で、
  それより細かいと落ちる。
- 拘束面のほうは `kind="plane"`(面積 175.93mm^2)なので問題にならない。

**SIMP を1回も呼ぶ前に落ちる**(実行2秒。`Model.assemble` の呼び出し回数0)。

## 対処した(2026-09-09・duocad `d3b1229`)

**閾値は緩めていない。比べる相手を面の面積重心(`CenterOf.MASS`)に変えた。**
焼き込んだ `center` は読まなくなり、門は実行時に測り直す。

| | 拘束面(平面) | 荷重面(トーラス) |
|---|---|---|
| 4.4mm(許容 13.274mm) | 1.480mm | 0.365mm |
| 2.4mm(許容 7.223mm) | 0.173mm | **0.055mm** |

**細かくするほど距離が縮む** —— 離散化の誤差を測る門として正しい挙動になった。
「別のボディの面を掴んだ」級(50mm以上)の検出力はそのまま。焼き込んだ `center` は
指紋の照合に使われていない(`optimize.fingerprint_mismatch` は面積と法線だけを見る)ので、
**保存済みの文書はそのまま通る**。

関連: [[hanger-product-path-passes-only-at-4400um-and-4800um]](修正前の要素サイズ別の可否表と、もう1つの門) /
[[build123d-face-center-is-not-the-area-centroid]](`center()` の既定が面積重心でない件)
