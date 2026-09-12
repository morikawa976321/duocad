---
name: duocad-toolbar-command-stays-armed-after-commit
description: duocadのUI検査の落とし穴 — ツールバーからコマンドを起動すると確定後も既定コマンドへ戻らず、次のコマンドの入力欄が出ない
created: 2026-08-15
updated: 2026-08-15
---

# duocad: 確定してもコマンドは既定へ戻らない(実機検査で踏んだ)

`web/src/commands/CommandHost.svelte` の `commit()` は入力欄を `reset()` するだけで、
**`commandId` を既定(`DEFAULT_COMMAND_ID` = `extrude_face`)へ戻さない**。戻すのは `abort()`(Esc)だけ。

そのため、ツールバーのボタンで何かのコマンド(例: 「STEP読み込み」)を起動して Enter で確定すると、
**そのコマンドが構えたまま**になる。この状態で面をクリックしても押し出しの距離入力
`#distance` はDOMに存在せず、puppeteer が `No element found for selector: #distance` で落ちる。

- 2026-08-15に `smoke.mjs` を「アプリ内でSTEPを読み込んでから押し出す」形へ書き換えた際に踏んだ。
  **対処は読み込み直後に `Escape` を1回送る**こと(`#file-path` がDOMから消えるのを待つと確実)。
- 人が手で触るぶんには「次に押したいボタンを押す」ので気づきにくい。**自動検査でだけ露わになる**類の癖。
