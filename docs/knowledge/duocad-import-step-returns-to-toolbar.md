---
name: duocad-import-step-returns-to-toolbar
description: 判断+理由明言(2026-09-05) — duocadのSTEP読み込みを左サイドバーのファイルパネルからツールバーのverbへ戻す。書き出し2つはパネルに残す。2026-08-27の全部移設の部分撤回
created: 2026-09-05
updated: 2026-09-05
---

duocadの **STEP読み込み(`import_step`)だけ**を、左サイドバーのファイルパネルから
**通常モードのツールバー(コマンドの行)へ戻す**と本人が決めた。

本人の言葉: **「STEP読み込みはそもそもファイルバーじゃなくて通常モードの上部に出るべきだった」**。
書き出し(STEP書き出し・STL書き出し)については **「出力はよいのだが」「出力はそのままでいい」**
と明言し、パネルに残す。

これは [[duocad-file-io-moves-into-files-panel]](2026-08-27にSTEP/STLの入出力3つを
まとめてファイルパネルへ移した判断)の**部分撤回**である。3つのうち読み込み1つだけが戻り、
書き出し2つは移設先に留まる。

実装は常設の「入力欄+ボタン」ではなく **`CommandDef`(他のverbと同じ形)**にした:
押して起動 → パス欄が出る → 打つ → Enter。`web/src/commands/files.ts` の `IMPORT_STEP` で、
`commands/sketch.ts` の `IMPORT_SVG` と同じ作り。`id`/`inputId`/`buttonId` は移設前と
同じ名前のまま(実機検査が名前で掴んでいるので、叩く口を変える理由が無い)。
`CommandCategory` に `file` が戻り、`App.svelte` の `runCommand` に `case 'file'` が入った。

**分ける線は「文書を変えるか」**である。読み込みは操作ログに1行載り、undoで戻り、
読んだ直後から面をクリックして押し出せる ——ファイルの出し入れではなくモデリングの1手。
書き出しは文書を1ミリも変えない。この線は `commands/files.ts` の `changesDocument` が
以前から `import_step` だけを真にしていたのと同じものである。

関連: [[duocad-ui-interaction-principles]] / [[duocad-show-only-what-is-needed]]
