---
name: duocad-smoke-tests-run-against-stale-web-dist
description: duocadの実機smoke検査はweb/distを静的配信するため、npm run buildを挟まないとSvelte変更前の挙動のまま緑になる
created: 2026-08-27
updated: 2026-08-27
---

`duocad/server.py`(`duocad.mcp_server`が使う)は `web/dist` を静的配信する。`web/dist` は
`.gitignore` 対象のビルド成果物で、`web/src/*.svelte` を編集しても **`npm run build`
(`cd duocad/web && npm run build`)を挟まない限り実機には反映されない**。

2026-08-27、`ParametersPanel.svelte` を削除して `SketchPanel.svelte` に反復のパラメータ行を
追加するタスクで、`web/dist` を再ビルドせずに `acceptance.mjs` / `smoke-files.mjs` /
`smoke-regions.mjs` を実行したところ**3本とも緑になった**。しかし実際には
`ParametersPanel.svelte` はまだバンドルに残ったままで、新設した反復のパラメータ行(DOM上の
`#param-<patternId>-<key>`)も配信されておらず、**既存のentity単体編集(以前からあった
`#param-<entityId>-<key>`)だけで通っていた見せかけの合格**だった。`npm run build` を挟んで
再実行すると、新しいDOM要素が正しく現れ、以前は失敗していたはずの検査が正しく検証対象を
捉えるようになった。

**教訓**: duocadでSvelteコンポーネントを変更するタスクでは、実機smoke検査(`web/scripts/*.mjs`)
を「緑になった」で信用する前に、**直前に `npm run build` を実行したか**を必ず確認する。
`npm run check`(tsc/svelte-check)はソースを直接見るのでこの罠に引っかからないが、
実機検査(puppeteer)はビルド成果物しか見ないため独立して罠にかかる。
