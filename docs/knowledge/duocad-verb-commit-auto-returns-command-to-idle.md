---
name: duocad-verb-commit-auto-returns-command-to-idle
description: 判断(2026-08-31) — duocadのCommandHostは、verbの確定が成功したらツールバーを自動で無起動へ戻す。連続作図・連続フィレットの操作性が失われることを承知で全verb共通。戻すのはサーバの確認(client_op一致)が返った時点であり、commit直後に戻すと失敗時の打ち直し導線が壊れる
created: 2026-09-01
updated: 2026-09-04
---

# duocad: verb確定成功で自動的に無起動へ戻す

本人の判断(2026-08-31): **duocadの全verbで、verbを実行したら中断ボタンを押さなくても自動で「実行中」状態を終える**。

これは既存の設計(`add_line` 等スケッチ作図は確定してもコマンドは構えたままで続けて次の図形を描け、フィレット等も同じ半径を連続適用できた)を覆す変更である。AIから「全verb共通にすると連続作図・連続フィレットの操作性が失われるがよいか」と確認したところ、**それを承知のうえで文字通り全verb共通**を選んだ(範囲を絞る代替案 — フィーチャ系だけ・他を個別指定 — は選ばなかった)。

対象は `CommandHost.svelte`(ツールバー状態機械)を経由する全verb(sketch_create/add_line/add_circle/add_rectangle/add_arc/import_svg/extrude/extrude_face/fillet/chamfer/pattern_rectangular/pattern_circular/move_body/move_sketch_entities/形状最適化4verb)。`set_parameter`/`import_step`/`undo`/`redo` はCommandHostを経由しないため対象外(元々armed状態を持たない)。

起動時の既定が「見ているだけ」であることを決めた [[duocad-idle-default-state]] の続き(あちらは起動直後の既定、こちらは確定後の戻り先)。

## 実装上の注意: 戻すのはサーバ確認の後(実機で踏んだ)

最初は `commit()`(`onRun()` を呼んだ直後。サーバへの発火は楽観的で成否はまだ分からない)の中で即座に無起動へ戻す実装にし、実機smoke(`web/scripts/smoke-features.mjs`)で即座に壊れた。OCCTのフィレットは限界半径で失敗しうる([[occt-fillet-fails-at-the-exact-limit-radius]])ため「失敗したら打った半径の入力欄をそのまま残し、近くの値で打ち直す」が必須ワークフローだが、`commit()` と同時に `commandId = null` へ落とすと、失敗応答で値を復元(`restoreLastInputs()`)しても**入力欄自体がDOMから消えている**ため反映されない。

正しい実装は、応答の `client_op` が自分の出した操作と一致したときにだけ状態遷移を発火させる(既存の `pendingOp` 相関をそのまま流用)。一般に、楽観的発火のUIに後から足す「成功したら○○する」系の遷移は、**発火のタイミングではなくサーバの確認が返ったタイミング**に置く。

## 検査側の取り残し

`web/scripts/smoke-regions.mjs` は「領域を押し出して確定した後もコマンドは構えたまま(`wantsRegions === true`)」という2026-08-30時点の仕様を期待しており、この変更(duocad commit `1cf190d`、2026-09-01)以降ずっと赤。**機能は壊れておらず検査の期待が失効している**(2026-09-03に `main` の再ビルドで同じ失敗が出ることを確認。残り15本の実機smokeと `acceptance.mjs` は緑)。直すときは検査側を今の仕様へ書き直す。
