# 選択・ビューポート・ピックの契約

選択の同一性・3D ビューポート(射影・ビューキューブ・FOV・分割モジュール)・画面ピック・最適化デバッグビューの、ソースから読めない不変条件・依存禁止・罠。
**いまの挙動の正はコードとテスト**。設計判断の本体は [../VIEWPORT.md](../VIEWPORT.md) / [../UI.md](../UI.md)。
実機 smoke のうち現在 `web/scripts/` に残るのは `smoke.mjs` `smoke-sketch.mjs` `smoke-resolution.mjs` `smoke-mirror.mjs` `smoke-face-culling.mjs` `smoke-fillet-pick.mjs` と `tests/eval_*.mjs`。

---

## 選択参照の同一性(`duocad/selection.py` / `web/src/commands/command.ts`)

- **参照の同一性の鍵は `selection.ref_key(ref) -> tuple` の1箇所だけ**。形は region `("region", sketch, int(index))` / sketch_entity `("sketch_entity", sketch, entity, point)` / body `("body", body)`(添字を見ない)/ face・edge・vertex `(kind, body, int(index))`。参照の形でなければ `ref_kind` と同じ `ValueError`。`same_ref` は `ref_key(l) == ref_key(r)` そのもの。TS 側は `refKey(ref): string` で、`sameRef` はその一致で定義。
  - 等価判定を種別ごとの分岐として書き直さない(「集合引きが落とす重複」と「`same_ref` が同じと言う組」がずれ、**どちらも例外を出さないまま**重複が残る／別物が同一視される)。TS 側は `tests/eval_selection_ref_key.mjs` が押さえる。
  - 種別は鍵の第1要素だけが持つ。face/edge/vertex の鍵に `ref["kind"]` を重ねない(重ねると「種別を鍵から抜く」変異がもう一方に吸収され検査で示せない。意図的に3要素)。
  - `provenance` は鍵に入れない／`index` は `int()` に正規化／`point` は `.get()` で欠落と `None` を畳む。TS 側の `point` は `p` を付けて書く(`null` と空文字の点名を畳まないため)。
- **`modify_selection` の並び順は不変**: 追加は末尾に足すだけ、除去は残りの相対順序を保つ。既存参照の再 add で位置が動くと `fillet` の辺リストと半径リストの対応がずれる。
- **依存禁止**: `ref_key` の戻り値をシリアライズして保存しない／鍵の順序に意味を持たせない(並び順の正は `Selection.items` のリスト順)／TS `refKey` の文字列書式に外から依存しない(いま使うのは `CommandHost.svelte` の `selectionPrint`=再送判定の指紋だけ)。
- **罠**: `_coerce_ref` を通った body 参照は必ず `index: None` を持つので、「body の鍵に `index` を足す」変異は `modify_selection` 経由の検査では1件も落ちない。`ref_key` を直接叩いて `{"kind":"body","index":7}` を渡す検査が要る。
- **性能の測り方**: `modify_selection` の重複判定を鍵引きにして約810ms→0.8ms、残りは全部 `kernel.face_count`(M=2835 で 51.1ms)。fresh 起動5回の中央値で 1037ms→47.3ms。測るときは**プロセス1回につき1計測の fresh 起動5回の中央値**で見る(in-process の繰り返しは暖まりで 5〜15ms にばらつく)。

## 面数・辺数のカウントのキャッシュ(`selection._coerce_refs`)

