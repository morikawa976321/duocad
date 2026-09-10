# 当たり判定を画面上の半径[px]に揃える

**この文書の役割:** スケッチの点・線、ソリッドの辺・頂点の**クリック当たり判定**を、
Three.js Raycaster の world 空間 threshold から **CSS ピクセル距離**へ移した設計。
コードの正は `web/src/screenPick.ts` / `web/src/viewport.ts` / `web/src/vertexMarkers.ts` /
`web/src/viewportConstants.ts`。

| 何が | どこにあるか |
|---|---|
| 当たり半径の定数（5px / 6px / 4px） | `web/src/viewportConstants.ts` |
| 距離計算の純関数（three.js 非依存） | `web/src/screenPick.ts` |
| 射影と候補集め | `viewport.ts` の `projectForPick` / `pickPointsAt` / `pickLinesAt` |
| スケッチ図形の当たり | `viewport.ts` の `sketchEntityAt` |
| ソリッド辺の当たり | `viewport.ts` の `edgeAt` |
| 頂点マーカーの当たり | `vertexMarkers.ts` の `vertexAt` |
| 当たったあとの座標（スナップ） | `App.svelte` の `snappedPlanePoint` → `entityKinds.ts` の `snapToSketchEntity` |
| 画面1px の world 換算（**当たり判定からは外す**） | `viewport.ts` の `worldPerPixel()` ← `gridScale.ts` の `mmPerPixel` |
| 射影（既存・流用） | `viewport.ts` の `projectRaw` / `toScreen` |
| **本書** | 問題・設計・差し替え箇所・テスト・実装順 |

---

## 背景：何が「論理半径」になっていたか

定数 `SKETCH_HANDLE_PIXELS`(5) / `EDGE_PICK_PIXELS`(6) / `VERTEX_PIXELS`(4) は
コメント上「画素で決める」と書いてあったが、実際には

```typescript
raycaster.params.Line = { threshold: worldPerPixel() * EDGE_PICK_PIXELS }
raycaster.params.Points = { threshold: worldPerPixel() * SKETCH_HANDLE_PIXELS }
```

のように **world 空間[mm] の threshold** として Raycaster に渡している
（`sketchEntityAt` / `edgeAt` / `vertexAt` が同型）。

`worldPerPixel()` は注視点付近の代表値（`gridScale.mmPerPixel`）なので、

| 現象 | 原因 |
|---|---|
| ズームで掴みやすさが変わる | 注視点と対象の奥行きが違うと、同じ world threshold でも screen 半径が変わる |
| 斜めから見た線の当たりが楕円状 | Raycaster の Line threshold は 3D 垂線距離。screen 上の「太さ」と一致しない |
| 点の当たりが見た目のマーカーとずれる | Points threshold も 3D 球。`sizeAttenuation: false` の見た目は screen 円 |

**スケッチの線に至っては、そもそも定数が効いていなかった**（2026-09-11 実測）——
`handleLines` は太線（`Line2`）で、`LineSegments2.raycast` は `raycaster.params.Line.threshold`
を**見ずに** `material.linewidth` を使う。つまり当たり半径は `EDGE_PICK_PIXELS`(6) ではなく
**描いている線の太さ `SKETCH_LINE_WIDTH_PX`(1.5)** で、線から2px外すともう掴めなかった。

**狙い:** 定数を字面どおり **画面上の半径[CSS px]** として使い、
カーソル `(offsetX, offsetY)` から射影後の幾何までの **2D 距離**で hit / miss を決める。

**スコープ外（変えない）:**

- 1mm グリッドへの丸め（`App.svelte` の `snappedToGrid1mm`）
- 当たったあと UV へ落とす幾何（`entityKinds.ts` の各 `snap()`）
- 面メッシュのピック（三角形 Raycaster のまま）
- 矩形ウィンドウ選択（既に `projectRaw` で screen 判定）

---

## 設計

### 方針

1. **純関数を `web/src/screenPick.ts` に集約** —— three.js を import しない
   （`gridScale.ts` / `entityKinds.ts` と同じ。Node から直接テストできる）。
2. **`viewport.ts` は射影だけ担当** —— `Vector3.project(camera)` で world → CSS px。
3. **優先順位は現行維持** —— スケッチは点 → 線、ソリッドは `resolveHit` のゲート順のまま。
4. **tie-break** —— 半径内に複数候補があるときは **screen 距離が最小**のものを取る
   （Raycaster の 3D 距離順とは限らない）。

