# フロントエンド・テスト基盤の知見

**いつ読むか**: Svelteコンポーネント・WebSocket通信・puppeteerによる実機検査スクリプトを書く/直すとき。

## Svelte 5 (runes) の落とし穴

- **`$effect` 内の `?.` 短絡は依存の登録ごと飛ばす**: `viewport?.showBasePlanes(wants)` のように初回 `viewport===null` で `?.` が短絡すると、引数 `wants` が一度も読まれず依存ゼロのまま登録され、`wants` が以後どれだけ変わっても再実行されない。型検査・lintも通り、初期状態では「何も起きないのが正しく見える」ため気づきにくい（duocadでは「コマンド起動時に基準平面が指せない」として発覆、2026-08-22実測)。対処: `const on = wants` のように先にローカル変数へ読んでから渡す。
- **runesモードのインスタンススクリプトは `export` を持てない**: `<script lang="ts">` にexportを書くとエラーになる。プロパティ入口は `$props()` に一本化されているため。型や定数を公開したいときは `<script module lang="ts">` ブロックへ置く(duocadの先例は `Toast.svelte` の `showToast`)。ビルド時 (`npm run check`) に検出できる(2026-09-05)。
- **`$state`/`$derived` のプロキシ(配列・オブジェクトとも)はpuppeteerの`page.evaluate`でそのまま返すと`{}`になる**: CDPの`returnByValue`ディープシリアライズがProxyを写せないため。例外も警告も出ず、受け側で`params.width`が`undefined`になり、`Math.abs(undefined-8)>0.05`のような検査は`NaN>0.05===false`で素通りしてしまう。duocadで2026-08-10/08-23/08-27/08-31/09-06と繰り返し実測。
  - 第一選択: ページ側で `$state.snapshot(obj)` を呼んでから返す(プロキシを外した素のディープコピー)。
  - 代替: `JSON.stringify`してから文字列で返しNode側で`JSON.parse`／配列はスプレッド`[...arr]`／primitiveだけ個別に`return`する、のいずれか(いずれも「形が変わると落とし忘れる」弱点あり)。
  - 数値を読む実機検査には`Number.isFinite`を条件に入れておくと、この潰れが「値が無い」として表に出る。
  - 既存検査に`JSON.stringify`が挟まっていたら様式ではなく必要と読むこと。
  - 同じ症状はSvelteと無関係にDOMの組み込み型でも起きる(次項)。

## puppeteer / ヘッドレスChrome の落とし穴

- **`getBoundingClientRect()`(DOMRect)を`page.evaluate`単体で返すとフィールドが消える**: `left`/`top`等はプロトタイプのgetterでown enumerableでないため、CDPの構造化複製で運ばれず`undefined`になる。後続の`x + rect.left`が`NaN`になり、`page.mouse.click(NaN, NaN)`が`Invalid parameters ... double value expected`で落ちる(実際の例外はmouse.click側に出るので原因究明が1テンポ遠い)。対処: rectの取得とそれを使う計算を同じ`page.evaluate`コールバック内で行い、プレーンオブジェクトにしてから返す(duocad `web/scripts/smoke-move.mjs` の `clickFace()` が先例、2026-09-06実測)。
- **`page.click()`はdisabledな`<button>`に対して例外を投げず黙って何もしない**: 非同期処理待ちで`disabled`のままクリックすると、クリック自体は正常終了しログにも「押した」と出るが実際には発火せず、後続の`waitForFunction`が15分のタイムアウトいっぱいまでハングしてから失敗する(probe_topopt、2026-09-04実測)。対処: ボタンが有効化される条件を`page.click()`の**前**に`waitForFunction`で待つ。
- **puppeteer-core 25.5.0(duocadの検査基盤)には`page.waitForTimeout`が無い**(v22台で非推奨→撤去済み)。代替は `page.evaluate(() => new Promise(r => setTimeout(r, ms)))`。固定時間待ちが必要な場面(アニメーション完了待ち等)で使う。既存スクリプトは`waitForFunction`や独自の`waitTwoFrames`を優先している。
- **「値が一致するまで待つ」型の待機は、両辺が既定値のとき即座に真になり検査が何も見ないまま緑で通る**: 「ビューポートの目標偏差 == サーバのメッシュ偏差」を条件にすると、ズーム前(両者とも既定値0.2mm)で即真になり、まだ減衰中のカメラ値を読んでしまう(2026-08-30実測)。対処: 待機条件に「まだ何も起きていない状態」を弾く項(例: `worldPerPixel > 0`)を必ず入れ、さらに同じ値が複数フレーム続くことも見る。一般化: AとBの一致判定は初期値が同じだと成立しないので、「初期値ではないこと」を別項で足す。
- **ヘッドレスChrome(`--headless=new`)はこのPCで実GPU(RTX 5060 Ti/D3D11)を掴める**(2026-08-22実測)。duocadの`--use-angle=swiftshader --enable-unsafe-swiftshader`はEdge headless時代(2026-08-10 `2f6bbfa`)の名残で、Chrome優先化(2026-08-15 `391d599`)でも素通りしていた。`--enable-unsafe-swiftshader`はフォールバック許可であり単独ではソフトGL強制にならない。強制していたのは`--use-angle=swiftshader`のみ。
  - 合成負荷比較(1280×860・MSAA・毎フレーム2万インスタンス、6秒): SwiftShaderはgpu-process CPU 1463%/0.7fps、D3D11は6%/36fps。
  - 実アプリでは倍率が当てはまらず、検査所要時間はほぼ変わらない(smoke.mjs 163→150秒/smoke-toast 15→15秒/acceptance 16→15秒)。律速はOCCT/Python側とWebSocket往復であり描画fpsではないため。変わるのはCPU消費のみ(chrome累積CPU: SwiftShader 1550秒・平均9.5コア → D3D11 215秒・平均1.4コア)。合否は同一。
  - 対応済み: `web/scripts/browser.mjs`から`--use-angle=swiftshader`を除去し、起動時のRENDERER文字列を`GL :`行に出すようにした(`--enable-unsafe-swiftshader`は残置)。taskbox drain環境(タスクスケジューラ起動)での実GPU取得は未実測、drainログの`GL :`行で確認する。
