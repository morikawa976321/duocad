---
name: duocad-ai-runs-on-the-working-machine
description: 判断(2026-08-22) — duocadをwebアプリ化しても、AIはClaudeもローカルLLMも「作業しているマシン」で実行する。サーバ側に置いたAIを共用する形は採らない
created: 2026-08-22
updated: 2026-08-22
---

# duocadのAIは作業するマシンで実行する

本人が2026-08-22に確定:

- duocadをwebアプリとしてデプロイしても、**AIはClaudeにせよローカルLLMにせよ、
  作業しているマシンで実行する**
- サーバ機に置いたAI（ローカルLLM等）を全端末から共用する形は**採らない**

## 効いてくること

サーバとAIが別マシンに分かれるため、**LLM呼び出しの接続の向きを反転させる必要がある**
（サーバがクライアント側のLLMを呼びに行けない）。duocadの現行実装は
AgentLoopがPythonサーバ側でLLMを叩く作りなので、ここが設計上の分岐点になる。

[[duocad-wants-web-deploy-remote-save]] の「借り物のPCでも使いたい」と組み合わせると、
ローカルLLM経路は自分の端末（インストール可）でだけ成立し、借り物のPCでは
Claude側の経路だけが残る。
