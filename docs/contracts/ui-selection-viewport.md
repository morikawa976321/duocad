# 選択・ビューポート・ピックの契約

選択の同一性・3D ビューポート(射影・ビューキューブ・FOV・分割モジュール)・画面ピック・最適化デバッグビューの、ソースから読めない不変条件・依存禁止・罠。
**いまの挙動の正はコードとテスト**。設計判断の本体は [../VIEWPORT.md](../VIEWPORT.md) / [../UI.md](../UI.md)。
実機 smoke のうち現在 `web/scripts/` に残るのは `smoke.mjs` `smoke-sketch.mjs` `smoke-resolution.mjs` `smoke-mirror.mjs` `smoke-face-culling.mjs` `smoke-fillet-pick.mjs` と `tests/eval_*.mjs`。

---

## 選択参照の同一性(`duocad/selection.py` / `web/src/commands/command.ts`)

- **参照の同一性の鍵は `selection.ref_key(ref) -> tuple` の1箇所だけ**。形は region `("region", sketch, int(index))` / sketch_entity `("sketch_entity", sketch, entity, point)` / body `("body", body)`(添字を見ない)/ face・edge・vertex `(kind, body, int(index))`。参照の形でなければ `ref_kind` と同じ `ValueError`。等価判定は `ref_key(l) == ref_key(r)`。TS 側は `refKey(ref): string` で、`sameRef` はその一致で定義。
  - 等価判定を種別ごとの分岐として書き直さない(集合引きの重複除去と別の規則になり、**どちらも例外を出さないまま**重複が残る／別物が同一視される)。TS 側は `tests/eval_selection_ref_key.mjs` が押さえる。
  - 種別は鍵の第1要素だけが持つ。face/edge/vertex の鍵に `ref["kind"]` を重ねない(重ねると「種別を鍵から抜く」変異がもう一方に吸収され検査で示せない。意図的に3要素)。
  - `provenance` は鍵に入れない／`index` は `int()` に正規化／`point` は `.get()` で欠落と `None` を畳む。TS 側の `point` は `p` を付けて書く(`null` と空文字の点名を畳まないため)。
- **`modify_selection` の並び順は不変**: 追加は末尾に足すだけ、除去は残りの相対順序を保つ。既存参照の再 add で位置が動くと `fillet` の辺リストと半径リストの対応がずれる。
- **依存禁止**: `ref_key` の戻り値をシリアライズして保存しない／鍵の順序に意味を持たせない(並び順の正は `Selection.items` のリスト順)／TS `refKey` の文字列書式に外から依存しない(いま使うのは `CommandHost.svelte` の `selectionPrint`=再送判定の指紋だけ)。
- **罠**: `_coerce_ref` を通った body 参照は必ず `index: None` を持つので、「body の鍵に `index` を足す」変異は `modify_selection` 経由の検査では1件も落ちない。`ref_key` を直接叩いて `{"kind":"body","index":7}` を渡す検査が要る。
- **性能の測り方**: `modify_selection` の重複判定を鍵引きにして約810ms→0.8ms、残りは全部 `kernel.face_count`(M=2835 で 51.1ms)。fresh 起動5回の中央値で 1037ms→47.3ms。測るときは**プロセス1回につき1計測の fresh 起動5回の中央値**で見る(in-process の繰り返しは暖まりで 5〜15ms にばらつく)。

## プレビュー要求の寿命(`web/src/previewController.ts`)

- `App` が `createPreviewController(deps)` で1つ作り、`CommandHost` は引数・方式・選択の指紋を `update` へ渡す。
  要求番号、重複抑止、デバウンスの起点、再送する要求、2種類のタイマーはコントローラだけが所有する。
- 純関数 `transitionPreview(state, event)` は次の状態と送信・描画・タイマー操作の指示を返す。時刻はイベントに載せる。
  実行器は注入された送信・描画・時計だけを呼び、Workspace と入力欄の同期は持たない。
  共有引数の差分送信・入力表示との調整は `sharedArgsController.ts` が別に所有する
  ([ui-sketch-input.md](ui-sketch-input.md)「共有引数と入力表示の同期」)。引数の反映後も `CommandHost` の入力からプレビューを更新する。
- 応答を採れるのは、現在の入力・選択に対応する要求番号だけ。打ち直し・選択変更・形状配信・切断で古い応答の採用を止める。
  形が変わらない粗さだけの配信では再送せず、幽霊を残す。形状配信後の再送待ちと入力のデバウンス待ちは別々に保ち、送信順を変えない。