- **headless検査中、Chromeのgpu-processが8コア級のCPUを食うのは異常ではない**(3要因の積、2026-08-22実測 800%/14論理コア中約8コア相当): ①上記SwiftShader強制、②`viewport.ts`の`tick()`が変化がなくても毎フレーム`controls.update→render→requestAnimationFrame`を回す(puppeteerは既定で`--disable-background-timer-throttling`を付けるため画面が無くても60fpsで継続)、③`WebGLRenderer({antialias:true})`のMSAA。実GPUを掴めれば②③のコストはほぼ無料になるため、最終的な対処は前項の「swiftshader引数を外す」。
- **実機確認スクリプトは、ポートが埋まっていると古いサーバを撮って緑になる**: `probe_topopt/shot.mjs`は`server.py`を子プロセスで起動しブラウザを向けるが、ポート衝突でbindに失敗して子プロセスが死んでも、ブラウザは既に走っていた(古いコードを読み込んだままの)サーバに繋がる。成功条件(`state==='done'`等)は全部満たされ「OK」と出る。実測: 巻き順修正後に撮ったPNGが修正前とmd5まで一致(`fca3b7e2…`)。ログに`OSError: [Errno 10048] bind ...`が出ていたが終了条件にしていなかった(2026-09-02実測)。対処: `PROBE_HTTP_PORT`/`PROBE_WS_PORT`に空きポートを渡す(動いている方を落とすのではなくこちらがずれる)。一般化: 子プロセスの起動失敗をクライアント側の成功条件だけで判定しない。

## WebSocket

- **ローカルWebSocketは「127.0.0.1へbind」では守れない**: WebSocketは同一オリジンポリシーの対象外なので、任意のWebページが`ws://127.0.0.1:<port>`へ繋いで読み書きできる。狙い撃ちは不要で、duocadを立てたまま別サイトを開くだけで文書の読み書き・`export_step`での任意パス書き込み・`ai_chat`経由のAnthropic課金消費が可能だった(2026-08-20)。
  - **本人の決定**: Origin許可リストを導入。許可はduocad自身が配るページのみ(静的配信/Vite devを`127.0.0.1`/`localhost`両綴りで)。`Origin`ヘッダ無しは意図的に許可する(ブラウザは必ずOriginを送るためJSから偽装できず、素のWSクライアント検査は壊れない)。
  - 実装上の罠: 「ヘッダ無し(`None`)の許可」と「文字列`"null"`の許可」を取り違えると、sandboxed iframeや`file://`ページ(`Origin: null`)が通ってしまう。
- **繋がらないWebSocketの再接続の実周期はクライアントの待ち時間定数だけでは決まらない**(2026-08-23実測、Chrome): `onclose`が返るまで約2秒、配信元停止時の`fetch`も失敗まで2.0秒。結果、再接続の実周期は開いたばかりの画面で5秒、失敗を積んだ画面で26秒まで伸びた(同一コード・同一定数2秒)。帰結: 「N回失敗≒N×待ち時間秒」という換算は成立しない。「繋がらない」の通知は回数で切ること(時間で切ると無音区間の幅がぶれる)。周期が伸びる原因(同一ホストへの失敗の累積か)は未確認。

## SVGの落とし穴

- **SVGコメント内の2連ハイフンはXMLとして不正になり、画像が無言で壊れる**: `<!-- ... -->`内に`--`を書くとXMLパースが落ちる(CSS変数名を説明に書くと自然に踏む)。壊れ方は無言で、例外もログも出ず`<img>`が壊れた画像アイコンになるだけ(HTTPは200、ファイルサイズも正常)。検出は `python -c "import xml.dom.minidom; xml.dom.minidom.parse('favicon.svg')"`。手書きSVG納品前に1回パースを通すこと(duocadのfaviconで2026-08-26実測)。

## キーボードイベントの落とし穴

