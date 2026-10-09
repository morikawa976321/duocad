# 選択・ビューポート・ピックの契約

duocad の「選択の同一性」「3D ビューポート（射影・ビューキューブ・FOV・分割モジュール）」「画面ピック」「最適化デバッグビュー」まわりの、
ソースを読んでも分からない不変条件・依存禁止・踏んだ罠を、元の作業タスクの契約から分野別に蒸留した文書。
**いまの挙動の正はコードとテスト**で、ここは「なぜ壊れるか」の記録。設計判断の本体は [../VIEWPORT.md](../VIEWPORT.md) / [../UI.md](../UI.md) にある。

出典は項目末尾の括弧 `(タスクID, YYYY-MM-DD)`。日付はタスクIDの先頭8桁。
`(対象コード不在・要確認)` は、契約が名指した関数・ファイルをリポジトリ内で見つけられなかった印（移動先が分かれば併記）。
当時の実機 smoke 名（`smoke-move.mjs` `smoke-hover.mjs` `smoke-optimize*.mjs` `smoke-vertex.mjs` 等）の多くは、現在の `web/scripts/` に残っていない。
残っているのは `smoke.mjs` `smoke-sketch.mjs` `smoke-resolution.mjs` `smoke-mirror.mjs` `smoke-face-culling.mjs` と `tests/eval_*.mjs`。個別には再確認しない。

---

## 選択参照の同一性（`duocad/selection.py` / `web/src/commands/command.ts`）

- **参照の同一性の鍵は `selection.ref_key(ref) -> tuple` の1箇所だけ**。形は region `("region", sketch, int(index))` / sketch_entity `("sketch_entity", sketch, entity, point)` / body `("body", body)`（添字を見ない）/ face・edge・vertex `(kind, body, int(index))`。参照の形でなければ `ref_kind` と同じ `ValueError`（黙って別物にしない）。`same_ref` は `ref_key(l) == ref_key(r)` そのもの。TS 側は `refKey(ref): string` で、`sameRef` はその一致で定義。`(20260904-0211-duocad-selection-dedup-by-key, 2026-09-04)`
  - 等価判定を種別ごとの分岐として書き直さない。戻すと「集合引きが落とす重複」と「`same_ref` が同じと言う組」がずれ、**どちらも例外を出さないまま**重複が残る／別物が同一視される。TS 側は `tests/test_selection_ref_key.py::test_the_client_derives_same_ref_from_the_same_key`（静的）と `tests/eval_selection_ref_key.mjs`（実行）が押さえる。`(20260904-0211-duocad-selection-dedup-by-key, 2026-09-04)`
  - 種別は鍵の第1要素だけが持つ。face/edge/vertex の鍵に `ref["kind"]` を重ねて置かない（重ねると「種別を鍵から抜く」変異がもう一方に吸収され、その要素が効いていることを検査で示せなくなる。意図的に3要素）。`(20260904-0211-duocad-selection-dedup-by-key, 2026-09-04)`
  - `provenance` は鍵に入れない／`index` は `int()` に正規化／`point` は `.get()` で欠落と `None` を畳む（どれも `same_ref` の既存挙動の写し）。TS 側の `point` は `p` を付けて書く（`null` と空文字の点名を畳まないため）。`(20260904-0211-duocad-selection-dedup-by-key, 2026-09-04)`
- **`modify_selection` の並び順は不変**: 追加は末尾に足すだけ、除去は残りの相対順序を保つ。既存参照の再 add で位置が動くと `fillet` の辺リストと半径リストの対応がずれる。`(20260904-0211-duocad-selection-dedup-by-key, 2026-09-04)`
- **依存禁止**: `ref_key` の戻り値をシリアライズして保存しない（選択はセッション状態。タプルの要素数も種別で違う）／鍵の順序に意味を持たせない（並び順の正は `Selection.items` のリスト順）／TS `refKey` の文字列書式に外から依存しない（いまは `CommandHost.svelte` の `selectionPrint`＝再送判定の指紋だけが使う）。`(20260904-0211-duocad-selection-dedup-by-key, 2026-09-04)`
- **罠**: `_coerce_ref` を通った body 参照は必ず `index: None` を持つ。よって「body の鍵に `index` を足す」変異は `modify_selection` 経由の検査では1件も落ちない。`ref_key` を直接叩いて `{"kind":"body","index":7}` を渡す検査が要る。`(20260904-0211-duocad-selection-dedup-by-key, 2026-09-04)`
- **罠（性能の読み違え）**: `modify_selection` の重複判定を鍵引きにして約810ms→0.8ms になったが、当時の残り時間は全部 `kernel.face_count`（M=2835 で 51.1ms）。fresh 起動5回の中央値で 1037ms→47.3ms。測るときは**プロセス1回につき1計測の fresh 起動5回の中央値**で見る（in-process で繰り返すと JIT・キャッシュの暖まりで 5〜15ms にばらつき、5ms 未満の判定がノイズに埋もれる）。`(20260904-0211-duocad-selection-dedup-by-key, 2026-09-04; 20260904-0212-duocad-face-count-once-per-body, 2026-09-04)`

