---
name: local-llm-drives-duocad-tool-protocol
description: 実測(2026-08-20) — Qwen3.8-27B-Q3_K_M(llama-server)がduocadの29ツールとpropose→commit→verbプロトコルを完遂できた。ローカルLLMがCADエージェントの土台に足りるかという最大の未知が解消
created: 2026-08-20
updated: 2026-08-20
---

# ローカルLLM(Qwen3.8-27B)はduocadの29ツールを完遂できる

2026-08-20に実測。llama-server(port 8080・`-c 16384`)で `Qwen3.8-27B-Q3_K_M.gguf` を立て、
`DocumentTools.descriptors()` をOpenAI互換 `tools` へ詰め替えてエージェントループを回した。
対象は `PC_case_feet.step`(5面6辺)。

**通ったこと**:

- ツール呼び出しが出る(`finish_reason=tool_calls`)。Qwen3.8のテンプレートはツール対応。
- `import_step_editable` が返す参照ボディと編集用ボディを**取り違えない**(説明文だけで理解した)。
- `describe_predicates`(述語の自己記述)を**教えなくても自発的に呼び**、そこから述語JSONを組む。
- **「全部の辺」を選ぶ述語が語彙に無い**問題を `length_gt: 0` で回避し、さらに `describe_shape` の
  `edge_count=6` と選択6件を突き合わせて**自分で検算した**。
- `propose_selection` → `commit_selection` → `fillet(radius=1)` まで到達。操作ログは
  `['import_step_editable', 'fillet']`、面数5→9・体積11538.0→11505.5mm³で幾何も正しい。

**実測値**: 全7ターン170秒。プロンプトは4,005→5,789トークン(16Kに余裕)。
ツール定義29個だけで**3,626トークン**(10,716文字)を毎ターン占める。

設計上の含意: 「提案のままでは verb に渡せない」ゲートは27Bクラスでも理解できる。
[[duocad-wants-in-app-ai-provider-picker]] のローカルLLM側の懸念は解消した。
落とし穴は [[llm-agent-thinking-runaway-on-contradictory-task]]。
