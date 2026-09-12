---
name: smoke-move-first-check-flakes-on-camera-drag
description: 落とし穴(2026-09-06・duocad実機検査) — smoke-move.mjsの最初の検査「左ドラッグでカメラが動かない」が間欠的に落ちる。変更と無関係に赤が出るので、原因の切り分けを自分の差分から始めない
created: 2026-09-06
updated: 2026-09-06
---

`web/scripts/smoke-move.mjs` の**最初の検査**（左ドラッグでカメラが動かないこと）が
**間欠的に落ちる**。2026-09-06の長方形の角度対応のタスクで、変更とまったく無関係な形で
1回踏んだ（凍結後の通し1回目で赤、再実行で緑）。

**赤が出たときに自分の差分を疑うところから始めない。** この節だけの赤なら、まず
もう一度通す。原因は未特定で、直っていない。

同じファイルの別の flake は2026-09-03に解決済みだが（[[move-body-edge-pick-fails-on-clean-main]]）、
それとは別物である。赤の切り分けの型（pristine と変更後をそれぞれ2回以上）は
[[first-red-run-is-not-always-your-own-change]]。