- `_coerce_refs(doc, refs, *, bodies=None, counts=None)` は face/edge/vertex 参照を `(body_id, kind)` ごとに `kernel.face_count` / `edge_count` / `count_subshapes` を1回だけ呼んで使い回す。戻り値・例外の型とメッセージは `_coerce_ref` を1件ずつ呼んだときと同じ。モジュール内部関数(外から直接呼ぶ経路を増やさない)。戻りは入力 `refs` と1対1・同順。
- **`modify_selection` は `add` 用・`remove` 用の2回の `_coerce_refs` 呼び出しへ同じ `bodies`/`counts` 辞書を渡す**(引数の副作用で共有)。新しい辞書だと同じボディの面が混ざったとき最大2回数える(実物27ボディで `_face_map` 42回→27回)。`set_selection` は単発なので既定でよい。
- **可視性の門番 `_visible_body` は body_id ごとに最低1回は必ず通す**。キャッシュしてよいのは数え上げであって門番ではない(不可視ボディへの参照が通ると AI が人の隠したものを黙って選ぶ)。`tests/test_face_count_once_per_body.py` の `test_hidden_body_face_ref_is_refused_through_{modify,propose}_selection` が押さえる。
- **キャッシュ鍵は `(body_id, kind)` の両方**。`kind` を落とすと面数と辺数が入れ替わる(既存 pytest が捕まえる)。`body_id` を落とすと違うボディの面数が使い回される(**`two_shapes` fixture(箱+円柱)でしか捕まらない**)。
- **キャッシュの寿命は「1回の呼び出し」のローカル変数**。`kernel.py`・モジュールレベル・`Document`/`Body` レベルへ持ち出さない。
- **検査設計の罠**: 既存 pytest は全 fixture が同一寸法の箱(面6/辺12/頂点8)で、違うボディの面数の取り違えを検出できなかった。fixture を増やすなら**形状の種類まで変える**(寸法違いの箱では検出力は上がらない)。「1回の呼び出しで」という条件があるときは、内部で同じ処理を複数回行う経路(`add`/`remove` の2回)を洗い出して検証する。

## 矩形選択の射影(`web/src/viewport.ts` / `selectionBand.ts`)

- `projectRaw(x,y,z,width,height)` / `geometryInside(...,rect,width,height)` / `objectInside(...)` は末尾に `width, height` を取る。**`collectInBox` の入口で `size()` を1回だけ読んで下流へ渡す**。新しい呼び出し元も自分の呼び出し1回の中で `size()` を1回だけ読み、ループの中で読まない(実測 `collectInBox` 38.3ms→3.9ms、27部品2835面)。`geometryInside`/`objectInside` は `selectionBand.ts`。矩形選択以外で `projectRaw` を新設・流用するなら同じ規約に従う。
- **`size()` の結果をモジュール側の変数へキャッシュしない**(リサイズで同じ画素が別のものを指す)。巻き上げてよいのは「1回の呼び出しの中だけ」。画面ピックの `pickPointsAt` / `pickLinesAt` も同じ。
- **罠**: `web/dist` は `.gitignore` 対象のビルド成果物。`web/src` を編集しても `npm run build` を挟まないと実機 smoke・計測に反映されない(計測フックが「見つからない」で timeout する偽の失敗。`git status` は「ビルドした」の証拠にならない)。A/B で別サーバ起動をまたいで選択結果を比べるときは、`bodyId` がインポートごとに非決定的なので**ボディごとの面インデックス集合に正規化**してから比較する。

## 画面ピック(`web/src/screenPick.ts` / `viewport.ts` / `vertexMarkers.ts`)

- `screenPick.ts` は three.js 非依存の純関数。`distSqPointSegment`(t を [0,1] に clamp、長さ0でも0除算しない)と `pickNearestWithinRadius`(**半径は inclusive**、**同着は先に来た候補が勝つ**、`NaN` の `distSq` は最短を横取りしない)。`viewport.ts` 内部の `projectForPick`(当たり判定専用の射影。`toScreen` と別物で viewport 矩形でも `clickable()` でも切らない)/ `pickPointsAt` / `pickLinesAt`(`polylineOf` は `{count, at(index,out)}` を返し world 座標配列を作らせない。実体は `bufferPolyline` と `sketchPolyline`)。
- **当たり半径は画面上の CSS px**。`EDGE_PICK_PIXELS`=6 / `VERTEX_PIXELS`=4 / `SKETCH_HANDLE_PIXELS`=7。world 空間の threshold を Raycaster へ渡す形へ戻さない(`tests/eval_screen_pick.mjs` 等が視点を動かしても 2px hit / 12px miss が変わらないことを固定)。
- 優先順位: スケッチは点→線、`resolveHit` は `preferVertices`→辺→領域→頂点→面。ゲート(出していないものは指せない)も維持。**tie-break は screen 距離最小**(Raycaster の3D距離順ではない。意図的変更)ので、重なって見える辺で以前と違う辺が取れうる。
- **依存禁止**: `Line2`(スケッチの図形)の `geometry.getAttribute('position')` を折れ線だと思わない(太線描画用の箱テンプレート8頂点)。world 座標の点列は `buildEntityLine` が入れる `userData.polylineWorld: THREE.Vector3[]`(矩形選択も同じものを読む)。`worldPerPixel()` は当たり判定の正ではない(残る用途はグリッド段・可変解像度・clip・カメラ静止判定・`resolveHit` の「面と基準平面の同着は面が勝つ」1画素しきい値)。`edgeAt` はホバーから毎フレーム走る。
- **Raycaster を粗い broad phase に降格する案は入れない**: 27部品 mATX PC ケース(`web/scripts/models.mjs` の `PC_CASE_STEP`)の最大ボディ(233面・辺645本・折れ線点1,768)、1280×860 で `edgeAt` 1回が p50 0.2ms／p95 0.7ms／最大1.4ms(基準 p95>16ms に20倍以上の余裕)。`edgeLines` は常に1ボディぶん(`showEdges` の契約)。
- **未確認の手動受入**: 斜め視点で bezier に沿ったホバー、27部品モデルで狙った辺が取れること、`preferVertices` を通る実機 smoke(`vertexAt` 単体の検査しか無く退行を捕まえない)。
- **罠**: ズーム直後に判定を読むと時々落ちる(ズームが可変解像度の測り直しを起こし、スケッチの折れ線が作り直される)。`stateEpoch` が止まるまで待ってから測る。pytest は `.venv\Scripts\python.exe` で(素の `python` は `OCP` 無しで51件が収集エラー)。

