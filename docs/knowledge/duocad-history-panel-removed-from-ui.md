---
name: duocad-history-panel-removed-from-ui
description: 判断(2026-08-23) — duocadの操作履歴パネル(HistoryPanel.svelte)をアプリUIから削除。mine印(myOperations/markOwnership)も道連れで削除、undo/redoの件数(redo_count)自体はコア状態として残る
created: 2026-08-23
updated: 2026-08-23
---

# duocad: 操作履歴パネルをUIから削除

## 判断(本人・2026-08-23)

> duocadの操作履歴欄をアプリUIから削除。

`web/src/HistoryPanel.svelte` を削除し、`App.svelte` からの参照(import・使用・
`myOperations` state・`markOwnership` 関数・test hookの `myOperations` フィールド)も
併せて除去した。`redo_count`(あと何回やり直せるか)自体はコア側の状態として残り、
undo/redoボタンはステータスバーにそのまま残る —— 消えたのは「操作の一覧を横並びで
見せる画面」と、それにだけ使われていた「この画面が出した操作」の印付けロジック。

## 影響したファイル

- 削除: `web/src/HistoryPanel.svelte`
- `web/src/App.svelte`(import・使用・`myOperations`/`markOwnership`)
- `web/src/FilesPanel.svelte` / `web/src/styles.css` / `web/src/session.ts`
  (HistoryPanel.svelteを前提にしていたコメント)
- `web/scripts/smoke-files.mjs`(`#history-position` / `#history-list` に依存していた
  検証を、hookの `redoCount`/`log` だけで見る形に書き換え)
- `README.md`(UIチートシートの図・「AIがやった/人がやった」区別の既知の制限を削除)

`ACCEPTANCE.md` / `docs/IMPLEMENTATION_PLAN.md` / `tests/README.md` はプロジェクト
自身の規約で「当時の記録として凍結」されているドキュメントなので、書き換えずそのまま残した。

関連: [[duocad-ui-audit-seven-verdicts]](その7番「履歴の行クリックで一括undo — 見送り」は
undo機能追加の話で別件。今回はパネルそのものの削除)
