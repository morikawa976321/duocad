---
name: duocad-mirror-verbs-added
description: 事実(2026-09-06) — duocadにmirror_sketch_entities(スケッチ図形を既存の線分で鏡映複製)・mirror_body(ボディをXY/XZ/YZまたは既存の平面な面で鏡映複製)を追加。MCP・UI両経路から使える
created: 2026-09-06
updated: 2026-09-06
---

duocadに鏡映(mirror)系のverbを2つ追加した。どちらも**常に複製を作る**(in-place反転の口は無い)。

- `mirror_sketch_entities`: 確定選択のスケッチ図形を、既存の線分1本を軸に鏡映複製する。
- `mirror_body`: 確定選択の面/辺/頂点が属するボディを、3平面(XY/XZ/YZ、原点通過)または
  既存ソリッドの平面な面1枚を鏡映面として鏡映複製する。

実装は2段階のタスクに分かれた:

1. verb本体・catalog登録・MCP経路(taskbox `20260906-1100`)。この時点では「既存の線分/面を
   鏡映基準に選ぶ」経路はUIに開けず、MCP専用のまま意図的に未決で残した(対象の確定選択と
   衝突する構造的な問題があったため)。3平面(`base_plane`)側だけはUIまで通した。
2. 既存の線分/面をUIから選べるようにする経路(taskbox `20260906-1500`)。この解決の詳細は
   [[pick-arg-spec-solves-target-vs-reference-selection-conflict]]。

長方形の鏡映で型(kind)が変わってしまう問題は [[duocad-rectangle-representation-decisions]] の
角度パラメータ追加で別方向から解消された(鏡映側でのValueErrorやフォールバック処理は
不要になった)。

実装差分の正はtaskbox `done/20260906-1100-duocad-add-mirror-verb.md` と
`done/20260906-1500-duocad-mirror-reference-picking-ui.md` の「後続への契約」節
(taskboxは実行状態なので、詳細な引数名・ログ形式・触ったファイルはそちらを見ること。
ここには恒久的な決定事実だけを置く)。
