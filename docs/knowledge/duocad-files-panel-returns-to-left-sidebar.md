---
name: duocad-files-panel-returns-to-left-sidebar
description: 判断(2026-08-26) — duocadのファイル一覧をツールバーから開閉可能な左サイドバーへ戻す。2026-08-23のツールバー統合の裁定(第1項)を撤回。SelectionPanel削除(第2項)は有効のまま
created: 2026-08-26
updated: 2026-08-26
---

# duocad: ファイル一覧を開閉可能な左サイドバーへ戻す

## 判断(本人・2026-08-26)

`FilesPanel`(現在のファイル名・新規・名前入力・新規保存・一覧・開く/上書き)を、
ツールバーの帯から**丸ごと左サイドバーへ戻す**。ヘッダのボタンで開閉する形も戻す。
**理由の明言は無い。**

これは [[duocad-files-panel-selection-panel-verdict]] の**第1項(FilesPanelをツールバーへ
統合する)の撤回**である。**同ノート第2項のSelectionPanel削除は有効なまま。**

## 経緯(2026-08-23の同日中に往復していた)

- 14:18 `2ed946e` ファイル一覧を右サイドバー→**左サイドバー**(ヘッダの「ファイル」トグルで開閉)
- 19:22 `0eca2c6` 本人裁定で**ツールバーへ統合**(縦の一覧→横スクロールのチップ列に作り替え)
- 2026-08-26 本判断で左サイドバーへ戻す

## 実装で気をつける点(2026-08-26時点の実測)

- **`git revert 0eca2c6` では戻せない**。0eca2c6以降にFilesPanelは「新規」ボタン
  (`#file-new`、コミット `ae3a12a`)を得ており、revertするとこれが消える。
- **左サイドバーの既定は開いた状態**でなければならない。畳んだ状態を既定にすると
  `smoke-files.mjs` が `#file-name-input` 等を押せず落ちる。
- ビューポートが240px狭くなる＝カメラのアスペクトが変わるので、実機検査は全部回す必要がある
  ([[viewport-resize-silently-changes-what-a-click-grabs]])。

続報: STEP/STLの入出力もこのパネルへ寄せる([[duocad-file-io-moves-into-files-panel]]・2026-08-27)。
関連: [[duocad-ui-moved-to-fusion-layout]] / [[duocad-ui-interaction-principles]]
