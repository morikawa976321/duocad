---
name: duocad-modes-lifted-above-verbs
description: 判断(2026-09-03) — duocadのモード(通常/スケッチ/形状最適化)をverbより1つ上の次元に揃える。スケッチの入口だけがverbの行に残っていたのをモードの行へ移す。コアとMCPにはモードを持ち込まない
created: 2026-09-03
updated: 2026-09-03
---

# duocad: モードをverbより1つ上の次元に揃える

本人の判断。**画面のモード(通常 / スケッチ / 形状最適化)をverbの上位次元として扱う**と決めた。

決めた内容:

- **スケッチの入口をverbの行からモードの行へ移す。** 形状最適化は入口も出口も既にモードの行
  (`.mode-row`)にいたが、スケッチだけは**入口が `sketch_create` というverbのボタン**として
  押し出し・フィレットと同じ行に並んでいた(出口の「抜ける」は既にモードの行にいた)。
  非対称だったのは入口だけである。
- **コアとMCPにはモードを持ち込まない。** `duocad/` 側にモードの概念を作らない(`Document` に
  フラグを置かない・`enter_sketch`/`exit_sketch` verbを作らない・MCPのツール集合をモードで
  変えない)。モードは画面だけの状態のままにする。
- **手数は増やさない。** モードセレクタや常設のモード切替バーは作らない。スケッチは
  「ボタン→貼る先をクリック」の2手のまま([[duocad-sketch-start-is-two-steps-by-pointing]])。

実装したこと(2026-09-03、同じ会話の中で):

- `App.svelte` のモードを**タグ付きユニオン** `{kind:'idle'} | {kind:'sketch-pending'} |
  {kind:'sketch',id} | {kind:'optimize'}` の1変数にし、遷移を `setMode()` 1本へ寄せた。
  以前の `sketchId`(string|null)と `optimizeModeRequested`(boolean)の独立2変数、および
  「両立を防ぐ」ための派生値ガードは消えた(不正状態が表現できなくなったため)。
- `sketch-pending`(スケッチモードだが貼る先未定)を新設。**新しい状態ではなく**、
  「通常モードで `sketch_create` を起動している間」をモード側へ持ち上げただけ。
- 選択の掃除規則を**「行き先が使えない参照を落とす」の1つ**に統一(以前は4つの遷移関数が
  それぞれ別の流儀で掃除しており、それが [[duocad-sketch-switch-keeps-foreign-selection]] の
  抜け道を生んでいた)。
- 「スケッチを抜けたら最適化モードへ戻る」という挙動は、以前は独立ラッチから偶然そうなって
  いただけだったので、`resume` として型に持たせて明示した。

関連: [[cad-verb-granularity-rule]](モード遷移は「1回のundo/ログ1行/MCP1呼び出し」のどれとも
一致しないのでverbではない)、[[duocad-optimize-mode-contract]]、[[duocad-idle-default-state]]