### 新モジュール `screenPick.ts`

```typescript
/** 点 P から線分 AB までの距離²[px²]（t を [0,1] に clamp） */
export function distSqPointSegment(px, py, ax, ay, bx, by): number

export interface ScreenPickCandidate<T> {
  item: T
  distSq: number
}

/** 半径 radiusPx 以内で distSq 最小の候補。無ければ null */
export function pickNearestWithinRadius<T>(
  radiusPx: number,
  candidates: Iterable<ScreenPickCandidate<T>>,
): T | null
```

`entityKinds.ts` の `projectToSegment` と同じ clamp 式を使う（UV 用と screen 用で式を二重管理しないよう、
将来共通化してもよいが、初回は screenPick 内に閉じる）。

### 射影ヘルパ（`viewport.ts` 内）

当たり判定専用。**`toScreen` とは分ける。**

| | `toScreen`（既存） | `projectForPick`（新規） |
|---|---|---|
| viewport 外 | `null` | **距離計算のため返す**（クリップしない） |
| `clickable()` チェック | する | **しない**（cube 上は呼び出し側で除外済み） |
| z（カメラ後ろ） | `null` | `null`（`projected.z` が clip 外なら除外） |

```typescript
function projectForPick(
  world: THREE.Vector3,
  width: number,
  height: number,
): { x: number; y: number } | null {
  return projectRaw(world.x, world.y, world.z, width, height)
}
```

`projectRaw` は既に viewport 矩形クリップをしない。`toScreen` が持つ `clickable` / 矩形チェックだけ外す。
**寸法を引数で受ける**のは `projectRaw` と同じ理由 —— 1回の pick で数千点を射影するので、
点ごとに `size()`（DOM 読み取り）を呼ばせない。`size()` を読むのは `pickPointsAt` /
`pickLinesAt` の入口で1回だけ（リサイズのたびに読み直すので、寸法が変わったときのずれは出ない）。

### 点の pick

```typescript
function pickPointsAt<T>(
  offsetX: number,
  offsetY: number,
  radiusPx: number,
  meshes: readonly THREE.Points[],
  readItem: (mesh: THREE.Points) => T | null,
): T | null
```

各 `Points` メッシュについて:

1. `position` 属性 × `matrixWorld` で world 座標
2. `projectForPick` → screen
3. `distSq = (sx - offsetX)² + (sy - offsetY)²`
4. `pickNearestWithinRadius(radiusPx, candidates)`

### 線の pick

```typescript
/** 折れ線1本ぶんの world 座標の読み口。**配列は作らない**。 */
interface PickPolyline {
  count: number
  at(index: number, out: THREE.Vector3): void
}

function pickLinesAt<L extends THREE.Object3D, T>(
  offsetX: number,
  offsetY: number,
  radiusPx: number,
  lines: readonly L[],
  readItem: (line: L) => T | null,
  polylineOf: (line: L) => PickPolyline | null,
): T | null
```

折れ線を隣接ペアごとに:

1. 両端を world → screen（どちらか `null` ならその segment は skip）
2. `distSqPointSegment(offsetX, offsetY, ax, ay, bx, by)`
3. 全 segment の最小 distSq で `pickNearestWithinRadius`

**点列の出どころは2つあり、同じようには読めない**（`polylineOf` を分けてある理由）:

| 対象 | 読み口 | 理由 |
|---|---|---|
| ソリッドの辺（`THREE.Line`） | `geometry` の `position` 属性 × `matrixWorld` | 素の `BufferGeometry` |
| スケッチの図形（`Line2`） | `userData.polylineWorld` | `LineGeometry` の `position` は**太線を描く箱のテンプレート**であって折れ線ではない |

**bezier / 円弧**は当たり判定用の折れ線（`handleLines`）が既に解像度付きで存在するので、
screen pick 側は **その折れ線を信じる**（`entityKinds` の `snap` との整合は現行と同じ）。

---

## 差し替え箇所

### 1. `sketchEntityAt`（`viewport.ts`）

```
現: Raycaster + worldPerPixel * SKETCH_HANDLE_PIXELS / EDGE_PICK_PIXELS
新:
  hit = pickPointsAt(..., SKETCH_HANDLE_PIXELS, target.handleMeshes, ...)
  if (hit) return { sketchId, entityId, point }
  hit = pickLinesAt(..., EDGE_PICK_PIXELS, target.handleLines, ...)
  if (hit) return { sketchId, entityId, point: null }
  return null
```

