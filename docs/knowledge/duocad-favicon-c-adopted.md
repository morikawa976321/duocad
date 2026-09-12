---
name: duocad-favicon-c-adopted
description: 判断(2026-08-26) — duocadのブラウザタブアイコンは6案のうちC案「提案の重なり」(確定した形のamber塗り+AI提案ゴーストのcyan線)を採用。タブのタイトルは「duocad M1」から「duocad」へ
created: 2026-08-26
updated: 2026-08-26
---

duocadのファビコンを6案(面取りとフィレット/アイソメ立方体/提案の重なり/スケッチのd/原点ギズモ/半分は実体・半分は線)から選び、**C案「提案の重なり」を採用**した。理由の明言は無い。

図柄は角丸矩形2つだけ。手前に確定した形(塗り `#ffb454` = styles.css の accent、viewport.ts の `HIGHLIGHT_COLOR`)、奥にAI提案のゴースト(線 `#36d1ff` = viewport.ts の `PROPOSED_COLOR`)。propose→commit の契約を1枚にしたもので、色はアプリ画面から取っている。

実装は `web/public/favicon.svg` 1枚と `web/index.html` の `<link rel="icon" type="image/svg+xml">` 1行。同時に `<title>` を「duocad M1」から「duocad」へ変えた(v1到達済みでM1表記が古いため)。**本番は `web/dist` を配る**ので、反映には `npm run build` が要る。

調整の過程では「ゴーストを手前に置いて重なりを溝(kerf)で抜く」案も作ったが、**塗りを欠けさせないため奥に置く**形に落ち着いた。確定した形が無傷で手前にあるほうが意味とも合う。
