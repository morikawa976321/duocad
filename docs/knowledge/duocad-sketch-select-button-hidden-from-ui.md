---
name: duocad-sketch-select-button-hidden-from-ui
description: 判断(2026-09-03) — duocadのスケッチモードツールバーから「選ぶ」ボタン(select_in_sketch)を人間向けUIから外した。理由は本人明言「人間は直接クリックで選べるので重複している」。AI用にverb自体は残す
created: 2026-09-03
updated: 2026-09-03
---

# duocad: スケッチモードの「選ぶ」ボタンをUIから外す

本人の判断: duocadのスケッチモードツールバーに出ていた「選ぶ」ボタン(`select_in_sketch`)を
人間向けの画面から外す。**理由は本人明言**「人間は直接クリックで選べるので重複している」。

**AI用にはそのまま残してよい**という指定どおり、`select_in_sketch` というコマンド定義・既定
コマンドとしての役割(`DEFAULT_SKETCH_COMMAND_ID`)・Escや確定後にそこへ戻る挙動は一切変えて
いない。変えたのは`CommandHost.svelte`の描画だけ(`CommandDef`に`hidden?: boolean`を新設し、
ボタン一覧の`{#each}`から除外)。汎用フラグとして実装したので、他のverbが将来同じ理由で
ボタンを隠したくなったときも使い回せる。