## 面数・辺数のカウントのキャッシュ（`selection._coerce_refs`）

- `_coerce_refs(doc, refs, *, bodies=None, counts=None)` は face/edge/vertex 参照を `(body_id, kind)` ごとに `kernel.face_count` / `edge_count` / `count_subshapes` を1回だけ呼んで使い回す。戻り値・例外の型とメッセージは `_coerce_ref` を1件ずつ呼んだときと完全に同じ。モジュール内部関数（外から直接呼ぶ経路を増やさない）。`(20260904-0212-duocad-face-count-once-per-body, 2026-09-04)`
- **`modify_selection` は `add` 用・`remove` 用の2回の `_coerce_refs` 呼び出しへ同じ `bodies`/`counts` 辞書を渡す**（引数の副作用で共有する設計）。呼び出しごとに新しい辞書だと、`add`/`remove` に同じボディの面が混ざったとき1回の呼び出しで最大2回数える（実物27ボディで修正前 `_face_map` 42回／修正後27回）。`propose_selection` は単発なので既定に任せてよい。`(20260904-0212-duocad-face-count-once-per-body, 2026-09-04)`
- **可視性の門番 `_visible_body` は body_id ごとに最低1回は必ず通す**。キャッシュしてよいのは数え上げであって門番ではない（不可視ボディへの参照が例外なしで通ると、AI が人の隠したものを黙って選ぶ経路が開く）。`tests/test_face_count_once_per_body.py` の `test_hidden_body_face_ref_is_refused_through_{modify,propose}_selection` が押さえる。`(20260904-0212-duocad-face-count-once-per-body, 2026-09-04)`
- **キャッシュ鍵は `(body_id, kind)` の両方**。`kind` を落とすと面数と辺数が入れ替わる（既存 pytest が捕まえる）。`body_id` を落とすと違うボディの面数が使い回される——**`two_shapes` fixture（箱＋円柱）でしか捕まらない**。`(20260904-0212-duocad-face-count-once-per-body, 2026-09-04)`
- **キャッシュの寿命は「1回の呼び出し」のローカル変数**。`kernel.py`・モジュールレベル・`Document`/`Body` レベルへ持ち出さない。`_coerce_refs` の戻りは入力 `refs` と1対1・同順（`modify_selection` の並び順保存がこの上に乗る）。`(20260904-0212-duocad-face-count-once-per-body, 2026-09-04)`
- **罠（検査設計）**: 既存 pytest は全 fixture が同一寸法の箱（面6/辺12/頂点8）で「違うボディの面数を取り違える」バグを検出できなかった（キャッシュ鍵から `body_id` を落としても緑）。fixture を増やすなら**形状の種類まで変える**（寸法違いの箱を増やしても検出力は上がらない）。また完了条件に「1回の呼び出しで」とあるときは、その呼び出しが内部で同じ処理を複数回行う経路（`add`/`remove` の2回）を洗い出して検証する。`(20260904-0212-duocad-face-count-once-per-body, 2026-09-04)`
- 契約のTS側（`App.svelte`・`command.ts`）に同種の面数キャッシュは要るか未検討（サーバ側のカウント回数のみの変更）。`(20260904-0212-duocad-face-count-once-per-body, 2026-09-04)`

## 矩形選択の射影（`web/src/viewport.ts` / `selectionBand.ts`）

