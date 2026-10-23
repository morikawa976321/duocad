# duocad — フロントエンドの落とし穴（three.js / Svelte 5 / puppeteer / CSS）

**読むとき**: `web/src` を触って「型検査も pytest も通るのに実機で変」が出たとき、または同じ罠を踏む前に。
特定の行に紐づくものはコードのコメントが正（`viewport.ts` / `viewportConstants.ts` / `planeGrid.ts` /
`App.svelte`）。ここは**ファイルをまたぐもの・一般化した形**。

## three.js（0.185.1 実測）

- **`Object3D.DEFAULT_UP` はカメラ・コントロール・ライトを1つも `new` する前に書き換える**（`viewportConstants.ts`）。
  `OrbitControls` はコンストラクタで `_quat` を `object.up` から計算して `update()` で再計算しないので、後から
  `camera.up` を変えても**オービットの極は変わらない**（見た目だけ直って回転軸が旧いまま）。`HemisphereLight` は
  コンストラクタで `position` に `DEFAULT_UP` を複写する。平面スケッチのために一時的に `camera.up` を倒す実装は、
  そのモード中だけオービットの極がずれる（`_quat` は私有 API）。
- **`OrbitControls.mouseButtons` の既定は LEFT=ROTATE / MIDDLE=DOLLY / RIGHT=PAN。** 「中ドラッグ=パン」は既定ではない。
  左をウィンドウ選択に明け渡すには `LEFT: null` を入れる —— 何か割り当てたままだと矩形を引いている間にカメラが動く。
- **`Line2` / `LineSegments2` の当たり判定は `raycaster.params.Line.threshold` を見ない。** 当たり半径は
  `LineMaterial.linewidth`（描いている太さ[px]）で決まる。スケッチ線の半径を 6px のつもりで threshold に入れていたが
  効いていたのは 1.5px で、線から 2px 外すと掴めなかった（実機 smoke が線の真上しかホバーしていなかった）。
  `LineSegmentsGeometry` の `position` は太線を描く箱のテンプレート（8頂点）で折れ線ではない。
- **`side: THREE.DoubleSide` は自己遮蔽を直さない。** 塊の背面にある面が既定視点で描かれなかった原因はバックフェース
  カリングではなく、同じ塊の手前の面に隠れていたこと。直すのはカメラの向き。色のチャンネル比較で面を検出する検査は
  半透明の境界線ヘルパーを誤検出しうる —— 1色ずつ隠して画素数が0近くまで落ちることを確かめる。
- **シーンから除去すべきオブジェクトの配列と、ピック判定に要る配列を兼用しない。** 面の境界線（`LineSegments`）が
  どの配列にも入っておらず `root.add` されたまま二度と `remove` されなかった（ゴースト線＋リーク）。
  1つの論理単位が複数のオブジェクトを持つなら除去専用の束を持つ —— `web/src/sceneBatch.ts` の `createBatch(parent)` は
  `add()` が親へ足すのと控えるのを同時に行うので、push 忘れが構造的に起きない（ピック用の配列は `add()` の戻り値を別に push する）。
- **`ShaderMaterial` は必ず `#version 300 es` が付く**（`RawShaderMaterial` でなければ）。GLSL1 流儀で書いてよく、
  `gl_FragDepth` / `fwidth` / `inverse` が使える。組み込み材質と同じ見え方にするにはフラグメント末尾に
  `#include <colorspace_fragment>`（忘れると同じ色コードで暗く出る）。フラグメントの prefix に `projectionMatrix` は無い。
  テンプレートリテラルの GLSL コメントに日本語のバッククォートを書くとリテラルが終端し、`TS1443` という遠いエラーになる。
- **`WebGLRenderer` の `stencil` は既定が false**（r163 以降）。`stencilWrite` を使う材質は例外も警告も出さずに効かない
  （断面の切り口で実測: ANGLE/D3D11 で板が描かれず、切り口の画素は奥の面の色のまま）。
