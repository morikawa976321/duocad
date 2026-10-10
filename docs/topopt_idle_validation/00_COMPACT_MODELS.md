# 小型モデル2つ(不要材料検証の題材)

「仕事をしていない材料」の2原因を hanger より桁違いに速く再現するための小型モデルの定義・認定条件・採用パラメータ。
生成は `topopt_checks/idle_validation/build_compact_models.py`(決定的)。モデルJSON・出力JSONは `models/`・`output/` で git 管理外(再生成で作る)。
モデルは `Session.save_document`/`open_document` の往復を検査済み。検証全体の結論は [10_SYNTHESIS.md](10_SYNTHESIS.md)。

| モデル | 対象の原因 | 認定 |
|---|---|---|
| `models/topopt_idle_dilution_small.json` | 原因1(母数の希釈) | 認定(実FEMで母数反実仮想と熱いセルの実測が揃う) |
| `models/topopt_idle_path_small.json` | 原因2(経路喪失・局所解) | 格子依存の局所解は再現。同格子での優越形は手作業の構成(04a)でパーセンタイル評価に限り認定(10_SYNTHESIS 付録C)。自動方式としては未確立 |

## dilution_small(原因1)

- 形状: 24×24×12mm の台座に半径4mmの貫通穴(`mode="cut"`)。穴の内周円筒面(`query.describe_faces` の `kind == "cylinder_full"`、全周1枚)を拘束。荷重パッドは 10×10×12mm、台座右端から36mm離れた位置の上面に -Z 荷重。
- 採用パラメータ: **300N**、要素2.0mm、最小幅2セル(4mm)。設計要素 19,672(格子 51×28×22)。
- 荷重の選定(全材料の安全率。SF×荷重 = 1676.2988934 で一定): 150N 11.18 / 200N 8.38 / 250N 6.71 / **300N 5.59** / 350N 4.79(全材料でも目標4.8未達=`full_material_infeasible`)。300Nは全材料で余裕があり希釈の効果が明確に測れる。
- 製品既定1回: 18評価・58〜60秒、tracemalloc ピーク 386 MB、材料 88,368 mm³、SF 4.836(`done`、`converged`)。
- 母数反実仮想(保持セルから半径Rセル以内を代表応力の母集団から除いて探索し、見つけた候補を製品どおりの代表応力で再評価)。`measure_dilution_counterfactual.py --radius R`(R=-1 が製品どおりの基準)。除いた探索は毎回さらに削る(-50〜-77%)が、再評価はすべて目標4.8未達:

| R | 材料 mm³ | 再評価SF |
|---:|---:|---:|
| -1(基準) | 88,368 | 4.836 pass |
| 0 | 44,600 (-50%) | 4.560 fail |
| 1 | 41,512 (-53%) | 4.537 fail |
| 2 | 32,208 (-64%) | 4.412 fail |
| 3 | 20,256 (-77%) | 3.904 fail |

- 機構(`measure_dilution_hot_cells.py`): 目標応力(許容40 MPa ÷ 4.8 = 8.33 MPa)を超える熱いセルの数は、材料が19,672から2,532セルへ87%減ってもリング近傍で 40〜54 とほぼ一定。0.5%×N は 98.4 から 12.7 へ材料に比例して縮む。基準(N=11,046)では 0.5%×N=55.2 > リング近傍の熱いセル44 なので99.5%点が熱いセルの外側に落ちて合格し、R=0 以降はこの逆転が起きて不合格になる。遠方の材料は「重り」として99.5%点を薄めている。

## path_small(原因2)

- 形状: 支持パッド 10×20×10mm を2つ50mm離して(x=0..10 と 50..60)置き外向き端面を拘束。荷重パッド 10×20×10mm を x=42..52(右支持との隙間8mm)・z=30..40 に浮かせ、底面に下向き荷重。右寄りの偏心で「右支持だけの腕(曲げ)」と「両支持を使う斜材(三角形)」が同じ最小幅で成立する。
- 採用パラメータ: **3000N**、最小幅 **6.0mm 固定**(`min_width_mm=6.0`)。セル数指定だと格子ごとに実mmが変わり(3.0mm格子で6mm、2.5mmで5mm…)、解像度をまたぐ比較の「同条件」が崩れる。6.0mm は 3.0mm 要素の下限(`max(2×要素サイズ, 1mm)`)と一致する最小の整数mm値(4.0・5.0mm は 3.0mm 要素で下限未満エラー)。
- **罠**: 低レベル呼び出し(`_build_search_conditions`+`find_best_feasible`)を直接使う検査スクリプトは `min_width_cells` を明示しないと既定 1(制約なし)で走る(製品入口 `optimize_run.run` は自動で渡す)。
- 解像度スイープ(製品既定1回、`run_compact_search.py --model topopt_idle_path_small.json --size <size> --min-width-mm 6.0`):