- `projectRaw(x,y,z,width,height)` / `geometryInside(...,rect,width,height)` / `objectInside(...)` は末尾に `width, height` を取る。**`collectInBox` の入口で `size()` を1回だけ読んで下流へ渡す**。新しい呼び出し元を足すなら自分の呼び出し1回の中で `size()` を1回だけ読んで渡す（ループの中で読んではいけない。実測 `collectInBox` 38.3ms→3.9ms、27部品2835面）。`geometryInside`/`objectInside` は現在 `selectionBand.ts` にある。`(20260904-0213-duocad-hoist-viewport-size-out-of-loop, 2026-09-04)`
- **`size()` の結果をモジュール側の変数へキャッシュしない**。リサイズで寸法が変わり、同じ画素が別のものを指す。巻き上げてよいのは「1回の呼び出しの中だけ」。この規約は画面ピックの `pickPointsAt` / `pickLinesAt` にも同じに適用される。`(20260904-0213-duocad-hoist-viewport-size-out-of-loop, 2026-09-04; 20260911-0024-duocad-screen-pick-implementation, 2026-09-11)`
- 単発クリックの `projectRaw` 経路は元から1頂点なので対象外。矩形選択以外で `projectRaw` を新設・流用するなら同じ巻き上げ規約に従う。`(20260904-0213-duocad-hoist-viewport-size-out-of-loop, 2026-09-04)`
- **罠**: `web/dist` は静的配信のビルド成果物で `.gitignore` 対象。`web/src` を編集しても `npm run build` を挟まないと実機 smoke・計測に反映されない（計測フックが「見つからない」で timeout する偽の失敗）。コミットに現れないので、緑の `git status` は「ビルドした」の証拠にならない。A/B で別サーバ起動をまたいで選択結果を比べるときは、`bodyId` がインポートごとに非決定的なので**ボディごとの面インデックス集合に正規化**してから比較する。`(20260904-0213-duocad-hoist-viewport-size-out-of-loop, 2026-09-04; 20260910-1615-duocad-grid-follow-cursor, 2026-09-10; 20260911-0024-duocad-screen-pick-implementation, 2026-09-11)`

## 画面ピック（`web/src/screenPick.ts` / `viewport.ts` / `vertexMarkers.ts`）

- `screenPick.ts` は three.js 非依存の純関数（Node から直接読める）。`distSqPointSegment`（t を [0,1] に clamp、長さ0でも0除算しない）と `pickNearestWithinRadius`（**半径は inclusive**、**同着は先に来た候補が勝つ**、`NaN` の `distSq` は最短を横取りしない）。`viewport.ts` 内部の `projectForPick`（当たり判定専用の射影。`toScreen` と別物で viewport 矩形でも `clickable()` でも切らない）/ `pickPointsAt` / `pickLinesAt`（`polylineOf` は `{count, at(index,out)}` を返し world 座標配列を作らせない。実体は `bufferPolyline` と `sketchPolyline`）。`(20260911-0024-duocad-screen-pick-implementation, 2026-09-11)`
- **当たり半径は画面上の CSS px**。`EDGE_PICK_PIXELS`=6 / `VERTEX_PIXELS`=4 / `SKETCH_HANDLE_PIXELS`（契約時5、**現在コードは7**）。world 空間の threshold を Raycaster へ渡す形へ戻さない（`tests/eval_screen_pick.mjs` 等が視点を動かしても 2px hit / 12px miss が変わらないことを固定）。`(20260911-0024-duocad-screen-pick-implementation, 2026-09-11)`
- 優先順位は変えていない（スケッチは点→線、`resolveHit` は `preferVertices`→辺→領域→頂点→面。ゲート＝出していないものは指せない、もそのまま）。**tie-break は screen 距離最小へ変わった**（Raycaster の3D距離順ではない。意図的変更）。重なって見える辺で以前と違う辺が取れうる。`(20260911-0024-duocad-screen-pick-implementation, 2026-09-11)`
- **依存禁止**: `Line2`（スケッチの図形）の `geometry.getAttribute('position')` を折れ線だと思わない（太線描画用の箱テンプレート8頂点）。world 座標の点列は `buildEntityLine` が入れる `userData.polylineWorld: THREE.Vector3[]`（矩形選択も同じものを読む）。`worldPerPixel()` は当たり判定の正ではなくなった（残る用途はグリッド段・可変解像度・clip・カメラ静止判定・`resolveHit` の「面と基準平面の同着は面が勝つ」1画素しきい値）。`edgeAt` はクリック専用ではなくホバーから毎フレーム走る。`(20260911-0024-duocad-screen-pick-implementation, 2026-09-11)`
- **判断（Phase B を入れなかった根拠）**: Raycaster を粗い broad phase に降格する案は入れない。27部品 mATX PC ケース（`web/scripts/models.mjs` の `PC_CASE_STEP`）の最大ボディ（233面・辺645本・折れ線点1,768）、1280×860 で `edgeAt` 1回の上界が p50 0.2ms／p95 0.7ms／最大1.4ms（基準 p95>16ms に20倍以上の余裕）。`edgeLines` は常に1ボディぶん（`showEdges` の契約）なので部品数が増えても本数は増えない。計測スクリプトは未コミット、製品に計測フックは無い。`(20260911-0024-duocad-screen-pick-implementation, 2026-09-11)`
- **未確認の手動受入**: 斜め視点で bezier に沿ったホバー、27部品モデルで狙った辺が取れること（tie-break 変更で変わりうる）、`preferVertices` を通る実機 smoke（回転中心の頂点待ちだけが立つ。`vertexAt` 単体の検査しか無く退行は誰も捕まえない）。`docs/SCREEN_PICK.md` は存在しない（契約時の参照先。`(対象コード不在・要確認)`）。`(20260911-0024-duocad-screen-pick-implementation, 2026-09-11)`
- **罠**: ズーム直後に判定を読むと時々落ちる（ズームが可変解像度の測り直しを起こし、返ってきた状態でスケッチの折れ線が作り直される）。`stateEpoch` が止まるまで待ってから測る。新設検査の「歯」はスケッチ側の節にある（ソリッド辺は旧実装でも通る）。pytest は `.venv\Scripts\python.exe` で（素の `python` は `OCP` 無しで51件が収集エラー）。`(20260911-0024-duocad-screen-pick-implementation, 2026-09-11)`

