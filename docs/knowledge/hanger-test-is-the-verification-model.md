---
name: hanger-test-is-the-verification-model
description: 本人決定(2026-09-10) — 形状最適化の検証には hanger_bar_attachment ではなく hanger_test(同じ部品を密集させて外接直方体を3.2倍小さくしたもの)を要素サイズ2.4mmで使う。理由は本人明言「検証では何度も回すので速い方」。2.4mmで322,384要素・45.5秒/反復・8.3GB(3.4倍小さく3.5倍速く3.1倍軽い)。ただし2026-09-11実測で、hanger_bar_attachmentで見た「取付点が消える」病理はhanger_testでは本番解像度でも再現しない — 費用の測定には使えるが病理の再現には使えない
created: 2026-09-10
updated: 2026-09-12
---

# 検証用の実物モデルは `hanger_test`、要素サイズ2.4mm(2026-09-10本人決定)

**`models/hanger_test.json` を検証用の実物モデルとする。** 本人の言:「同じ部品を使っているがより密集させることで速度を高めている。検証では何度も回すのでこちらを」。

条件の構成は `hanger_bar_attachment.json` と同一(拘束面3・荷重面1・保持ソリッド4・除外ソリッド7)で、保持の体積もほぼ同じ(17,603 vs 18,558mm^3)。部品そのものは同じで外接直方体だけが小さい(容量 4,655,592 vs 15,249,793mm^3、**3.2倍小さい**)。

## 要素サイズは2.4mm(本人決定)

hangerの作業解像度が2.4mmである以上([[hanger-working-resolution-2400um]])、採否の判断は本番の解像度で出した数字に基づくべきで、`hanger_test` ならそれでも十分に安い。実測(機械が空いている状態):

| | 箱内要素 | spacing | 1反復 | 峰メモリ | 40反復1回 |
|---|---|---|---|---|---|
| `hanger_test` 2.4mm | **322,384** | 2.4120mm | **45.5秒** | **8.3GB** | 約31分 |
| `hanger_bar_attachment` 2.4mm | 1,096,894 | 2.4076mm | 159.8秒 | 26.1GB | 約1.8時間 |

**3.4倍小さく、3.5倍速く、3.1倍軽い。** 安全率探索を端から端まで通しても3〜5時間(`hanger_bar_attachment` は8時間級)。

製品コードと受入検査に残っている `hanger_bar_attachment` の記述は過去の実測の出典なのでそのまま(`kernel.py` / `voxelize.py` / `optimize_run.py` / `check_design_space.py` のコメント)。差し替えるのは**これから回す検証の題材**である。

## 限界: 病理の再現には使えない(2026-09-11実測)

2026-09-10に `hanger_bar_attachment` を2.4mmで完走させたとき、保持ソリッド `_1`/`_2`(5mm厚の円盤)が浮き島になって `mesh.remove_islands` に消えた。これを直すために反力の下限制約を入れた([[reaction-constraint-forces-a-load-path-at-any-epsilon]])。ところが**`hanger_test` ではその病理が1度も再現しない**(反力の内訳を測れるようにして判明):

| 題材 | 要素サイズ | 材料 | 面11 | 面3 | 面19 | 保持ソリッドの生存 |
|---|---|---|---|---|---|---|
| hanger_test | 5.319mm | 10% | 0.993 | 0.993 | 1.895 | 4/4 全要素 |
| hanger_test | 5.319mm | 30% | 0.973 | 0.973 | 1.903 | 4/4 全要素 |
| hanger_test | 3.989mm | 30% | 0.984 | 0.984 | 1.927 | 4/4 全要素 |
| hanger_test | **2.412mm(本番)** | 30% | **0.981** | **0.981** | **1.917** | **4/4 全要素** |
| hanger_bar_attachment | 5.281mm | 30% | 0.842 | 0.842 | 1.668 | 9/9 全要素 |

数字は荷重の合力を1としたときの各拘束面の受け持ち(反力の大きさ。合計が1にならない理由は[[reaction-magnitudes-do-not-sum-to-the-applied-load]])。40反復・`update="oc"`(2.412mmの行は `update="mma"` でも 0.979/0.979/1.917 で同じ)。生データは duocad `duocad/topopt/output/reaction_constraint.json`。

**部品の配置を変えれば取付点と荷重の距離関係が変わり、荷重経路の選び方も変わる。** 速い代替題材は費用の測定には使えても、病理の再現には使えないことがある。

2026-09-11の追加実測で、病理の引き金は「外接直方体に空きが多いこと」で、箱を縮めた `hanger_test` は構造上これを出せないと確定した。**塊の導出に関する回帰の判定は `hanger_bar_attachment` でしか止められない** → [[hanger-test-cannot-reproduce-the-stranded-fixture-pathology]]。比較の計測(`p` を振る等)には引き続き `hanger_test` を使う。
