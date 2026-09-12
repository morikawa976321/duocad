---
name: boundingclientrect-returned-alone-from-page-evaluate-loses-its-fields
description: 落とし穴(2026-09-06・duocadの実機smoke作成中に実測) — puppeteerのpage.evaluate()でgetBoundingClientRect()の戻り値(DOMRect)をそのままreturnすると、left/top等はプロトタイプのgetterなのでCDPの構造化複製で運ばれずundefinedになる。後続のx+rect.leftがNaNになりpage.mouse.click(NaN,NaN)が「double value expected」で落ちる。rectの取得と使用は同じevaluateコールバック内で行いプレーンオブジェクトで返す
created: 2026-09-06
updated: 2026-09-06
---

# `getBoundingClientRect()` を `page.evaluate` 単体で返すとフィールドが消える

puppeteerで `const rect = await page.evaluate(() => document.querySelector('.viewport').getBoundingClientRect())` のように**DOMRectだけを返す**と、`rect.left` / `rect.top` 等が `undefined` になる。DOMRectのプロパティはown enumerableではなくプロトタイプのgetterなので、CDP越しの構造化複製で運ばれない。Svelte 5の `$state` プロキシが `{}` になる [[svelte-state-proxy-lost-in-cdp-serialization]] と症状は同じだが原因は無関係 —— こちらはSvelteと無関係にDOMの組み込み型で起きる。

症状は静かに壊れる形を取る: `rect.left` が `undefined` になり、後続の `x + rect.left` が `NaN` になって、`page.mouse.click(NaN, NaN)` が `Invalid parameters ... double value expected` で例外を投げる。例外はmouse.click側で出るので、原因がDOMRectのシリアライズだと気づくまで1テンポ遠い。

**対処**: `rect` の取得と、それを使う計算(`{x: pos.x + rect.left, y: pos.y + rect.top}` 等)を**同じ `page.evaluate` コールバックの中で行い、プレーンオブジェクトにしてから返す**。duocadの `web/scripts/smoke-move.mjs` の `clickFace()` は最初からこの形になっている。分けて書くと踏む。
