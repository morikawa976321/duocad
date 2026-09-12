---
name: duocad-file-io-moves-into-files-panel
description: duocadのSTEP/STL入出力のUIを、ツールバーのコマンドから左サイドバーのファイルパネルへ移すと本人が決めた（2026-08-27）。2026-09-05にSTEP読み込みだけツールバーへ戻した(部分撤回)
created: 2026-08-27
updated: 2026-09-05
---

2026-08-27、duocadの **STEP/STLの読み込み・書き出し（`import_step` / `export_step` / `export_stl`）のUIを、ツールバーのコマンド（`CommandHost`）から左サイドバーのファイルパネル（`FilesPanel.svelte`）へ移す**と本人が決定した。**理由の明言は無い。**

前提として、2026-08-26に決めた「ファイル一覧（FilesPanel）を左サイドバーへ戻す」（[[duocad-files-panel-returns-to-left-sidebar]]）で移ったのは**保存済み文書の保存・開く・新規だけ**であり、STEP/STLの入出力はツールバーのコマンドのまま残っていた。今回の決定はその残りを寄せるもの。

これにより、`web/src/commands/files.ts` のモジュールdocstringが書いていた分担の線（「保存/開くだけは `CommandHost` に無い＝ファイル名の一覧から選ぶ形だから」）は引き直しになる。

**部分撤回(2026-09-05)**: STEP読み込み(`import_step`)だけをツールバーのverbへ戻した。書き出し2つはパネルに残る。
本人の言葉は「STEP読み込みはそもそもファイルバーじゃなくて通常モードの上部に出るべきだった」。
分ける線は「文書を変えるか」 → [[duocad-import-step-returns-to-toolbar]]。
