---
name: duocad-mcp-stays-project-scoped
description: 判断(2026-08-26) — duocadのMCP登録はリポジトリ同梱の .mcp.json(projectスコープ)だけにし、userスコープへは登録しない。代わりにClaude Codeをduocadフォルダで開く運用にする
created: 2026-08-26
updated: 2026-08-26
---

# duocadのMCP登録はprojectスコープだけにする

2026-08-26、本人が決定（理由の明言は無し）。

- **`.mcp.json`(リポジトリ直下・projectスコープ)だけを使う。** `claude mcp add --scope user` は
  しない。
- 代わりに**Claude Codeをduocadフォルダで開く**運用にする。`.mcp.json` は起動時のcwdだけを
  見て親を遡らないので、`C:\Users\tm976\mf` で開くと読まれない。

## 将来これを「直そう」としないこと

userスコープへ足せばどのディレクトリからでも繋がるようになるが、**本人はそれを選ばなかった**。
検討時に挙がった、projectスコープだけが持つ性質:

- 登録がリポジトリに載るので、クローンすれば新しいマシンでもそのまま繋がる（userスコープは
  `~/.claude.json` の中にしか無く、マシンを変えると再登録が要る）
- リポジトリを読んだ人（無人drainのrunnerを含む）に接続方法が見える

なおコンテキスト消費は判断材料として小さかった（実測: 遅延読み込みが既定なので、
セッション開始時に載るのはツール名29個731文字＋instructions 836文字だけ。
全スキーマ10,169文字が載るのは `alwaysLoad` か `ENABLE_TOOL_SEARCH=false` のときだけ）。

関連: [[duocad-mcp-http-transport-decided]] / [[mcp-http-minimal-subset-works]]