## 辺だけを食うコマンド(フィレット・面取り)

- 起動中(`App.svelte` の `edgeOnlyCommand` = 選択引数の種別が辺だけ)は**面を指せない**(`viewport.pickFaces(false)`。クリック・ホバー・矩形選択のすべて)。起動した時点で選択に残っている**面だけを外す**(辺・頂点などは残す。本人判断 2026-10-13)。面で辺を出すボディを切り替える手は無くなるので、別ボディの辺は起動前にそのボディを選ぶかアクティブにする。
- 辺は**連なり**で選ぶ(`viewport.groupEdgeChains(true)`)。連なりは `kernel.blend_chains` = OCCT のフィレットが辺1本から広げる範囲(一直線上で割れた辺・直線→円弧の接線連続)で、`EdgeGeometry.chain` で届く。1本ずつ選ばせると、光った1本より広く丸まる(実測: 1本を `Add` すると連なり全部にフィレット・面取りがかかる)。トグルは「全部入っていれば全部外す、そうでなければ欠けている分を足す」。矩形選択も連なりへ広げる。
- 選択・操作ログに入るのは辺1本ずつの添字のまま(カーネルの辺は統合しない。辺には面の `face_map` にあたる由来が無く、統合で添字が変わると保存済みログの再生が別の辺を指す)。測定・回転軸は連なりにしない(辺を1本として指す)。

## ピックの優先順位・頂点待ち(`preferVertices`)

- **角の頂点は既定のピック順序では取れない**。`resolveHit` は辺を頂点より先に見る(フィレット中に辺の端を掴むため)ので、辺が常に出ていると頂点狙いのクリックが100%辺に取られる(症状は「クリックしても何も起きない」だけ)。待っている間だけ `viewport.preferVertices(true)` で順序を入れ替える。**出しっぱなしにしない**(戻す責任は要求側)。
- 頂点マーカーは選択に入ったものしか出ない=指せる的が画面に無いので、待っている間だけ対象ボディの全頂点を候補として出す(`vertexCandidateBody`、`__duocad.wantedPick` とともに実機検査用の穴。製品の分岐に使わない)。待ち受けが降りた瞬間に消える。
- メッシュボディのピックは `App.svelte` の `pickedRef` でボディ参照へ畳む(畳まないと `select_face` が断り、ドラッグの一括選択が丸ごと落ちる)。`dedupeRefs` を外さない(重複 `{kind:"body"}` が `modify_selection` へ並ぶ)。**スケッチを貼る先だけは畳まず断る**。詳細は ui-optimize-integration.md。

## 頂点マーカー・分割モジュール(`viewport.ts` と兄弟)

