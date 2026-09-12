---
name: occupancy-raises-when-no-grid-column-hits-the-shape
description: duocad.topopt.voxelize.occupancy は「格子と重なっているのに1本のレイも形状を貫かない」小さい形状で ValueError になる。0要素として扱いたい呼び出し側は捕まえる必要がある
created: 2026-09-05
updated: 2026-09-05
---

`voxelize.occupancy(soup, grid)` は各列 (i,j) の**セル中心**を通る +z のレイを飛ばして
巻き数を数える。形状が小さくて**どの列のセル中心の直線も形状を通らない**と、交点が1つも
取れず「レイのずらしを6通り試したがすべて縮退した。最後の理由: 三角形とレイの交点が
1つも無い」という `ValueError` になる。**例外文は「格子が形状をまったく覆っていない」と
言うが、実際には格子は形状を覆っている** —— 覆っているかどうかと、列が貫くかどうかは別。

実測(2026-09-05・duocad `20260904-1704`): 要素サイズ 4.04mm の格子に 1mm 角の箱を置くと
必ずこれになる(列の中心の x が 28.94 と 32.98 で、箱の 30..31 を跨いでいる)。

**「この解像度でこの形状は表現できているか」を数えたい呼び出し側にとって、これは異常では
なく答えそのもの**(= 0要素)なので、`except ValueError` して0として扱う必要がある。
duocad の形状最適化の事後検出(`optimize_run._region_representation`)はそうしている。

関連: [[voxelize-ray-degeneracy-is-not-monotonic-in-resolution]] / [[grid-for-bbox-padding-inflates-short-axes-at-coarse-resolution]] /
[[resolution-slider-only-moves-toward-finer]](事前計算+事後検出の二段構えのうち、事後検出の側)
