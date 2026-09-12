---
name: voxelize-ray-degeneracy-is-not-monotonic-in-resolution
description: 落とし穴(2026-09-04実測) — レイキャスト式ボクセル化の「ずらしがすべて縮退した」は解像度を下げるほど起きるのではなく、飛び飛びの解像度で起きる。1点で通ったから採る、をやると次の環境で落ちる
created: 2026-09-04
updated: 2026-09-05
---

duocad `probe_topopt` の `voxelize.occupancy` は、三角形群へ +z のレイを飛ばして巻き数で内外を
決める。レイが頂点・稜線に当たると縮退するので位置を6通りずらして再試行するが、**6通りとも
縮退すると `ValueError: レイのずらしを6通り試したがすべて縮退した` で落ちる**。

受入検査の解像度を下げる作業でこれを踏んだ。**壊れ方が解像度に対して単調ではない**:

- `check_design_space.py` の案1の解像度: **12 と 16 で落ち、13/14/15/17/18/20/22 は通る**。
- `check_absolute_volume.py` の要素サイズ: **9.0mm 以上で落ち、6.0〜8.5mm は通る**。

格子の原点と要素サイズが形状の平面・稜線とたまたま揃うかどうかで決まるので、
「粗いほど危ない」という単調な直感が効かない。

**帰結: 解像度を1点だけ試して通ったら採る、をやらないこと。** 前後を振って通る値の帯を
確かめ、その真ん中を採る。帯の端を採ると、形状やライブラリのわずかな変化で落ちる。

関連: 同じ例外文の別の機序 — [[grid-for-bbox-padding-inflates-short-axes-at-coarse-resolution]](粗い格子で短軸のpaddingが実寸を超える) /
[[occupancy-raises-when-no-grid-column-hits-the-shape]](小さい形状をどの列も貫かない)