| 要素(要求) | 設計要素数 | 最小幅セル(保証mm) | 評価 | 時間 | 材料 mm³ | SF |
|---:|---:|---:|---:|---:|---:|---:|
| 3.0mm | 6,461 | 2 (6.0) | 24 | 34.8秒 | 85,941 | 4.839 |
| 2.5mm | 11,372 | 3 (7.41) | 19 | 39.7秒 | 57,425 | 4.836 |
| 2.0mm | 21,152 | 3 (6.0) | 27 | 72.8秒 | 71,928 | 4.842 |
| 1.5mm | 50,758 | 4 (6.0) | 18 | 153.2秒 | 83,130 | 4.844 |

  2.5mm だけが顕著に軽い(実現最小幅は7.41mmとやや厳しいのに軽いので、最小幅の緩さでは説明できない)。2.5mm の採用マスクは遠い支持への接続が細い単材の斜材。材料は解像度に対して単調でない。
- 歩幅だけでは抜けない: 2.0mm 格子で `max_step_ratio` 0.05 は 72,016 mm³(上限0.2の71,928とほぼ同じ)。
- 移植: 2.5mm の採用マスク(57,425 mm³)を 2.0mm へ写すと 56,832 mm³・SF 4.400、隙間を閉じても 57,656 mm³・SF 4.481 でいずれも目標4.8未達(`acceptance.inspect` の6条件は最小幅を含まないので `binary_search._mask_satisfies_min_width` を別途呼ぶ。最小幅は pass)。

## 不採用にしたもの

- 第1候補 `tests/session_support.py` の `two_pads`/`four_conditions`: 拘束が平面パッドで保持リング特有の応力集中を持たず、原因1の証拠にならない。拘束・荷重が同一直線上の片持ちで斜材の要否も分かれない。
- path の対称配置(荷重パッドを支持中央 x=25): 全解像度で同じ対称アーチ形に収束し局所解の分岐が現れなかった。
- 荷重の探索範囲: dilution 150N は削減余地が大きいが希釈の絶対量が小さい。path 1500N(全材料SF 13.2)〜8000N 以上(`full_material_infeasible`)。

## 代替できないもの

hanger の複数保持リング(取付穴3つ)の相互作用、STEP由来の複雑形状の三角形分割誤差、hanger サイズのメモリ上限挙動は小型では代替できない。

## 再現コマンド

```
.\.venv\Scripts\python.exe topopt_checks\idle_validation\build_compact_models.py            # 生成(--check で要素数のみ)
.\.venv\Scripts\python.exe topopt_checks\idle_validation\run_compact_search.py --model topopt_idle_dilution_small.json --size 2.0 --cells 2 --out output\idle_validation_20260928\dilution_2.0mm_m2.json
.\.venv\Scripts\python.exe topopt_checks\idle_validation\measure_dilution_counterfactual.py --radius 2
.\.venv\Scripts\python.exe topopt_checks\idle_validation\measure_dilution_hot_cells.py
.\.venv\Scripts\python.exe topopt_checks\idle_validation\run_compact_search.py --model topopt_idle_path_small.json --size 2.5 --min-width-mm 6.0 --out output\idle_validation_20260928\path_2.5mm_m2.json
.\.venv\Scripts\python.exe topopt_checks\idle_validation\measure_path_local_optimum.py mesh --sizes 3.0 2.5 2.0 1.5
.\.venv\Scripts\python.exe topopt_checks\idle_validation\measure_path_local_optimum.py step --size 2.0 --max-step-ratio 0.05
.\.venv\Scripts\python.exe topopt_checks\idle_validation\measure_path_local_optimum.py transplant --src-tag mesh_2.5mm --dst-size 2.0 --closing-iterations 0
```