- 分割の形は `planeGrid.ts` と同じ: `create*(deps)` がハンドルを返し、three.js オブジェクトは `object` として渡すだけ。`scene.add` と `renderOrder` は `viewport.ts` 側が行う。分割先から `viewport.ts` を import しない(逆向き依存ゼロ)。`viewCubeOverlay.ts` だけは同一 canvas に第2パスを描くので `renderer` を直接受け取る。
  - 兄弟: `viewportTypes.ts`(App に見せる型と `Viewport` IF)/ `viewportConstants.ts`(色・`LAYER`・深度バイアス・`FOV_*`・当たり判定画素・基準平面表)/ `viewCubeOverlay.ts` / `optimizeAnnotations.ts` / `optimizeDebug.ts` / `vertexMarkers.ts` / `selectionBand.ts` / `ghostLayer.ts` / `gridPlanes.ts` / `sketchCursorOverlay.ts`。`viewport.ts` は約2,900行。足す前に切れ目を探す(CLAUDE.md「ファイルの大きさ」)。
- **公開面を増やさない**: `viewport.ts` の `export` は `createViewport` + `Viewport` + 型 + `FOV_DEFAULT/FOV_ORTHO` + `isSelectableFov`。新モジュールの `create*`/`*Deps` を再エクスポートしない。`viewportConstants.ts` を `App.svelte` から import しない。three.js の型を Svelte 層へ出さない不変条件は「`viewport.ts` とその兄弟群の外へ出さない」。
- **`camera` は `let` で、投影切替(`activateCamera`)のたびに差し替わる**。`viewCubeOverlay` と `vertexMarkers` は値ではなく `camera()` ゲッターで受け、各関数の先頭で `const camera = deps.camera()` を取る。値で渡すと正投影へ切り替えた後も古いカメラを使い続け、**型検査も実機 smoke も通ったまま**「見えている角をクリックしたのに隣が取れる」になる。
- **`THREE.Object3D.DEFAULT_UP.copy(WORLD_UP)` はモジュール読み込み時の副作用**で `viewportConstants.ts` にある。`OrbitControls` は生成時の `object.up` を焼き込み、`HemisphereLight` は `position` に `DEFAULT_UP` を複写するので、**この行より前にカメラ・controls・ライトを `new` してはいけない**。`viewportConstants.ts` を `import type` だけにすると `verbatimModuleSyntax` で丸ごと消えこの副作用も消える(いまは値 `LAYER` 等を import しているので生きている)。
- `highlightColor` は共有オブジェクトの参照渡し(`vertexMarkers` が `highlightColor.vertex` を書き、`viewport.ts` の `highlightColors()` が読む。値渡しにすると色の検査だけが静かに古い値で通る)。`VertexMarkersDeps` は `pickPointsAt` を使い、`size`/`camera`/`raycaster`/`worldPerPixel` は持たない。
- `setOptimizeState` は2箇所: 役割の表(`bodyRoles`/`fixtureFaceKeys`→`applyFaceMaterials`)は `viewport.ts`、装飾の生成は `optimizeAnnotations.draw(state)`。**`clear()`→役割→`applyFaceMaterials()`→`draw()` の順序に意味がある**(`draw` を先に呼ぶと役割色が1フレーム遅れる)。
- `pointerDown`/`pointerUp` は**キューブが引き取ったら `true`** を返し、`viewport.ts` の `onPointerDown`/`onPointerUp` がその戻り値で早期 return する。`optimizeAnnotations`/`optimizeDebug`/`vertexMarkers` にグループを自分で `scene` へ足す実装へ変えない(初回フレーミングと near/far の計算対象が変わる)。
- 分割が純粋な移設であることは、テキスト一致(連結差分ゼロ。挿入した `const camera = deps.camera()` 等を除く)と公開面差分ゼロで機械確認する。

## ビューキューブ(`viewCubeOverlay.ts`)

