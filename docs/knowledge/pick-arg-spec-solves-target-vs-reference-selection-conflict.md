---
name: pick-arg-spec-solves-target-vs-reference-selection-conflict
description: 運用知見(2026-09-06・duocad) — verbが「対象の確定選択」とは別に「既存の図形/面への参照」を必要とするとき、UI側は既存のPickArgSpec枠組みを拡張すればよく、新しい入力モードを設計する必要はない
created: 2026-09-06
updated: 2026-09-06
---

duocadのverbには、対象(確定選択で取る)とは別に「既存の図形/面への参照」を引数で必要とする
ものがある(`move_body` の `axis_edge`/`center_vertex`、`mirror_body` の `plane_face`、
`mirror_sketch_entities` の `axis`)。対象の確定選択と同じ集合に参照用の図形を混ぜるとコアが
どちらか区別できないため、この種の引数は `commit_selection` を介さない別経路で受ける必要がある。

`mirror_body`/`mirror_sketch_entities` を新設した時点(taskbox `20260906-1100`)では、この
「UIでどう指すか」は `move_body.axis_edge` と同型の未解決問題として扱われ、MCP専用に据え置か
れた。しかし実際にUIへ開く段になって判明したのは、**この問題は既に解決済みだった**という
こと —— `move_body.axis_edge`/`center_vertex` は既に `PickArgSpec` という「対象とは別
チャンネルで図形を1つ直接ピックする」既存の枠組みの上に乗っており、新しい種別
(`face`/`sketchEntity`)を `PickTarget` に2つ足すだけで `mirror_body`/`mirror_sketch_entities`
にも同じ経路が使えた(taskbox `20260906-1500`)。新設が要ったのは「修飾キー(Shift/Ctrl)
付きクリックはピックへ行かせない」という1点のみ(必須のピックは対象が揃うと自動で構えるので、
これが無いと2つ目の対象を選べなくなる)。

**教訓**: 対象選択と衝突する参照引数を持つverbを新設するとき、「UIでの指し方は未決」と
即断せず、まず `PickArgSpec`(`web/src/commands/command.ts`)が使えないか確認すること。
面のピックだけは辺とピック優先順位が競合するため、追加の配慮(狙う種別以外を出さない)が要る
([[a-target-under-a-hit-priority-rule-can-be-structurally-unpickable]])。

修飾キー付きクリックをピックへ行かせない件の落とし穴と解き方は
[[an-auto-armed-pick-eats-the-clicks-meant-for-the-target-set]]。verb側の事実は [[duocad-mirror-verbs-added]]。