- 確定とコマンドの終了・入れ替わりは `cancel()`、子の破棄は `detach()`、
  `App` の破棄は `dispose()` で保留タイマー・再送用要求・応答待ちを終える。モード変更とプレビュー失敗の `clear()` は
  サーバ要求・再送を取り消し、入力の重複抑止と後から打った入力のデバウンスは残す。モード変更では子の破棄が残る入力待ちも終える。
  取消で要求番号を進める従来の契約と、送信の回数を保ち、取消済みタイマーのコールバックもトークンで拒否する。
  同じホスト内の `cancel()` は最大待ちの起点だけを従来どおり残す（送信・空入力・ローカル入力で解け、ホスト破棄で捨てる）。
- `window.__duocad.previewing` はサーバ計算の幽霊が表示されている印のまま。ローカル幽霊の表示では立てない。
  境界は `tests/eval_preview_controller.mjs` と `tests/test_ts_evaluators.py` が検査する。

## 面数・辺数のカウントのキャッシュ(`selection._coerce_refs`)

- `_coerce_refs(doc, refs, *, bodies=None, counts=None, allow_hidden=False)` は face/edge/vertex 参照を `(body_id, kind)` ごとに `kernel.face_count` / `edge_count` / `count_subshapes` を1回だけ呼んで使い回す。戻り値・例外の型とメッセージは `_coerce_ref` を1件ずつ呼んだときと同じ。モジュール内部関数(外から直接呼ぶ経路を増やさない)。戻りは入力 `refs` と1対1・同順。
- **`modify_selection` は `add` 用・`remove` 用の2回の `_coerce_refs` 呼び出しへ同じ `bodies`/`counts` 辞書を渡す**(引数の副作用で共有)。新しい辞書だと同じボディの面が混ざったとき最大2回数える(実物27ボディで `_face_map` 42回→27回)。`set_selection` は単発なので既定でよい。
- **可視性の門番は参照ごとに必ず通す**(`bodies` に入っているボディでも `visible` を見る。辞書は `allow_hidden` の違う呼び出しの間で共有されうる)。キャッシュしてよいのは数え上げであって門番ではない(不可視ボディへの参照が通ると AI が人の隠したものを黙って選ぶ)。門を外すのは外す側(`modify_selection` の `remove`・`toggle_selection` で選択に入っている参照)だけ: 隠したボディの項目は選択に残るので、外せないと「いまの選択を外して新しく選ぶ」が丸ごと拒否される。`tests/test_face_count_once_per_body.py` の `test_hidden_body_face_ref_is_refused_through_{modify,propose}_selection` が押さえる。
- **キャッシュ鍵は `(body_id, kind)` の両方**。`kind` を落とすと面数と辺数が入れ替わる(既存 pytest が捕まえる)。`body_id` を落とすと違うボディの面数が使い回される(**`two_shapes` fixture(箱+円柱)でしか捕まらない**)。
- **キャッシュの寿命は「1回の呼び出し」のローカル変数**。`kernel.py`・モジュールレベル・`Document`/`Body` レベルへ持ち出さない。
- **検査設計の罠**: 既存 pytest は全 fixture が同一寸法の箱(面6/辺12/頂点8)で、違うボディの面数の取り違えを検出できなかった。fixture を増やすなら**形状の種類まで変える**(寸法違いの箱では検出力は上がらない)。「1回の呼び出しで」という条件があるときは、内部で同じ処理を複数回行う経路(`add`/`remove` の2回)を洗い出して検証する。

## 矩形選択の射影(`web/src/viewport.ts` / `selectionBand.ts`)

- `projectRaw(x,y,z,width,height)` / `geometryInside(...,rect,width,height)` / `objectInside(...)` は末尾に `width, height` を取る。**`collectInBox` の入口で `size()` を1回だけ読んで下流へ渡す**。新しい呼び出し元も自分の呼び出し1回の中で `size()` を1回だけ読み、ループの中で読まない(実測 `collectInBox` 38.3ms→3.9ms、27部品2835面)。`geometryInside`/`objectInside` は `selectionBand.ts`。矩形選択以外で `projectRaw` を新設・流用するなら同じ規約に従う。
- **`size()` の結果をモジュール側の変数へキャッシュしない**(リサイズで同じ画素が別のものを指す)。巻き上げてよいのは「1回の呼び出しの中だけ」。画面ピックの `pickPointsAt` / `pickLinesAt` も同じ。
- **罠**: `web/dist` は `.gitignore` 対象のビルド成果物。`web/src` を編集しても `npm run build` を挟まないと実機 smoke・計測に反映されない(計測フックが「見つからない」で timeout する偽の失敗。`git status` は「ビルドした」の証拠にならない)。A/B で別サーバ起動をまたいで選択結果を比べるときは、`bodyId` がインポートごとに非決定的なので**ボディごとの面インデックス集合に正規化**してから比較する。

