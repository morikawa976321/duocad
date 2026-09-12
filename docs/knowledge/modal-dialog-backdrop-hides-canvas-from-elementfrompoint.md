---
name: modal-dialog-backdrop-hides-canvas-from-elementfrompoint
description: 落とし穴(2026-08-28・duocadで実測) — native <dialog> の showModal() を使うと、画面のどこを指しても document.elementFromPoint が <dialog> を返す。「覆っているか」で判定していないコードまで巻き添えで死ぬ
created: 2026-08-28
updated: 2026-09-04
---

native `<dialog>` を `showModal()` で開くと、**::backdrop が top-layer で画面全体を占める**ため、
`document.elementFromPoint(x, y)` が**画面のどこを指しても `<dialog>` 要素を返す**。
ダイアログ自身の矩形がその点に重なっていなくても返る(`getBoundingClientRect()` は重なりを
否定しているのに、`elementFromPoint` は肯定する)。

duocadで実測した影響: `viewport.ts` の `clickable()` は
`document.elementFromPoint(...) === renderer.domElement` で「その画素が本当に押せるか」を
判定しており([[duocad-floating-panel-eats-viewport-clicks]])、**モーダルを開いている間は
`project*` が全部 null を返す**。3Dのピックも全部死ぬ。画面上はビューポートが普通に見えている
(ダイアログは端に小さく出ており、`::backdrop` は透明にしてある)ので、**見た目からは
何も塞がれていないように見える**のが厄介である。

実機検査への含意: **モーダルを開く手を足したら、次の手の前に必ず閉じる**。閉じ忘れると
「クリックが全部効かない」形で、モーダルと無関係な検査が落ちる。

**duocadの設定ダイアログはこの罠の実例だったが、2026-09-04に非モーダル(`show()`)へ変えて
外した**([[duocad-settings-dialog-goes-non-modal]])。同日の実測では `elementFromPoint` が
ビューキューブの画素で `CANVAS` を返し、開いたままクリックも回転も効く。**一般の落とし穴
としては有効**なので、他の `showModal()` を足すときは同じことが起きると考えてよい。
