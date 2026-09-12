---
name: mcp-changes-broadcast-without-applied
description: 落とし穴(2026-08-30・duocadで実測) — MCP起点の文書変更はどのブラウザ接続にも`applied`を付けずに配信される。`applied.verb`分岐を試す実機smokeはMCP経由の呼び出しでは検査したい経路を一度も通らない
created: 2026-08-30
updated: 2026-08-30
---

duocadの `duocad/net/ws.py` は、**文書を変えた主体がMCP(サブスクのAI・スクリプト)か、
ブラウザ自身のWebSocket接続か**で配信の形を変える。

- **ブラウザ自身のWS接続からのverb呼び出し**: `_dispatch` が `applied` 付きで配信し、
  操作を出した**その接続だけ**が `applied`(`{verb, ...}` や `{change: "document"|"selection"|"visibility", ...}`)
  を受け取る。他の接続には同じ `state` だけが届く。
- **MCP経由の変更**(`Session.import_step()` を `mcp_server.py` から直接呼ぶ等):
  `_on_core_change` → `_spawn_push()` が `self._push(None, applied=None)` を呼ぶので、
  **全接続へ `applied: None` のまま配信される**。`_on_core_change` のdocstring曰く
  「MCPからの変更は誰の操作でもない外からの変化であり、`applied` は操作を出した
  クライアントへの確認応答だから」。

`web/src/App.svelte` の `handleState()` は `applied.verb === 'import_step'` のような
判定で「この操作に反応して何かする」分岐を作る(例: verb応答限定の副作用)。**この種の
分岐を実機smokeで検査するとき、`mcp.callTool('import_step', ...)` のようにMCP経由で
操作を起こすと、`applied` が一度も乗らないので検査したい分岐そのものを一度も通らない
まま、別の経路(例: `viewport.update()` の一般的な初回フレーミング)だけで緑になって
しまう**(空振りの検査。`web/scripts/smoke-reframe-on-import.mjs` を書く過程で実際に
踏んだ: `viewport?.frameAll()` を足したのに `mcp.callTool('import_step', ...)` で
検査したらカメラの移動量が0mmのままで、原因を辿ってこの仕様に気づいた)。

**対策**: `applied.verb`/`applied.change` に依存する挙動を検査するときは、
`smoke-files.mjs` と同じく**本物のDOM操作(ファイルパネルの入力欄+クリック)で、
ブラウザ自身のWS接続から**操作を起こすこと。MCPはあくまで「AIが操作したら
ブラウザの画面が変わる」という別の経路を検査するときに使う(`smoke-mcp.mjs` は
むしろこの仕様を逆手に取って、その経路そのものを検査対象にしている)。