- **全域のkeydownハンドラがEscapeに`preventDefault()`を掛けていると、native `<dialog>`のEscで閉じるが黙って死ぬ**(`showModal()`のモーダルでも同様)。duocadの`CommandHost.svelte`が`svelte:window onkeydown`でEscapeにpreventDefault(コマンド中断用)を持っていたため、`SettingsDialog.svelte`は新設時から一度もEscで閉じたことが無かった(コメントには「`showModal()`で自動的に付く」と誤記されていた)。非モーダル化時(2026-09-04)にEscを自前で拾うハンドラを足して初めて閉じるようになった。直す際は自前ハンドラで`preventDefault()`/`stopPropagation()`を掛け返さないこと(Escを中断にも使っているため、片方を止めると他方の挙動まで変わる)。ネイティブの既定動作に頼る機能は、全域キーハンドラがある画面では実際に押して確認すること。

## フレームワーク選定の判断根拠

- **破壊的変更はSvelteコアではなくSvelteKitに集中している**(2026-08-10調査)。Svelteコアの大きな書き換えは3(2019-04)→4(2023-06、軽微)→5(2024-10、runes導入)で約5年間隔、2026-08時点でSvelte 6は未登場(5が22ヶ月継続)。いずれも「動かなくなる」ではなく「書き方が変わる」でlegacyモードと`sv migrate`がある。対して**SvelteKitは1.0(2022-12)→2.0(2023-12)→3.0(2026)とほぼ年1回のメジャー**でマイナーでも壊れる。ルーター・SSR・アダプタ・フォームが不要ならSvelteKitを入れないだけでchurnの大半を回避できる。
  - 依存の危険順位(duocad実測): ①three.js(semverなし、r番号で毎月リリース、非推奨API定期削除) > ②言語バインディング(OCP等は維持者が少なく冗長性最小) > ③小規模ライブラリ > ④フレームワーク(最下位)。
  - ローカル専用ツールは上げなければ壊れない(バージョン固定+静的ファイル)。実コストは「壊れる」ではなく「イディオムのずれ」(次の大版後にAIが書く「今風の書き方」が手元コードと噛み合わずAI支援の質が落ちる)。
  - 長寿命の個人ツールでの技術選定指針: ①メタフレームワークを外せないか検討する、②churnの激しい依存を1ファイルの狭いインタフェースに閉じる、③バージョンを固定する。部品10個以下なら「書き換えが1日で済む規模にsurfaceを保つ」で足りる。

## duocadアーキテクチャ上の判断・落とし穴

- **同じSessionをMCP(AI)経路とWebSocket(UI)経路の2つから見るとき、MCP側が`Session`のプロパティではなく生の`document.log`を直に読むと契約が経路ごとにずれる**(2026-08-28)。実際に2件壊れた:
  - `can_undo`: MCP側が`document.log.can_undo`を素で見ていたため、STEP読み込み等の直後に「undoできる」と答えたのに実行拒否された。`Session.can_undo`へ統一して解決。
  - `applied_count`: MCP側が`len(document.log.entries)`(undoで減る)を返し、UI側の`Session._applied_count`(undo/redoで動かない)とずれた。`Session.applied_count`プロパティを追加して両方をそこへ通した(コミット`1557442`)。
  - 揃える向きは「UIの保存済み判定が依存している方」で決まる — `App.svelte`は`applied_count`がundo/redoで動かないことに依存して「保存時点へ戻したか」を判定しているため。
  - 再発の型: `describe_document`に新しいキーを足すときは、`Session`に同名プロパティがあれば必ずそれを通す。同じ関数内に前例のコメントがあっても揃え忘れは起きる。
- **SVG読み込み(`import_svg`)は「新しいスケッチを1枚作る」から「いま開いているスケッチへ取り込む」verbへ変更すると決定**(2026-08-27。理由の明言は無し)。「SVG読み込みはスケッチモードに移動して」という指示に対し3案(①開いているスケッチへ取り込む=コア変更あり/②置き場だけ移動=コア変更なし/③`sketch_create`の隣へ並べ替え)を提示し①を選択。これにより貼る先は開いているスケッチの平面になり、旧来の`base_plane`引数(貼る先を指定)は消える。STEP/STLの入出力をファイルパネルへ寄せる決定([[duocad-file-io-moves-into-files-panel]]、engine外参照のため名称のみ)とは同じ会話で出たが別判断。

## Python運用(検査基盤)

- **importlibでモジュールを単体ロードして中の`@dataclass`を使うなら、`exec_module`より前に`sys.modules`へ登録する**(2026-08-20)。パッケージの`__init__.py`(重い依存を引く)を避け`importlib.util.spec_from_file_location`でファイル単体を読むとき、`sys.modules[spec.name] = module`をexec前に置かないと、`from __future__ import annotations`併用時に`dataclasses`が型注釈解決で`sys.modules[cls.__module__]`を引けず`AttributeError: 'NoneType' object has no attribute '__dict__'`で落ちる。import時に落ちるためそのモジュールを読む全テストが一斉に赤くなる。実例: duocadの`duocad/catalog.py`をOCP依存の`duocad/__init__.py`経由せずに検査しようとして踏んだ。
