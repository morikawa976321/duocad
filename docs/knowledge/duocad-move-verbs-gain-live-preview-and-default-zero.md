---
name: duocad-move-verbs-gain-live-preview-and-default-zero
description: 判断(2026-09-04) — duocadのmove_body/move_sketch_entitiesに数値連動プレビューを付け、空欄の軸は0(=動かさない)扱いにすると決めた。実装は2026-09-05にプレビューだけが入り、空欄=0はコードに無い
created: 2026-09-04
updated: 2026-09-05
---

# duocad: move系verbに数値連動プレビューと空欄=0の既定値

本人の判断(2026-09-04): duocadの`move_body`(X/Y/Z)・`move_sketch_entities`(X/Y)に
**打鍵と連動したプレビュー**を付ける(それまで両方とも`preview: 'none'`で確定前の姿が
一切出なかった)。あわせて、**入力しなかった軸は自動で0として扱う**(=その軸は動かさない)
と決めた。全軸空欄のまま確定しようとした場合は既存の「移動量が全部0」チェックが効く。

**空欄=0は、この特定2verbのdx/dy/dzだけに絞る**という判断も同時に出ている ——
半径・距離・個数のような「空欄=まだ決めていない」引数には付けない(0が有効値でない
引数に黙って0を埋めると壊れるため、汎用の既定値ではなく個別opt-inにする)。

## 実装の現況(2026-09-05時点)

- **プレビューは入った。** 2026-09-05に `move_body` / `move_sketch_entities` を含む4verbへ
  実装した([[preview-belongs-on-every-verb-that-can-have-one]])。ただし方式は
  **両方ともブラウザだけで描くローカル方式**である —— `move_body` をサーバの投機実行
  (`previewable=True`)に載せる案は採らなかった(位置しか変わらないので、投機実行しても
  形が変わったBodyが1つも返らない)。
- **空欄=0(`NumberArgSpec.default`)はコードに無い。** 全履歴を通して一度も入っていない。

この食い違いの経緯と、そこから引いた運用上の教訓は
[[a-note-can-claim-work-that-was-never-committed]]。
