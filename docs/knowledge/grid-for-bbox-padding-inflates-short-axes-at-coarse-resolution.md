---
name: grid-for-bbox-padding-inflates-short-axes-at-coarse-resolution
description: probe_topoptの落とし穴 — voxelize.grid_for_bboxの固定1セルpadding(DEFAULT_PAD)は、解像度を粗くする(spacingを大きくする)ほど短軸を相対的に大きく膨らませる。極端なアスペクト比の形で粗い格子を作ると、短軸のpaddingが実寸を超えてグリッドが形状を覆わなくなり、レイ縮退のValueErrorになる
created: 2026-09-05
updated: 2026-09-05
---

# 粗い格子のpaddingが短軸を相対的に膨らませる

`probe_topopt/voxelize.py` の `grid_for_bbox` は、各軸の要素数を
`ceil(辺長/spacing) + 2*pad` で決める(`DEFAULT_PAD=1`)。**paddingは「セル数」で一定
(片側1セル)だが、その物理的な大きさ(mm)は spacing に比例する** —— spacing が大きい
(=解像度が粗い)ほど、paddingの絶対量は大きくなる。

長軸に対して短軸が極端に薄い形(例: 厚み12mmの薄板、長さ182mm)で、解像度を粗く
(`elements_long_axis` を小さく)すると、spacing が長軸基準で決まるため大きくなり
(実測: elements_long_axis=12 で spacing≈16.68mm)、**短軸のpadding(2セル=約33mm)が
短軸の実寸(12mm+余白)を軽く超える**。結果、短軸の格子範囲が形状の実寸の2倍以上に
膨らみ、`voxelize.occupancy` がその範囲でレイを飛ばして縮退し
「格子が形状をまったく覆っていない」という一見不可解なエラーになる
(グリッド範囲がむしろ形状より大きいのに「覆っていない」と出るのが読み取りにくい点)。

## 実例(probe_topopt、薄板題材、margin_ratio=0.05)

- `elements_long_axis=14`: グリッド (16,9,5)、spacing 14.3mm → 成功
- `elements_long_axis=12`: グリッド (14,8,4)、spacing 16.68mm → **失敗**
  (z軸のグリッド範囲が66.7mmまで膨らみ、形状のz実寸12mmを大幅に超える)

## 教訓

- **粗い解像度(coarse_elements_long_axis等)を選ぶときは、長軸基準の解像度だけでなく
  最も薄い軸がpaddingに対してどれだけ余裕があるかを確認すること。** 1点(1つの解像度)で
  試して通ったら採る、をやらない([[voxelize-ray-degeneracy-is-not-monotonic-in-resolution]]
  と同種の教訓 —— こちらは「解像度に対する非単調性」、今回は「短軸への相対的な
  padding膨張」という別の機序)。
- 対策は「短軸に十分な厚みが出るまで解像度を上げる」か「極端なアスペクト比の形には
  別の粗い解像度を個別に選ぶ」のどちらか。`voxelize.py` の `DEFAULT_PAD` 自体を
  変える改修はしていない(ブラスト半径が広く、既存426件の受入検査の基準値に影響しうる)。

`probe_topopt/bench.py` の `DESIGN_SPACE_COARSE`(coarse_elements_long_axis=12)は
ブラケット題材向けにチューニングされた値で、極端なアスペクト比の形にはそのまま
使えない(`safety_loop.py` の単調性測定で薄板だけ dilate_mm と coarse解像度を
個別調整する必要があった。`probe_topopt/README.md`「plate は既定の粗い段の設定が
効かない」参照)。