## ピックの優先順位・頂点待ち（`preferVertices`）

- **角の頂点は既定のピック順序では絶対に取れない**。`resolveHit` は辺を頂点より先に見る（フィレット中に辺の端を掴むため）ので、辺が常に出ている状況では頂点狙いのクリックが100%辺に取られる。症状は「クリックしても何も起きない」だけでエラー無し。待っている間だけ `viewport.preferVertices(true)` で順序を入れ替える。**出しっぱなしにしない**（フィレット中に辺の端が掴めなくなる。戻す責任は要求側）。`(20260906-0916-duocad-move-verb-ui-rotation-picking, 2026-09-06)`
- 頂点マーカーは選択に入ったものしか出ない＝指せる的が画面に無い。待っている間だけ対象ボディの全頂点を候補として出す配線（`vertexCandidateBody`、`__duocad.wantedPick` とともに実機検査用の穴。製品の分岐に使わない）。出した候補は待ち受けが降りた瞬間に消える。`(20260906-0916-duocad-move-verb-ui-rotation-picking, 2026-09-06)`
- メッシュボディのピックは `App.svelte` の `pickedRef` でボディ参照へ畳む（畳まないと `select_face` が断り、ドラッグの一括選択は1回でまとめて送るので選択が丸ごと落ちる）。`dedupeRefs` を外さない（同じメッシュの面が何枚囲まれても参照は1つに畳まれ、重複 `{kind:"body"}` が `modify_selection` へ並ぶ）。**スケッチを貼る先だけは畳まず断る**。詳細は ui-optimize-integration.md。`(20260904-1705-duocad-optimize-run-ui, 2026-09-04)`

## 頂点マーカー・分割モジュール（`viewport.ts` と兄弟）

