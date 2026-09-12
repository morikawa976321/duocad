---
name: duocad-mcp-unknown-arg-is-protocol-error-not-tool-error
description: duocadのMCPサーバは未知の引数をtools/call実行前のスキーマ検査(_check_arguments)で弾き、JSON-RPCのトップレベルerror(-32602)として返す。call_error()ヘルパー(isError封筒)では拾えず、requestで直接見る必要がある
created: 2026-08-27
updated: 2026-08-27
---

`duocad/mcp_server.py` の `McpServer._call_tool` は、ツール本体(`DocumentTools.call`)を呼ぶ**前**に `_check_arguments` でスキーマ突合(必須・未知キー・型)を行う。ここで未知の引数(例: 廃止した `body_ids` を渡す)が見つかると `_RpcError(INVALID_PARAMS)` を送出し、これは `tools/call` の結果(`isError: true` の構造化封筒)ではなく、**JSON-RPCのトップレベル `error`**(`response["error"]["code"] == -32602`)として返る。

`tests/test_mcp_verbs.py` の `call_error()` ヘルパーは `assert "error" not in response` を前提にしており、この種の失敗には使えない(使うとアサーションで落ちる)。未知引数を弾く検査は `request(server, "tools/call", {...})` を直接呼び、`response["error"]["code"]` を見る必要がある。

失敗の3層(プロトコルエラー/ツールのisError/成功)を書き分けるテストを書くときに踏みやすい。

関連: [[duocad-export-defaults-to-visible-bodies]]
