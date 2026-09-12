---
name: duocad-right-sidebar-becomes-collapsible-ai-only
description: 判断(2026-08-30) — SketchPanel全体(数値入力欄だけでなくモード表示・正面から見る・抜けるボタンも)を右サイドバーから外しコマンドツールバーへ移設。右サイドバーはAiPanel専用になり、左サイドバー同様に開閉可能にする。開閉ボタンはヘッダー右端
created: 2026-08-30
updated: 2026-08-30
---

# 右サイドバーはAiPanel専用にして開閉可能にする

## 判断（本人）

[[duocad-sketch-panel-replaced-by-direct-selection]] でスケッチパラメータの数値入力欄を右サイドバー(`SketchPanel.svelte`)から除いたが、それだけでは残存部分(スケッチモードの見出し「編集中 ...」・「正面から見る」・「抜ける」ボタン)がスケッチモード中は右サイドバーに残ってしまう。本人はこれらもモードに応じて作り直される**コマンドツールバー側**(`App.svelte` 1304行付近、モードが変わるたびに内容を作り直す段)へ移すことを選び、`SketchPanel.svelte` 自体を右サイドバーから完全に無くすと決めた。

結果として右サイドバーは常に `AiPanel` だけになり、**完全にAI専用**になる。

これに合わせて、右サイドバーを左サイドバー(`data-panel` / `dataPanelOpen`。ファイル一覧、開閉ボタンは`#data-panel-toggle`でヘッダー左端)と同じ形で開閉可能にする。開閉ボタンは**ヘッダーの右端**(ファイル開閉ボタンと同じ段の逆端。現状ヘッダー最後尾の`#settings-open`より後ろ)に置く。

## 関連

実装は [[duocad-sketch-panel-replaced-by-direct-selection]] / [[duocad-move-verb-split-sketch-and-body]] と同じtaskboxタスク(`taskbox/queue/20260830-1506-duocad-sketch-direct-selection-move.md`)に含めた。move_sketch_entities / move_bodyの実機確認が済んでからSketchPanelを移設・削除する順序を守ること(先に削除するとスケッチの抜け方が無くなる)。
