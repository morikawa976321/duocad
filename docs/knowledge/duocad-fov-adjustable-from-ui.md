---
name: duocad-fov-adjustable-from-ui
description: 判断(2026-08-27〜2026-09-04) — duocadのビューポートカメラのFOVは0(完全正投影)/30〜90度を内部APIが受け付ける。**UIは2026-09-04に正投影/透視30度の2値だけへ縮小**(スライダーは廃止・既定は30)。距離補正あり(見かけの大きさを保つ)、値はlocalStorageに保存
created: 2026-08-27
updated: 2026-09-05
---

duocadの3DビューポートのカメラFOV(従来 `PerspectiveCamera(45, ...)` 固定)を、UIから調整できるようにすると本人が決定(2026-08-27)。仕様は以下の3点も本人が確定した。

- **内部APIが受け付ける範囲は0または30〜90度**(`viewport.ts` の `isSelectableFov`)
- **距離補正あり** — FOVを変えたとき `tan(fov/2) × 注視点までの距離` を一定に保ち、カメラを `controls.target` 方向へ押し引きする。パースだけが変わり、画面上の見かけの大きさは変わらない
- **値はlocalStorageへ保存する** — duocadで localStorage を使う最初の1件になる

FOVは文書の状態ではなく画面の設定なので、コア(Python)にも操作ログにも入れない。verbにもしない([[cad-verb-granularity-rule]] の「1 undo/ログ1行/MCP1呼び出し」に照らして、undo対象でもMCPから触るものでもないため)。

UIの置き場所は設定モーダルに決着([[duocad-low-frequency-settings-in-a-modal]])。

## 完全正投影は「FOV 0」として追加(2026-08-28)

スライダー実装の数時間後、本人が**完全正投影の状態も選べるようにする**と決定した。
**選べる値の形そのものを本人が指定した**: 「0度と30〜90が選べるようにしてほしい」——
つまり **0 と 30〜90 の2択構成で、あいだの1〜29という値は無い**。理由の明言は無い。

実装上の帰結(本人の指定ではなくこちらの判断): 0度は近似ではなく
`OrthographicCamera` そのものへ差し替える。

## UIは正投影/透視30度の2値へ縮小、既定は30(2026-09-04決定・2026-09-05実装)

本人の実使用で「30/40/50のあいだには実用上の差が無く、90は使う角度ではない」ことが
分かったため、**画面のスライダー(`#settings-fov`)と現在値表示を撤去し、`#settings-ortho`
チェックボックス1つに縮めた**。既定は「45度は歪みすぎる」という判定により**30度へ変更**
(`FOV_DEFAULT`)。判断の詳細・弁別閾との違いは[[duocad-fov-ui-reduced-to-binary]]。

**内部API(`setFov`/`isSelectableFov`)は変えていない**——0または30〜90を今も受け付ける。
`web/scripts/smoke-files.mjs` の45/47/49/51/53/55度スイープ
([[duocad-coplanar-base-plane-pick-is-decided-by-the-last-bits]]の回帰検査)は
UIから到達できない値をコンソール(`window.__duocad.setFov()`)から使い続けている。
正投影を解くと角度は常に`FOV_DEFAULT`(30)に戻る——「戻る先の角度を覚える」仕組み
(`App.svelte`の`fovAngle`)はUIが2値になったことで不要になり削除した。

**既定FOVを変えると、それだけで済まない場所が1つあった**:
[[fov-default-change-exposed-a-duplicate-hardcoded-camera-distance]]。