`worldPerPixel()` の import / 使用をこの関数から削除。

### 2. `edgeAt`（`viewport.ts`）

ソリッド辺は本数が多い（27部品で数千本）。**二段階**にする。

**Phase A（実装したのはこちら）:** 全 `edgeLines` を screen pick（スケッチと同じ `pickLinesAt`）。
　候補が多いが、まず正しさを取る。**計測の結果 Phase B は要らなかった**（下の「性能」）。

**Phase B（要らなかった）:** Raycaster を **粗い broad phase** に降格。

1. `threshold = worldPerPixel() * EDGE_PICK_PIXELS * BROAD_PHASE_FACTOR`（例: 3〜5）
2. ヒットした Line だけ screen 距離で再判定
3. 再判定を通ったものだけ `pickNearestWithinRadius`

Broad phase 用の `worldPerPixel` は **候補絞り込みだけ**に残す（screen 半径の正ではない）。

### 3. `vertexAt`（`vertexMarkers.ts`）

`pickPointsAt` + `VERTEX_PIXELS`。deps から `worldPerPixel` を外せる
（`VertexMarkersDeps` の型も更新）。

### 4. `viewportConstants.ts`

定数の docstring を更新:

```typescript
/** …当たり半径[CSS px]。screen 空間距離で判定する(screenPick.ts)。 */
```

`worldPerPixel` への言及を削除。

### 5. 触らないもの

| ファイル | 理由 |
|---|---|
| `App.svelte` | 当たり結果の型は同じ。スナップ入口は変更不要 |
| `entityKinds.ts` | UV 射影は当たり後の話 |
| `gridScale.ts` | 格子表示・clip 用の `mmPerPixel` は残る |
| 矩形選択 `selectInBox` | 既に screen 射影。整合は「同じ `projectRaw`」で自然に取れる |

---

## 性能

| 経路 | 頻度 | 想定 |
|---|---|---|
| `sketchEntityAt` | mousemove（hover） | 候補は数十 Line + 数十 Points。全走査で十分 |
| `edgeAt` | mousemove（hover）とクリック | 1Body ぶんの Line × 頂点数。>16ms なら Phase B |
| `vertexAt` | クリック | 選択中の頂点だけ。無視できる |

**`edgeAt` はクリック専用ではない** —— `resolveHit` はホバーの解決にも使われるので、
ポインタを動かしているあいだ毎フレーム走る。

**実測（2026-09-11・Phase A）**: 27部品の mATX PC ケース、最大のボディ（233面・**辺645本 /
折れ線の点 1,768**）を 1280×860 で表示し、`__duocad.projectEdge`（内部で `edgeAt` を1〜7回
呼ぶ = `edgeAt` 1回の上界）を計測 —— 見えている辺1本を300回で **p50 0.2ms / p95 0.7ms /
最大 1.4ms**、645本を1周して **p50 0.2ms / p95 0.3ms / 最大 1.6ms**。
16ms に対して20倍以上の余裕があるので **Phase B は入れない**。
`edgeLines` が持つのは常に**1つのボディぶんだけ**（`showEdges` の契約）なので、
部品数が増えてもこの本数は増えない —— 増えるとしたら1部品の辺の本数そのものである。

---

## テスト

### `tests/eval_screen_pick.mjs` + `tests/test_screen_pick.py`

`eval_grid_scale.mjs` と同型。**three.js 不要** —— `screenPick.ts` の純関数だけ Node で assert。

最低限のケース:

| ケース | 期待 |
|---|---|
| カーソルが線分の真上（距離 0） | hit |
| 距離 = radiusPx | hit（境界 inclusive） |
| 距離 = radiusPx + ε | miss |
| 斜め線分への垂足が segment 外 | 端点への距離で判定 |
| 候補2つ（点 3px・点 8px）、radius 6 | 近い方 |
| 空候補 | null |

### `web/scripts/smoke-screen-pick.mjs`（実機）

**受入条件1（ズーム・画角・投影を変えても同じ px で掴める）は pytest では判定できない** ——
純関数テストが固定しているのは距離計算の規則だけで、そこへ渡る screen 座標が視点でどう動くかは
本物のカメラでしか出ない。辺の中点から外向きに **2px なら hit / 12px なら miss** を、
**折れ線の読み口2つの両方**で見る:

