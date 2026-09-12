---
name: duocad-move-verbs-gain-rotation
description: 判断(2026-09-05・本人) — duocadのmove_body/move_sketch_entitiesに回転を足す。新verbは作らずmodeで並進と排他切替。軸はスケッチ=平面法線固定・ソリッド=任意3D軸、軸と中心は確定選択でなく引数でIDを受ける、既定の中心は重心(境界箱中心ではない)、複数対象でも中心は1つ。実装duocad 27cacc2
created: 2026-09-05
updated: 2026-09-06
---

# move verbに回転を足す(2026-09-05の本人判断)

[[duocad-move-verb-split-sketch-and-body]] で作った2つの move verb は並進だけだった。回転を足すにあたって本人が決めたのは次の6点。

1. **軸の自由度**: スケッチは1軸(スケッチ平面の法線。固定で可変不可)、ソリッドは任意の3D軸(点+方向)。
2. **軸・中心の指定方法**: **確定選択には載せない。** `pattern_circular` の `entities` と同じく、`describe_document` / `describe_shape` で得たIDを**引数でそのまま渡す**。理由(本人): この2つのverbは確定選択を既に「動かす対象の解決」に使っているので、軸や中心を同じチャンネルへ載せると、選ばれているものが対象なのか軸なのか区別が付かなくなる。
3. **回転中心の既定値**: 対象の重心を軸上へ直交射影した点。**バウンディングボックス中心ではない** —— 理由(本人): 境界箱は軸の向きに依存して動くので中心が不安定になる。
4. **並進と回転は排他**: `extrude` の `mode`(join/cut/new)と同じ形で `mode: "translate"(既定) | "rotate"`。同じ呼び出しで dx/dy/dz と angle の両方は渡せない。「渡された側だけが有効」ではなく mode で明示的に切り替える。
5. **複数対象の回転中心は1つ**(合成重心。対象ごとに個別の中心は計算しない)。
6. **verbは新設しない**。`rotate_body` のような別verbは作らず、既存の2つを拡張する。

## 実装で分かったこと(2026-09-05)

**3Dの回転は軸線上のどの点を基準にしても同じ変換**なので、判断3の「重心を軸へ直交射影する」は、軸の位置が既に決まっている場合(辺で指す・点+向きで指す)には**恒等操作**である。中心が結果を変えるのは、軸の**向きだけ**を渡して位置を渡さなかったとき(軸を重心へ通す)と、頂点を指して軸をそこへ寄せるときだけ。判断3が意味を持つのは「向きだけ指定」の経路である。

実装は duocad `27cacc2`。確定前の幽霊は [[preview-belongs-on-every-verb-that-can-have-one]]、軸のピックまわりは [[send-the-reference-resolve-numbers-only-for-the-preview]] / [[a-target-under-a-hit-priority-rule-can-be-structurally-unpickable]]。
