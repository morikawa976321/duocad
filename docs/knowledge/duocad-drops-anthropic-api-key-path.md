---
name: duocad-drops-anthropic-api-key-path
description: 判断(2026-08-27) — duocadのアプリ内AIから「ClaudeをAPIキーで使う」経路(anthropicプロバイダ)を完全廃止(本人が「不要」と明言)。Claudeを使う道はMCP側だけになる。プロバイダ選択の仕組み自体は畳まず次の差込口として残す(A案・本人選択)
created: 2026-08-27
updated: 2026-08-28
---

# duocadのAPIキー経由のClaude利用を廃止

本人の判断（2026-08-27）: duocadで **ClaudeをAPIキーで設定して使う使い方は「不要」なので完全に廃止する**。

対象は `duocad/agent/providers.py` の `anthropic` プロバイダ一式（`AnthropicProvider`・`load_api_key`/`api_key_configured`・`ANTHROPIC_API_KEY` と `duocad_secrets.json` の読み口）と、その上に載る `server.py` の `ai_status.api_key_configured` / `ai_configure` のプロバイダ分岐、AIパネルのプロバイダ選択。

- 廃止後、**アプリ内AIパネルに残るのは llama-server（`openai_compat`）だけ**になる。
- **Claudeを使う道が無くなるわけではない** —— MCP経路（サブスク認証のClaude Codeが `/mcp` へ繋ぐ）は残る。[[duocad-mcp-http-transport-decided]]・[[duocad-mcp-stays-project-scoped]]
- 実質的な機能喪失はゼロ。本人はAPIキーを持たず（サブスク契約のみ）、`duocad_secrets.json` も環境変数も未設定で、既定プロバイダは元から `openai_compat` に落ちていた。
- [[duocad-wants-in-app-ai-provider-picker]]（アプリ内でAIを選ぶ）の「当面はClaudeとllama-serverの2つ」という前提のうち、Claude側がこの判断で消える。復活の芽があるとすれば [[duocad-cli-attach-plan-on-hold]] の段2+3（claude CLIのサブプロセス起動＝サブスク認証）であり、APIキー経路ではない。

## プロバイダ選択の仕組みは畳まない（実装の2案から本人がAを選択）

- **A（採用）** — `anthropic` アダプタと鍵の読み口だけ抜く。`Provider` / `build_provider` / `ai_configure(provider=…)` / `ai_status.providers` は残す。画面のプロバイダ選択UIは `providers` の長さで駆動し、**1つのときは出さない**（サーバ側の一覧に2つ目が入れば自動で出る）。
- **B（不採用）** — WSプロトコルから `provider` を落とし、AIパネルをllama-server固定にする。

派生する規律: **鍵を持つプロバイダを足すときは `Server._ai_configure` に `base_url` の拒否を書き戻す**。今 `base_url` を自由に差し替えられるのは、残ったプロバイダが鍵を持たないからにすぎない（送り先を画面から選べれば、値をブラウザへ出していなくても外部へ送られるのと同じになる）。この条件はコードとテストの両方にコメントで残した。
