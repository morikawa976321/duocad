---
name: duocad-files-panel-selection-panel-verdict
description: duocad右サイドバーのFilesPanel→上部バー統合(2026-08-26撤回・左サイドバーへ復元)とSelectionPanel全体削除の本人裁定(頂点pick/highlight新設が前提条件)
created: 2026-08-23
updated: 2026-08-26
---

本人裁定(2026-08-23):

1. ~~**FilesPanel(ファイル名表示・名前入力・新規保存・一覧・開く/上書き)を右サイドバーから上部バー(ツールバー)へ丸ごと統合する**。理由: 保存機能だけをサイドバーで特別扱いする必然性が無い。~~
   **2026-08-26に撤回**。本人が左サイドバー(`.data-panel`)への復元を決定した(理由の明言は無い)。詳細は [[duocad-files-panel-returns-to-left-sidebar]]。
2. **SelectionPanel(「選択」パネル)を丸ごと削除する**(見出し・要約・一覧・「外す」/「全解除」/「承認する」ボタンすべて含む)。理由: viewportのハイライトを見れば選択中のものが分かるため。
3. 削除に伴う操作機能の代替(本人回答): 「外す」→viewport内で外したい部分をクリック。「全解除」→viewport内で何もないところをクリック(既存の空クリック処理=`App.svelte`の`onPick`で対応済み)。「承認する」(AI提案選択のcommit_selection)→明示的な承認は不要、そのまま操作を行うかAIに指示を出せばよい。
4. **前提条件**: 頂点(vertex)は現状viewportでピック・ハイライトのどちらも不可(バックエンドに頂点座標を返すクエリが存在しない — `mcp_server.py`に「頂点は位置を返す問い合わせが無い」と既知の未実装として明記されている)。SelectionPanel削除で頂点選択が人間側から完全に見えなくなるため、**頂点のピック・ハイライトをバックエンドのクエリ新設から含めフルスタックで実装してから、SelectionPanelを削除する**、と裁定。実装順を逆にしない。

関連: [[duocad-ui-moved-to-fusion-layout]]（FilesPanel移設はこのレイアウト変更の一部として実施）