- 分割の形はすべて `planeGrid.ts` と同じ: `create*(deps)` がハンドルを返し、three.js オブジェクトは `object` として渡すだけ。`scene.add` と `renderOrder` は `viewport.ts` 側が行う（分割先は `scene` を知らない）。分割先から `viewport.ts` を import しない（逆向き依存ゼロ）。`viewCubeOverlay.ts` だけは同一 canvas に第2パスを描くので `renderer` を直接受け取る。`(20260906-0200-duocad-split-viewport, 2026-09-06)`
  - 当時の7ファイル: `viewportTypes.ts`（App に見せる型と `Viewport` IF）/ `viewportConstants.ts`（色・`LAYER`・深度バイアス・`FOV_*`・当たり判定画素・基準平面表）/ `viewCubeOverlay.ts` / `optimizeAnnotations.ts` / `optimizeDebug.ts` / `vertexMarkers.ts`。その後さらに `selectionBand.ts` `ghostLayer.ts` `gridPlanes.ts` `sketchCursorOverlay.ts` 等へ切り出されており、`viewport.ts` は約2,900行（契約時3,198行で上限3,200まで2行しか無かった）。足す前に切れ目を探す（CLAUDE.md「ファイルの大きさ」）。
- **公開面を増やさない**: `viewport.ts` の `export` は `createViewport` + `Viewport` + 型 + `bodyGhostMatrix` + `FOV_MIN/MAX/DEFAULT/ORTHO` + `isSelectableFov`。新モジュールの `create*`/`*Deps` を再エクスポートしない。`viewportConstants.ts` を `App.svelte` から import しない（画角の口は `viewport.ts` 経由）。three.js の型を Svelte 層へ出さない不変条件は「`viewport.ts` とその兄弟群の外へ出さない」で読む。`(20260906-0200-duocad-split-viewport, 2026-09-06)`
- **`camera` は `let` で、投影切替（`activateCamera`）のたびに差し替わる**。`viewCubeOverlay` と `vertexMarkers` は値ではなく `camera()` ゲッターで受け、各関数の先頭で `const camera = deps.camera()` を取る。値で渡すと正投影へ切り替えた後もキューブと頂点のレイだけ古いカメラを使い続け、**型検査も実機 smoke も通ったまま**「見えている角をクリックしたのに隣が取れる」になる。`(20260906-0200-duocad-split-viewport, 2026-09-06)`
- **`THREE.Object3D.DEFAULT_UP.copy(WORLD_UP)` はモジュール読み込み時の副作用**で `viewportConstants.ts` にある。`OrbitControls` は生成時の `object.up` を焼き込み、`HemisphereLight` は `position` に `DEFAULT_UP` を複写するので、**この行より前にカメラ・controls・ライトを `new` してはいけない**。`viewportConstants.ts` を `import type` だけにすると `verbatimModuleSyntax` で丸ごと消えこの副作用も消える（いまは値 `LAYER` 等を import しているので生きている。値 import が全部なくなる改修で真っ先に壊れる）。`(20260906-0200-duocad-split-viewport, 2026-09-06)`
- `vertexMarkers` は本編と同じ `raycaster` を共有していた（`vertexAt` が `raycaster.params.Points` を一時的に書き換えて戻す）。**画面ピック実装後は `VertexMarkersDeps` から `size`/`camera`/`raycaster`/`worldPerPixel` が外れ、`pickPointsAt` に置き換わった**ので、この共有 raycaster の話は現行ではない。`highlightColor` は共有オブジェクトの参照渡し（`vertexMarkers` が `highlightColor.vertex` を書き、`viewport.ts` の `highlightColors()` が読む。値渡しにすると `smoke-vertex` の色検査だけが静かに古い値で通る）。`(20260906-0200-duocad-split-viewport, 2026-09-06; 20260911-0024-duocad-screen-pick-implementation, 2026-09-11)`
- `faceUvBounds` は最適化の装飾モジュールへ移していない（実際の呼び手は任意面グリッド `refreshGridSketchFrame`→`planeGrid.setBounds`。一緒に動かすとグリッド枠がスケッチと無関係なモジュールに載る）。`(20260906-0200-duocad-split-viewport, 2026-09-06)`
- `setOptimizeState` は2箇所に分かれた。役割の表（`bodyRoles`/`fixtureFaceKeys`→`applyFaceMaterials`）は `viewport.ts`、装飾の生成は `optimizeAnnotations.draw(state)`。**`clear()`→役割→`applyFaceMaterials()`→`draw()` の順序に意味がある**（`draw` を先に呼ぶと役割色が1フレーム遅れる）。`(20260906-0200-duocad-split-viewport, 2026-09-06)`
- `pointerDown`/`pointerUp` は**キューブが引き取ったら `true`** を返し、`viewport.ts` の `onPointerDown`/`onPointerUp` がその戻り値で早期 return する。`optimizeAnnotations`/`optimizeDebug`/`vertexMarkers` にグループを自分で `scene` へ足す実装へ変えない（初回フレーミングと near/far の計算対象が変わる）。`(20260906-0200-duocad-split-viewport, 2026-09-06)`
- 分割が純粋な移設であることはテキスト一致（連結差分ゼロ、挿入した `const camera = deps.camera()` 等を除く）と公開面差分ゼロで機械確認した。以後の分割も同じ方式で確認する。`(20260906-0200-duocad-split-viewport, 2026-09-06)`

