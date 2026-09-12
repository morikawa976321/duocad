---
name: optimize-result-is-a-mesh-that-cannot-be-edited
description: 形状最適化の結果は三角形メッシュで表示とSTLエクスポートだけ — duocadはB-repしか持てず、verbは全部B-repの面・辺・領域を要求する
created: 2026-09-04
updated: 2026-09-05
---

# 最適化の結果は編集できない(B-repに戻す道は実質無い)

2026-09-04本人決定。「最適化したモデルは三角形メッシュとして duocad で表示してほしい。
基本的には編集せずそのまま STL としてエクスポートしてスライサーに入れる使い方を想定している」。

当初は「**メッシュなので一応押し出しやフィレットや面取りはできる**」という前提だったが、
コードを確かめると**成立しない**:

- `duocad/document.py:19` —— `_VALID_KINDS = {"solid"}`。ボディは `shape`(TopoDS_Shape)を
  1つ持つだけで、メッシュを持つ種別が無い。
- `duocad/verbs/fillet.py:25` —— `fillet(doc, edges: Sequence[int], ...)` は**B-repの辺の添字**を
  取る。chamfer も同じ。extrude はスケッチの領域、extrude_face は B-rep の面。
- `duocad/io_step.py:69` —— `export_stl` は**B-repを受け取って**テッセレーションして書く。
  **STLの読み込み経路は存在しない。**

## 「B-repに変換すれば verb が効く」が実質できない理由

marching cubes の出力は**デモの粗い解像度でも6,000三角形**、実用解像度なら数万。三角形1枚を
1面とするシェルとして OCCT に入れると面数が数万になる。そして
[[duocad-box-select-cost-is-a-quadratic-dedup]] の途中経過の実測では **`kernel.face_count` が2,835面で
ドラッグ一括選択1回51.1ms**を占めていた(その後ボディごと1回に減らして1.5msまで下げたが、面数に比例する費用で
あることは変わらない)。数万面では操作のたびに秒単位で待つ。
加えて**三角形の辺にフィレットを掛けるのは幾何的に無意味**である。

まともな B-rep にするには面の再構成(平面・曲面フィッティング)が要り、それは別プロジェクト。

## 帰結

- メッシュのボディ種別を足し、**表示とSTLエクスポートだけ**できるようにする。編集 verb は
  **メッセージ付きで拒否**する(押せるのに断られるのを避けるため、画面側でも出さない)。
- **編集したい場合は、結果を参考形状として見ながら隣で自分でモデリングする**使い方になる。
  だから**元のボディは全部残す**(本人決定)。結果は新しいボディとして足す。
- フロントは既に `positions`/`indices` で三角形を受け取って描いている
  (`web/src/viewport.ts:42-67`)ので、**画面側は無改造で描ける見込み**。

関連: [[fold-the-pick-instead-of-filtering-the-toolbar]](メッシュのピックをボディ参照へ畳む) /
[[duocad-has-no-rename-verb-for-any-body]] / [[id-of-none-as-a-change-key-never-changes]](`Body.mesh` を足したときの版の落とし穴) /
[[topopt-code-moved-into-duocad-core]] / [[no-oss-mesh-to-editable-brep]]
