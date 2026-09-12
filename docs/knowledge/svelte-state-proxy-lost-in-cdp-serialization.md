---
name: svelte-state-proxy-lost-in-cdp-serialization
description: Svelte 5の$state/$derivedプロキシ(配列もオブジェクトも)をpuppeteerのpage.evaluateでそのまま返すと{}になる。例外も警告も出ず、受け側のMath.abs(undefined-8)>0.05はNaN>0.05===falseで素通りする。第一選択は$state.snapshot()で包む(スプレッド/JSON.stringify/プリミティブだけ返すでも取れる)。数値を読む実機検査はNumber.isFiniteを条件に入れて「値が無い」を表に出す。既存の検査がJSON.stringifyを挟んでいたら様式ではなく必要
created: 2026-08-10
updated: 2026-09-06
---

ヘッドレスブラウザ(puppeteer / CDP)から Svelte 5 のアプリの状態を読むとき、**`$state` で作ったオブジェクト・配列をそのまま `page.evaluate` の戻り値にすると中身が欠ける**。原因は `$state` がオブジェクトを Proxy で包むことで、CDP の `returnByValue` によるディープシリアライズがプロキシを素直に写せないため。duocadの実機検査で2026-08-10 / 08-23 / 08-27 / 08-31 / 09-06 と繰り返し踏んだ。

- ページ内の `waitForFunction` では `typeof record.t_geometry_updated === 'number'` が真(ページ内には値がある)なのに、`page.evaluate(() => ({ record: window.__x.lastRecord }))` で受け取るとそのキーが `undefined` になり引き算が `NaN` になった(2026-08-10)。
- `$state` の**配列**(`window.__duocad.errors`)は `{}` で返る。`errors.filter is not a function` のような別の場所のエラーとして出るか、「エラーは0件だった」と読んで緑になる(2026-08-23)。
- **新しいプレーンオブジェクトに包んで間接的に返しても駄目**: トップレベルのprimitive(文字列・数値)は渡るが、ネストしたプロキシオブジェクトをプロパティとして返すとそのプロパティだけ空 `{}` になる(2026-08-27。`{ id: pattern.id, params: pattern.params }` で `params` だけ `{}`)。
- `window.__duocad.state.optimize.load` のようにネストした `$state` オブジェクトを直接返すと `{}`。同じ値をページ内で `JSON.stringify()` すると正しく出る(2026-08-31)。壊れているのは「Proxyのまま戻り値として運ぶ」経路だけで、ページ内での読み取りや、`page.evaluate` 内で条件判定してbooleanを返す形は影響を受けない。

**症状が悪い**: 例外も警告も出ず、空オブジェクトが返るだけ。受け側で `params.width` を読むと `undefined` になり、`Math.abs(undefined - 8) > 0.05` のような検査は `NaN > 0.05 === false` で**素通りする** —— 検査を書いたつもりで何も見ていない状態になる(2026-09-06)。数値を読む実機検査では `Number.isFinite` を条件に入れておくと、この潰れが「値が合わない」ではなく「値が無い」として表に出る。

**正しく取れる形**:

```js
// 1. Svelte 5 なら `$state.snapshot()` が正攻法(ページ側のコードで包む)。
//    プロキシを外した素のディープコピーを返すので、そのまま page.evaluate で運べる
//    (2026-09-05にduocadで実測。ネストした grid オブジェクトが {} になっていたのが直った)
window.__hook = () => $state.snapshot(someReactiveObject)

// 2. ブラウザ内でJSON.stringifyしてから文字列として返し、Node側でJSON.parseする
JSON.parse(await page.evaluate(() => JSON.stringify(someReactiveArray[0])))

// 3. 配列ならスプレッドで素の配列にしてから返す
await page.evaluate(() => [...window.__duocad.errors])

// 4. primitive値だけを個別にreturnする(duocadのsmoke検査群が一貫して使っている形)
await page.evaluate(() => someReactiveArray[0].id)
```

**1が第一選択**。2〜4は「素のJSONへ落とす」を呼び出し側が手で守る形なので、状態の形が変わるたびに落とし忘れが起きる(スプレッドで包む形は入れ子だけが静かに落ちる)。`$state.snapshot()` は落とす作業そのものをフレームワークに任せる。**既存の検査が `JSON.stringify` を挟んでいたら、それは様式ではなく必要**だと読むこと(duocadの `smoke-files.mjs` はこの理由で当初から挟んでいた)。

一般化: **フレームワークのリアクティブ状態(Svelte 5・Vue 3のreactive等)を外部プロセスへ渡すときは、境界で必ず素のJSONへ落とす。** 「ページ内では見えているのに外から見えない」は原因が分かりにくく、テストが嘘の失敗をする。Svelteと無関係にDOMの組み込み型(DOMRect)で同じ症状が出る件は [[boundingclientrect-returned-alone-from-page-evaluate-loses-its-fields]]。
