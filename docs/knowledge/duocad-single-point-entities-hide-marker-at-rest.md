---
name: duocad-single-point-entities-hide-marker-at-rest
description: 判断(2026-09-11・本人) — duocadのスケッチ点マーカーは、pointKeyが1個しかない種別(長方形・円・円弧)は常時表示をやめ、ホバー/選択時だけ見せる。線分・ベジエ(点が複数)は常時表示のまま
created: 2026-09-11
updated: 2026-09-11
---

duocadのスケッチで、図形の「動かせる点」(`pointKeys`)を示す小さな四角マーカーは、これまで種別を問わず常時表示していた。長方形は基準コーナー(x,y)1個だけがpointKeyで、残り3隅は`width`/`height`/`angle`からの導出値なので、長方形を描くと4隅のうち1つだけマーカーが出るという見た目の非対称があった。本人がこれを指摘し(「4つとも同じ挙動なのに表示されるのが1つだけ」)、解決の方向として「pointKeyが1個しかない種別は常時表示をやめる」を提案・承認した。

## 発見した事実(判断の技術的根拠)

`duocad/verbs/move_sketch.py`の`_moved_params`を読むと、pointKeyが1個しかない種別(rectangle/circle/arc)は「その点だけ動かす」(`point='x'`等)と「図形全体=辺を動かす」(`point=None`→`EntityKindOps.translate`)が**完全に同じ処理**になる。`duocad/sketch.py`の`_translate_rectangle`はどちらの経路でも結局`x,y`を直接足すだけで、他のパラメータ(width/height/angle)は触らない。つまりこれらの種別では、点は「独立に動かせる特別な場所」ではなく「たまたま名前が付いた、辺上のどこでも同じ結果になる1点」でしかない。

対して線分(2点)・ベジエ(4点)は、片方の点だけ動かすと形が変わる(=図形全体を動かすのとは別の結果)ため、この理屈は当てはまらない。

## 実装

`web/src/entityKinds.ts`に`entityHasIndependentPoints(kind)`(pointKeysが2個以上か)を追加。`web/src/viewport.ts`の点マーカー生成・ホバー材質更新の両方で、これがfalseの種別は`mesh.visible = 選択中 || ホバー中`だけにした(常時trueの項を外した)。**ピック可能な座標(`pickPointsAt`が読む位置)は変えていない** —— `mesh.visible`はpicking判定を通らないので、`sketchPoint`参照・`center_entity`(回転中心の明示指定)等、点を名前で参照する既存の使い道は保たれる。

関連: [[duocad-rectangle-representation-decisions]](長方形が1点+w/h/angleのままである理由)。カーソルの□/×表示統一・当たり判定の優先度統一の続き(同日の会話)。
