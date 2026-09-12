---
name: same-canvas-second-pass-also-eats-clicks
description: 運用知見(2026-09-03・duocadビューキューブ実装で実測) — 同一canvasへ第2パスで描くオーバーレイ(setViewport+setScissor)は、elementFromPointが常にcanvasを返すため落とし穴[[duocad-floating-panel-eats-viewport-clicks]]のelementFromPoint判定をすり抜ける。判定の一本化に矩形除外を足す必要がある
created: 2026-09-03
updated: 2026-09-03
---

# 同一canvasの第2パスも、浮かせたDOMパネルと同じ症状を起こす

[[duocad-floating-panel-eats-viewport-clicks]] は「別のDOM要素を重ねると `elementFromPoint` が
canvasを返さなくなる」という前提で `clickable(x,y)` の判定(`document.elementFromPoint(...) ===
renderer.domElement`)を導入した。**ビューキューブ(`20260903-0309-duocad-view-cube`)のように、
別DOM要素を使わず同一canvasへ`setViewport`/`setScissor`で第2パス描画する実装では、その画素も
`elementFromPoint` は変わらずcanvasを返す** —— つまり**判定が常に「取れる」で固定されてしまい、
実際にはキューブが吸うはずのクリックが、浮かせたパネルの場合とは逆に、素通りしてモデルのピック側へ
届いてしまう。

## 対応

`clickable(x,y)` の**入口を1つに保ったまま**、`elementFromPoint` の判定の前に矩形の明示的な除外を
足した(`web/src/viewport.ts`):

```ts
function clickable(x: number, y: number): boolean {
  if (insideCubeRect(x, y)) return false
  const rect = container.getBoundingClientRect()
  return document.elementFromPoint(rect.left + x, rect.top + y) === renderer.domElement
}
```

これにより `project*` 系(`project`/`projectEdge`/`projectBasePlane`/`projectRegion` など)が
**自動的に**キューブの矩形を候補から除外するようになる(候補が複数ある射影は次の候補へ逃げる ——
[[duocad-floating-panel-eats-viewport-clicks]] の「候補が複数あるかどうかで結果が割れる」節と同型)。
`clickable()` という**単一の choke point** に判定をまとめてあったからこそ、この修正だけで
ピック・矩形選択開始・ホバー予告のすべてに波及した。

## 教訓

「クリックを吸う」原因はDOM要素の重なりだけではない —— **同一canvasの複数パス描画(第2ビューポート)
も同じ症状を起こす**。`elementFromPoint` は「別のDOM要素に覆われていないか」しか見ないので、
canvas自身の中でパスが競合するケースはこの判定の外側にある。`clickable()` のような一本化した
判定点を持つ設計なら、新しい種類の「吸うオーバーレイ」が増えるたびにそこへ条件を足すだけで済む。

関連: [[duocad-floating-panel-eats-viewport-clicks]] / [[duocad-view-cube-adopted]] /
[[duocad-depth-overlay-rule]]
