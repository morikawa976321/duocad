---
name: duocad-parameters-panel-deleted-for-sketch-panel
description: 判断(2026-08-26) — duocad右サイドバーのParametersPanelを削除し、その固有物である反復(pattern)の寸法の打ち直し口をSketchPanelへ移す。非アクティブなスケッチのパラメータへ直接触れなくなる損失は受け入れる
created: 2026-08-26
updated: 2026-08-26
---

duocadの右サイドバーにある「パラメータ」と「スケッチ」の重複をユーザーが指摘し、
**ParametersPanelを削除してSketchPanelへ寄せる案を採用**と決めた（提案どおり実装、と明言）。

決まったこと:

- `web/src/ParametersPanel.svelte` は削除する。
- そのパネルだけが持っていた**反復(pattern)の寸法**（矩形は `dx`/`dy`/`nx`/`ny`、円形は
  `cx`/`cy`/`count`/`total_angle`）の打ち直し口を SketchPanel へ移す。
- **非アクティブなスケッチのパラメータに直接触れなくなる**（「再開」でそのスケッチに入る
  1手が挟まる）。この損失は受け入れる。
- パラメータの改名機能と、2つの図形が同じ `d{n}` を共有する形は v1範囲外のまま。

一覧パネルを消して実物の側へ寄せる形は、[[duocad-files-panel-selection-panel-verdict]]・
[[duocad-history-panel-removed-from-ui]]・[[duocad-ui-audit-seven-verdicts]]（領域一覧の削除）と
同じ向きで、[[duocad-ui-interaction-principles]] の延長にある。

実装は taskbox の queue に積んだ（2026-08-26）。実行状態なので詳細はそちらが持つ。
