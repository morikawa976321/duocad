---
name: duocad-files-panel-drops-duplicated-name-and-label
description: 判断(2026-09-05) — duocadのファイルパネルから「ファイル」ラベルと編集中ファイル名(#current-document)を削除。編集中は一覧の行の強調で示す。左パネルの空白は放置と決定
created: 2026-09-05
updated: 2026-09-05
---

duocadの左サイドバー(ファイルパネル)から、**ヘッダと重複していた2つ**を削除すると本人が決めた。

1. **編集中のファイル名** —— `#current-document`。ヘッダの `#current-file` と同じ文字列が
   150px離れて2度出ていた。**ヘッダ側を残す**([[duocad-header-shows-name-and-save-state-only]]
   の裁定でヘッダに置くと決まっており、隣の「未保存」「保存」が指しているのが同じものだから)。
2. **「ファイル」の行ラベル** —— ヘッダの開閉ボタン(`#data-panel-toggle`)が同じ語を
   既に言っていた。**ヘッダ側を残す**(畳むとパネルが消えるので、外に出す口が要る)。

削除で失われる「どれを開いているか」は、**一覧の中の当該行を光らせる**形へ移した
(`.files-row.active`。`BrowserPanel.svelte` が編集中のスケッチを `.sketch-row.active` で
示すのと同じ)。問いが「一覧のどれか」なら答えも一覧の中にある。

**モデルが空のとき左パネルの中央が大きく空く件は「いったん放置でいい」**と本人が決めた
(同日に指摘した4件のうちこれだけを見送り)。

関連: [[duocad-import-step-returns-to-toolbar]] / [[duocad-files-panel-returns-to-left-sidebar]]