## 画面ピック(`web/src/screenPick.ts` / `polylinePick.ts` / `occlusion.ts` / `solidFeatures.ts` / `viewport.ts`)

規則(何をどの順で指せるか・遮蔽・矩形選択)の正は docs/VIEWPORT.md「ピック」。ここは作りの約束だけ。

- `screenPick.ts` は three.js 非依存の純関数。`distSqPointSegment`(t を [0,1] に clamp、長さ0でも0除算しない)・`pickNearestWithinRadius`(**半径は inclusive**、**同着は先に来た候補が勝つ**、`NaN` の `distSq` は最短を横取りしない)・`perspectiveParam`(画面上の位置を3Dの位置へ戻す透視補正)・`rankByOverlap`(同じ細かさの候補を並べる。スケッチの的に重なりの幅を足し、同着は鍵の順)。`polylinePick.ts` が world → screen の射影と折れ線の歩き方を持ち、距離と一緒に狙われた3Dの点を返す(`closestOnPolyline`。遮蔽の判定がその点へ視線を引く)。`polylineOf` は `{count, at(index,out)}` を返し world 座標配列を作らせない。
- **当たり半径は画面上の CSS px**。`EDGE_PICK_PIXELS`=6 / `VERTEX_PIXELS`=4 / `SKETCH_HANDLE_PIXELS`=7、重なりの幅 `PICK_OVERLAP_PIXELS`=2。world 空間の threshold を Raycaster へ渡す形へ戻さない(`tests/eval_screen_pick.mjs` 等が視点を動かしても 2px hit / 12px miss が変わらないことを固定)。
- **ホバー・クリック・`project*`・矩形選択は同じ種類(`pickKinds`)と同じ遮蔽で判定する**。`project*` は `resolveHit` で「その画素で本当にそれが取れるか」を確かめてから返す(面の `project` も。辺・頂点・スケッチの線が近くて取られる画素は返さない)。矩形選択だけは遮蔽を奥行きの読み出し(`occlusion.snapshot`)で引く。
- **依存禁止**: `Line2`(スケッチの図形)の `geometry.getAttribute('position')` を折れ線だと思わない(太線描画用の箱テンプレート8頂点)。world 座標の点列は `buildEntityLine` が入れる `userData.polylineWorld: THREE.Vector3[]`(矩形選択も同じものを読む)。`worldPerPixel()` は当たり判定の正ではない(遮蔽の許しは `occlusion.pixelAt` = その点の奥行きでの1画素)。`resolveHit` はホバーから毎フレーム走る(ポインタ・カメラ・世代が変わったときだけ)。
- **辺・頂点のデータはボディごと**(`viewport.setBodyEdges` / `setBodyVertices`)。渡すのは今の形のものだけ(`App.svelte` の `sameShape`: 版 `revision` は粗さでも上がるので、頼んだ時点の形の版 `shape_revision` で照らす)。メッシュボディは空で渡す(辺・頂点を持たない。待ち続けないため)。
- **未確認の手動受入**: 斜め視点で bezier に沿ったホバー、27部品モデルで狙った辺が取れること。
- **罠**: ズーム直後に判定を読むと時々落ちる(ズームが可変解像度の測り直しを起こし、スケッチの折れ線が作り直される)。`stateEpoch` が止まるまで待ってから測る。pytest は `.venv\Scripts\python.exe` で(素の `python` は `OCP` 無しで51件が収集エラー)。

## 辺だけを食うコマンド(フィレット・面取り)

