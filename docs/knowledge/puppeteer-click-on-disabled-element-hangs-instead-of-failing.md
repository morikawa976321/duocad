---
name: puppeteer-click-on-disabled-element-hangs-instead-of-failing
description: puppeteer-coreの落とし穴(2026-09-04・probe_topoptで実測) — page.click()はdisabled属性のついた<button>に対して例外を投げずに黙って何もしない。UIが非同期でボタンを有効化する設計だと、クリック前に「有効になった」ことを待たないとheadlessテストが原因不明のタイムアウトでハングする
created: 2026-09-04
updated: 2026-09-04
---

# `page.click()` は disabled ボタンに対して黙って何もしない

probe_topopt(taskbox `20260903-2304`)の実機確認スクリプト(`shot.mjs`)で踏んだ。
ページを開いた直後、まだ非同期処理(WebSocket経由でサーバから初期データを取得中)が
終わっておらず`<button id="run" disabled>`のままの状態で`await page.click('#run')`を
呼んだところ、**`click()`自体は例外を投げずに正常終了し**、ログにも「ボタンを押した」が
出た。しかしブラウザの標準挙動どおり`disabled`要素はクリックイベントを発火しないので、
実際には何も起きておらず、後続の`page.waitForFunction(() => state==='done')`が
**15分のタイムアウトいっぱいまでハングしてから失敗した**。

**厄介な点**: 失敗の兆候が「クリックした直後のログは正常」にしか出ない。原因が
「クリックが効いていない」ことだと、タイムアウトの長い待機処理の後にしか判明しない。

**直し方**: ボタンが有効化される条件(このケースでは`window.__probe.state === 'conditions'`)
を`page.click()`の**前**に`waitForFunction`で待つ。UI側が「読み込み中は押せない」設計に
なっている(=`disabled`属性で早押しレースを防いでいる)場合、テストスクリプト側もその
設計に付き合う必要がある——テストが「早ければ早いほどよい」で書かれていると、
UIの防御機構とテストの前提が正面から衝突する。

一般化: **非同期で有効化されるUIをheadlessテストするときは、クリックの前に「クリック可能に
なったこと」を明示的な条件として待つ。** `disabled`属性の有無、あるいはボタンのテキスト・
ARIA状態などで判定できる。
