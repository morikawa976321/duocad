---
name: project-point-does-not-clip-xy
description: duocadのviewport.projectPointはzクリップ(近/遠平面)しか見ず、画面外へ大きくはみ出たxy座標でも値を返す。smokeスクリプトの「画面に映るまでズームする」判定は自前でrect突き合わせが必要
created: 2026-09-11
updated: 2026-09-11
---

`web/src/viewport.ts` の `projectPoint(point)` は `THREE.Vector3.project(camera)` の結果のうち
**`z`(奥行き)が `[-1,1]` の範囲内かだけを見て `null` を返す**。`x`/`y` がどれだけ画面の外へ
はみ出していても(例: NDCで7.5のような値)、そのまま `((x+1)/2)*width` を計算して返す —— つまり
**戻り値が非nullでも、実際には画面外の座標(`.viewport` のrectの外)であることがある**。

`smoke-grid-scale.mjs` の `zoomOutUntilVisible`/`hoverPlaneAt` は元からこれを踏まえて
`at.x >= 0 && at.x <= rect.width && at.y >= 0 && at.y <= rect.height` を自前でチェックしている。
新しいsmokeスクリプトで同じ「ズームアウトして特定の点が画面に映るまで待つ」パターンを書くとき、
`projectPoint` の戻り値の有無だけで「映った」と判定すると、**実際には数千pxも外側にある座標へ
`page.mouse.move` してしまい**、その画素がcanvas外(あるいはウィンドウ外の別要素)にあるため
`pointermove` がcanvasの `renderer.domElement` に届かず、期待した挙動(ホバー・カーソル追従など)が
一切発火しない —— しかも `page.mouse.move` 自体はエラーにならないので、「動いたはずなのに反映され
ない」という原因不明の失敗に見える(`20260910-1615-duocad-grid-follow-cursor` タスクで実際に踏み、
`document.elementFromPoint` でcanvas外の座標だったことを確認して気づいた)。

**教訓**: `projectPoint` を「画面に映っているか」の判定に使うときは、必ず呼び出し側で
`.viewport` の `getBoundingClientRect()` と突き合わせて `x`/`y` の範囲チェックを行う
(`smoke-grid-scale.mjs` の既存パターンを流用する)。