- 起動中は辺だけを指せる(`pickKinds`。クリック・ホバー・矩形選択のすべて)。起動した時点で選択に残っている**面だけを外す**(辺・頂点などは残す。本人判断 2026-10-13)。矩形選択のトグルも連なり単位(`toggleUnits`)。
- 辺は**連なり**で選ぶ(`viewport.groupEdgeChains(true)`)。連なりは `duocad/kernel_chains.py` = OCCT のフィレットが辺1本から広げる範囲(一直線上で割れた辺・直線→円弧の接線連続)で、辺の折れ線には入れず、ホバー・クリック・矩形で囲んだ辺の分だけ `get_edge_chains` で取り寄せる(全辺ぶんは大きな部品で 0.5 秒。issue #167)。1本ずつ選ばせると、光った1本より広く丸まる(実測: 1本を `Add` すると連なり全部にフィレット・面取りがかかる)。連なりの届く前のクリック・矩形選択は、届くまで待ってから連なりで入れる(1本だけ入れない)。届くまでのホバーはその1本だけ光る。トグルは「全部入っていれば全部外す、そうでなければ欠けている分を足す」。矩形選択も連なりへ広げる。
- 選択・操作ログに入るのは辺1本ずつの添字のまま(カーネルの辺は統合しない。辺には面の `face_map` にあたる由来が無く、統合で添字が変わると保存済みログの再生が別の辺を指す)。測定・回転軸は連なりにしない(辺を1本として指す)。

## 直接ピックの待ち(回転軸・回転中心・鏡映面)

- 待っている種類は起動中に指せる種類へ足す(`pickKinds` = 選択の引数の種類 ∪ 待っている種類)。待ち受けは素のクリックだけを食い、修飾キー付きのクリックは対象の選択を足し外しする(`onPick`)。
- メッシュボディのピックは `App.svelte` の `pickedRef` でボディ参照へ畳む(畳まないと `modify_selection` が断り、ドラッグの一括選択が丸ごと落ちる)。`dedupeRefs` を外さない(重複 `{kind:"body"}` が `modify_selection` へ並ぶ)。**スケッチを貼る先だけは畳まず断る**。詳細は ui-optimize-integration.md。

## 分割モジュール(`viewport.ts` と兄弟)

- 分割の形は `planeGrid.ts` と同じ: `create*(deps)` がハンドルを返し、three.js オブジェクトは `object` として渡すだけ。`scene.add` と `renderOrder` は `viewport.ts` 側が行う。分割先から `viewport.ts` を import しない(逆向き依存ゼロ)。`viewCubeOverlay.ts` だけは同一 canvas に第2パスを描くので `renderer` を直接受け取る。
  - 兄弟: `viewportTypes.ts`(App に見せる型と `Viewport` IF)/ `viewportConstants.ts`(色・`LAYER`・深度バイアス・`FOV_*`・当たり判定画素・基準平面表)/ `viewCubeOverlay.ts` / `optimizeAnnotations.ts` / `optimizeDebug.ts` / `solidFeatures.ts`(ボディの辺・頂点の当たりと描画)/ `occlusion.ts`(遮蔽)/ `polylinePick.ts` / `selectionBand.ts` / `ghostLayer.ts` / `gridPlanes.ts` / `sketchCursorOverlay.ts`。`viewport.ts` は約2,900行。足す前に切れ目を探す(CLAUDE.md「ファイルの大きさ」)。
- **公開面を増やさない**: `viewport.ts` の `export` は `createViewport` + `Viewport` + 型 + `FOV_DEFAULT/FOV_ORTHO` + `isSelectableFov`。新モジュールの `create*`/`*Deps` を再エクスポートしない。`viewportConstants.ts` を `App.svelte` から import しない。three.js の型を Svelte 層へ出さない不変条件は「`viewport.ts` とその兄弟群の外へ出さない」。
- **`camera` は `let` で、投影切替(`activateCamera`)のたびに差し替わる**。`viewCubeOverlay` と `occlusion` は値ではなく `camera()` ゲッターで受け、各関数の先頭で `const camera = deps.camera()` を取る。値で渡すと正投影へ切り替えた後も古いカメラを使い続け、**型検査も実機 smoke も通ったまま**「見えている角をクリックしたのに隣が取れる」になる。
- **`THREE.Object3D.DEFAULT_UP.copy(WORLD_UP)` はモジュール読み込み時の副作用**で `viewportConstants.ts` にある。`OrbitControls` は生成時の `object.up` を焼き込み、`HemisphereLight` は `position` に `DEFAULT_UP` を複写するので、**この行より前にカメラ・controls・ライトを `new` してはいけない**。`viewportConstants.ts` を `import type` だけにすると `verbatimModuleSyntax` で丸ごと消えこの副作用も消える(いまは値 `LAYER` 等を import しているので生きている)。
- `SolidFeaturesDeps` は `pixelAt`(遮蔽の `occlusion.pixelAt`)だけを借り、射影は呼び手が渡す `ScreenProjector`(1回の当たり判定で固定したカメラ)を使う。`OcclusionDeps.surfaceDistance` は `viewport.ts` の `surfaceAlong`(面と断面の切り口。`faceHitAt` と同じ判定)。
- `setOptimizeState` は2箇所: 役割の表(`bodyRoles`/`fixtureFaceKeys`→`applyFaceMaterials`)は `viewport.ts`、装飾の生成は `optimizeAnnotations.draw(state)`。**`clear()`→役割→`applyFaceMaterials()`→`draw()` の順序に意味がある**(`draw` を先に呼ぶと役割色が1フレーム遅れる)。
- `pointerDown`/`pointerUp` は**キューブが引き取ったら `true`** を返し、`viewport.ts` の `onPointerDown`/`onPointerUp` がその戻り値で早期 return する。`optimizeAnnotations`/`optimizeDebug`/`solidFeatures` にグループを自分で `scene` へ足す実装へ変えない(初回フレーミングと near/far の計算対象が変わる)。
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
- WS: `run_optimization` メッセージの `debug: [...]`。`DEBUG_KINDS` に無い文字列は落とす。頼まれたときだけ `{"type":"optimize_debug",...}` を配る(**進捗とは別の口**。進捗に混ぜない)。画面: `debugFlags.ts` の `readDebugFlags(location.search)`(`?debug=space` / `?debug=anchors`、カンマでも `&` でも複数指定可、`?voxel=1`)、`viewport.setOptimizeDebug(state | null)`(まるごと差し替え。`anchors` には `grid` を必ず入れる=球半径 `spacing*0.34` の基準)。
- **フラグが無いときの画面は1ピクセルも変わらない**: `App.svelte` はフラグが1つも立っていなければ `setOptimizeDebug(null)` すら呼ばない。**デモ題材に固定しない**(材料はいま走らせた最適化から取る)。
- **面アンカーの球は `depthTest: false`**(落とすと裏側の節点が見えない)。
- **セル中心の規約**(`duocad/topopt/mesh.py` 付近): 要素 (i,j,k) の中心は `origin + spacing*(i+0.5,...)`、節点は `origin + spacing*(i,...)`。**変換点は `optimizeDebug.ts` の `buildDebugVoxels` の1箇所だけ**(サーバは素の添字を送る)。両側で足すと半要素ずれる(絵は出るので目視で気づけない)。上限超過は**間引いて** `stride/total/shown` を payload に出す(黙って全部送らず、黙って切り捨てない)。
- **罠**: `viewport?.setOptimizeDebug({...})` と直接書くと、この `$effect` は二度と再実行されない(初回 `viewport` が `null` でオプショナル連鎖が引数評価ごと短絡し依存を1つも登録できない。症状は「フラグを付けても何も出ない」で `npm run check` にもコンソールにも出ない)。**依存を先にローカルへ読み切ってから呼ぶ**。`$state` は入れ子まで Proxy なので、素のスプレッドで `window.__duocad` へ出すと puppeteer の `evaluate` で入れ子だけ `{}` になる。`$state.snapshot()` を通す。
- 失敗・中断のあともデバッグ表示を消さない(失敗したときこそ塊を見たい。次の実行が `runOptimization()` の先頭で消す)。`?voxel=1` で結果メッシュとボクセルが数十ms重なるのは過渡として受容。
- **撮影台** `node web/scripts/shoot-topopt.mjs`(重い段は `shoot_topopt_export.py` を自分で spawn する。**単体で呼ばない**): `--model <ファイル名>` + `--panel "<ラベル>:<つまみ>"` を2つ以上(つまみは `element_mm`。`--views` `--dir` `--zoom` `--out` `--models-dir` `--skip-export` `--width` `--height`)。カメラ・光源・背景・画角を面ごとに持てない構造(1枚の canvas に `setScissor` で並べる)を、分けて描く作りに戻さない。`_data` は出力先ごとに1つ(同じ `--out` へ別題材を撮ると上書き。`--skip-export` は manifest の `subject` を照合してから使う)。
- 検査設計の罠: 「真っ黒の合格」を防ぐ検査は**面ごと**に見る(全面の三角形数の合算 `>0` は1面だけ真っ黒でも合格する)。headless Chrome は `/favicon.ico` を必ず取りに行く(404 にするとページ側エラーの網にかかるので 204 を返す)。テンプレートリテラル内にバッククォート付き日本語コメントを書くとリテラルが閉じる(`shoot-topopt.mjs` の `PAGE` ではコード引用に `'` を使う)。

