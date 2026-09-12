---
name: duocad-set-parameter-becomes-ai-only
description: 波及(2026-08-30) — スケッチパラメータパネルの廃止で duocad の set_parameter は人間側のUIを失い、AI(MCP)専用の操作になった。到達表は「20操作・人間19/20・AI20/20」へ変わった
created: 2026-08-30
updated: 2026-08-30
---

# set_parameterは人間側の口を失った(AI専用になった)

## 事実

[[duocad-sketch-panel-replaced-by-direct-selection]] の実装で `SketchPanel.svelte` を削除した
結果、**寸法(半径・幅高さ・角度)を人が打ち直す口がアプリから無くなった**。位置の直しは
`move_sketch_entities` / `move_body` が引き受けるが、寸法の編集口は今回作らないと決めてある
([[duocad-move-verb-split-sketch-and-body]] のスコープ外)。

## 波及

- 受入チェック(`web/scripts/acceptance.mjs`)の到達表は **20操作**になり、`set_parameter` の
  人間列だけが空欄(`AI_ONLY`)になった。人間 19/20 ／ AI 20/20。
- 実機検査で「打ち直したら形が追随する」を見ていた3箇所(`smoke.mjs` / `smoke-files.mjs` /
  `smoke-regions.mjs`)は、UIが無くなったのでMCP経由で打つ形へ移した。
- `set_parameter` の口(`Session.set_parameter` / MCPツール / 配信の `refs`)はどれも残っている。
  消えたのは画面の入力欄だけである。

## 次に寸法編集を作るとき

「寸法を変えると押し出し済みソリッドの再計算が絡む」が見送りの理由なので、そこを決めるのが先。