## ビューキューブ（`viewCubeOverlay.ts`）

- キューブ用カメラは本編と同じ**透視/正投影の2台持ち**（`cubePerspective`/`cubeOrthographic`）。`updateCubeCamera()` が `renderCubePass()` の先頭で毎フレーム、本編の投影種別（`camera === orthographic`）を見て `cubeActive` を指し替え、姿勢（`position/up`）と透視のとき `fov/near/far` を引き直す。定数 `CUBE_ORTHO_HALF`(1.8) / `CUBE_CAMERA_DISTANCE`(4、正投影のみ) / `CUBE_CIRCUMRADIUS`(√3) / `CUBE_NEAR_SAFETY`(0.5) / `CUBE_FAR_MARGIN`(1.5)。キューブの内部カメラは `Viewport` インターフェースに公開していない。`(20260904-0311-duocad-view-cube-follows-fov, 2026-09-04)`
- **`cubeHitAt`（ピック）と `renderCubePass`（描画）は常に同じ `cubeActive` でレイ/描画を作る**。カメラを増やす・差し替えるならこの対称性を壊さない。`(20260904-0311-duocad-view-cube-follows-fov, 2026-09-04)`
- 透視のときの距離式は `CUBE_ORTHO_HALF / sin(fov/2)`。**FOV の値をキューブ側へ写して保持しない**（`perspective.fov` を毎フレーム直接読む。保持すると `setFov` からの明示通知が要りその場追従が壊れる）。near/far は `distance` から毎フレーム引き直し、定数で固定しない（FOV 変化で手前の角が食われるかラベルが z-fighting する）。`cubeLight` は `cubeActive` の子で、投影切替のたびに `updateCubeCamera` が付け替える（忘れるとキューブが真っ暗）。`(20260904-0311-duocad-view-cube-follows-fov, 2026-09-04)`
- **既定視点 `HOME_VIEW_OFFSET=(1,-1,0.6)` は立方体の対角 (1,1,1) 方向そのものではない**。この向きで FOV90 にするとキューブの見た目が非対称・くさび形に見える（バウンディングボックスが対角視点比で線形約18%小さい実測）。**バグではなく、球フィット距離式を非球体・非対角視点へ適用したときの数学的帰結**（NDC z は全頂点 [-1,1] に収まり near/far クリップではない。(1,-1,1) の真の対角へスナップすれば FOV90 でも他 FOV と遜色なし）。気になるなら距離式の係数調整ではなく視点ごとに係数を変える等の別設計判断。`(20260904-0311-duocad-view-cube-follows-fov, 2026-09-04)`
- **罠**: FOV90 の見た目を近接クリッピングのバグと誤認しかけた。見た目の印象で判断する前に NDC 等を数値で機械的に確かめ、さらに真の対角へスナップして比較して切り分けた。`puppeteer-core` 25系は `page.waitForTimeout` が無い——固定時間待ちは `page.evaluate(() => new Promise(r => setTimeout(r, ms)))`。`(20260904-0311-duocad-view-cube-follows-fov, 2026-09-04)`

## FOV・初期カメラ（`viewportConstants.ts` / `viewport.ts` / `SettingsDialog.svelte`）

