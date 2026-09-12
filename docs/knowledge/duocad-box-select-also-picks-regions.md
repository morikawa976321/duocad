---
name: duocad-box-select-also-picks-regions
description: 実機検査の落とし穴2件(2026-09-06・smoke-move.mjs) — スケッチモードのウィンドウ選択(左ドラッグ)は囲んだ図形だけでなく領域(region)の参照も選択に入れるので、「図形1つだけ選ばれた」をitems.every(...)で書くと落ちる(item.entity !== undefinedで先に絞る)。選択の正はコアなので、クリック直後にwindow.__duocad.selectionを読むと往復前の値を見る(必ずwaitForFunctionで書く)
created: 2026-09-06
updated: 2026-09-06
---

# ウィンドウ選択は領域も一緒に拾う / 選択はクリック直後には反映されていない

実機検査(`web/scripts/smoke-move.mjs`)で長方形を**図形まるごと**選ぼうとして2回続けて詰まった。原因は別々の2つ。

## 1. `onBoxSelect` は領域も選択に入れる

左ドラッグのウィンドウ選択は、完全包含された**面・辺・頂点・領域・スケッチ図形**を全部拾う(`App.svelte` の `onBoxSelect`)。長方形を囲むと、その長方形が作っている**領域の参照**も一緒に選択へ入る。

したがって「長方形1つだけが選ばれた」を

```js
items.every((item) => ids.has(item.entity) && item.point === null)   // 落ちる
```

と書くと、領域の参照が `entity` を持たないので必ず偽になる。**先に `item.entity !== undefined` で図形だけに絞る**(幽霊の側 `ghostSources` も同じ絞り方をしている)。

辺のクリックで長方形を指す道もあるが、辺の上は内側の領域と重なっていて当たりが不安定だった。**囲む(ウィンドウ選択)ほうが確実**。完全包含された図形は `point: null`(=図形まるごと)で入る。

## 2. 選択の正はコアなので、クリック直後に読んではいけない

クリックもドラッグも `modify_selection` をサーバへ投げ、状態が配信されて初めて `window.__duocad.selection` が変わる。`await page.mouse.click(...)` の直後に `page.evaluate` で選択を読むと**往復前の値**が返る。これで「ドラッグが効いていない」と誤読して当たり判定の側を疑いかけた。**選択に関する判定は必ず `waitFor`(`page.waitForFunction`)で書く。**

関連: [[modify-selection-remove-rejects-whole-op-on-hidden-body]](選択が古いまま変わらないもう1つの原因)