- **`Raycaster` は材質の `clippingPlanes` を見ない。** クリップ平面で見えなくした面にもレイが当たるので、指せるかどうかは
  平面の式で自前に捨てる（[VIEWPORT.md](VIEWPORT.md)「断面」）。
- **`createViewport()` は宣言の順序がそのまま初期化の順序。** 初期化中に後方の `const` を読むと TDZ で落ち、
  `window.__duocad.ready` が永久に立たず「画面が出ない」だけになる（`tsc` / `svelte-check` は検出しない）。
  初期化中に読むものを増やす変更は宣言順を確認し、依存の無いもの（`new THREE.Group()`）は前方へ上げる。
- **画面座標へ変換するヘルパの最内ループで DOM を読まない。** `projectRaw` が頂点ごとに `size()`（`clientWidth`）を
  読んでいて、矩形選択 83,084 頂点で 32.1ms → ループ外へ巻き上げて 2.5ms（遅さの 92% が DOM 読み。オブジェクト割付は
  2.5→2.3ms でほぼ効かない）。寸法は引数で受けるか呼ぶ側がループ外で1回読む。

## Svelte 5

- **`$effect` の `?.` 短絡は依存の登録ごと飛ばす。** `viewport?.showBasePlanes(wants)` は初回 `viewport===null` で
  `wants` を一度も読まず依存ゼロで登録され、二度と再実行されない（「コマンドを起動しても基準平面が指せない」）。
  リアクティブ値を先にローカルへ読む。`void selection` のような明示的に読む1行はこの穴を塞ぐイディオム。
- **`{#key}` の作り直しは子の通常の中断経路を通らない。** プレビューの寿命は `previewController.ts` が持つ。
  `CommandHost` は破棄時に `preview.detach()`、`App` はモード変更で `preview.clear()`、自身の破棄で
  `preview.dispose()` を呼ぶ。描画の破棄だけで済ませると、保留タイマーや遅い応答から幽霊が戻る。
  遷移は `tests/eval_preview_controller.mjs` が時刻と応答順を固定して検査する。
- **runes モードのインスタンススクリプトに `export` は書けない。** 型や定数を公開するなら `<script module lang="ts">`
  （`Toast.svelte` が先例）。`npm run check` で出る。
- **`$state` のオブジェクト・配列を `page.evaluate` の戻り値にすると中身が欠ける**（Proxy が CDP のディープシリアライズに
  乗らない）。配列は `{}`、ネストしたプロキシはそのプロパティだけ `{}`、新しいプレーンオブジェクトに包んでも駄目。
  **例外も警告も出ず**、`Math.abs(undefined - 8) > 0.05` は `NaN > 0.05 === false` で素通りする。
  第一選択は `$state.snapshot()`（ページ側の hook で包む。`web/src/testHooks.svelte.ts` の `__duocad`）。`JSON.stringify` を挟んで
  文字列で返す・スプレッドで素の配列にする・primitive だけ返す、でもよいが落とし忘れが起きる。
  **既存の検査が `JSON.stringify` を挟んでいたら様式ではなく必要。**
- **条件付きレンダリングは DOM を消すが状態は消さない**（AI パネルのエンドポイント欄。[AI_INTEGRATION.md](AI_INTEGRATION.md)）。
  隠す判断と送る判断を同じ場所で持つ。

## DOM・ダイアログ・キー

- **`showModal()` の `::backdrop` は `elementFromPoint` を全画面 `<dialog>` にする**（矩形が重なっていなくても）。
  `clickable()` が全部 false になり 3D ピックが死ぬ。見た目は何も塞がれていない。duocad の設定ダイアログは
  `show()` を使っている。他で `showModal()` を足せば同じ。