- キューブ用カメラは本編と同じ**透視/正投影の2台持ち**(`cubePerspective`/`cubeOrthographic`)。`updateCubeCamera()` が `renderCubePass()` の先頭で毎フレーム、本編の投影種別(`camera === orthographic`)を見て `cubeActive` を指し替え、姿勢(`position/up`)と透視のとき `fov/near/far` を引き直す。定数 `CUBE_ORTHO_HALF`(1.8) / `CUBE_CAMERA_DISTANCE`(4、正投影のみ) / `CUBE_CIRCUMRADIUS`(√3) / `CUBE_NEAR_SAFETY`(0.5) / `CUBE_FAR_MARGIN`(1.5)。内部カメラは `Viewport` インターフェースに公開しない。
- **`cubeHitAt`(ピック)と `renderCubePass`(描画)は常に同じ `cubeActive` でレイ/描画を作る**。カメラを増やす・差し替えるならこの対称性を壊さない。
- 透視のときの距離式は `CUBE_ORTHO_HALF / sin(fov/2)`。**FOV の値をキューブ側へ写して保持しない**(`perspective.fov` を毎フレーム直接読む)。near/far は `distance` から毎フレーム引き直し、定数で固定しない(FOV 変化で手前の角が食われるかラベルが z-fighting する)。`cubeLight` は `cubeActive` の子で、投影切替のたびに `updateCubeCamera` が付け替える(忘れるとキューブが真っ暗)。
- **既定視点 `HOME_VIEW_OFFSET=(1,-1,0.6)` は立方体の対角 (1,1,1) 方向ではない**。この向きで FOV90 にするとキューブが非対称・くさび形に見える(バウンディングボックスが対角視点比で線形約18%小さい実測)。**バグではなく、球フィット距離式を非球体・非対角視点へ適用した数学的帰結**(NDC z は全頂点 [-1,1] に収まり near/far クリップではない。(1,-1,1) の真の対角へスナップすれば FOV90 でも他 FOV と遜色なし)。直すなら距離式の係数調整ではなく視点ごとに係数を変える等の別設計。
- 罠: 見た目の印象で近接クリッピングのバグと断じる前に NDC を数値で確かめる。`puppeteer-core` 25系は `page.waitForTimeout` が無い。固定時間待ちは `page.evaluate(() => new Promise(r => setTimeout(r, ms)))`。

## FOV・初期カメラ(`viewportConstants.ts` / `viewport.ts` / `SettingsDialog.svelte`)

- `FOV_DEFAULT` は 30。`FOV_MIN`(30)/`FOV_MAX`(90)/`FOV_ORTHO`(0)/`isSelectableFov`/`setFov` は**内部 API として 0 または 30〜90 の連続値を受け付ける**。画面の口は `#settings-ortho` チェックボックス1つだけ(`#settings-fov` スライダー／`#settings-fov-value` は無い)。`onchange` は `onFov(checked ? FOV_ORTHO : FOV_DEFAULT)`。**正投影を外すと常に `FOV_DEFAULT` へ戻る**。ラベルは「正投影」の1語で角度を出さない。
- **`isSelectableFov` の受理範囲を `{0,30}` のような離散集合へ絞ってはならない**(基準平面と面の共平面ピック回帰検査(FOV 45〜55° スイープ)が連続値に依存)。
- **起動直後のカメラ距離は `fitDistance(diagonal / 2)` を通す**(`createViewport` 冒頭、`camera.position.copy(HOME_VIEW_OFFSET).multiplyScalar(distance)` の直前)。`diagonal * 1.2` のような決め打ち定数を再導入しない。`SKETCH_FIT_MARGIN`(1.243)は `fitDistance` と組み合わさって FOV 非依存。FOV 関連の定数を変える前に `grep -rn "diagonal \* [0-9]" web/src/viewport.ts` で決め打ちが無いか確認する。
- **罠**: FOV を 45→30 に変えただけだと、決め打ち `diagonal * 1.2` が起動時カメラ位置とスケッチ未内容時の `lookAtSketch`(半径0で「前の距離のまま」に倒す経路)に残り、カメラが1.55倍近くに寄ったまま固まった。症状は原因を名指ししない「線分(辺)をクリックで選べない(タイムアウト)」。**赤が出ても自分の変更のせいと決めつけず pristine との往復比較で切り分ける**(一過性の flake は pristine/変更後とも連続で緑、本物の回帰は pristine 緑・変更後連続赤)。

## 最適化デバッグビューと撮影台

(最適化の実行そのものは ui-optimize-integration.md。ここは画面の可視化。)

