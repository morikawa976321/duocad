---
name: duocad-optimize-set-picks-toggle-without-modifier
description: duocadの保持/除外ソリッド・拘束面の3verbは、修飾キー無しのクリックでもトグルにした(2026-09-11)
created: 2026-09-11
updated: 2026-09-12
---

duocadの形状最適化(`set_preserve_bodies` / `set_exclude_bodies` / `set_fixtures` の3つ、
`OPTIMIZE_PRESET_ROLE` に載る集合代入verb)は、修飾キー無しのクリックでもトグル(選択/非選択の
付け外し)になるようユーザーが変更を選んだ。他のverb(フィレットの辺選択・押し出す面選択等)は
従来どおり修飾キー無し=置き換えのまま(Shift+クリック=追加・Ctrl+クリック=除外の現状は [[duocad-sketch-panel-replaced-by-direct-selection]] に記述)。

本人の変更依頼の文面: 「verb起動時に既存の割り当てが選択状態で現れるので、新しい面を足すには
Ctrl+クリックが要る。これをやめて素クリックでトグルにしたい」。

実装は `duocad/web/src/App.svelte` の `toggleOnlyOptimizeSet`(`onCommandStart` で
`OPTIMIZE_PRESET_ROLE[verb]` の有無から立て、`onWantsKinds` で無起動へ戻ったときに下ろす)。
