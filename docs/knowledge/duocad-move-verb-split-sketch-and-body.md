---
name: duocad-move-verb-split-sketch-and-body
description: 判断(2026-08-30) — duocadのmove verbはmove_sketch_entities(点/辺, dx dy, copy on/off)とmove_body(選択中の面/辺/頂点が属する全ボディ, dx dy dz, copy on/off)の2つに分けて新設する。寸法パラメータの編集は今回スコープ外
created: 2026-08-30
updated: 2026-09-05
---

# move verbはスケッチ用/ボディ用の2つに分ける

## 判断（本人）

新設するmove verbは、catalogの「1コマンド=1静的スキーマ」の設計([[duocad-verb-single-declaration]])に合わせて2つに分ける:

- **move_sketch_entities**: スケッチモードで、選択された**点**(エンティティのx,y系パラメータ1個。線の端点・円/弧の中心・矩形の角・ベジエの制御点)または**辺**(エンティティ全体を1単位として、両端点を同じ量だけ動かす=形は変えず平行移動)を対象に `dx, dy` を取る。
- **move_body**: 通常モードで、選択中の面/辺/頂点が属するボディを対象に `dx, dy, dz` を取る。**選択が複数ボディにまたがる場合は全ボディを同じ量だけ動かす**(本人明言・既存のfillet等と同じ「確定選択に対して1verb」のパターンを流用)。ボディを動かすための新しい「ボディ選択」という概念は不要 — 面/辺/頂点のShapeRefが既に`body`を持つのでそこから逆引きできる。

両方とも `copy` ON/OFF を持つ(OFF=移動、ON=元を残して新しい位置に複製)。copyはentity単体が対象で、反復(pattern)フィーチャーには適用しない。

## スコープ外(今回は作らない)

半径・幅高さ・角度・パターンの間隔/個数といった**寸法系パラメータの編集口は今回作らない**(将来課題)。理由(本人明言): 寸法を変えると、既にそのスケッチをextrudeして作ったソリッドの再計算(押し出し解決)が絡んで面倒になるため、現時点では見送る。位置の平行移動(move)だけなら再計算問題を(新たには)持ち込まないので今回のスコープに含めた——ただし完全に無関係というわけではない、詳細は [[duocad-sketch-body-desync-accepted-for-move]]。

## 関連

[[duocad-sketch-panel-replaced-by-direct-selection]] とセットの判断。

続報: 選択リセットの例外は却下([[duocad-move-verbs-keep-the-selection]]、2026-08-31)。2026-09-04に数値連動プレビューと「空欄の軸=0」を追加([[duocad-move-verbs-gain-live-preview-and-default-zero]])。2026-09-05に回転を追加([[duocad-move-verbs-gain-rotation]]) —— 新しいverbは作らず `mode` で並進と排他に切り替える。
