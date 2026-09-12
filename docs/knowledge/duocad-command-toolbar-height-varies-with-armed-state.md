---
name: duocad-command-toolbar-height-varies-with-armed-state
description: 落とし穴(2026-08-28・duocadで実測) — コマンドを起動/中断するとツールバーの行数(=高さ)が変わり、.viewportのbounding rectを使い回すクリック座標がずれる。使う直前に測り直す必要がある
created: 2026-08-28
updated: 2026-08-28
---

# duocad: コマンドの起動状態でツールバーの高さが変わり、キャッシュした座標がずれる

## 経緯

`20260827-2322-duocad-idle-default-command` で通常モードの既定を「無起動」に変えた
(`CommandHost.svelte`)。無起動では引数の行(数値・選択肢)と `#command-abort` の行が
まるごとDOMから消えるので、**コマンドを起動する/しないでツールバーの行数=高さが変わる**
ようになった(以前は既定コマンドが常に構えていたので、この行はほぼ常に存在した)。

## 実測で踏んだ症状

`web/scripts/smoke-hover.mjs` はページ読み込み直後(=無起動、行が無い最短の状態)に
`.viewport` の `getBoundingClientRect()` を1回だけ測って `box` に保存し、以後の全ステップで
`box.left + x` / `box.top + y` を使い回していた。フィレット(`#fillet`)・領域押し出し
(`#extrude-regions`)を起動すると引数の行が増えてツールバーが伸び、`.viewport` が下へ
縮む —— しかし `box` は起動前の値のままなので、**領域をホバーしたつもりが別の面を掴んで
しまい**「領域をホバーしたのに領域が返らない」で赤くなった。

## 教訓

- **3Dビューポートの座標を使う実機検査は、`.viewport` の bounding rect を使い回さない**。
  面・辺の座標は `projectFace`/`projectEdge` 等で「使う直前に」射影し直す流儀が既に
  smoke.mjsにあるが(`locateFace`/`locateFaceWhenStill`)、**box(rectのoffset)自体も
  同じ扱いが要る** —— 中身の座標だけ実測し直しても、足し合わせる原点がずれていれば無意味。
  対処は `viewportBox()` のような関数にして、コマンドの起動/中断をまたぐたびに呼び直す。
- **コマンドを1つも起動していない状態は、これまでで最もツールバーが短い状態になった**
  (2026-08-28以降)。既存の検査が「最初に1回測る」形で書かれていたら、無起動を挟む
  変更のたびにこの穴を疑う。

関連: [[viewport-resize-silently-changes-what-a-click-grabs]](同じ「寸法が変わると
クリックが化ける」系だが、あちらはサイドバー幅、こちらはツールバーの行数が原因) /
[[duocad-toolbar-command-stays-armed-after-commit]]
