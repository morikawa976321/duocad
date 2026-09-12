---
name: duocad-cli-attach-plan-on-hold
description: 未決(2026-08-23保留、2026-08-25に段1のみ確定) — duocadのアプリ内AIをClaude Code CLI(サブスク認証)へ繋ぐ3段構成のうち、段1(MCP HTTPトランスポート)は実装済み。段2+3とネイティブアプリ移植案は未決のまま
created: 2026-08-23
updated: 2026-08-26
---

# duocadのCLI接続案は段1だけ動き、段2+3は保留のまま

2026-08-23、duocadのアプリ内AIパネルからClaudeへ繋ぐ手段を検討し、**本人が保留と判断した**
（理由の明言は無し）。**その後、下記3段構成のうち段1のみ2026-08-25に実装確定となり、
2026-08-26に実装済み**（[[duocad-mcp-http-transport-decided]] / [[mcp-http-minimal-subset-works]]）。
段2+3の採否は依然明言されていない。

## 検討した案

サブスク認証のまま使うため、duocadが `claude` CLIをサブプロセスで起動して繋ぐ3段構成:

1. duocadのMCPに**HTTPトランスポート**を足す（`handle()` はトランスポート非依存なので、
   8766の静的配信にPOSTを1本生やす。127.0.0.1 bind＋起動ごとのトークンを
   `--mcp-config` の `headers` で渡して検査）
2. AIパネルの送信で `claude -p --output-format stream-json --mcp-config ... --model ...` を起動し、
   NDJSONの各行をトランスクリプトへ流す
3. プロバイダを3択にする（Claude CLI・サブスク / Claude API・キー / llama-server）

1は単独で価値がある（アプリを先に立てて後からClaude Codeを繋げるようになり、起動順の制約が消える）。

## 併せて宙に浮いている問い

本人は「webアプリであることが邪魔をしているのでは。ネイティブに移植すべきか」と問うた。
**この移植の可否も未決のまま**（保留時点で結論を出していない）。

関連: [[duocad-wants-in-app-ai-provider-picker]]（この案が具体化しようとしていた志向） / [[no-anthropic-api-key-subscription-only]]・[[claude-cli-has-subscription-auth-sdk-does-not]]（CLI経由が唯一のサブスク認証経路、という前提）