- サーバ: `optimize_run.run(..., debug=None, debug_kinds=None)`(`Session.run_optimization` が素通し)。`debug` は `Callable[[dict], None]`、`debug_kinds` は `DEBUG_KINDS=("space","anchors","rho")` の部分集合。**頼まなければ配列を1本も作らない**。形は素の値のみ(`json.dumps` が通る): `{"kind":"space"|"rho","stage":"derive"|"mapping"|"final","grid":{origin,spacing,shape},"elements":[C順の通し添字],"total","shown","stride"}`(`rho` は `"threshold":0.5`)／`{"kind":"anchors","stage","grid","fixed":{face,count,points,area_mm2},"load":{...}}`(`points` はワールド mm の平坦配列)。定数 `DEBUG_VOXEL_LIMIT`(15万)/`DEBUG_KINDS`/`DEBUG_RHO_THRESHOLD`(0.5)。**`run()` の戻り値にデバッグの中身は入っていない**(`debug` コールバックだけで配る)。
- **`stage`**: `"derive"`(塊の導出直後)／`"mapping"`(`anchors`=境界条件の確定直後。探索・FEM が使う確定表の節点)／`"final"`(`rho`=探索後の最終確定マスク。0/1 の二値)。塊(`space`)は探索を通して1つ。
- WS: `run_optimization` メッセージの `debug: [...]`。`DEBUG_KINDS` に無い文字列は落とす。頼まれたときだけ `{"type":"optimize_debug",...}` を配る(**進捗とは別の口**。進捗に混ぜない)。画面: `debugFlags.ts` の `readDebugFlags(location.search)`(`?debug=space` / `?debug=anchors`、カンマでも `&` でも複数指定可、`?voxel=1`)、`viewport.setOptimizeDebug(state | null)`(まるごと差し替え。`anchors` には `grid` を必ず入れる=球半径 `spacing*0.34` の基準)。検査の穴 `__duocad.debugFlags/optimizeDebugCounts/optimizeDebugPayloads`。
- **フラグが無いときの画面は1ピクセルも変わらない**: `App.svelte` はフラグが1つも立っていなければ `setOptimizeDebug(null)` すら呼ばない。**デモ題材に固定しない**(材料はいま走らせた最適化から取る)。
- **面アンカーの球は `depthTest: false`**(落とすと裏側の節点が見えない)。見張りは固定と荷重の**両方**の材質を見る。
- **セル中心の規約**(`duocad/topopt/mesh.py` 付近): 要素 (i,j,k) の中心は `origin + spacing*(i+0.5,...)`、節点は `origin + spacing*(i,...)`。**変換点は `optimizeDebug.ts` の `buildDebugVoxels` の1箇所だけ**(サーバは素の添字を送る)。両側で足すと半要素ずれる(絵は出るので目視で気づけない)。上限超過は**間引いて** `stride/total/shown` を payload に出す(黙って全部送らず、黙って切り捨てない)。
- **罠**: `viewport?.setOptimizeDebug({...})` と直接書くと、この `$effect` は二度と再実行されない(初回 `viewport` が `null` でオプショナル連鎖が引数評価ごと短絡し依存を1つも登録できない。症状は「フラグを付けても何も出ない」で `npm run check` にもコンソールにも出ない)。**依存を先にローカルへ読み切ってから呼ぶ**。`$state` は入れ子まで Proxy なので、素のスプレッドで `window.__duocad` へ出すと puppeteer の `evaluate` で入れ子だけ `{}` になる。`$state.snapshot()` を通す。
- 失敗・中断のあともデバッグ表示を消さない(失敗したときこそ塊を見たい。次の実行が `runOptimization()` の先頭で消す)。`?voxel=1` で結果メッシュとボクセルが数十ms重なるのは過渡として受容。
- **撮影台** `node web/scripts/shoot-topopt.mjs`(重い段は `shoot_topopt_export.py` を自分で spawn する。**単体で呼ばない**): `--model <ファイル名>` + `--panel "<ラベル>:<つまみ>"` を2つ以上(つまみは `element_mm`。`--views` `--dir` `--zoom` `--out` `--models-dir` `--skip-export` `--width` `--height`)。カメラ・光源・背景・画角を面ごとに持てない構造(1枚の canvas に `setScissor` で並べる)を、分けて描く作りに戻さない。`_data` は出力先ごとに1つ(同じ `--out` へ別題材を撮ると上書き。`--skip-export` は manifest の `subject` を照合してから使う)。
- 検査設計の罠: 「真っ黒の合格」を防ぐ検査は**面ごと**に見る(全面の三角形数の合算 `>0` は1面だけ真っ黒でも合格する)。headless Chrome は `/favicon.ico` を必ず取りに行く(404 にするとページ側エラーの網にかかるので 204 を返す)。テンプレートリテラル内にバッククォート付き日本語コメントを書くとリテラルが閉じる(`shoot-topopt.mjs` の `PAGE` ではコード引用に `'` を使う)。

