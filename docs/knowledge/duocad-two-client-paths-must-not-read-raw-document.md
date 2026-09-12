---
name: duocad-two-client-paths-must-not-read-raw-document
description: duocadは同じSessionをMCP(AI)とWebSocket(UI)の2経路から見るので、MCP側が生のDocument/log.entriesを読むと契約が経路ごとにずれる。can_undoとapplied_countの2件が実際にそれで壊れた
created: 2026-08-28
updated: 2026-08-28
---

duocadでは、AI(`DocumentTools.describe_document`)とブラウザ(`Session.state_meta`)が**同じ
`Session` を別の口から見る**。このとき MCP 側が `Session` のプロパティではなく**生の
`document.log` を直に読む**と、同じ名前のキーが経路ごとに違う意味になる。実際に2件壊れた:

- **`can_undo`** — MCP側が `document.log.can_undo` を素で見ていたので、土台(STEP読み込み・
  編集用ボディ)の直後に「undoできる」と答えたのに実行が拒否された。`Session.can_undo` へ
  揃えて解決済み(コメントも残っている)。
- **`applied_count`** — MCP側が `len(document.log.entries)` を返していたので**undoで減り**、
  `Session._applied_count`(undo/redoで動かない)を返すUI側とずれた。2026-08-28に
  `Session.applied_count` プロパティを足して両方をそこへ通した(コミット `1557442`)。

**揃える向きは「UIの保存済み判定が依存しているほう」で決まった** —— `web/src/App.svelte` は
「undoで保存時点へ戻した文書」と「undoしてから別のverbを打った文書」を見分けるために
`applied_count` が undo/redo で動かないことに依存している。ログの長さへ揃えるとUI側の保存
判定を作り直すことになる。

**再発の型**: `describe_document` の返り値に新しいキーを足すとき、`Session` に同じ値の
プロパティがあるなら必ずそれを通す。1件目(`can_undo`)を直したときに2件目を揃え忘れたのが
今回であり、**同じ関数の中に前例のコメントがあっても揃え忘れは起きる**。
