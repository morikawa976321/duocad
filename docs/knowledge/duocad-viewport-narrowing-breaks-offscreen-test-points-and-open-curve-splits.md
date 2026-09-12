---
name: duocad-viewport-narrowing-breaks-offscreen-test-points-and-open-curve-splits
description: duocadの実機検査で実測 — ビューポートを横に狭める変更(左サイドバー追加等)はu軸(横)の可視範囲だけを削り、v軸(縦)は変わらない。矩形の外へ逃がしていたクリック座標が画面外になったとき、矩形の内側へ動かす修正は`BRepAlgoAPI_Splitter`が開いた線分・円弧も分割線として使うため「invalid shape」で壊れる
created: 2026-08-27
updated: 2026-08-27
---

duocadの左サイドバー(240px)復元(`web/src/App.svelte`)で `web/scripts/smoke-features.mjs` と
`web/scripts/acceptance.mjs` が実機検査で赤くなった実測ログ。

## 実測1: 狭まるのはu軸(横)だけ

PerspectiveCameraの垂直FOVは固定(`camera.fov`)で、水平方向の可視角は
`aspect(=width/height)` に比例する(`web/src/viewport.ts`)。左サイドバーが幅だけを削ると、
同じカメラ距離でも**u軸(横)の可視範囲が縮み、v軸(縦)はそのまま**になる。以前の
下段バー削除(高さが増える→aspectが下がる)でもu軸が詰まったのと同じ方向の効果で、
今回はさらに強く効いた(`window.__duocad.projectSketchPoint(u,v)` の実測で
u=40(矩形の右端そのもの)すら画面外、v=-35までは可視という非対称が出た)。

**教訓**: ビューポートを狭める変更のたびにテスト座標を勘で詰め直さない。
`page.evaluate(() => window.__duocad.projectSketchPoint(u, v))` を候補グリッドで
呼んで実測してから座標を選ぶ(勘の再計算より確実で速い)。横方向が窮屈なら、
可能な限りv軸方向(縦)へ逃がす方が安全マージンが大きい。

## 実測2: 領域計算は閉じていない曲線でも面を割る

`duocad/region.py` の `regions()` は `BRepAlgoAPI_Splitter` で平面を**スケッチの辺すべて**
(線分・円弧のような閉じていない曲線も含む)で分割する。矩形の外に逃がすスペースが
無くなったからと安易に「矩形の内側」へ線分・円弧の置き場を動かすと、内部を横切る形に
なった瞬間に押し出し(`extrude`)が `ValueError: join produced an invalid shape (BRepCheck)`
で壊れる(acceptance.mjsで実測)。

**教訓**: 閉じた図形(円)は矩形の内側に置いても「面積を引いた穴」になるだけで安全だが、
**開いた図形(線分・円弧)は矩形の外に完全に離すこと**。詰まった横方向の代わりに、
矩形の外側でまだ見えている帯(v<0など)へ逃がすのが正しい修正方向で、矩形の内側へ
動かすのは別の壊れ方を生む。
