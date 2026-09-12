---
name: duocad-sketch-mode-region-pick-bypasses-wants-gates
description: duocadのスケッチモード中は window.__duocad.wantsRegions / wantsBasePlanes が常にfalseになる。領域・基準平面のピックはスケッチモード専用の別経路で常時有効なため、実機smokeでこれらのフラグを待つと永久に揃わない
created: 2026-08-28
updated: 2026-08-28
---

`web/src/App.svelte` の `wantsRegions` / `wantsBasePlanes` は、どちらも `sketchId === null &&
...`（通常モード限定）で定義されている。スケッチモードに入っている間（`activeSketch !==
null`）は常に `false` を返す。

理由はコード中のコメントにある: スケッチモード中の領域ピックは `setActiveSketch` 側の規律
（そのスケッチの領域だけ）で常時動いており、`wantsRegions` のゲートを通すと**別のスケッチの
領域まで拾えてしまう**ため、あえて対象外にしてある。基準平面も同じ理由（スケッチ平面上で
描いている最中に奥の基準平面を掴むと線が引けなくなる）。

**実機smoke(`web/scripts/*.mjs`)を書くとき**: スケッチモードに入った状態で領域や基準平面の
クリック位置を取りたいなら、`window.__duocad.wantsRegions === true` / `wantsBasePlanes ===
true` を待ってはいけない（スケッチモード中は絶対に真にならず、待機が必ずタイムアウトする）。
`window.__duocad.projectRegion(index)`（第2引数省略時は `activeSketch` を対象にする）を
直接呼び、返ってきた座標へそのままクリックすればよい。`wantsRegions`/`wantsBasePlanes` の
待機が要るのは**通常モード**（`sketchId === null`）でのピックだけである
（例: `web/scripts/smoke-regions.mjs` の従来の使い方）。

`20260827-2151-duocad-import-svg-into-active-sketch` の `smoke-svg.mjs` 書き換え時に、
スケッチモードのまま領域を押し出す手順を書こうとして最初にこの待機を書き、実測で気づいた。