- `FOV_DEFAULT` は 30（旧45）。`FOV_MIN`(30)/`FOV_MAX`(90)/`FOV_ORTHO`(0)/`isSelectableFov`/`setFov` は**内部 API として 0 または 30〜90 の連続値を受け付けたまま**。画面の口は `#settings-ortho` チェックボックス1つだけ（`#settings-fov` スライダー／`#settings-fov-value` は要素ごと無い）。`onchange` は `onFov(checked ? FOV_ORTHO : FOV_DEFAULT)`。**正投影を外すと常に `FOV_DEFAULT` へ戻る**（内部 API で 70 等を入れていても）。ラベルは「正投影」の1語で角度を出さない。`(20260904-2138-duocad-fov-ui-becomes-binary, 2026-09-04)`
- **`isSelectableFov` の受理範囲を `{0,30}` のような離散集合へ絞ってはならない**。基準平面と面の共平面ピック回帰検査（FOV 45〜55° スイープ）が内部 API の連続値に依存している。`(20260904-2138-duocad-fov-ui-becomes-binary, 2026-09-04)`
- **起動直後のカメラ距離は `fitDistance(diagonal / 2)` を通す**（`createViewport` 冒頭、`camera.position.copy(HOME_VIEW_OFFSET).multiplyScalar(distance)` の直前）。`diagonal * 1.2` のような決め打ち定数を再導入しない。`SKETCH_FIT_MARGIN`（1.243）は `fitDistance` と組み合わさって常に FOV 非依存（現在のカメラ FOV で自動的に辻褄が合う）。FOV 関連の定数を変える前に `grep -rn "diagonal \* [0-9]" web/src/viewport.ts` で `fitDistance` を経由しない決め打ちが無いか確認する。`(20260904-2138-duocad-fov-ui-becomes-binary, 2026-09-04)`
- **罠**: 45→30 に変えただけだと、45度時代の `diagonal * 1.2` が起動時カメラ位置とスケッチ未内容時の `lookAtSketch`（半径0で「前の距離のまま」に倒す経路）に生きていて、カメラが1.55倍近くに寄ったまま固まった。症状は原因を名指ししない「線分（辺）をクリックで選べない（タイムアウト）」。**赤が出ても自分の変更のせいと決めつけず、pristine との往復比較で切り分ける**（smoke-features の一過性 flake は pristine/変更後とも2連続緑、smoke-move は pristine 緑・変更後2連続赤＝本物の回帰）。`(20260904-2138-duocad-fov-ui-becomes-binary, 2026-09-04)`

## 最適化デバッグビューと撮影台

（最適化の実行そのものは ui-optimize-integration.md。ここは画面の可視化。）

- サーバ: `optimize_run.run(..., debug=None, debug_kinds=None)`（`Session.run_optimization` が素通し）。`debug` は `Callable[[dict], None]`、`debug_kinds` は `DEBUG_KINDS=("space","anchors","rho")` の部分集合。**頼まなければ配列を1本も作らない**。形は素の値のみ（`json.dumps` が通る）: `{"kind":"space"|"rho","stage":"derive"|"final","grid":{origin,spacing,shape},"elements":[C順の通し添字],"total","shown","stride"}`（`rho` は `"threshold":0.5`）／`{"kind":"anchors","stage","grid","fixed":{face,count,points,area_mm2},"load":{...}}`（`points` はワールド mm の平坦配列）。定数 `DEBUG_VOXEL_LIMIT`(15万)/`DEBUG_KINDS`/`DEBUG_RHO_THRESHOLD`(0.5)。**`run()` の戻り値にデバッグの中身は入っていない**（`debug` コールバックだけで配る）。`(20260905-1700-duocad-debug-views-and-shooting-rig, 2026-09-05)`
- **`stage` は2回来る**: `"derive"` は実行ボタンから数秒で届く速報（案1=bbox の塊。案5を必ず含む**上界**であって本番の塊ではない）、`"final"` が本番の塊と実際に解いた密度場。後から来たほうで置き換える。`(20260905-1700-duocad-debug-views-and-shooting-rig, 2026-09-05)`  
  （`rho` は契約時 SIMP 連続密度の閾値描画、案1/案5は当時の塊導出。現行の最適化は二値探索に置き換わっている。`DEBUG_KINDS`/`stage` の配り方は現行 `_DebugSink` に残るが、`rho` と案1・案5の記述は `(要確認: どちらが現行か不明)`。）
