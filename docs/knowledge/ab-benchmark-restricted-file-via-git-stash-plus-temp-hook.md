---
name: ab-benchmark-restricted-file-via-git-stash-plus-temp-hook
description: 触ってよいファイルが1つに限られる実機A/B計測は、その1ファイルにだけ一時フックを生やし、git stash push/pop で新旧のコードを切り替えて同じブラウザ検査を2回走らせれば足りる(duocadで実施)
created: 2026-09-04
updated: 2026-09-04
---

duocadの `viewport.ts` 単体の性能改修タスクで、「触ってよいのは `viewport.ts` のみ・
`App.svelte` は禁止」という制約下で実機A/B計測(修正前後の実時間比較)が必要になった。
既存の計測穴 `window.__duocad` は `App.svelte` の `$effect` が毎回まるごと作り直す
オブジェクトなので、そこへ計測用フィールドを足すには `App.svelte` を触る必要があり、
制約に抵触する。

**やった手順**(`App.svelte` に一切触れずに前後を比較):

1. `viewport.ts`(触ってよいファイル)の `return {...}` の直前に、`window` へ直接
   計測フィールドを生やす1行を足す(例: `window.__duocadPerfCollectInBox = (rect) =>
   collectInBox(rect)`)。`App.svelte` の `__duocad` オブジェクトとは別物として共存する。
2. 修正後のコード + このフックで `npm run build` → 実機で計測 → `after` を記録。
3. `git stash push -- web/src/viewport.ts` で修正を退避し、コミット済み(修正前)の
   ファイルへ戻す。
4. 同じフックをもう一度(手作業で)同じ場所に足し、`npm run build` → 同じ実機検査を
   再実行 → `before` を記録。
5. `git checkout -- web/src/viewport.ts` でフック入りの修正前ファイルを捨て、
   `git stash pop` で本来の修正(フック無し)を復元。
6. 最後にフックを手で消してから最終ビルド・型検査・smokeを回す(計測用の口は
   コミットしない)。

**成立条件**: `web/dist` は静的配信されるビルド成果物なので、フックの有無を
切り替えるたびに **必ず `npm run build` を挟む**([[duocad-smoke-tests-run-against-stale-web-dist]])。
挟み忘れると前回のビルドのまま計測してしまい、numberが変わらない/フックが
見つからないという分かりやすい形で失敗する(今回も1回踏んだ)。

**利点**: 別セッションが `git stash@{0}`(`qa-temp`)を退避済みでも、`stash push -- <path>`
は対象パスだけを新しいstashスロットへ積むので**他セッションの退避物には触れない**
([[git-stash-hides-other-sessions-uncommitted-work]])。`stash list` で自分のスロットが
どれか確認してから `pop`/`checkout` すること。