- **native `<dialog>` の「Esc で閉じる」は keydown の既定動作なので、全域ハンドラが Escape に `preventDefault()` を
  掛けていると閉じない**（`CommandHost.svelte` の中断がそれ。モーダルでも守られない）。
  ネイティブの既定動作に頼る機能は、全域キーハンドラがある画面では実際に押して確かめる。自前の Esc ハンドラで
  `preventDefault` / `stopPropagation` を掛け返さない（同じ Esc を中断にも使っている）。
- **`tagName === 'INPUT'` だけの除外はチェックボックスを巻き込む。** Ctrl+Z の除外を Enter の除外（全 INPUT）と
  同じ書き方に揃えると、可視性チェックボックスにフォーカスが残った状態で undo しなくなる。目的が違えば除外集合は
  一致させない —— Enter は誤送信防止なので全 INPUT、Ctrl+Z はテキスト編集中のブラウザ標準 undo を奪わないためなので
  `type` が checkbox/radio でない INPUT と TEXTAREA だけ（`App.svelte` `isTextEditingField`）。
- **puppeteer の `page.click(selector, { clickCount: 3 })` は入力欄を全選択しない**（puppeteer-core 25・headless Chrome で実測）。
  キャレットが先頭に置かれるだけで、`21.9` の欄に打った `5+1` が `5+121.9` になった。打ち直すときは Ctrl+A を本物のキーで押す。
- **同一 canvas の第2パス（`setViewport`/`setScissor`）も浮かせた DOM パネルと同じくクリックを吸うが、
  `elementFromPoint` では見えない**（[VIEWPORT.md](VIEWPORT.md) `clickable`）。

## CSS

- **素の `input { width: … }` は `type=checkbox` / `radio` / `range` にも当たる。** チェックボックスが 7em の箱になり
  flex の中で `flex-shrink:1` で潰れ、潰れ方が同じ行の他要素の長さで決まるので同じ列なのに行ごとに幅が変わる
  （27行で名前の左端が 13px 幅でがたつく）。グローバル側を型で絞る。
  `accent-color` も指定しないとダークテーマの中でブラウザ既定の青が出る（`styles.css`）。
- **`flex-direction: column-reverse` + `justify-content: flex-end` の容器は溢れた分へスクロールできない**
  （main-start 側の到達不能オーバーフロー。30行で `scrollHeight === clientHeight`）。「新しい行を上に出したい」なら
  配列を逆順にして通常の `column` で描く。テキストや data 属性だけ読む smoke は素通りする。
- **縦の flex 容器の中で区分に `min-height: 0` を置くと区分が中身より低く潰れ、リストが次の区分の見出しに重なる**
  （`overflow: visible` のまま。スクロールバーは出るので一見スクロールできている）。区分は `flex: 0 0 auto` で
  中身なりの高さを保ち、スクロールは容器が1つだけ持つ。見分け方は各区分の `getBoundingClientRect().height` と
  `scrollHeight` の食い違い。
- **SVG のコメントに `--` を書くと XML パースが落ちる**（CSS 変数名を説明に書くと踏む）。無言で壊れた画像アイコンに
  なるだけ。手書き SVG は `python -c "import xml.dom.minidom; xml.dom.minidom.parse('favicon.svg')"` を1回通す。

## ブラウザ・ネットワーク

- **繋がらない WebSocket の `onclose` は約2秒、落ちている配信元への `fetch` も 2.0秒。** 再接続の実周期は開いたばかりで
  5秒、失敗を積んだ画面で 26秒まで伸びる（同じコード・同じ待ち時間定数。原因未確認）。
  「N回失敗＝約N×待ち時間」は成り立たないので「繋がらない」を知らせる条件は回数で切る。
- **ヘッドレス Edge のスクリーンショットではスマホ幅のレイアウト崩れを判定できない。** `--window-size=390,844` でも
  ページは約 500px 幅でレイアウトされる（デスクトップ版の最小ウィンドウ幅）。
- **`--use-angle=swiftshader` を付けなければ headless Chrome は実 GPU を掴む**（[SMOKE_TESTING.md](SMOKE_TESTING.md)）。
