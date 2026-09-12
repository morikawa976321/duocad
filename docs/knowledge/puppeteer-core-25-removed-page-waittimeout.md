---
name: puppeteer-core-25-removed-page-waittimeout
description: puppeteer-core 25.5.0(duocadのweb/scripts検査基盤)は`page.waitForTimeout`が無い。`page.evaluate(() => new Promise(r => setTimeout(r, ms)))`で代替する
created: 2026-09-04
updated: 2026-09-04
---

duocadの実機検査(`web/scripts/*.mjs`)は `puppeteer-core` 25.5.0 を使っている。旧APIの
`page.waitForTimeout(ms)` はこのバージョンには**存在しない**(v22台で非推奨→撤去済み)。
呼ぶと `TypeError: page.waitForTimeout is not a function` で即死する。

代替は `await page.evaluate(() => new Promise((resolve) => setTimeout(resolve, ms)))`
(ページのJS側でsetTimeoutする)。アニメーション完了待ち(例: ビューキューブのスナップ
`CUBE_SNAP_MS`)など、固定時間待ちが要る場面で使う。既存の `web/scripts/*.mjs` は
`waitForFunction`(条件待ち)や独自の `waitTwoFrames`(requestAnimationFrame二重待ち)を
優先しており、固定時間待ちが必要になったのはこの落とし穴で気づいた。
