---
name: orbitcontrols-middle-button-is-dolly
description: 実測(2026-08-30) — three.js の OrbitControls の既定は LEFT=ROTATE / MIDDLE=DOLLY / RIGHT=PAN で、中ボタンドラッグはパンではない。CADらしい割り当てにするには3つとも書き換える必要がある
created: 2026-08-30
updated: 2026-08-30
---

# OrbitControlsの中ボタン既定はパンではなくドリー

## 事実(three.js 0.185.1・duocadで実測)

`OrbitControls.mouseButtons` の既定は

    LEFT: MOUSE.ROTATE / MIDDLE: MOUSE.DOLLY / RIGHT: MOUSE.PAN

である。**「中ドラッグ=パン」は既定ではない。** duocadのマウス割り当てを
「左=ウィンドウ選択 / 中=パン / 右=回転」へ変えるとき、タスクの記述は中ボタンを
「既存のままでよい」としていたが、実際には3つとも書き換える必要があった。

左をウィンドウ選択に明け渡すには `LEFT: null` を入れる —— 何か割り当てたままだと、
矩形を引いている間にカメラが動いて「囲ったつもりの範囲」と実際の範囲がずれる。
