---
name: flex-column-reverse-unscrollable-overflow
description: flex column-reverse + justify-content:flex-end のスクロール容器は溢れた分に一切スクロールできない（duocad EのQAで実測。実ブラウザのsmokeテストも素通りした）
created: 2026-08-14
updated: 2026-08-14
---

# flex の column-reverse + justify-content:flex-end は溢れた分へスクロールできない

duocad タスクEのQA（2026-08-14）で、操作履歴パネルの `.list`（`overflow:auto; flex-direction:column-reverse; justify-content:flex-end`）を headless Edge で実測した結果:

- 30行入れても `scrollHeight === clientHeight`（68px == 68px）となり、**溢れた26行はスクロール到達不能**（scrollTop は 0 に固定、負値も正値も効かない）
- 見えるのは DOM末尾（＝視覚上の上端）の約4行だけ

一般則: **スクロール容器で main-start 側に溢れたコンテンツは「到達不能オーバーフロー」になる**。column-reverse で main-start は下端になり、そこへ `justify-content:flex-end` で押し出された古い行は永久に見えない。「新しい行を上に出したい」なら、CSSで反転せず **配列を逆順にして通常の `column` で描く**のが正しい。

検査上の教訓: 実ブラウザのsmokeテストでも、**テキストや data 属性だけ読んでいると見えない**（この件は smoke-files.mjs が PASS のまま素通りした）。一覧UIは「先頭以外の行に実際に到達できるか」まで見る必要がある。