- WS: `run_optimization` メッセージの `debug: [...]`。`DEBUG_KINDS` に無い文字列は落とす。頼まれたときだけ `{"type":"optimize_debug",...}` を配る（**進捗とは別の口**。進捗に混ぜない）。画面: `debugFlags.ts` の `readDebugFlags(location.search)`（`?debug=space` / `?debug=anchors`、カンマでも `&` でも複数指定可、`?voxel=1`）、`viewport.setOptimizeDebug(state | null)`（まるごと差し替え。`anchors` には `grid` を必ず入れる＝球半径 `spacing*0.34` の基準）。検査の穴 `__duocad.debugFlags/optimizeDebugCounts/optimizeDebugPayloads`。`(20260905-1700-duocad-debug-views-and-shooting-rig, 2026-09-05)`
- **フラグが無いときの画面は1ピクセルも変わらない**: `App.svelte` はフラグが1つも立っていなければ `setOptimizeDebug(null)` すら呼ばない。**デモ題材に固定しない**（材料はいま走らせた最適化そのものから取る。`design_space.CASES` を参照する経路は本体に1本も無い）。`(20260905-1700-duocad-debug-views-and-shooting-rig, 2026-09-05)`
- **面アンカーの球は `depthTest: false`**（落とすと裏側の節点が見えず「見えているつもり」になる）。見張りは固定と荷重の**両方**の材質を見る（片方だけだと他方の退化を見逃す）。`(20260905-1700-duocad-debug-views-and-shooting-rig, 2026-09-05)`
- **セル中心の規約**（`duocad/topopt/mesh.py` 付近）: 要素 (i,j,k) の中心は `origin + spacing*(i+0.5,...)`、節点は `origin + spacing*(i,...)`。**変換点は `viewport.ts`（現 `optimizeDebug.ts`）の `buildDebugVoxels` の1箇所だけ**（サーバは素の添字を送る）。両側で足すと半要素ずれる——絵は出るので目視で気づけない。上限超過は黙って全部送らず黙って切り捨てず**間引いて** `stride/total/shown` を payload に出す。`(20260905-1700-duocad-debug-views-and-shooting-rig, 2026-09-05)`
- **罠**: `viewport?.setOptimizeDebug({...})` と直接書くと、この `$effect` は二度と再実行されない（初回 `viewport` が `null` でオプショナル連鎖が引数評価ごと短絡し依存を1つも登録できない。症状は「フラグを付けても何も出ない」で `npm run check` にもコンソールにも出ない）。**依存を先にローカルへ読み切ってから呼ぶ**。`$state` は入れ子まで Proxy なので、素のスプレッドで `window.__duocad` へ出すと puppeteer の `evaluate` で入れ子だけ `{}` になる——`$state.snapshot()` を通す。`(20260905-1700-duocad-debug-views-and-shooting-rig, 2026-09-05)`
- 失敗・中断のあともデバッグ表示を消さない（失敗したときこそ塊を見たい。古くならないのは次の実行が `runOptimization()` の先頭で消すため）。`?voxel=1` で結果メッシュとボクセルが数十ms重なるのは過渡として受容。`(20260905-1700-duocad-debug-views-and-shooting-rig, 2026-09-05)`
- **撮影台** `node web/scripts/shoot-topopt.mjs`（重い段は `shoot_topopt_export.py` を自分で spawn する。**単体で呼ばない**）: 現行は `--model <ファイル名>` ＋ `--panel "<ラベル>:<つまみ>"` を2つ以上（つまみは `element_mm`。`--views` `--dir` `--zoom` `--out` `--models-dir` `--skip-export` `--width` `--height`）。契約時にあった `--case` と σ/Taubin/rmin のつまみは SIMP 廃止とともに消えた。カメラ・光源・背景・画角を面ごとに持てない構造（1枚の canvas に `setScissor` で並べる）を、分けて描く作りに戻さない。`_data` は出力先ごとに1つ（同じ `--out` へ別題材を撮ると上書き。`--skip-export` は manifest の `subject` を照合してから使う）。`(20260905-1700-duocad-debug-views-and-shooting-rig, 2026-09-05)`
- 罠（検査設計）: 「真っ黒の合格」を防ぐ検査は**面ごと**に見る（全面の三角形数を合算して `>0` を見ると1面だけ真っ黒でも合格する）。headless Chrome は `/favicon.ico` を必ず取りに行く（404 にするとページ側エラーの網にかかるので 204 を返す）。テンプレートリテラル内にバッククォート付き日本語コメントを書くとリテラルが閉じる（`shoot-topopt.mjs` の `PAGE` ではコード引用に `'` を使う）。`(20260905-1700-duocad-debug-views-and-shooting-rig, 2026-09-05)`