- **ソリッドの辺**（`bufferPolyline`）を2視点 —— 押し出した箱でフィレットを起動して辺を出す
- **スケッチの図形**（`sketchPolyline`）を5視点（既定 / ズームアウト / ズームイン / 画角90度 /
  正投影）＋角の点マーカーの真上で **点**が取れること

見かけの長さが 5.6倍動いても結果が変わらないことを見る。

この smoke は**旧実装（Raycaster）ではスケッチの節で即落ちる**ことを確認済み（2026-09-11。
上記の `Line2` の当たり半径 1.5px のため。ソリッドの辺の節は旧実装でも通るので、
**teeth があるのはスケッチ側**である）。

**ホバーの読みは null なら数フレーム置いて読み直す**（最大3回）。当たり側だけが救われる
非対称な待ちで、外れ側（12px）は何度読んでも null なので判定は緩まない。
視点を動かした後は `stateEpoch` が止まるまで待つ —— ズームは可変解像度の測り直しを起こし、
返ってきた状態でスケッチの折れ線が作り直されるので、待たないと**時々落ちる**（実測）。

### 手動受入（実装後）

1. ~~スケッチ `add_line`: 寄り引きしても点・線の掴みやすさが一定~~
   → `smoke-screen-pick.mjs` が自動で見る
2. 斜め視点で bezier に沿って hover —— 見えている線と幽霊がずれない（既存不変条件）
3. 27部品モデルで辺ピック —— 意図した辺が取れる（Phase A/B どちらでも）
4. 頂点選択 —— 辺より先に頂点が取れる（`preferVertices` ゲート）。
   **`preferVertices` を立てるのは回転中心の頂点待ちだけで、実機smokeが1つも通っていない**
   （`vertexAt` 自体が引きでも当たることは `smoke-vertex.mjs` が見ている）
5. viewport 端付近 —— `projectForPick` で pick が消えない

---

## 実装順（2026-09-11 に Step 1〜6 すべて完了）

```
Step 1  screenPick.ts + eval_screen_pick.mjs + test_screen_pick.py
Step 2  viewport.ts: projectForPick + pickPointsAt/pickLinesAt（内部）+ sketchEntityAt 差し替え
Step 3  vertexMarkers.ts: vertexAt 差し替え、deps から worldPerPixel 削除
Step 4  viewport.ts: edgeAt 差し替え（Phase A）
Step 5  計測 → 必要なら edgeAt Phase B（broad phase）
Step 6  viewportConstants コメント更新、README 文書地図（本書）確認
```

各 Step で `npm run build` + 該当 pytest / node eval が通ること。

---

## 受入条件

1. **`SKETCH_HANDLE_PIXELS` / `EDGE_PICK_PIXELS` / `VERTEX_PIXELS` が screen 半径として機能する**
   —— ズーム・画角変更・正投影切替後も、同じ px 定数で体感が大きく変わらない。
2. **既存の層をまたぐ契約を壊さない**
   - クリックの `PickEvent.sketchEntity` と hover の `snappedPlanePoint` が同じ `sketchEntityAt` を通る
   - `projectSketchPoint` / `projectEdge` / `projectVertex` が「その画素で本当に取れる」検証を通る
3. **純関数テストが CI で skip されない**（`test_screen_pick.py` は node 必須。`test_view_cube.py` 同様）。
4. **`worldPerPixel()` は当たり判定の正ではなくなる** —— グリッド・可変解像度・clip 用に残る。

---

## リスクと対処

| リスク | 対処 |
|---|---|
| `edgeAt` 全走査が重い | 計測した（「性能」）。p95 0.7ms で Phase B は要らなかった |
| 深度が近い辺の tie-break が変わる | screen 距離最小で統一（意図的変更。手動受入で確認） |
| `projectForPick` と `toScreen` の混同 | 名前を分け、当たり判定は `projectForPick` のみ |
| リサイズ直後のずれ | 既存と同様 `applySize` → `pickChanged()`。screen pick も毎回 `size()` を読む |

---

## 将来（本タスク外）

- 面ピックの hover 半径（現状は三角形そのもの）
- スナップ強度の UI 設定（定数を settings に出す）
- `projectToSegment` と `distSqPointSegment` の共通化（2D 汎用 `segment2d.ts`）
