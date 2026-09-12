---
name: duocad-wants-in-app-ai-provider-picker
description: 志向(2026-08-20) — duocadを「先にアプリを開き、アプリ内UIからAIを選んで接続する」形にしたい。当面はClaudeとllama-serverの2つ。現行のMCPサーバ方式(Claude Codeがプロセスを所有)への不満が動機
created: 2026-08-20
updated: 2026-08-20
---

# duocadのAI接続をアプリ内から選ぶ形にしたい

(2026-08-27追記: 「当面はClaudeとllama-serverの2つ」のうちClaudeのAPIキー経路は廃止された → [[duocad-drops-anthropic-api-key-path]])

2026-08-20、本人が希望として表明。「もう少し柔軟にしたい。まずアプリを開いて、アプリ内UIから、
claudeやローカルLLMやその他のAIを自由に選んで接続したい」。

- **当面の対象は Claude と llama-server の2つでよい**（「その他のAI」は将来の含み）。
- 動機は現行方式の硬さ。duocadはMCPサーバなので**MCPクライアント(Claude Code)がプロセスを所有**し、
  起動トリガーがClaude Codeのセッション開始に、人間用UI(8766)の寿命がセッション寿命に縛られる。
  AI無しで触るには別の立て方が要り、その立て方だと後からAIが繋がらない（排他）。
- 実装の向きとしては、duocadがMCPサーバから**AIの呼び出し側(エージェントループの持ち主)**に回る反転になる。

未決（この時点では方針表明のみで、着手の判断はしていない）。

関連: [[duocad-single-launch-command-only]] / [[cad-manual-ai-division-principle]]
