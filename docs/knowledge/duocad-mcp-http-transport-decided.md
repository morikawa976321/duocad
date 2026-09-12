---
name: duocad-mcp-http-transport-decided
description: 判断(2026-08-25) — duocadのMCPにHTTPトランスポートを足す案(保留3段構成の段1)を実装確定、2026-08-26に実装済み。段2+3(claude CLIのサブプロセス起動)の採否は明言なし
created: 2026-08-25
updated: 2026-08-26
---

# duocadのMCP HTTPトランスポートは実装確定

2026-08-25、本人が「段1の実装は確定」と判断した。**段1は2026-08-26に実装済み**（実装の実測は [[mcp-http-minimal-subset-works]]）。
[[duocad-cli-attach-plan-on-hold]] で保留になった3段構成のうち**段1のみ**が動き出す。

- **段1** = duocadのMCPにHTTPトランスポートを足す（`handle()` はトランスポート非依存なので
  8766の静的配信にPOSTを1本生やす）。結果として
  **README 1.1の「Claude Codeにduocadのプロセスを立てさせる」コマンドが廃止**され、
  先にアプリを立てて後からClaude Codeが `--transport http` で繋ぎに来る形になる。
- **段2+3**（AIパネルから `claude -p` をサブプロセス起動・プロバイダ3択）の採否は
  この時点で明言していない。

動機は本人の言葉で「1.1のコマンドを廃止してすべてをアプリ内からできるようにしたい。
ただしあまり美しくない実装になる場合は断念してもいい」。

関連: [[duocad-wants-in-app-ai-provider-picker]] / [[claude-cli-embedding-constraints]] / [[duocad-single-launch-command-only]]
