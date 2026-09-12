---
name: viewport-init-order-tdz-looks-like-a-blank-screen
description: 落とし穴(2026-09-03・duocadで実測) — 巨大な単一関数の初期化コードから後方のconstを読むとTDZで落ち、例外が初期化の途中で投げられるので「画面が出ない・smokeが60秒待って死ぬ」という原因から遠い症状になる
created: 2026-09-03
updated: 2026-09-03
---

duocadの `createViewport()`(`web/src/viewport.ts`)は3500行超の**単一の関数**で、
**宣言の順序がそのまま初期化の順序**である。初期化コードから**後方で宣言している `const`**
を読むと `Cannot access 'X' before initialization`(TDZ)で落ちる。

2026-09-03に実際に踏んだ形: グリッドの描画範囲を「すべてのスケッチとソリッド」から
決めるように変えたところ、初期化中に走る `applyGridPlanes()` が `contentBoxForGrid()` 経由で
`sketchRoot` を読むようになった。`sketchRoot` の宣言は2000行以上あとの「C: スケッチ」節に
あったため、**アプリが起動しなくなった**。

**症状が原因から遠い。** 例外は `createViewport` の途中で投げられるので
`window.__duocad.ready` が永久に立たず、見えるのは「画面が出ない」だけ。実機smokeは
`ready` を待って**60秒でタイムアウト**し、`Waiting failed: 60000ms exceeded` としか言わない。
**型検査(`tsc` / `svelte-check`)は1件も検出しない**(TypeScriptはこの種の順序をエラーにしない)。

## 教訓

- **初期化中に読むものを増やす変更は、宣言順を必ず確認する。** 「関数だから呼べる」は
  巻き上げ(hoisting)の話で、`const` には効かない。
- 依存が増えた側の宣言を**前方へ上げる**のが素直な直し方(`new THREE.Group()` のように
  初期化に依存が無いものは安全に上げられる)。上げたら、元の位置に「上へ上げた」旨と
  理由を1行残す。
- **「画面が出ない・実機smokeが待って死ぬ」を見たら、まず初期化時の例外を疑う。**
  ブラウザのコンソール(`page.on('pageerror')`)には出ているので、smokeが握り潰していないか
  確認する。

関連: [[move-body-edge-pick-fails-on-clean-main]](実機smokeが「静かに失敗する」もう1つの型)
