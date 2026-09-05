/**
 * ビューポート — three.js をここに閉じ込める(実装計画書 §4.2 予防2)。
 *
 * **three.js の型を外へ漏らさない**。公開するのは `viewportTypes.ts` の `Viewport`
 * インタフェースだけで、three.js のオブジェクトは1つも戻り値・引数に出さない。three.js は
 * semver を持たず r番号でほぼ毎月リリースし非推奨APIを定期的に削除するため、**このスタックで
 * 最も churn が激しい依存**である。three.js を触るのはこのファイルとその兄弟モジュール群
 * (`planeGrid.ts` / `faceCulling.ts` / `viewCubeOverlay.ts` など)だけで、破壊的変更の影響は
 * Svelte 層へ出ない。
 *
 * **canvas はフレームワークの外**(実装計画書 §4「守るべき唯一のルール」)。Svelte から
 * 受け取るのは空の `HTMLElement` 1つだけで、canvas の生成・破棄・再描画は全部この中で行う。
 * Svelte はこのコンテナの中身を二度と触らない。
 *
 * **Bodyごとの差分更新に対応している。** 文書はSTEPの読み込みなどでソリッドごとに
 * 分かれる(パーツ一覧UI)ので、「変わったBodyだけ差し替える」`update()` が要る。
 * **ここが27部品ぶんを毎回全部作り直すと、サーバ側を速くした意味(差分送信)が描画で
 * 消える**(モジュールの利用側 `App.svelte` 参照)。面の当たり判定・強調表示・射影は
 * body_id + face_index の組で指す(1個のBodyの中でしか一意でない `faceIndex` 単独では
 * 指さない)。
 */
import * as THREE from 'three'
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'

// 辺の折れ線の形は `session.ts` の `EdgeGeometry` が正(サーバが配る形そのもの)。
import { entityHandles, entityPoints } from './entityKinds'
import { DEFAULT_RESOLUTION, viewResolution } from './adaptiveResolution'
import { sampleFaces, selectFineFaces, type BodySamples } from './faceCulling'
import {
  BASE_EXTENT,
  TIERS,
  boundingSquare,
  extentFor,
  mmPerPixel,
  tierAlphas,
  type CameraProjection,
  type UvBounds,
  type UvBox,
} from './gridScale'
import { createPlaneGrid, type PlaneFrame, type PlaneGrid } from './planeGrid'
import { POLE_Z, upForDirection } from './viewCube'
import { createViewCubeOverlay } from './viewCubeOverlay'
import type { BasePlaneName, EdgeGeometry } from './session'
import type {
  BodyGeometryUpdate,
  BodyGhost,
  BodyGhostRotation,
  BoxSelectEvent,
  EdgeRef,
  FaceGeometry,
  FaceRef,
  GeometryPayload,
  OptimizeDebugAnchors,
  OptimizeDebugGrid,
  OptimizeDebugState,
  OptimizeDebugVoxels,
  OptimizeLoadVisual,
  OptimizeVisualState,
  PickEvent,
  PickMode,
  SketchDraw,
  SketchEntityHit,
  SketchPlaneRef,
  VertexMarker,
  VertexRef,
  Viewport,
  ViewportResolution,
} from './viewportTypes'
import { bodyGhostMatrix, usableRotation } from './viewportTypes'
import {
  AXIS_X_COLOR,
  AXIS_Y_COLOR,
  AXIS_Z_COLOR,
  BACKGROUND_COLOR,
  BASE_COLOR,
  BASE_PLANES,
  BASE_PLANE_AXES,
  BASE_PLANE_HOVER_OPACITY,
  BASE_PLANE_PROBES,
  BOX_SELECT_MIN_PX,
  CLICK_MOVE_LIMIT_PX,
  CLICK_TIME_LIMIT_MS,
  DEBUG_ANCHOR_FIXED_COLOR,
  DEBUG_ANCHOR_LOAD_COLOR,
  DEBUG_RHO_COLOR,
  DEBUG_SPACE_COLOR,
  DEPTH_BIAS_LAYER,
  DEPTH_BIAS_UNIT,
  EDGE_COLOR,
  EDGE_PICK_PIXELS,
  FOV_DEFAULT,
  FOV_MAX,
  FOV_MIN,
  FOV_ORTHO,
  GRID_ARMED_COLOR,
  GRID_COLOR,
  HIGHLIGHT_COLOR,
  HOME_VIEW_OFFSET,
  HOVER_LIGHTEN,
  LAYER,
  OPTIMIZE_EXCLUDE_COLOR,
  OPTIMIZE_FIXTURE_COLOR,
  OPTIMIZE_FIXTURE_MARK_COLOR,
  OPTIMIZE_LOAD_COLOR,
  OPTIMIZE_PRESERVE_COLOR,
  PICK_EDGE_COLOR,
  PREVIEW_COLOR,
  PROPOSED_COLOR,
  REGION_COLOR,
  REGION_SELECTED_COLOR,
  SKETCH_COLOR,
  SKETCH_FIT_MARGIN,
  SKETCH_GHOST_COLOR,
  SKETCH_HANDLE_COLOR,
  SKETCH_HANDLE_PIXELS,
  SKETCH_SELECTED_COLOR,
  VERTEX_PIXELS,
  WORLD_UP,
  isSelectableFov,
  lighten,
  withDepthBias,
  type BodyMeshes,
} from './viewportConstants'

export { bodyGhostMatrix, FOV_DEFAULT, FOV_MAX, FOV_MIN, FOV_ORTHO, isSelectableFov }
export type {
  BodyGeometryUpdate,
  BodyGhost,
  BodyGhostRotation,
  BoxSelectEvent,
  EdgeRef,
  FaceGeometry,
  FaceRef,
  GeometryPayload,
  OptimizeDebugAnchors,
  OptimizeDebugGrid,
  OptimizeDebugState,
  OptimizeDebugVoxels,
  OptimizeLoadVisual,
  OptimizeVisualState,
  PickEvent,
  PickMode,
  SketchDraw,
  SketchEntityHit,
  SketchPlaneRef,
  VertexMarker,
  VertexRef,
  Viewport,
  ViewportResolution,
}

export function createViewport(container: HTMLElement): Viewport {
  const renderer = new THREE.WebGLRenderer({ antialias: true })
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2))
  renderer.domElement.style.display = 'block'
  renderer.domElement.style.width = '100%'
  renderer.domElement.style.height = '100%'
  renderer.domElement.style.touchAction = 'none'
  container.appendChild(renderer.domElement)

  const scene = new THREE.Scene()
  scene.background = new THREE.Color(BACKGROUND_COLOR)

  /**
   * **カメラは2つあり、`camera` が今どちらを使っているかを指す。**
   *
   * 正投影は「画角0度の透視」ではなく別種の投影なので、1つのカメラでは表せない
   * (`FOV_ORTHO` の説明を参照)。**姿勢(position / up / near / far)は切り替えのたびに
   * 写す**ので、どちらを使っていても視点は同じ ——「どちらのカメラか」は投影の作り方
   * だけの違いに閉じている。
   *
   * **`camera` を直接見てよいのは姿勢と投影の共通部分だけ**。`fov` / `aspect` は
   * 透視にしか無く、`left`/`right`/`top`/`bottom`/`zoom` は正投影にしか意味が無いので、
   * それらを触る処理は `perspective` / `orthographic` を名指しすること。
   */
  const perspective = new THREE.PerspectiveCamera(FOV_DEFAULT, 1, 0.1, 10000)
  const orthographic = new THREE.OrthographicCamera(-1, 1, 1, -1, 0.1, 10000)
  let camera: THREE.PerspectiveCamera | THREE.OrthographicCamera = perspective
  // 空の文書でも原点まわりが見える位置。部品が届いたら `frameToContent` が上書きする。
  // **`BASE_EXTENT` を使う**(可変の `gridExtent` ではない) —— ここは起動直後の1回きりで、
  // `gridExtent` はまだ宣言されておらず(下の平面グリッドの節より前)、その初期値は
  // 常に `BASE_EXTENT` だから同じ値になる。
  //
  // **距離は `fitDistance` で求める(2026-09-04訂正)。** 以前はここだけ `diagonal * 1.2`
  // という決め打ち定数を持っていた —— `fitDistance` が無かった頃の名残で、FOV_DEFAULTが
  // 45度だったあいだは `radius / tan(22.5°) ≈ radius * 2.4142`(半径ベースでは `diagonal
  // * 1.2071`)とほぼ一致していたので気づかれなかった(差0.6%)。**既定を30度へ下げたら
  // この定数は式と55%もずれ**、起動直後の空の文書やまだ内容の無いスケッチ(`lookAtSketch`
  // が半径0で「前の距離のまま」に倒すとき)がFOV30度の想定より大きく寄って見える事故になる
  // (`smoke-move.mjs` の辺クリックがこれで外れて実測した)。`frameToContent` と同じ式を
  // 通すことで、FOV_DEFAULTがいくつでも起動直後の見かけの大きさが揃う。
  {
    const diagonal = Math.hypot(BASE_EXTENT * 2, BASE_EXTENT * 2, BASE_EXTENT * 2)
    const distance = fitDistance(diagonal / 2)
    camera.position.copy(HOME_VIEW_OFFSET).multiplyScalar(distance)
  }
  scene.add(camera)
  // 光をカメラに付ける(視点を回しても面が真っ黒にならない。見た目の作り込みではなく可視性)
  const headlight = new THREE.DirectionalLight(0xffffff, 1.6)
  headlight.position.set(0.4, 0.7, 1)
  camera.add(headlight)
  scene.add(new THREE.HemisphereLight(0xdfe7f2, 0x191c21, 1.0))

  // **型引数を明示する** —— 省くと生成に渡した `perspective` で固まり、正投影へ
  // 差し替えるときに `controls.object` が受け取れない(`activateCamera`)。
  const controls: OrbitControls<THREE.PerspectiveCamera | THREE.OrthographicCamera> =
    new OrbitControls(camera, renderer.domElement)
  controls.enableDamping = true
  controls.dampingFactor = 0.15
  // **マウスの割り当て(2026-08-30本人決定)**: 左=ウィンドウ選択(このファイルが自前で
  // 実装する)/ 中=パン / 右=カメラ回転。
  //
  // `OrbitControls` の既定は LEFT=ROTATE / MIDDLE=DOLLY / RIGHT=PAN なので、**3つとも
  // 変える**(中ボタンは「既定のままパン」ではなくドリーだった)。左を `null` にするのは、
  // 左ドラッグを丸ごとこちらへ渡すため —— 左に何か割り当てたままだと、矩形を引く間に
  // カメラが動いて「囲ったつもりの範囲」と実際の範囲がずれる。
  controls.mouseButtons = {
    LEFT: null,
    MIDDLE: THREE.MOUSE.PAN,
    RIGHT: THREE.MOUSE.ROTATE,
  }

  const root = new THREE.Group()
  scene.add(root)

  // **スケッチのグループもここで作る**(使うのは下の「C: スケッチ」節だが、宣言だけ先に置く)。
  // グリッドの描く範囲が**すべてのスケッチとソリッド**から決まる(`contentBoxForGrid`)ので、
  // 初期化中の `applyGridPlanes()` が既にこれを読む。宣言をC節に置いたままにすると
  // **TDZで落ちる**(`Cannot access 'sketchRoot' before initialization`) —— 例外は
  // `createViewport` の途中で投げられ、`window.__duocad.ready` が永久に立たないので、
  // 症状は「画面が出ない・実機smokeが60秒待って死ぬ」という原因から遠い形で出る。
  const sketchRoot = new THREE.Group()
  scene.add(sketchRoot)

  // ------------------------------------------------------------------ 形状最適化(第3のモード)の注記
  //
  // 拘束の固定記号・荷重の矢印+ラベル。**`root` には入れない**(グリッドと同じ理由 ——
  // 外接直方体に混ざると初回フレーミングやnear/far計算を歪める)。**指せない**(装飾)ので
  // ピックの対象にも足さない。`setOptimizeState` が唯一の書き手。
  const optimizeRoot = new THREE.Group()
  optimizeRoot.renderOrder = LAYER.optimize
  scene.add(optimizeRoot)
  let optimizeDisposables: Array<{ dispose(): void }> = []

  // ------------------------------------------------------------------ 開発用のデバッグ表示
  //
  // 塊(`?debug=space`)・平滑化前のボクセル(`?voxel=1`)・面アンカーの節点
  // (`?debug=anchors`)。**`root` には入れない**(`optimizeRoot` と同じ理由 ——
  // 外接直方体に混ざると初回フレーミングやnear/far、グリッドの範囲まで歪む)。
  // ピックの対象にも足さない。`setOptimizeDebug` が唯一の書き手で、
  // **既定(`null`)では子が1つも生まれない。**
  const debugRoot = new THREE.Group()
  debugRoot.renderOrder = LAYER.debug
  scene.add(debugRoot)
  let debugDisposables: Array<{ dispose(): void }> = []
  let debugCounts: {
    space: number
    rho: number
    fixed: number
    load: number
    spaceStride: number
    rhoStride: number
    anchorsDepthTest: boolean | null
  } = { space: 0, rho: 0, fixed: 0, load: 0, spaceStride: 1, rhoStride: 1, anchorsDepthTest: null }
  /** `?voxel=1` で隠している結果ボディ。**表示だけ**(文書には触れない)。 */
  let debugHiddenBodyId: string | null = null

  // ------------------------------------------------------------------ 平面グリッド(3段固定・無限)
  //
  // **「ある平面frame(原点・u軸・v軸)を受け取って描くもの」**(`planeGrid.ts`)。XY床は
  // その一利用者にすぎず、どの平面を出すかは状態が決める(`desiredGridPlanes`)。
  // `root` には入れない —— 入れると初回の `frameToContent` がグリッドの箱に引っ張られる。
  // ピック対象にもしない(判定は `basePlaneAt` の解析交差が別に持つ)。
  const gridRoot = new THREE.Group()
  // Group の `renderOrder` は three.js では groupOrder として子全体に効く。中身どうしの
  // 前後は各オブジェクトの `LAYER.*` が決める。
  gridRoot.renderOrder = -1
  scene.add(gridRoot)

  /**
   * 基準平面のピック範囲の半幅[mm]。**描画範囲ではない**(格子は無限) ——
   * `basePlaneAt` のピック範囲・`updateClipPlanes` のnear/far下限・面外の軸線の長さが
   * ここを見る。`applyGridScale` だけが動かす。
   */
  let gridExtent = BASE_EXTENT

  /** 基準3平面を指せるか(`showBasePlanes`)。**出していない間は当たり判定を回さない**。 */
  let basePlanesOn = false

  /**
   * スケッチ中のスケッチ平面のframe(モード外は null)。**書き手は `setGridSketchFrame`
   * だけ**で、`setActiveSketch` と `showSketches` の両方から呼ぶ —— モードに入った時点では
   * まだ描画指示が届いておらず、frameを知らないことがある。
   */
  let sketchGridFrame: PlaneFrame | null = null

  /**
   * `sketchGridFrame` が任意面(基準平面でない)のときの描く正方形。
   * `faceUvBounds` が測れなかった・基準平面のときは `null`。**書き手は
   * `setGridSketchFrame` だけ**(`sketchGridFrame` と対で更新する)。
   */
  let sketchGridBounds: UvBounds | null = null

  /**
   * いまのスケッチが**実ジオメトリの面(`plane.kind === 'face'`)へ貼られているか**。
   * **QA指摘で追加(2026-09-03)** —— 以前は `basePlaneForFrame(sketchGridFrame)` の
   * frame一致だけで「基準平面か任意面か」を判定していたが、「XY床にスケッチ→押し出し→
   * その底面に再度スケッチ」のように**実ジオメトリの面でも frame が数値的に基準平面と
   * 一致する**ことがあり(原点(0,0,0)・u/vが基準平面と同じ)、その場合に任意面のグリッド
   * (深度バイアス層・extentの有限化・直交軸なし)が不発になっていた。**その面は実ソリッドの
   * メッシュと完全同一平面**なので、本来はz-fighting対策が最も要る場面である。
   * 判定は `plane.kind` を正とし、`basePlaneForFrame` はもう「基準平面か否か」の判定には
   * 使わない(使うのは `plane.kind==='base'` のときにXY/XZ/YZのどれかを選ぶ用途だけ)。
   */
  let sketchGridIsFacePlane = false

  /** 実機検査フック(`gridTiers`)へ出す、いま表示している平面の名前。 */
  let gridPlanesShown: string[] = []
  /**
   * `gridPlanesShown` と対で持つ、各グリッドに適用中の深度バイアス層番号
   * (`DEPTH_BIAS_LAYER`)。**実機検査フック専用**(2026-09-03 QA指摘) —— 任意面のグリッドが
   * 本当に `sketchFaceGrid` 層(基準平面の `planeGrid` 層とは別)を使っているかは、
   * z-fightingの有無を静止画のスクリーンショットだけでは機械的に見分けられないため、
   * 数値そのものを検査できるようにする。
   */
  let gridDepthBiasLayers: number[] = []

  /** `gridPlanesShown` と対で持つ、各グリッドに適用中の描画範囲(実機検査フック `gridTiers`)。 */
  let gridPlaneBounds: Array<UvBounds | null> = []

  /**
   * 同時に出しうる平面は最大3枚(対象待ちの XY/XZ/YZ)。**作り直さず使い回す** ——
   * frameを差し替えるだけで別の平面になる。
   */
  const planeGrids: PlaneGrid[] = [createPlaneGrid(), createPlaneGrid(), createPlaneGrid()]
  for (const grid of planeGrids) {
    grid.object.renderOrder = LAYER.planeGrid
    // 深度バイアスは `applyGridPlanes()`(直後に必ず呼ぶ)が状態ごとに入れ直す
    // (`GridPlaneSpec.depthBiasLayer`) —— ここでは初期化しない。
    grid.setVisible(false)
    gridRoot.add(grid.object)
  }

  /** 面外の軸(1本)。面内の軸はグリッドのシェーダーが描くので、線にするのはこれだけ。 */
  const gridAxisMaterial = withDepthBias(
    new THREE.LineBasicMaterial({
      color: AXIS_Z_COLOR,
      // 不透明だが**透明パスへ入れる** —— 実体より後に描かないと、共平面の面に隠れる。
      transparent: true,
      depthWrite: false,
    }),
    DEPTH_BIAS_LAYER.gridAxisLine,
  )
  let gridAxisLine: THREE.Line | null = null
  let gridAxisShown: 'X' | 'Y' | 'Z' | null = null

  /**
   * ホバー中の基準平面に重ねる板(**同時に見えるのは多くても1枚**)。ピック範囲が変わる
   * たびに作り直す(`rebuildBasePlaneHover`)。
   *
   * 平面ごとの色分けを線でやる代わりに半透明の板を重ねて「今カーソルが指しているのは
   * この平面」を出す(2026-08-27本人決定)。**当たり判定には使わない** —— 判定は
   * 今までどおり `basePlaneAt` の解析交差である。
   */
  const basePlaneHoverMeshes = new Map<BasePlaneName, THREE.Mesh>()
  let basePlaneHoverDisposables: Array<{ dispose(): void }> = []

  function basePlaneSpec(name: BasePlaneName): (typeof BASE_PLANES)[number] {
    const found = BASE_PLANES.find((spec) => spec.name === name)
    if (found === undefined) throw new Error(`基準平面が表に無い: ${name}`)
    return found
  }

  function baseFrame(name: BasePlaneName): PlaneFrame {
    const spec = basePlaneSpec(name)
    return { origin: [0, 0, 0], u: spec.u, v: spec.v }
  }

  /**
   * frameがどの基準平面のものか(u/vの一致で見る)。基準平面でなければ null。
   *
   * **これは「基準平面か任意面か」の判定には使わない**(2026-09-03 QA指摘で訂正) ——
   * 実ジオメトリの面(`plane.kind==='face'`)でも、frameが基準平面と数値的に一致することが
   * ある(「XY床にスケッチ→押し出し→その底面に再度スケッチ」が典型例。原点(0,0,0)・u/vが
   * 基準平面と同じになる)。その判定は `sketchGridIsFacePlane`(`plane.kind` を正とする)が
   * 持つ。ここが答えるのは「`plane.kind==='base'` のとき、XY/XZ/YZのどれを表示するか」だけ。
   */
  function basePlaneForFrame(frame: PlaneFrame): BasePlaneName | null {
    const same = (a: readonly number[], b: readonly number[]): boolean =>
      Math.abs(a[0] - b[0]) < 1e-6 && Math.abs(a[1] - b[1]) < 1e-6 && Math.abs(a[2] - b[2]) < 1e-6
    if (!same(frame.origin, [0, 0, 0])) return null
    for (const spec of BASE_PLANES) {
      if (same(frame.u, spec.u) && same(frame.v, spec.v)) return spec.name
    }
    return null
  }

  interface GridPlaneSpec {
    /** 実機検査フック(`gridTiers`)へ出す名前。 */
    name: string
    frame: PlaneFrame
    /** 面内の軸の色。`null` の側は描かない(同じ世界軸を2枚が二重に描かないため)。 */
    uColor: number | null
    vColor: number | null
    /** 描く正方形(frame上の中心と半径[mm])。`null` は範囲が測れなかったときだけ。 */
    bounds: UvBounds | null
    /** クリップ空間の深度バイアスの層番号(`DEPTH_BIAS_LAYER`)。 */
    depthBiasLayer: number
  }

  /**
   * いま出す平面と、面外に線で描く軸(完了条件4の表そのもの)。
   *
   * | 状態 | 表示 |
   * |---|---|
   * | 通常(スケッチ外) | XY平面 + Z軸 |
   * | スケッチ起動中(対象待ち・`showBasePlanes(true)`) | 3平面すべて |
   * | 基準平面へのスケッチ中 | その平面 + それに直交する軸 |
   * | 任意面へのスケッチ中 | その面のframeのグリッドのみ(直交軸なし。本人指定) |
   *
   * 対象待ちを3平面のままにするのは、基準平面も**クリックして指す**から
   * (engine `duocad-sketch-start-is-two-steps-by-pointing`)。ここでXZ/YZを隠すと
   * 指せなくなる。3枚のときは3本の軸が全部どれかの面内に出るので、面外の軸は要らない。
   */
  function desiredGridPlanes(): {
    planes: GridPlaneSpec[]
    axis: (typeof BASE_PLANE_AXES)[BasePlaneName]['outOfPlane'] | null
  } {
    if (basePlanesOn) {
      return {
        planes: [
          { name: 'XY', frame: baseFrame('XY'), uColor: AXIS_X_COLOR, vColor: AXIS_Y_COLOR, bounds: contentBoundsForPlane(baseFrame('XY')), depthBiasLayer: DEPTH_BIAS_LAYER.planeGrid },
          { name: 'XZ', frame: baseFrame('XZ'), uColor: null, vColor: AXIS_Z_COLOR, bounds: contentBoundsForPlane(baseFrame('XZ')), depthBiasLayer: DEPTH_BIAS_LAYER.planeGrid },
          { name: 'YZ', frame: baseFrame('YZ'), uColor: null, vColor: null, bounds: contentBoundsForPlane(baseFrame('YZ')), depthBiasLayer: DEPTH_BIAS_LAYER.planeGrid },
        ],
        axis: null,
      }
    }
    if (sketchGridFrame !== null && sketchGridIsFacePlane) {
      // 実ジオメトリの面(`plane.kind==='face'`)へのスケッチ中。その面のframeへそのまま
      // 乗せる —— 原点・u/v軸はスケッチ平面のもの(完了条件4)。直交軸は出さない
      // (完了条件3・本人指定)。**frameが基準平面と数値的に一致していてもここへ来る**
      // (2026-09-03 QA指摘で修正。以前は `basePlaneForFrame` のframe一致だけで判定しており、
      // 「XY床にスケッチ→押し出し→その底面に再度スケッチ」のような日常的な構成で
      // 基準平面の分岐へ誤って落ちていた)。貼り付けた面(A群のソリッド)と完全同一平面に
      // なるので、バイアスの層番号は別枠(`sketchFaceGrid`)を使う
      // ([[duocad-depth-overlay-rule]] B群・タスク指示)。
      return {
        planes: [
          {
            name: 'sketch-face',
            frame: sketchGridFrame,
            uColor: null,
            vColor: null,
            bounds: sketchGridBounds,
            depthBiasLayer: DEPTH_BIAS_LAYER.sketchFaceGrid,
          },
        ],
        axis: null,
      }
    }
    // ここへ来るのは「スケッチ外(通常表示)」か「`plane.kind==='base'` のスケッチ中」だけ。
    // 後者の平面名は frame一致で解決する(基準平面のframeは常に3つのどれかと一致するので
    // `sketchPlane` は通常 null にならないが、万一の不一致は安全側の既定 'XY' へ落とす)。
    const sketchPlane = sketchGridFrame === null ? null : basePlaneForFrame(sketchGridFrame)
    const name: BasePlaneName = sketchPlane ?? 'XY'
    const axes = BASE_PLANE_AXES[name]
    return {
      planes: [
        {
          name,
          frame: baseFrame(name),
          uColor: axes.uColor,
          vColor: axes.vColor,
          bounds: contentBoundsForPlane(baseFrame(name)),
          depthBiasLayer: DEPTH_BIAS_LAYER.planeGrid,
        },
      ],
      axis: axes.outOfPlane,
    }
  }

  function rebuildGridAxisLine(
    axis: (typeof BASE_PLANE_AXES)[BasePlaneName]['outOfPlane'] | null,
    halfLength: number,
  ): void {
    if (gridAxisLine !== null) {
      gridRoot.remove(gridAxisLine)
      gridAxisLine.geometry.dispose()
      gridAxisLine = null
    }
    gridAxisShown = axis === null ? null : axis.name
    if (axis === null) return
    gridAxisMaterial.color.setHex(axis.color)
    const geometry = new THREE.BufferGeometry().setFromPoints([
      new THREE.Vector3(-axis.dir[0], -axis.dir[1], -axis.dir[2]).multiplyScalar(halfLength),
      new THREE.Vector3(axis.dir[0], axis.dir[1], axis.dir[2]).multiplyScalar(halfLength),
    ])
    const line = new THREE.Line(geometry, gridAxisMaterial)
    line.renderOrder = LAYER.gridAxisLine
    line.raycast = () => {}
    gridRoot.add(line)
    gridAxisLine = line
  }

  /**
   * ホバー板をいまのピック範囲で作り直す。**`basePlaneAt` の判定範囲と同じ `gridExtent`
   * から作る** —— ここを取りこぼすと「板は見えているのに指せない(逆も)」という原因の
   * 遠い壊れ方をする。
   *
   * `applyBasePlaneHover()` はここでは呼ばない —— 初回呼び出しはまだ `hoverPlane` が
   * 宣言される前(TDZ)に走るため。作り直し後にホバー状態を反映するのは呼び出し元
   * (`applyGridScale`)の仕事にしてある。
   */
  function rebuildBasePlaneHover(extent: number): void {
    for (const item of basePlaneHoverDisposables) item.dispose()
    basePlaneHoverDisposables = []
    for (const mesh of basePlaneHoverMeshes.values()) gridRoot.remove(mesh)
    basePlaneHoverMeshes.clear()

    const hoverMaterial = withDepthBias(
      new THREE.MeshBasicMaterial({
        color: GRID_ARMED_COLOR,
        side: THREE.DoubleSide,
        transparent: true,
        opacity: BASE_PLANE_HOVER_OPACITY,
        // 半透明どうしで真っ黒にならないように奥行きは書かない(`previewMaterial` と同じ)。
        depthWrite: false,
      }),
      DEPTH_BIAS_LAYER.basePlaneHover,
    )
    basePlaneHoverDisposables.push(hoverMaterial)
    for (const spec of BASE_PLANES) {
      const corners = ([[-1, -1], [1, -1], [1, 1], [-1, 1]] as const).map(
        ([su, sv]) =>
          new THREE.Vector3(
            (su * spec.u[0] + sv * spec.v[0]) * extent,
            (su * spec.u[1] + sv * spec.v[1]) * extent,
            (su * spec.u[2] + sv * spec.v[2]) * extent,
          ),
      )
      const geometry = new THREE.BufferGeometry().setFromPoints([
        corners[0], corners[1], corners[2],
        corners[0], corners[2], corners[3],
      ])
      const mesh = new THREE.Mesh(geometry, hoverMaterial)
      mesh.renderOrder = LAYER.basePlaneHover
      mesh.visible = false
      gridRoot.add(mesh)
      basePlaneHoverDisposables.push(geometry)
      basePlaneHoverMeshes.set(spec.name, mesh)
    }
  }

  /** 出す平面・面内の軸・色・面外の軸線を、いまの状態から入れ直す。 */
  function applyGridPlanes(): void {
    const wanted = desiredGridPlanes()
    const color = basePlanesOn ? GRID_ARMED_COLOR : GRID_COLOR
    planeGrids.forEach((grid, index) => {
      const spec: GridPlaneSpec | undefined = wanted.planes[index]
      if (spec === undefined) {
        grid.setVisible(false)
        return
      }
      grid.setFrame(spec.frame)
      grid.setAxisColors(spec.uColor, spec.vColor)
      grid.setColor(color)
      // **常に内容を一回り大きく包む正方形で切る**(2026-09-03本人決定。無限に描くのをやめた)。
      // 基準平面は「すべてのスケッチとソリッド ∪ 原点」、任意面はその面自身の範囲が正。
      grid.setBounds(spec.bounds)
      // 任意面は貼り付けた面と同一平面になるので、バイアスの層番号を別枠にする
      // (`desiredGridPlanes` が選ぶ)。他の状態は従来どおり `planeGrid` のまま。
      grid.setDepthBias(spec.depthBiasLayer * DEPTH_BIAS_UNIT)
      grid.setVisible(true)
    })
    gridPlanesShown = wanted.planes.map((spec) => spec.name)
    gridDepthBiasLayers = wanted.planes.map((spec) => spec.depthBiasLayer)
    gridPlaneBounds = wanted.planes.map((spec) => spec.bounds)
    // 面外の軸線は**格子の枠と同じ長さ**にする(枠より長いと1本だけ飛び出して見える)。
    rebuildGridAxisLine(wanted.axis, wanted.planes[0]?.bounds?.half ?? gridExtent)
  }

  /**
   * スケッチ平面のframeを差し替える(モード外は null)。表示する平面がこれで決まる。
   * `isFacePlane` は `plane.kind==='face'` かどうか(基準平面か任意面かの判定そのもの。
   * 2026-09-03 QA指摘でframe一致判定から切り替えた)。`bounds` は `isFacePlane` のときだけ
   * 効く(基準平面・モード外は `desiredGridPlanes` が無視する)。
   */
  function setGridSketchFrame(
    frame: PlaneFrame | null,
    bounds: UvBounds | null,
    isFacePlane: boolean,
  ): void {
    sketchGridFrame = frame
    sketchGridBounds = bounds
    sketchGridIsFacePlane = isFacePlane
    applyGridPlanes()
  }

  /**
   * 段ごとの不透明度とカメラ行列を毎フレーム入れ直す。**CPUの仕事はこれだけ**で、
   * ジオメトリには一切触らない(格子はシェーダーが作る)。
   *
   * 不透明度は `gridScale.ts` の `tierAlpha`(＝Nodeから検査できる純関数)が決める。
   * シェーダーはそれを上限に、1画素ごとの局所的な線間隔でさらに落とす。
   */
  let gridMmPerPixel = 0
  let gridTierAlphaValues: [number, number, number] = [0, 0, 0]
  const gridViewProjection = new THREE.Matrix4()
  const gridInverseViewProjection = new THREE.Matrix4()

  function updateGridUniforms(): void {
    gridMmPerPixel = worldPerPixel()
    gridTierAlphaValues = tierAlphas(gridMmPerPixel)
    camera.updateMatrixWorld()
    gridViewProjection.multiplyMatrices(camera.projectionMatrix, camera.matrixWorldInverse)
    gridInverseViewProjection.copy(gridViewProjection).invert()
    for (const grid of planeGrids) {
      grid.setTierAlphas(gridTierAlphaValues)
      grid.setCamera(gridViewProjection, gridInverseViewProjection)
    }
  }

  rebuildBasePlaneHover(gridExtent)
  applyGridPlanes()

  const cube = createViewCubeOverlay({
    renderer,
    camera: () => camera,
    perspective,
    orthographic,
    controls,
    size,
  })

  function applyBasePlaneHover(): void {
    for (const [name, mesh] of basePlaneHoverMeshes) mesh.visible = hoverPlane === name
  }

  /**
   * 今ホバーで当てている色(**実機検査フック専用**)。
   *
   * **材質から読み戻す** —— ホバーは色相を変えず明度だけを上げるので、`highlightColors`
   * では変化が出ない(選択中の面は橙のままである)。「本当に明るくなったか」は実際に
   * 当たっている材質を見るしかない。
   */
  function hoverColorNow(): number | null {
    const pick = (object: THREE.Object3D | undefined): number | null => {
      const material = (object as { material?: { color?: THREE.Color } } | undefined)?.material
      return material?.color ? material.color.getHex() : null
    }
    const face = hoverFace
    if (face !== null) {
      return pick(
        allFaceMeshes.find(
          (mesh) =>
            mesh.userData.bodyId === face.bodyId && mesh.userData.faceIndex === face.faceIndex,
        ),
      )
    }
    const edge = hoverEdge
    if (edge !== null) {
      return pick(edgeLines.find((line) => line.userData.edgeIndex === edge.edgeIndex))
    }
    const region = hoverRegion
    if (region !== null) {
      return pick(
        sketches
          .get(region.sketchId)
          ?.regionMeshes.find((mesh) => mesh.userData.regionIndex === region.index),
      )
    }
    if (hoverPlane !== null) return pick(basePlaneHoverMeshes.get(hoverPlane))
    return null
  }

  /**
   * 面の材質は **(役割 × 選択状態 × ホバー)** をキーにした遅延生成のキャッシュから取る。
   *
   * 役割は形状最適化(第3のモード)の保持/除外/拘束で、素の面(`'none'`)を含めて4種。
   * 選択状態は3種(未選択 `'plain'` / 確定 `'user'` / AI提案 `'proposed'`)、ホバーは2種。
   * 組み合わせは最大24だが、**使った組だけ**をこの中で作る(全部を手で書き下さない) ——
   * 役割が無かった頃の6個(base/highlight/proposed × 通常/ホバー)から3倍に増える軸を、
   * 静的な定数ではなく関数1つに閉じるための形。
   *
   * **選択は役割に勝つ**(本人決定)。`state !== 'plain'` のときは役割を見ない ——
   * 保持ソリッド(緑)を選ぶとハイライト色になり、緑は一時的に見えなくなる。
   */
  type OptimizeFaceRole = 'none' | 'preserve' | 'exclude' | 'fixture'
  type FaceMaterialState = 'plain' | 'user' | 'proposed'
  const faceMaterialCache = new Map<string, THREE.MeshLambertMaterial>()

  function roleBaseColor(role: OptimizeFaceRole): number {
    if (role === 'preserve') return OPTIMIZE_PRESERVE_COLOR
    if (role === 'exclude') return OPTIMIZE_EXCLUDE_COLOR
    if (role === 'fixture') return OPTIMIZE_FIXTURE_COLOR
    return BASE_COLOR
  }

  function faceMaterialFor(
    role: OptimizeFaceRole,
    state: FaceMaterialState,
    hovered: boolean,
  ): THREE.MeshLambertMaterial {
    const effectiveRole: OptimizeFaceRole = state === 'plain' ? role : 'none'
    const key = `${effectiveRole}:${state}:${hovered ? 1 : 0}`
    const cached = faceMaterialCache.get(key)
    if (cached) return cached
    const base =
      state === 'user' ? HIGHLIGHT_COLOR : state === 'proposed' ? PROPOSED_COLOR : roleBaseColor(effectiveRole)
    const material = new THREE.MeshLambertMaterial({
      color: hovered ? lighten(base, HOVER_LIGHTEN) : base,
      side: THREE.DoubleSide,
    })
    faceMaterialCache.set(key, material)
    return material
  }
  const previewMaterial = new THREE.MeshLambertMaterial({
    color: PREVIEW_COLOR,
    side: THREE.DoubleSide,
    transparent: true,
    opacity: 0.55,
    // 半透明どうしが互いを隠して真っ黒な塊にならないようにする(奥行きは書かない)
    depthWrite: false,
  })
  const edgeMaterial = new THREE.LineBasicMaterial({ color: EDGE_COLOR })

  const raycaster = new THREE.Raycaster()
  /** body_id -> そのBodyが画面に持つメッシュ。非表示・未読込のBodyはキーごと無い。 */
  const bodies = new Map<string, BodyMeshes>()
  /**
   * body_id -> サーバから届いた生の `GeometryPayload`。
   *
   * three.jsのメッシュ(`bodies`)とは別に持つ — `getGeometry()` は実機検査フックが
   * 「今どんな形が届いているか」をJSONで読むためのもので、three.jsのバッファは読めない
   * (型も漏らさない設計)。`bodies` と寿命を揃える(build/clear/pruneで同時に出し入れする)。
   */
  const rawGeometry = new Map<string, GeometryPayload>()
  /** `rawGeometry` と寿命を揃える可視判定用サンプル(`computeFocus` が2835面を毎フレーム走査しない)。 */
  const faceSamples = new Map<string, BodySamples>()
  /** `faceSamples` が変わった印。姿勢が止まっていても形が届けば `computeFocus` をやり直す。 */
  let focusGeneration = 0
  /** 全Body分の面メッシュを1本にまとめたもの(ピック用)。update() のたびに作り直す。 */
  let allFaceMeshes: THREE.Mesh[] = []
  /** プレビュー(幽霊)のメッシュ。**ピックの対象にしない**ので `bodies` とは別に持つ。 */
  const previewMeshes = new Map<string, { objects: THREE.Object3D[]; disposables: Array<{ dispose(): void }> }>()
  let pickListener: ((event: PickEvent) => void) | null = null
  let boxSelectListener: ((event: BoxSelectEvent) => void) | null = null
  let planeMoveListener: ((point: { u: number; v: number } | null) => void) | null = null

  // ---------------------------------------------------------------- ホバー強調
  //
  // **「そのままクリックしたら取れるもの」を1つだけ明るくする**(2026-08-27本人決定)。
  // 対象は面・辺・領域・基準平面で、**頂点は出さない** —— 選択に入っている頂点しか
  // マーカーが無く、「クリックすれば選択されるもの」を出せる状態にない。
  //
  // 当たり判定は `resolveHit` **1つだけ**をクリックと共有する。優先順位を2箇所に
  // 書くとプレビューが嘘をつき、この機能の存在意義が消える。
  /** ポインタの現在位置(canvasの外なら null)。**解決はフレームに1回**(`tick`)。 */
  let hoverAt: { x: number; y: number } | null = null
  /** 視点回転中(=そのポインタの動きはクリックではない)。回している間は解決しない。 */
  let orbiting = false
  let hoverFace: FaceRef | null = null
  let hoverEdge: EdgeRef | null = null
  let hoverPlane: BasePlaneName | null = null
  let hoverRegion: { sketchId: string; index: number } | null = null
  // 「変わったときだけ材質を書き換える」ための印(毎フレーム全メッシュを走査しない)。
  let hoverFaceKey = ''
  let hoverEdgeKey = ''
  let hoverRegionKey = ''
  /**
   * 直近にホバーを解決したときの「ポインタ位置+視点+`pickGeneration`」。**同じなら
   * 当たるものも同じ**なので、レイキャストごと省く(止まって眺めている間は毎フレーム
   * 2835面を撃たない)。
   */
  let hoverProbe = ''
  /**
   * **「何が指せるか」が変わるたびに増やす番号**。`hoverProbe` に混ぜてあるので、
   * ポインタも視点も動いていなくても次のフレームで必ず解決し直す。
   *
   * これが無いと、**止まったまま予告だけが古くなる**。例: 領域をホバーしたまま Esc で
   * コマンドを解除すると `pickRegions(false)` で領域は指せなくなるのに、ポインタも視点も
   * 動いていないので解決が省かれ、明るいままの領域が「クリックすれば取れる」と嘘をつく。
   * 頂点マーカーが増えたとき・プレビューで本物を隠したとき・ビューポートの寸法が
   * 変わったときも同じ(いずれも当たるものが変わる)。
   */
  let pickGeneration = 0

  /** 「何が指せるか」を変えた側が呼ぶ。 */
  function pickChanged(): void {
    pickGeneration += 1
  }

  /**
   * ホバーの印を捨てる。**描画対象を作り直した側が呼ぶ** —— 作り直したメッシュは
   * 素の材質で生まれるので、印を残すと「変わっていない」と判断して明るくし直さない。
   */
  function invalidateHover(): void {
    hoverFace = null
    hoverEdge = null
    hoverRegion = null
    hoverFaceKey = ''
    hoverEdgeKey = ''
    hoverRegionKey = ''
    hoverPlane = null
    pickChanged()
    // **材質まで戻す**。参照と印を捨てるだけだと、作り直していない対象に付けた明るい材質が
    // そのまま残る(次の解決が「なし」や別種別だと、誰も戻さないまま画面に居座る)。
    applyFaceMaterials()
    applyEdgeMaterials()
    applyRegionMaterials()
    applyBasePlaneHover()
  }
  let framed = false
  let alive = true
  let frameHandle = 0

  function size(): { width: number; height: number } {
    return {
      width: Math.max(1, container.clientWidth),
      height: Math.max(1, container.clientHeight),
    }
  }

  function applySize(): void {
    pickChanged() // 寸法が変わると同じ画素が別のものを指す(engine `viewport-resize-silently-changes-what-a-click-grabs`)
    const { width, height } = size()
    renderer.setSize(width, height, false)
    const aspect = width / height
    perspective.aspect = aspect
    perspective.updateProjectionMatrix()
    // **正投影は縦(`top`/`bottom`)を正として横を作り直す。** 透視の `aspect` と同じで、
    // 縦の見え方を寸法に依存させない(`smoke-features.mjs` がv方向を基準に置いている)。
    // **`zoom` には触らない** —— あれは OrbitControls のドリーの持ち物である。
    orthographic.right = orthographic.top * aspect
    orthographic.left = -orthographic.top * aspect
    orthographic.updateProjectionMatrix()
    // 使っていない側も同時に整えてある(切り替えた瞬間に1フレーム歪むのを防ぐ)。
  }

  function clearBody(bodyId: string): void {
    const existing = bodies.get(bodyId)
    if (!existing) return
    for (const object of existing.sceneObjects) root.remove(object)
    for (const item of existing.disposables) item.dispose()
    bodies.delete(bodyId)
    rawGeometry.delete(bodyId)
    if (faceSamples.delete(bodyId)) focusGeneration += 1
  }

  /**
   * 1面ぶんの `BufferGeometry` を作る(本物と幽霊で**同じ形の作り方**)。
   *
   * 本物(`buildBody`)と確定前の幽霊(`showPreview`)はどちらも同じ三角形群から作るので、
   * **ここがずれると「プレビューと確定結果が違う形に見える」**という、いちばん困る形の
   * 不具合になる。
   *
   * 一方で**メッシュの組み立てまでは共通化しない** —— 本物は `userData`(ピックのための
   * body_id と面の添字)と面の境界線を持ち、幽霊はどちらも持たない(指せない・線を出さない)。
   * 材質だけの違いに見えて、実際には「何のために置くか」が違う。
   */
  function faceGeometry(face: FaceGeometry): THREE.BufferGeometry {
    const geometry = new THREE.BufferGeometry()
    geometry.setAttribute(
      'position',
      new THREE.BufferAttribute(new Float32Array(face.positions), 3),
    )
    geometry.setIndex(new THREE.BufferAttribute(new Uint32Array(face.indices), 1))
    geometry.computeVertexNormals()
    geometry.computeBoundingSphere()
    return geometry
  }

  function buildBody(bodyId: string, payload: GeometryPayload): void {
    const faceMeshes: THREE.Mesh[] = []
    const sceneObjects: THREE.Object3D[] = []
    const disposables: Array<{ dispose(): void }> = []
    for (const face of payload.faces) {
      if (face.indices.length === 0) continue // 三角形が取れなかった面(サーバが警告を出す)
      const geometry = faceGeometry(face)
      // **素の材質**で生む(役割・選択・ホバーは `applyFaceMaterials()` が
      // `rebuildPickIndex()`(→ `invalidateHover()`)経由ですぐに塗り直す)。
      const mesh = new THREE.Mesh(geometry, faceMaterialFor('none', 'plain', false))
      mesh.userData.bodyId = bodyId
      mesh.userData.faceIndex = face.index
      root.add(mesh)
      faceMeshes.push(mesh)
      sceneObjects.push(mesh)
      disposables.push(geometry)

      // 面の境界線。装飾ではなく「どこが1つの面か」を見せるためで、
      // これが無いと隣り合う同色の面を狙い分けられず、狙いにくさの計測が意味を失う。
      const edges = new THREE.EdgesGeometry(geometry, 20)
      const line = new THREE.LineSegments(edges, edgeMaterial)
      root.add(line)
      sceneObjects.push(line)
      disposables.push(edges)
    }
    bodies.set(bodyId, { faceMeshes, sceneObjects, disposables })
    rawGeometry.set(bodyId, payload)
    faceSamples.set(bodyId, sampleFaces(payload))
    focusGeneration += 1
  }

  function rebuildPickIndex(): void {
    allFaceMeshes = []
    for (const entry of bodies.values()) allFaceMeshes.push(...entry.faceMeshes)
    invalidateHover() // 作り直した面は素の材質で生まれる(`invalidateHover` のコメント)
  }

  function update(entries: BodyGeometryUpdate[]): void {
    if (entries.length === 0) return
    // 本物の形が届いた時点で、確定前の予想は用済み(古い幽霊を残さない)。
    clearPreview()
    for (const entry of entries) {
      clearBody(entry.bodyId)
      if (entry.geometry) buildBody(entry.bodyId, entry.geometry)
      // 形が変わったBodyの辺は**その場で消す**。古い添字の線を残すと、指した線と
      // 違う辺が丸まる(§7の「添字は形状変更で壊れる」の辺版)。要るなら呼び出し側が
      // 版の変化を見て取り直す(`App.svelte`)。
      if (entry.bodyId === edgeBodyId) clearEdges()
    }
    rebuildPickIndex()
    refreshContentDiagonal() // 見えているものの大きさが変わった(解像度の上下限に効く)
    updateClipPlanes() // 内容が変わった → 視錐台を引き直す(姿勢は初回のみ下で決める)

    // **`frameToContent()` が実際に合わせたときだけ機会を消費する**(2026-08-30)。
    // 空振り(箱が空)で `framed = true` にすると、以後どんなに大きな内容が届いても
    // 初回フレーミングが二度と起きない(上の関数docstring参照)。
    if (!framed && frameToContent()) {
      framed = true
    }

    // **作り直したボディに、デバッグ表示の「隠す」を当て直す。** `?voxel=1` は結果ボディを
    // 隠してボクセルを見せる —— ここで当て直さないと、結果が届いた直後の状態配信で
    // ボディが生まれ直し、平滑化メッシュとボクセルが重なって両方読めなくなる。
    applyDebugHidden()
  }

  // ------------------------------------------------------------------ プレビュー(幽霊)
  function setBodyVisible(bodyId: string, visible: boolean): void {
    const entry = bodies.get(bodyId)
    if (!entry) return
    for (const object of entry.sceneObjects) object.visible = visible
  }

  function showPreview(entries: BodyGeometryUpdate[]): void {
    pickChanged() // 本物のBodyを隠すので、同じ画素が別のもの(または何も)を指す
    clearPreview()
    for (const entry of entries) {
      if (!entry.geometry) continue
      const objects: THREE.Object3D[] = []
      const disposables: Array<{ dispose(): void }> = []
      for (const face of entry.geometry.faces) {
        if (face.indices.length === 0) continue
        const geometry = faceGeometry(face)
        const mesh = new THREE.Mesh(geometry, previewMaterial)
        root.add(mesh)
        objects.push(mesh)
        disposables.push(geometry)
      }
      // 本物を隠してから幽霊を出す(重ねると半透明の中に古い形が透けて読めない)
      setBodyVisible(entry.bodyId, false)
      previewMeshes.set(entry.bodyId, { objects, disposables })
    }
  }

  /**
   * body_id -> ローカル幽霊(変換後の姿)。**`previewMeshes` とは別のMapである** ——
   * あちらはサーバが投機実行した「別の形」で本物を隠す。こちらは「同じ形の別の場所」で、
   * 本物を隠さない(隠すと移動前と移動後を見比べられない)。
   */
  const bodyGhosts = new Map<string, { group: THREE.Group; disposables: Array<{ dispose(): void }> }>()
  /** いま出しているローカル幽霊の指示そのもの(`ghostInfo` が返す。実機検査用の穴)。 */
  let bodyGhostInfo: BodyGhost[] = []

  function showBodyGhosts(entries: BodyGhost[]): void {
    clearBodyGhosts()
    for (const entry of entries) {
      // **使えない回転は幽霊ごと落とす**(軸の向きが長さ0・値が非有限)。無視して
      // 平行移動だけ掛けると、元の位置にぴったり重なった幽霊が出て「回っていないのに
      // 幽霊は出ている」という嘘になる —— 描かないほうが正しい。
      if (entry.rotate && !usableRotation(entry.rotate)) continue
      // **届いている生メッシュをそのまま使う**(サーバへ投げない)。非表示・未読込の
      // ボディはここに無いので黙って落ちる —— 見えないものの幽霊を出す意味が無い。
      const payload = rawGeometry.get(entry.bodyId)
      if (!payload) continue
      const group = new THREE.Group()
      const disposables: Array<{ dispose(): void }> = []
      for (const face of payload.faces) {
        if (face.indices.length === 0) continue // 三角形が取れなかった面(`buildBody` と同じ)
        const geometry = faceGeometry(face)
        group.add(new THREE.Mesh(geometry, previewMaterial))
        disposables.push(geometry)
      }
      if (group.children.length === 0) continue
      // **変換はグループに掛ける**(面ごとに座標を書き換えない)。回転が入っても
      // 変わるのはここが行列になることだけで、生成・破棄・消える場面は `bodyGhosts` の
      // 側に閉じたままである(`BodyGhost` のdocstring)。
      group.matrixAutoUpdate = false // 位置ではなく行列を直に置くので自動再計算を切る
      group.matrix.copy(bodyGhostMatrix(entry))
      group.matrixWorldNeedsUpdate = true
      root.add(group)
      bodyGhosts.set(entry.bodyId, { group, disposables })
      bodyGhostInfo.push({
        bodyId: entry.bodyId,
        offset: [...entry.offset],
        rotate: entry.rotate
          ? {
              point: [...entry.rotate.point] as [number, number, number],
              dir: [...entry.rotate.dir] as [number, number, number],
              angleDeg: entry.rotate.angleDeg,
            }
          : null,
      })
    }
  }

  function clearBodyGhosts(): void {
    bodyGhostInfo = []
    if (bodyGhosts.size === 0) return
    for (const entry of bodyGhosts.values()) {
      root.remove(entry.group)
      for (const item of entry.disposables) item.dispose()
    }
    bodyGhosts.clear()
  }

  function clearPreview(): void {
    // **ローカル幽霊も一緒に消す**(interfaceの `clearPreview` のdocstring)。ここに
    // 入れてあるので、`update`/`prune`/`App.svelte` の中断は何も足さずに正しく消える。
    clearBodyGhosts()
    pickChanged() // 隠していた本物が戻る(`showPreview` の対)
    if (previewMeshes.size === 0) return
    for (const [bodyId, entry] of previewMeshes) {
      for (const object of entry.objects) root.remove(object)
      for (const item of entry.disposables) item.dispose()
      setBodyVisible(bodyId, true)
    }
    previewMeshes.clear()
  }

  function prune(validBodyIds: string[]): void {
    const valid = new Set(validBodyIds)
    const stale = [...bodies.keys()].filter((bodyId) => !valid.has(bodyId))
    if (stale.length === 0) return
    clearPreview() // 消えるBodyの幽霊が取り残されないように(update と同じ理由)
    if (edgeBodyId !== null && stale.includes(edgeBodyId)) clearEdges()
    for (const bodyId of stale) clearBody(bodyId)
    rebuildPickIndex()
    refreshContentDiagonal() // 見えているものの大きさが変わった(解像度の上下限に効く)
    updateClipPlanes() // 内容が変わった → 視錐台を引き直す
  }

  /**
   * 半径 `radius` の球が画面に収まる、注視点からの距離。FOVとアスペクトの狭い方で決まる。
   *
   * **FOVを可変にした以上、収まる距離を定数で決め打ちできない**(2026-08-28)。
   * 既定の30度・アスペクト1以上では `radius / tan(15°) = radius * 3.7321` になる。
   * `frameToContent` はこれを `radius = diagonal / 2` で呼ぶので `diagonal * 1.8660` となる。
   * **既定を45度から30度へ下げた(2026-09-04)ことで、カメラは以前より約1.55倍遠くに
   * 置かれる**(45度では `radius / tan(22.5°) = radius * 2.4142` で `diagonal * 1.2071`。
   * **以前の決め打ち定数 `diagonal * 1.2` はこの式を45度で評価して丸めた値そのもの**だった
   * が、既定FOVが変わった今は数値が一致しない。「既定のFOVでの見え方は実質変わらない」は
   * もう成り立たない —— 30度は45度よりパースが弱く見え、そのぶんカメラは遠い。
   * 縦長のビューポート(aspect<1)では距離が伸びるが、これは以前が横方向を見ていなかった
   * 取りこぼしの修正にあたる(狭い方に合わせないと横がはみ出す)。
   *
   * **正投影のときも透視の画角で評価する。** 正投影では距離が見え方に効かないので、
   * ここが返すのは「透視と同じ場所にカメラを置くための距離」でしかない(収める仕事は
   * `fitHalfHeight` + `setViewHalfHeight` がする)。
   *
   * **収める量は `fitHalfHeight` が持ち、ここはそれを距離へ直すだけ**(縦横の狭い方に
   * 合わせる判断はあちらに1つだけ置く)。
   */
  function fitDistance(radius: number): number {
    return fitHalfHeight(radius) / Math.tan(THREE.MathUtils.degToRad(perspective.fov) / 2)
  }

  /**
   * 半径 `radius` の球が画面に収まる「見かけの大きさ」の半分(＝注視点の面で画面の縦に
   * 収まる世界長さの半分)。**縦横の狭い方に合わせる** —— 横長の画面では縦が、
   * 縦長の画面では横が先に詰まる。
   *
   * **`fitDistance` と対で、同じ球を収める。** 透視は距離で、正投影は視錐台の大きさで
   * 収めるので、収める量そのもの(この関数)と、それを距離へ直す換算(`fitDistance`)に
   * 分けてある。分けないと正投影側が「収める距離」という意味の無い値を通ることになる。
   */
  function fitHalfHeight(radius: number): number {
    return perspective.aspect >= 1 ? radius : radius / perspective.aspect
  }

  /**
   * 注視点の面で画面の縦に収まる世界長さの半分。**2つのカメラを同じ物差しで測るための穴**
   * で、「見かけの大きさを保つ」処理(`setFov` の切り替え)と当たり判定の太さ
   * (`worldPerPixel`)がここを通る。
   *
   * 透視は距離×tan(画角/2)、正投影は視錐台の半分の高さ÷`zoom`(ドリーは `zoom` を動かす
   * だけでカメラを動かさないので、`zoom` を割らないと見かけの大きさにならない)。
   */
  function viewHalfHeight(): number {
    if (camera === orthographic) return orthographic.top / orthographic.zoom
    const distance = camera.position.distanceTo(controls.target)
    return distance * Math.tan(THREE.MathUtils.degToRad(perspective.fov) / 2)
  }

  /**
   * 見かけの大きさ(`viewHalfHeight`)を `half` にする。**透視は注視点からの距離で、
   * 正投影は視錐台の大きさで作る** —— 正投影ではカメラを前後させても大きさは1ミリも
   * 変わらないので、距離で調節する道が無い。
   *
   * 正投影側は `zoom` を1へ戻して視錐台そのものへ焼き込む(`zoom` は OrbitControls の
   * ドリーの持ち物なので、こちらが値を持ち続けると次のドリーで踏まれる)。
   */
  function setViewHalfHeight(half: number): void {
    const safe = Math.max(half, 1e-6)
    if (camera === orthographic) {
      const { width, height } = size()
      orthographic.top = safe
      orthographic.bottom = -safe
      orthographic.right = safe * (width / height)
      orthographic.left = -safe * (width / height)
      orthographic.zoom = 1
      orthographic.updateProjectionMatrix()
      return
    }
    const distance = safe / Math.tan(THREE.MathUtils.degToRad(perspective.fov) / 2)
    const offset = camera.position.clone().sub(controls.target)
    // 注視点にぴったり重なっていると向きが無い(長さ0のベクトルは伸ばせない)。既定の向きへ逃がす。
    if (offset.lengthSq() < 1e-18) offset.copy(HOME_VIEW_OFFSET)
    camera.position.copy(controls.target).add(offset.setLength(distance))
  }

  /**
   * 使うカメラを差し替える(透視 ⇄ 正投影)。**視点は持ち越す** —— 姿勢・クリップ範囲を
   * 写し、ヘッドライトとシーンへの所属、`OrbitControls` の操作対象を全部移す。
   *
   * **`OrbitControls` は `object` の差し替えを受け付ける**が、極(`_quat`)だけは生成時の
   * `up` から焼かれたまま動かない —— これは**そのままでよい**。両方のカメラの上方向は
   * ワールド+Zで同じなので、焼かれた極も同じである(`WORLD_UP` の説明を参照)。
   */
  function activateCamera(next: THREE.PerspectiveCamera | THREE.OrthographicCamera): void {
    if (next === camera) return
    next.position.copy(camera.position)
    next.up.copy(camera.up)
    next.quaternion.copy(camera.quaternion)
    next.near = camera.near
    next.far = camera.far
    // ヘッドライトはカメラの子(視点を回しても面が真っ黒にならない)。置き去りにすると暗くなる。
    camera.remove(headlight)
    scene.remove(camera)
    next.add(headlight)
    scene.add(next)
    camera = next
    controls.object = next
    next.updateProjectionMatrix()
  }

  /**
   * near/far(視錐台の前後端)を、いまの内容とカメラ距離から引き直す。
   *
   * **`frameToContent`(姿勢)から独立させてある**(2026-08-30)。姿勢はページ読み込みで
   * 1回決めれば十分だが、near/farは「最初に届いた形の大きさ」で固定してよいものではない
   * —— 後から大きなSTEPが届けばfarを追い越して手前が切り取られ(farはズーム・オービットでは
   * 一切広がらない)、逆に小さな細部へ深く寄ればnearが大きいまま手前が切れる。**内容が変わる
   * たび**(`refreshContentDiagonal` の呼び出し元)**とカメラが意味のある幅で動くたび**
   * (`cameraMoved` 判定に相乗り)の両方で呼ぶことで、視錐台を常時追随させる。
   *
   * near は**距離側からも**導く —— コンテンツの大きさだけで決めると、大きなモデルの中の
   * 小さな面へ深く寄ったときに手前が切れる(near は「いま見ている距離」に対しても十分
   * 小さくなければならない)。far は既存の `対角 × 20` の保守性をそのまま保ちつつ、
   * 距離が伸びた場合の下限も持つ(`frameToContent` の元のガードコメントの理由と同じ)。
   *
   * `span` の下限に `gridExtent * 2` を置くのは、空の文書やグリッドしか無い場面でも
   * 視錐台がグリッド全体を収める大きさを保つため(`contentDiagonal` はボディが無いと0)。
   * **`gridExtent` は固定ではない**(2026-08-30。`applyGridScale`)ので、グリッドが
   * 広がればここの下限も一緒に広がる —— 取りこぼすと、グリッドは見えているのに
   * その外側でnear/farが詰まる、という遠い壊れ方をする。
   */
  function updateClipPlanes(): void {
    const span = Math.max(contentDiagonal, gridExtent * 2)
    const distance = camera.position.distanceTo(controls.target)
    camera.near = Math.max(Math.min(span, distance) / 1000, 0.01)
    camera.far = Math.max(span * 20, distance * 4)
    camera.updateProjectionMatrix()
  }

  /**
   * グリッド計算専用の外接直方体。**`root` + `sketchRoot` だけ**から作る —— `gridRoot`
   * (グリッド自身)を含めると「グリッドが伸びる→箱が大きくなる→グリッドが伸びる」の
   * 発散フィードバックループになる(`gridRoot` は元々 `root` に入れていない。上のコメント
   * 参照)。`refreshContentDiagonal` の箱と同じもの(そちらは対角の長さだけを取り出す)。
   */
  function contentBoxForGrid(): THREE.Box3 {
    const box = new THREE.Box3().setFromObject(root)
    box.union(new THREE.Box3().setFromObject(sketchRoot))
    return box
  }

  /**
   * 基準平面のグリッドを描く正方形。**すべてのスケッチとソリッドの外接直方体**を、その平面の
   * u/v へ射影して求める(2026-09-03本人決定 —— 無限に描くのをやめた)。
   *
   * 射影するのは**箱の8隅**である。箱は軸並行なので、どの向きのframeでも8隅の射影の
   * 最小・最大が射影像の外接矩形になる(面の頂点を全部回す `faceUvBounds` と違い、内容が
   * 何万頂点あっても8点で済む)。
   *
   * `includeOrigin: true` —— モデルが原点から遠いとき、原点が枠の外へ出ると
   * 「原点がどこか読める」というグリッド本来の役割が失われる。
   */
  function contentBoundsForPlane(frame: PlaneFrame): UvBounds {
    const box = contentBoxForGrid()
    if (box.isEmpty()) return boundingSquare(null, { includeOrigin: true })
    const origin = new THREE.Vector3(...(frame.origin as [number, number, number]))
    const axisU = new THREE.Vector3(...(frame.u as [number, number, number])).normalize()
    const axisV = new THREE.Vector3(...(frame.v as [number, number, number])).normalize()
    const corner = new THREE.Vector3()
    let uv: UvBox | null = null
    for (let i = 0; i < 8; i++) {
      corner
        .set(
          i & 1 ? box.max.x : box.min.x,
          i & 2 ? box.max.y : box.min.y,
          i & 4 ? box.max.z : box.min.z,
        )
        .sub(origin)
      const u = corner.dot(axisU)
      const v = corner.dot(axisV)
      uv =
        uv === null
          ? { uMin: u, uMax: u, vMin: v, vMax: v }
          : {
              uMin: Math.min(uv.uMin, u),
              uMax: Math.max(uv.uMax, u),
              vMin: Math.min(uv.vMin, v),
              vMax: Math.max(uv.vMax, v),
            }
    }
    return boundingSquare(uv, { includeOrigin: true })
  }

  /**
   * **基準平面のピック範囲**の半幅を内容の大きさへ合わせる(`gridScale.ts` の `extentFor`)。
   * **格子の見た目には効かない** —— 刻みは3段固定で、格子はシェーダーが無限に描く。
   *
   * **広げるのは常に即座**(`allowShrink` に関係なく)。**縮めるのは `allowShrink` が
   * 立っているときだけ** —— `frameToContent` が実際に合わせたとき(＝`frameAll`)と、
   * 新規文書にしたとき(`resetGrid`)に限る。ふつうの編集(`refreshContentDiagonal` からの
   * 呼び出し)は常に `allowShrink: false` で、内容がしきい値付近で揺れても指せる範囲が
   * 前後に揺れることはない。
   *
   * 範囲が変わったら、ホバー板(`rebuildBasePlaneHover`)と面外の軸線の長さ
   * (`applyGridPlanes`)を組み直し、near/farの下限(`updateClipPlanes`)も引き直す。
   */
  function applyGridScale(box: THREE.Box3, allowShrink: boolean): void {
    const extent = extentFor(box)
    const changed = extent !== gridExtent && !(extent < gridExtent && !allowShrink)
    if (changed) {
      gridExtent = extent
      rebuildBasePlaneHover(gridExtent)
      applyBasePlaneHover() // 組み直した新しいメッシュへ、いまのホバー状態を反映し直す
      updateClipPlanes()
    }
    // **ピック範囲が動かなくても描画の枠は入れ直す** —— 枠は内容そのもの(`contentBoxForGrid`)
    // から引くので、`extentFor` の1-2-5の丸めやヒステリシスに載らない変化でも動く。
    applyGridPlanes()
  }

  /** シーンに今ある全メッシュの外接直方体にカメラを合わせる(初回だけ)。
   *
   * 複数Bodyでは初回に届く順序や本数が変わりうるので、**実際に置いたメッシュから計算する**方が
   * 頑丈(サーバのbbox形式に依存しない)。
   *
   * **姿勢(位置・向き)だけを決める。** near/farは `updateClipPlanes` の仕事(上記コメント)。
   *
   * **実際に合わせたかを boolean で返す**(2026-08-30)。箱が空(=描くものがまだ無い)なら
   * 何もせず `false` を返す —— 呼び出し側の `update()` はこれを見て初めて「一度きりの機会」
   * (`framed` フラグ)を消費する。戻り値を見ずに常に `framed = true` にすると、
   * `geometry: null` のBodyだけが届いた1通(本体の形状がまだ無い等)で空振りのまま
   * 機会が焼き切れ、以後どれだけ内容が届いても初回フレーミングが起きなくなる。
   */
  function frameToContent(): boolean {
    const box = new THREE.Box3().setFromObject(root)
    // **姿勢を合わせられるか(box空)に関わらず、グリッドの縮小許可は必ず1回通す**
    // (2026-08-31 QA指摘で修正)。`frameAll()`はここ(`frameToContent`)を呼ぶだけで、
    // 縮小の許可はここにしか無い —— 下の `box.isEmpty()` の早期returnより前に置かないと、
    // モデルを全部消してから`frameAll()`しても、広がったグリッドが二度と縮まなかった
    // (`contentBoxForGrid()`は`sketchRoot`も見るので、ボディだけ消してスケッチが残る
    // 場合も同じ早期returnを通る)。**姿勢(戻り値・カメラの位置/向き)の扱いは変えない**
    // —— これは「実際に合わせたか」を表す契約のままで、グリッドの縮小はそれとは独立に
    // 判定する(`applyGridScale`はその一手だけを担う)。
    applyGridScale(contentBoxForGrid(), true)
    if (box.isEmpty()) return false
    const center = new THREE.Vector3()
    box.getCenter(center)
    const diagonal = Math.max(box.getSize(new THREE.Vector3()).length(), 1e-6)
    const distance = fitDistance(diagonal / 2)
    camera.position.copy(center).addScaledVector(HOME_VIEW_OFFSET, distance)
    controls.target.copy(center)
    // **正投影は距離では収まらない。** カメラは上で透視と同じ場所へ置いた(回転の半径のため)が、
    // 収める仕事は視錐台がする。
    //
    // **`fitHalfHeight` をそのまま渡さない。** `HOME_VIEW_OFFSET` の長さが1ではないので、
    // 透視が実際に出すのは `fitHalfHeight` の 1.5362倍ぶん引いた絵である —— 素の
    // `fitHalfHeight` で視錐台を作ると、`frameAll` のたびに**正投影だけが1.54倍に寄る**
    // (2026-08-28に受入で実測。93.6px → 143.7px)。同じ係数を掛けて、どちらの投影でも
    // `frameAll` の見かけが揃うようにする。
    if (camera === orthographic) {
      setViewHalfHeight(fitHalfHeight(diagonal / 2) * HOME_VIEW_OFFSET.length())
    }
    updateClipPlanes()
    controls.update()
    return true
  }

  function setFov(deg: number): void {
    if (!Number.isFinite(deg)) return
    // **0だけが正投影**で、それ以外は 30〜90 へ丸める(あいだの1〜29という値は無い)。
    const next = deg === FOV_ORTHO ? FOV_ORTHO : Math.min(FOV_MAX, Math.max(FOV_MIN, deg))
    const wantOrtho = next === FOV_ORTHO
    const already = wantOrtho
      ? camera === orthographic
      : camera === perspective && Math.abs(next - perspective.fov) < 1e-6
    if (already) return
    // **切り替えの前に見かけの大きさを測り、切り替えた後に同じ値へ戻す。**
    // これをしないとパースの強さと一緒に拡大率まで変わり、ただの「ズーム」になってしまう
    // (正投影への切り替えでも同じ —— 視錐台の大きさを、測った値から作る)。
    const half = viewHalfHeight()
    activateCamera(wantOrtho ? orthographic : perspective)
    if (!wantOrtho) perspective.fov = next
    setViewHalfHeight(half)
    camera.far = Math.max(camera.far, camera.position.distanceTo(controls.target) * 4)
    camera.updateProjectionMatrix()
    controls.update()
  }

  function cameraPose(): {
    position: [number, number, number]
    target: [number, number, number]
    up: [number, number, number]
    near: number
    far: number
  } {
    return {
      position: [camera.position.x, camera.position.y, camera.position.z],
      target: [controls.target.x, controls.target.y, controls.target.z],
      up: [camera.up.x, camera.up.y, camera.up.z],
      near: camera.near,
      far: camera.far,
    }
  }

  function projectPoint(point: readonly [number, number, number]): { x: number; y: number } | null {
    const { width, height } = size()
    const projected = new THREE.Vector3(point[0], point[1], point[2]).project(camera)
    if (projected.z < -1 || projected.z > 1) return null
    return { x: ((projected.x + 1) / 2) * width, y: ((1 - projected.y) / 2) * height }
  }

  /** 直近に使ったハイライト色(`highlightColors`)。出していない種別は null。 */
  const highlightColor: { face: number | null; edge: number | null; vertex: number | null } = {
    face: null,
    edge: null,
    vertex: null,
  }

  /** 「持ち主+添字」の1本の鍵(面・辺・領域で共通)。 */
  function refKey(owner: string, index: number): string {
    return `${owner}\u0000${index}`
  }

  /** 今の選択(`highlight` が受け取ったもの)。**材質を決めるのは下の1箇所だけ**。 */
  let selectedFaceKeys = new Set<string>()
  let faceOrigin: 'user' | 'proposed' = 'user'
  /** ボディ単位の選択(`highlightBodies`。保持/除外を決めているあいだだけ埋まる)。 */
  let selectedBodyIds = new Set<string>()
  let bodyOrigin: 'user' | 'proposed' = 'user'

  /**
   * 形状最適化(第3のモード)の役割。**モードを抜けたら空にする**(`setOptimizeState(null)`)
   * ので、抜けているあいだは常に `'none'` になる(タスク仕様「モードを抜けているあいだは
   * 描かない」)。
   */
  let bodyRoles = new Map<string, 'preserve' | 'exclude'>()
  let fixtureFaceKeys = new Set<string>()

  /**
   * 面の材質を決める。**選択とホバーと役割の全部を見る唯一の場所**。
   *
   * 分けてはいけない —— どれも `mesh.material` を書くので、後から上書きする形にすると
   * 次のstate配信で走る `highlight()` の全走査に黙って消される。
   */
  function applyFaceMaterials(): void {
    highlightColor.face =
      selectedFaceKeys.size === 0 && selectedBodyIds.size === 0
        ? null
        : faceMaterialFor('none', selectedFaceKeys.size > 0 ? faceOrigin : bodyOrigin, false).color.getHex()
    for (const mesh of allFaceMeshes) {
      const bodyId = mesh.userData.bodyId as string
      const faceIndex = mesh.userData.faceIndex as number
      const hitAsFace = selectedFaceKeys.has(refKey(bodyId, faceIndex))
      const hitAsBody = selectedBodyIds.has(bodyId)
      const state: FaceMaterialState = hitAsFace ? faceOrigin : hitAsBody ? bodyOrigin : 'plain'
      const role: OptimizeFaceRole = fixtureFaceKeys.has(refKey(bodyId, faceIndex))
        ? 'fixture'
        : (bodyRoles.get(bodyId) ?? 'none')
      const isHovered =
        hoverFace !== null && hoverFace.bodyId === bodyId && hoverFace.faceIndex === faceIndex
      mesh.material = faceMaterialFor(role, state, isHovered)
    }
  }

  function highlight(refs: FaceRef[], origin: 'user' | 'proposed'): void {
    faceOrigin = origin
    selectedFaceKeys = new Set(refs.map((ref) => refKey(ref.bodyId, ref.faceIndex)))
    applyFaceMaterials()
  }

  /**
   * 選択中のボディを強調する(`highlight` のボディ版)。保持/除外ソリッドを決めている
   * あいだだけ非空になる —— **選択が役割色に勝つ**ので、ここに載ったボディは
   * `applyFaceMaterials()` が全面をハイライト色で塗る。
   */
  function highlightBodies(bodyIds: string[], origin: 'user' | 'proposed'): void {
    bodyOrigin = origin
    selectedBodyIds = new Set(bodyIds)
    applyFaceMaterials()
  }

  // ------------------------------------------------------------------ 形状最適化(第3のモード)の装飾
  /**
   * いま描いている装飾の**中身**(実機検査フック専用)。
   *
   * `optimizeAnnotationCount()`(子オブジェクトの総数)だけでは、拘束記号と荷重矢印を
   * 取り違えても・矢印が逆向きでも・ラベルが空文字でも数だけは合ってしまい検出できない
   * (2026-08-31 QA指摘)。ここに種別ごとの中身(拘束は対象の面、荷重は単位ベクトルと
   * ラベル文字列)を控えておき、`optimizeAnnotations()` で読めるようにする。
   */
  let optimizeAnnotationInfo: {
    fixtures: FaceRef[]
    loads: Array<{ bodyId: string; faceIndex: number; direction: [number, number, number]; label: string }>
  } = { fixtures: [], loads: [] }

  /** 拘束記号・荷重矢印+ラベルを全部消す。`setOptimizeState` が最初に呼ぶ。 */
  function clearOptimizeAnnotations(): void {
    optimizeRoot.clear()
    for (const item of optimizeDisposables) item.dispose()
    optimizeDisposables = []
    optimizeAnnotationInfo = { fixtures: [], loads: [] }
  }

  /**
   * 面の重心と平均法線を、**いま実際に描かれているメッシュ**から測る。
   *
   * サーバの配りもの(`FaceRef`)は添字だけで重心・法線を持たない。ここは装飾専用の
   * 簡易測定であって `duocad/optimize.py` の指紋(面積加重・照合用)とは別物 ——
   * 頂点の単純平均でよい(タスク仕様「あとから直すのは安い。凝りすぎない」)。
   */
  function faceCentroidNormal(
    bodyId: string,
    faceIndex: number,
  ): { center: THREE.Vector3; normal: THREE.Vector3 } | null {
    const mesh = allFaceMeshes.find(
      (candidate) => candidate.userData.bodyId === bodyId && candidate.userData.faceIndex === faceIndex,
    )
    if (!mesh) return null
    const position = mesh.geometry.getAttribute('position') as THREE.BufferAttribute
    const normalAttr = mesh.geometry.getAttribute('normal') as THREE.BufferAttribute
    if (!position || position.count === 0) return null
    const center = new THREE.Vector3()
    const normal = new THREE.Vector3()
    for (let i = 0; i < position.count; i++) {
      center.x += position.getX(i)
      center.y += position.getY(i)
      center.z += position.getZ(i)
      if (normalAttr) {
        normal.x += normalAttr.getX(i)
        normal.y += normalAttr.getY(i)
        normal.z += normalAttr.getZ(i)
      }
    }
    center.divideScalar(position.count)
    if (normal.lengthSq() < 1e-12) normal.set(0, 0, 1)
    else normal.normalize()
    return { center, normal }
  }

  /**
   * 貼った面の外接範囲を、スケッチのframe(u,v)で測る(`setBounds` へ渡す枠)。
   *
   * **基準平面の `extentFor` を使い回さない** —— あれの `BASE_EXTENT`(100mm)下限は
   * 「空の文書でもこれより狭くしない」という文書スケールの前提で、実測で40×20mmの面に
   * 対して100mmを返し、面の何倍もの大きさでグリッドが描かれて背景が線で埋まる破綻を
   * 起こした(スクリーンショットで発見。engine `extent-for-floor-leaks-document-scale-into-face-scale`)。
   *
   * **原点対称の半幅ではなく実測の min/max を返す**(2026-09-03) —— 面のframeの原点は
   * 面の中心ではなく**面の隅(OCCTのUVパラメータ化の原点)のことが多く**、対称な枠だと
   * 枠が面の片側へ大きく偏る(実測: u:0-40 / v:-20-0 の面に ±50mm の枠が出た)。
   *
   * 見つからなければ `null`(=範囲で切らずに描く。面が消えた・非表示等で測れないときに
   * 装飾が消えるより、広めに描いて残る方が安全)。**面の実際の輪郭では切らない**
   * (トリム境界のマスクはステンシルが要り費用に見合わない)。
   */
  function faceUvBounds(bodyId: string, faceIndex: number, frame: PlaneFrame): UvBounds | null {
    const mesh = allFaceMeshes.find(
      (candidate) => candidate.userData.bodyId === bodyId && candidate.userData.faceIndex === faceIndex,
    )
    if (!mesh) return null
    const position = mesh.geometry.getAttribute('position') as THREE.BufferAttribute
    if (!position || position.count === 0) return null
    const origin = new THREE.Vector3(...(frame.origin as [number, number, number]))
    const axisU = new THREE.Vector3(...(frame.u as [number, number, number])).normalize()
    const axisV = new THREE.Vector3(...(frame.v as [number, number, number])).normalize()
    const rel = new THREE.Vector3()
    let uv: UvBox | null = null
    for (let i = 0; i < position.count; i++) {
      rel.set(position.getX(i), position.getY(i), position.getZ(i)).sub(origin)
      const u = rel.dot(axisU)
      const v = rel.dot(axisV)
      uv =
        uv === null
          ? { uMin: u, uMax: u, vMin: v, vMax: v }
          : {
              uMin: Math.min(uv.uMin, u),
              uMax: Math.max(uv.uMax, u),
              vMin: Math.min(uv.vMin, v),
              vMax: Math.max(uv.vMax, v),
            }
    }
    if (uv === null) return null
    if (!(uv.uMax - uv.uMin > 0) && !(uv.vMax - uv.vMin > 0)) return null
    return boundingSquare(uv, { includeOrigin: false })
  }

  /** 荷重の大きさのラベル(canvasテクスチャの札)。**モデルの大きさへ追随**させる
   *  (`height` は呼び出し側が `contentDiagonal` から決める)。 */
  function makeMagnitudeLabel(
    text: string,
    height: number,
  ): { sprite: THREE.Sprite; disposables: Array<{ dispose(): void }> } {
    const canvas = document.createElement('canvas')
    canvas.width = 256
    canvas.height = 96
    const ctx = canvas.getContext('2d')
    if (ctx) {
      ctx.fillStyle = 'rgba(16, 22, 30, 0.85)'
      ctx.fillRect(0, 0, canvas.width, canvas.height)
      ctx.fillStyle = '#ffc857'
      ctx.font = 'bold 48px sans-serif'
      ctx.textBaseline = 'middle'
      ctx.fillText(text, 12, canvas.height / 2)
    }
    const texture = new THREE.CanvasTexture(canvas)
    const material = new THREE.SpriteMaterial({ map: texture, depthTest: false, depthWrite: false, transparent: true })
    const sprite = new THREE.Sprite(material)
    sprite.scale.set(height * (canvas.width / canvas.height), height, 1)
    return { sprite, disposables: [texture, material] }
  }

  /**
   * 形状最適化(第3のモード)の見た目を作り直す。**`null` は全部消すだけ**
   * (モードを抜けた・そもそも入っていない)。
   *
   * `preserveBodies` / `excludeBodies` は `applyFaceMaterials()` が読む役割の表を差し替える
   * だけ(ボディの色は選択と同じ経路)。拘束・荷重は**装飾**(`optimizeRoot`)を作り直す ——
   * 拘束面は固定記号(四角錐)を法線の上に置き、荷重面は矢印(`THREE.ArrowHelper`。長さは
   * 大きさの対数スケール)+ 大きさのラベルを描く。どちらも**モデルの大きさへ追随**させる
   * (`contentDiagonal` が0なら文書が空なので何も描けない)。
   */
  function setOptimizeState(state: OptimizeVisualState | null): void {
    clearOptimizeAnnotations()
    bodyRoles = new Map()
    fixtureFaceKeys = new Set()
    if (state === null) {
      applyFaceMaterials()
      return
    }
    for (const bodyId of state.preserveBodies) bodyRoles.set(bodyId, 'preserve')
    for (const bodyId of state.excludeBodies) bodyRoles.set(bodyId, 'exclude')
    fixtureFaceKeys = new Set(state.fixtures.map((ref) => refKey(ref.bodyId, ref.faceIndex)))
    applyFaceMaterials()

    const scale = Math.max(contentDiagonal, 1e-6)
    if (scale <= 1e-6) return // 空の文書(装飾できる面が無い)

    // -------------------------------------------------- 拘束の固定記号(四角錐)
    if (state.fixtures.length > 0) {
      const markSize = scale * 0.03
      const markMaterial = new THREE.MeshLambertMaterial({ color: OPTIMIZE_FIXTURE_MARK_COLOR })
      optimizeDisposables.push(markMaterial)
      for (const ref of state.fixtures) {
        const measured = faceCentroidNormal(ref.bodyId, ref.faceIndex)
        if (!measured) continue
        // **4角錐**(`radialSegments=4`)。既定は+Y軸を高さの向きに持つので、面へ刺さる向き
        // (法線の逆)へ回してから、根元が面の少し上に来るよう法線方向へ持ち上げる。
        const geometry = new THREE.ConeGeometry(markSize * 0.6, markSize, 4)
        optimizeDisposables.push(geometry)
        const cone = new THREE.Mesh(geometry, markMaterial)
        cone.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), measured.normal.clone().negate())
        cone.position.copy(measured.center).addScaledVector(measured.normal, markSize * 0.6)
        optimizeRoot.add(cone)
        optimizeAnnotationInfo.fixtures.push({ bodyId: ref.bodyId, faceIndex: ref.faceIndex })
      }
    }

    // -------------------------------------------------- 荷重の矢印+ラベル
    if (state.load !== null && state.loads.length > 0) {
      const { magnitude, direction, vector } = state.load
      // **長さは大きさの対数スケール**(タスク仕様)。1N未満でも長さが0にならないよう
      // `Math.max(magnitude, 1)` で下駄を履かせる(見た目専用の式で、正確な力学ではない)。
      const length = scale * 0.12 * (1 + Math.log10(Math.max(magnitude, 1)) * 0.3)
      const labelHeight = scale * 0.05
      const labelText = `${Number(magnitude.toFixed(2))}N`
      for (const ref of state.loads) {
        const measured = faceCentroidNormal(ref.bodyId, ref.faceIndex)
        if (!measured) continue
        const dir =
          direction === 'vector' && vector !== null
            ? new THREE.Vector3(vector[0], vector[1], vector[2]).normalize()
            : direction === '-normal'
              ? measured.normal.clone().negate()
              : measured.normal.clone()
        const arrow = new THREE.ArrowHelper(dir, measured.center, length, OPTIMIZE_LOAD_COLOR, length * 0.3, length * 0.18)
        optimizeRoot.add(arrow)
        optimizeDisposables.push(arrow) // `ArrowHelper.dispose()` が線・矢頭の材質を片付ける
        const { sprite, disposables } = makeMagnitudeLabel(labelText, labelHeight)
        sprite.position.copy(measured.center).addScaledVector(dir, length * 1.15)
        optimizeRoot.add(sprite)
        optimizeDisposables.push(...disposables)
        optimizeAnnotationInfo.loads.push({
          bodyId: ref.bodyId,
          faceIndex: ref.faceIndex,
          direction: [dir.x, dir.y, dir.z],
          label: labelText,
        })
      }
    }
  }

  // ------------------------------------------------------------------ 開発用のデバッグ表示
  //
  // **旧実験場(`probe_topopt/web/index.html`。2026-09-05に削除)にしか無かった3つを
  // 本体へ移したもの。**
  // 実験場は `design_space.CASES` の合成題材しか描けなかったが、ここは
  // **いま走らせた最適化そのもの**(利用者が置いた条件・利用者のボディ)を描く ——
  // 切り分けの道具が、切り分けたい対象を扱えないことがそもそもの問題だった。
  //
  // 出所と規約は `duocad/optimize_run.py` の `_DebugSink`。
  function clearOptimizeDebug(): void {
    debugRoot.clear()
    for (const item of debugDisposables) item.dispose()
    debugDisposables = []
    debugCounts = {
      space: 0,
      rho: 0,
      fixed: 0,
      load: 0,
      spaceStride: 1,
      rhoStride: 1,
      anchorsDepthTest: null,
    }
  }

  /**
   * 要素の通し添字 → 立方体の `InstancedMesh`。
   *
   * **セル中心の規約**(`duocad/topopt/mesh.py:169`): 要素 (i,j,k) の中心は
   * `origin + spacing*(i+0.5, j+0.5, k+0.5)`。**半要素ずらすと形が丸ごと動く**ので、
   * ここが唯一の変換点である(サーバ側は素の添字しか送らない)。
   */
  function buildDebugVoxels(voxels: OptimizeDebugVoxels, material: THREE.Material): THREE.InstancedMesh {
    const [, ny, nz] = voxels.grid.shape
    const spacing = voxels.grid.spacing
    const [ox, oy, oz] = voxels.grid.origin
    const geometry = new THREE.BoxGeometry(spacing, spacing, spacing)
    const count = voxels.elements.length
    const mesh = new THREE.InstancedMesh(geometry, material, Math.max(count, 1))
    mesh.count = count
    const dummy = new THREE.Object3D()
    for (let i = 0; i < count; i += 1) {
      const flat = voxels.elements[i]
      const ex = Math.floor(flat / (ny * nz))
      const rest = flat - ex * ny * nz
      const ey = Math.floor(rest / nz)
      const ez = rest - ey * nz
      dummy.position.set(
        ox + spacing * (ex + 0.5),
        oy + spacing * (ey + 0.5),
        oz + spacing * (ez + 0.5),
      )
      dummy.updateMatrix()
      mesh.setMatrixAt(i, dummy.matrix)
    }
    mesh.instanceMatrix.needsUpdate = true
    // **指せない。** `frustumCulled` は既定のままでよい(`InstancedMesh` は
    // `computeBoundingSphere()` を自前で持つ)。
    mesh.computeBoundingSphere()
    // **`InstancedMesh` 自身も片付ける**(インスタンス行列のGPUバッファを持っている)。
    debugDisposables.push(geometry, material, mesh)
    return mesh
  }

  /**
   * 節点を球で描く。**深度検査を切って最前面**(`depthTest: false`)——
   * 形状の裏側にある節点(拘束面はたいていカメラの反対側)まで見えていないと、
   * 「見えているつもりで見えていない」になる(probe から引き継いだ罠)。
   */
  function buildDebugNodes(
    points: number[],
    radius: number,
    color: number,
  ): THREE.InstancedMesh {
    const count = Math.floor(points.length / 3)
    const geometry = new THREE.SphereGeometry(radius, 12, 8)
    const material = new THREE.MeshLambertMaterial({
      color,
      emissive: color,
      emissiveIntensity: 0.6,
      depthTest: false,
      depthWrite: false,
    })
    const mesh = new THREE.InstancedMesh(geometry, material, Math.max(count, 1))
    mesh.count = count
    mesh.renderOrder = LAYER.debug
    const dummy = new THREE.Object3D()
    for (let i = 0; i < count; i += 1) {
      dummy.position.set(points[3 * i], points[3 * i + 1], points[3 * i + 2])
      dummy.updateMatrix()
      mesh.setMatrixAt(i, dummy.matrix)
    }
    mesh.instanceMatrix.needsUpdate = true
    mesh.computeBoundingSphere()
    debugDisposables.push(geometry, material, mesh)
    return mesh
  }

  /** `?voxel=1` で隠している結果ボディの表示を当て直す(`update()` が作り直した後にも要る)。 */
  function applyDebugHidden(): void {
    if (debugHiddenBodyId !== null) setBodyVisible(debugHiddenBodyId, false)
  }

  function setOptimizeDebug(state: OptimizeDebugState | null): void {
    // 前回隠したボディを戻す(次の状態がまた隠すなら下でもう一度隠す)。
    if (debugHiddenBodyId !== null) setBodyVisible(debugHiddenBodyId, true)
    debugHiddenBodyId = null
    clearOptimizeDebug()
    if (state === null) return

    if (state.space !== null && state.space.elements.length > 0) {
      // **半透明で薄く重ねる**(probe の `?debug=space` と同じ)。塊は「答えの周りの箱」
      // なので、不透明にすると中の形が1つも見えない。
      const mesh = buildDebugVoxels(
        state.space,
        new THREE.MeshLambertMaterial({
          color: DEBUG_SPACE_COLOR,
          transparent: true,
          opacity: 0.28,
          depthWrite: false,
        }),
      )
      mesh.renderOrder = LAYER.debug
      debugRoot.add(mesh)
      debugCounts.space = state.space.elements.length
      debugCounts.spaceStride = state.space.stride
    }

    if (state.rho !== null && state.rho.elements.length > 0) {
      // **平滑化する前のボクセルそのもの**(`?voxel=1`)。不透明で描く ——
      // marching cubes を通す前の階段が見えることがこの表示の目的である。
      const mesh = buildDebugVoxels(
        state.rho,
        new THREE.MeshLambertMaterial({ color: DEBUG_RHO_COLOR }),
      )
      debugRoot.add(mesh)
      debugCounts.rho = state.rho.elements.length
      debugCounts.rhoStride = state.rho.stride
    }

    if (state.anchors !== null) {
      // 球の半径は**格子の要素サイズ**に合わせる(節点の間隔が要素サイズなので、
      // 0.34要素なら隣とくっつかずに数えられる。probe と同じ比率)。
      // **アンカー自身が持っている格子**を使う(サーバが必ず載せてくる)。
      // 塊や密度場を頼んでいないときでも同じ大きさになる。
      const radius = state.anchors.grid.spacing * 0.34
      const fixed = buildDebugNodes(state.anchors.fixed.points, radius, DEBUG_ANCHOR_FIXED_COLOR)
      const load = buildDebugNodes(state.anchors.load.points, radius, DEBUG_ANCHOR_LOAD_COLOR)
      debugRoot.add(fixed)
      debugRoot.add(load)
      debugCounts.fixed = fixed.count
      debugCounts.load = load.count
      // **材質から読み直す**(定数を写さない) —— 写すと、材質側だけ直したときに
      // 検査が古い値で合格し続ける。**両方の OR** にしてあるのは、片方だけ深度検査を
      // 戻した退化を見逃さないため(2026-09-05 QA指摘。以前は固定側しか見ていなかった)。
      debugCounts.anchorsDepthTest =
        (fixed.material as THREE.Material).depthTest ||
        (load.material as THREE.Material).depthTest
    }

    debugHiddenBodyId = state.hiddenBodyId
    applyDebugHidden()
  }

  /**
   * 面のピック。**レイ距離も返す** —— 基準平面と面のどちらが手前かを比べるのに要る
   * (`onPointerUp`)。`pickAt` はこれの薄い覆いで、距離を要らない呼び出し側のためにある。
   */
  function faceHitAt(
    offsetX: number,
    offsetY: number,
  ): { ref: FaceRef; distance: number } | null {
    const { width, height } = size()
    const ndc = new THREE.Vector2((offsetX / width) * 2 - 1, -(offsetY / height) * 2 + 1)
    raycaster.setFromCamera(ndc, camera)
    // **隠しているメッシュは指せない**(プレビュー中の本物。three.js の Raycaster は
    // `visible` を見ないので、ここで明示的に外す)。
    const hits = raycaster.intersectObjects(
      allFaceMeshes.filter((mesh) => mesh.visible),
      false,
    )
    if (hits.length === 0) return null
    const bodyId = hits[0].object.userData.bodyId
    const faceIndex = hits[0].object.userData.faceIndex
    if (typeof bodyId !== 'string' || typeof faceIndex !== 'number') return null
    return { ref: { bodyId, faceIndex }, distance: hits[0].distance }
  }

  function pickAt(offsetX: number, offsetY: number): FaceRef | null {
    return faceHitAt(offsetX, offsetY)?.ref ?? null
  }

  // ------------------------------------------------------------------ 基準3平面(スケッチを貼る先)
  //
  // 「指せるか」の旗(`basePlanesOn`)は平面グリッドの節で宣言してある —— **どの平面を
  // 出すかがその旗で決まる**(`desiredGridPlanes`)ので、表示側と同じ場所に置く。

  function showBasePlanes(on: boolean): void {
    if (basePlanesOn === on) return
    basePlanesOn = on
    pickChanged()
    applyGridPlanes() // 出す平面(1枚 ⇄ 3枚)と色(通常 ⇄ 起動中)がこの旗で変わる
    // 指せなくなった平面の板を残さない(`basePlaneAt` が null を返すので次のフレームでも
    // 消えるが、ゲートを閉じた瞬間に消えるほうが「もう指せない」が伝わる)。
    if (!on) {
      hoverPlane = null
      applyBasePlaneHover()
    }
  }

  /**
   * 基準3平面のうち当たった1枚と、そのレイ距離(当たらなければ null)。
   *
   * **描いているグリッドは判定に使わない**(格子はシェーダーが無限に描くので、そもそも
   * 当たれるジオメトリが無い)。`planePointAt` と同じ解析交差を3枚ぶん回し、**ピック範囲**
   * の外(|u| か |v| が `gridExtent` 超)は捨て、**レイ距離が最小の1枚**を採る。
   *
   * **描画が無限になってもピック範囲は無限にしない**(2026-09-03) —— 無限にすると
   * engine `duocad-base-planes-lose-to-solids-by-depth` と
   * `viewport-resize-silently-changes-what-a-click-grabs` が同時に悪化する。
   * ホバー板(`rebuildBasePlaneHover`)も同じ `gridExtent` から作る —— ここだけずれると
   * 「板は見えているのに指せない」という遠い壊れ方をする。
   */
  function basePlaneAt(
    offsetX: number,
    offsetY: number,
  ): { name: BasePlaneName; distance: number } | null {
    if (!basePlanesOn) return null
    const { width, height } = size()
    const ndc = new THREE.Vector2((offsetX / width) * 2 - 1, -(offsetY / height) * 2 + 1)
    raycaster.setFromCamera(ndc, camera)
    let best: { name: BasePlaneName; distance: number } | null = null
    const hit = new THREE.Vector3()
    for (const spec of BASE_PLANES) {
      const plane = new THREE.Plane(new THREE.Vector3(...spec.normal), 0)
      if (raycaster.ray.intersectPlane(plane, hit) === null) continue // 平行 / カメラの裏
      const u = hit.dot(new THREE.Vector3(...spec.u))
      const v = hit.dot(new THREE.Vector3(...spec.v))
      if (Math.abs(u) > gridExtent || Math.abs(v) > gridExtent) continue
      const distance = raycaster.ray.origin.distanceTo(hit)
      if (best === null || distance < best.distance) best = { name: spec.name, distance }
    }
    return best
  }

  function projectBasePlane(name: BasePlaneName): { x: number; y: number } | null {
    const spec = BASE_PLANES.find((candidate) => candidate.name === name)
    if (!spec || !basePlanesOn) return null
    for (const [u, v] of BASE_PLANE_PROBES) {
      const world = new THREE.Vector3(
        u * spec.u[0] + v * spec.v[0],
        u * spec.u[1] + v * spec.v[1],
        u * spec.u[2] + v * spec.v[2],
      )
      const screen = toScreen(world)
      if (screen === null) continue
      // **本当にその平面が取れる画素か**を確かめてから返す(面の `project` と同じ流儀)。
      const plane = basePlaneAt(screen.x, screen.y)
      if (plane === null || plane.name !== name) continue
      const face = faceHitAt(screen.x, screen.y)
      if (face !== null && face.distance < plane.distance) continue // 部品に隠れている
      return screen
    }
    return null
  }

  function project(bodyId: string, faceIndex: number): { x: number; y: number } | null {
    const entry = bodies.get(bodyId)
    const mesh = entry?.faceMeshes.find(
      (candidate) => candidate.userData.faceIndex === faceIndex,
    )
    if (!mesh || !mesh.visible) return null
    const position = mesh.geometry.getAttribute('position')
    const index = mesh.geometry.getIndex()
    if (!position || !index) return null
    const { width, height } = size()
    const point = new THREE.Vector3()
    const corner = new THREE.Vector3()

    const triangleCount = index.count / 3
    for (let triangle = 0; triangle < triangleCount; triangle += 1) {
      point.set(0, 0, 0)
      for (let vertex = 0; vertex < 3; vertex += 1) {
        const at = index.getX(triangle * 3 + vertex)
        corner.set(position.getX(at), position.getY(at), position.getZ(at))
        point.add(corner)
      }
      point.multiplyScalar(1 / 3)
      mesh.localToWorld(point)
      const projected = point.clone().project(camera)
      if (projected.z < -1 || projected.z > 1) continue
      const x = ((projected.x + 1) / 2) * width
      const y = ((1 - projected.y) / 2) * height
      if (x < 0 || y < 0 || x > width || y > height) continue
      if (!clickable(x, y)) continue // 浮いているパネルに覆われている(次の三角形を試す)
      const hit = pickAt(x, y)
      if (hit && hit.bodyId === bodyId && hit.faceIndex === faceIndex) return { x, y }
    }
    return null
  }

  // ------------------------------------------------------------------ D: 辺(選べる線)
  //
  // **フィレット・面取りが人間側から使えるようになる場所**。輪郭線として常時出ている
  // `EdgesGeometry` の線(`buildBody` 参照)はコアの辺の添字と対応しておらず、どれを指しても
  // フィレットの引数にならない。ここで描くのはサーバが配る
  // `kernel.tessellate_edges` の折れ線で、**1本1本がコアの添字を持っている**。
  //
  // ソリッドともスケッチとも別のグループに持つ(`update()`/`prune()` は `root` しか
  // 触らないので、押し出しで辺のグループが巻き込まれない)。
  const edgeRoot = new THREE.Group()
  scene.add(edgeRoot)

  const pickEdgeMaterial = new THREE.LineBasicMaterial({
    color: PICK_EDGE_COLOR,
    // **深度検査をしない**(奥の辺も見え、奥の辺も指せる)。three.js の線のレイキャストは
    // 面による遮蔽を見ないので、見た目だけ隠すと「見えない辺が選ばれる」になる。
    depthTest: false,
  })
  const pickEdgeSelectedMaterial = new THREE.LineBasicMaterial({
    color: HIGHLIGHT_COLOR,
    depthTest: false,
  })
  const pickEdgeProposedMaterial = new THREE.LineBasicMaterial({
    color: PROPOSED_COLOR,
    depthTest: false,
  })
  // ホバー中の辺(面と同じで明度を足すだけ)。
  const pickEdgeHoverMaterial = new THREE.LineBasicMaterial({
    color: lighten(PICK_EDGE_COLOR, HOVER_LIGHTEN),
    depthTest: false,
  })
  const pickEdgeSelectedHoverMaterial = new THREE.LineBasicMaterial({
    color: lighten(HIGHLIGHT_COLOR, HOVER_LIGHTEN),
    depthTest: false,
  })
  const pickEdgeProposedHoverMaterial = new THREE.LineBasicMaterial({
    color: lighten(PROPOSED_COLOR, HOVER_LIGHTEN),
    depthTest: false,
  })

  /** 今出している辺(**1つのBodyぶんだけ**)。空なら辺モードではない。 */
  let edgeLines: THREE.Line[] = []
  let edgeBodyId: string | null = null
  /** 今出している辺の元になった配列(**同じものを渡されたら作り直さない**ための印)。 */
  let edgeSource: EdgeGeometry[] | null = null
  const edgeDisposables: Array<{ dispose(): void }> = []

  function clearEdges(): void {
    for (const line of edgeLines) edgeRoot.remove(line)
    for (const item of edgeDisposables) item.dispose()
    edgeDisposables.length = 0
    edgeLines = []
    edgeBodyId = null
    edgeSource = null
    invalidateHover()
  }

  function showEdges(bodyId: string | null, edges: EdgeGeometry[]): void {
    // **同じものを渡されたら何もしない**。呼び出し側(`App.svelte`)は選択が変わるたびに
    // 呼び直すので、毎回作り直すと**辺を1本クリックするたびに数百本ぶんの
    // BufferGeometry を捨てて作り直す**ことになる(27部品では1Bodyで600本超)。
    if (bodyId !== null && bodyId === edgeBodyId && edges === edgeSource) return
    clearEdges()
    if (bodyId === null || edges.length === 0) return
    edgeBodyId = bodyId
    edgeSource = edges
    for (const edge of edges) {
      // 2点(=6要素)未満は線にならない。`kernel.tessellate_edges` は失敗時も端点2点を
      // 返す契約なので**ここへは届かないはず**だが、届いたときに黙って壊れた線を
      // 作らないための番人として残す。
      if (edge.points.length < 6) continue
      const geometry = new THREE.BufferGeometry()
      geometry.setAttribute(
        'position',
        new THREE.BufferAttribute(new Float32Array(edge.points), 3),
      )
      geometry.computeBoundingSphere()
      const line = new THREE.Line(geometry, pickEdgeMaterial)
      line.renderOrder = LAYER.edge // 深度を書かないぶん、描く順序で面より手前に出す
      line.userData.bodyId = bodyId
      line.userData.edgeIndex = edge.index
      edgeRoot.add(line)
      edgeLines.push(line)
      edgeDisposables.push(geometry)
    }
  }

  /** 今出している辺のうち選択に入っているもの(面の `selectedFaceKeys` と同じ役目)。 */
  let selectedEdgeIndices = new Set<number>()
  let edgeOrigin: 'user' | 'proposed' = 'user'

  /** 辺の材質を決める。**選択とホバーの両方を見る唯一の場所**(面と同じ規律)。 */
  function applyEdgeMaterials(): void {
    const selected = edgeOrigin === 'proposed' ? pickEdgeProposedMaterial : pickEdgeSelectedMaterial
    const selectedHover =
      edgeOrigin === 'proposed' ? pickEdgeProposedHoverMaterial : pickEdgeSelectedHoverMaterial
    highlightColor.edge = selectedEdgeIndices.size === 0 ? null : selected.color.getHex()
    for (const line of edgeLines) {
      const edgeIndex = line.userData.edgeIndex as number
      const isHit = selectedEdgeIndices.has(edgeIndex)
      const isHovered =
        hoverEdge !== null &&
        hoverEdge.bodyId === (line.userData.bodyId as string) &&
        hoverEdge.edgeIndex === edgeIndex
      line.material = isHit
        ? isHovered
          ? selectedHover
          : selected
        : isHovered
          ? pickEdgeHoverMaterial
          : pickEdgeMaterial
    }
  }

  function highlightEdges(refs: EdgeRef[], origin: 'user' | 'proposed'): void {
    edgeOrigin = origin
    selectedEdgeIndices = new Set(
      refs.filter((ref) => ref.bodyId === edgeBodyId).map((ref) => ref.edgeIndex),
    )
    applyEdgeMaterials()
  }

  /**
   * いまのカメラを投影の種類ごとの最小限の形へ落とす(`gridScale.ts` の純関数へ渡す用)。
   * **正投影には「距離」に相当する量が無い**ので、透視と同じ形にはできない。
   */
  function cameraProjection(): CameraProjection {
    if (camera === orthographic) {
      return { kind: 'ortho', frustumHeight: 2 * Math.max(viewHalfHeight(), 1e-6) }
    }
    return {
      kind: 'persp',
      fovDeg: perspective.fov,
      distance: camera.position.distanceTo(controls.target),
    }
  }

  /**
   * 画面1画素ぶんの世界座標の長さ(当たり判定の太さを画素で決めるため)。
   * **式は `gridScale.ts` の `mmPerPixel` 1つに寄せてある** —— グリッドの段の判定と
   * 当たり判定の太さが同じ物差しで動く(ずれると「見えている線が指せない」になる)。
   */
  function worldPerPixel(): number {
    const { height } = size()
    return Math.max(mmPerPixel(cameraProjection(), height), 1e-9)
  }

  // ------------------------------------------------------------------ 可変解像度(ズーム連動)
  //
  // **寄ったら細かく、引いたら粗く。** 曲線が多角形に見えるのは弦と弧の乖離が数画素に
  // なったときなので、目標精度は世界座標のmmではなく**画面の画素**で決める
  // (式と定数は `adaptiveResolution.ts`。同じものが `duocad/resolution.py` にもある)。

  /** カメラが止まったと見なすまでの待ち[ms]。**減衰(`dampingFactor`)が収まる時間も含む。** */
  const RESOLUTION_SETTLE_MS = 180
  /** 目標偏差がこの比だけ動いて初めて作り直す(1割程度の揺れでは測り直さない)。 */
  const RESOLUTION_CHANGE_RATIO = 1.25
  /** `worldPerPixel` が「まだ動いている」と見なす相対差。減衰の収束判定。 */
  const RESOLUTION_STILL_EPSILON = 1.0e-4
  /** カメラが「まだ動いている」と見なす移動量[画素]。減衰の収束判定(`cameraMoved`)。 */
  const CAMERA_STILL_PIXELS = 0.5
  /** 同・向きの収束判定(四元数の内積が `1 - これ` を下回ったら動いている)。 */
  const CAMERA_STILL_ANGLE = 1.0e-7
  /**
   * **いま細かく測っている面が粗い側へ落ちるまでの余裕[画素]**(`faceCulling.ts` の
   * `selectFineFaces`)。0にすると、視錐台の縁にある面が「入った/出た」を往復し、
   * そのたびに27部品ぶんの測り直しがサーバへ飛ぶ。**画素で決める**のは、
   * 縁の余裕が寄り引きで変わってはいけないため(当たり判定の太さと同じ流儀)。
   */
  const FOCUS_HYSTERESIS_PIXELS = 16

  /** 直近に採用した解像度(スケッチの折れ線と `viewResolution()` が読む)。 */
  let appliedResolution: ViewportResolution = {
    ...DEFAULT_RESOLUTION,
    worldPerPixel: 0,
    sizeHint: 0,
    focus: null,
  }
  let resolutionListener: ((resolution: ViewportResolution) => void) | null = null
  let resolutionApplied = false
  let lastWorldPerPixel = -1
  let lastSizeHint = -1
  /** 直近に「止まっている」と記録したカメラ(位置・姿勢・倍率)。**下記の閾値で比べる。** */
  const lastCameraPosition = new THREE.Vector3(NaN, NaN, NaN)
  const lastCameraQuaternion = new THREE.Quaternion(0, 0, 0, 1)
  let lastCameraZoom = -1
  /** カメラが**意味のある幅で**動くたびに増える番号(`computeFocus` の再計算の鍵)。 */
  let cameraEpoch = 0
  let steadySince = 0
  /** 直近に `computeFocus` したときの姿勢と世代(同じなら再走査しない)。 */
  let lastFocusComputeEpoch = -1
  let lastFocusComputeGeneration = -1
  /** face単位の高精度化を使うか。**切れるのは実機検査のためだけ**(既定は入り)。 */
  let faceCullingEnabled = true
  let lastAppliedFocus: Record<string, number[] | null> | null = null
  /**
   * いま見えているものの外接対角[mm]。**毎フレーム測らない** —— `Box3.setFromObject` は
   * 全メッシュを走査するので、中身が変わったとき(`update` / `prune` / `showSketches`)に
   * だけ測り直す。上下限の丸めにしか使わないので、1フレーム古くても害が無い。
   */
  let contentDiagonal = 0

  function refreshContentDiagonal(): void {
    const box = contentBoxForGrid()
    contentDiagonal = box.isEmpty() ? 0 : box.getSize(new THREE.Vector3()).length()
    // **広げるのは即座、縮めない**(`allowShrink: false`) —— ふつうの編集で内容がしきい値
    // 付近で揺れても、グリッドが縮んで点滅することはない(`applyGridScale` のdocstring)。
    applyGridScale(box, false)
  }

  /**
   * カメラが**意味のある幅で**動いたか(位置・姿勢・正投影の倍率)。
   *
   * **一致ではなく閾値で見る。** `OrbitControls` の減衰は指数で近づくので、カメラは
   * いつまでも「完全には」止まらない —— 値の一致を「止まった」の条件にすると
   * `steadySince` が毎フレーム振り出しに戻り、**解像度が二度と採り直されない**
   * (2026-08-30に実測。ホイールを回しても目標偏差が動かなくなった)。
   * 閾値は `worldPerPixel` と同じ流儀で**画面の画素**で決める —— 世界座標のmmで決めると、
   * 20mmの立方体と600mmの筐体で「止まった」の意味が変わる。
   */
  function cameraMoved(): boolean {
    if (!Number.isFinite(lastCameraPosition.x)) return true
    if (camera.position.distanceTo(lastCameraPosition) > worldPerPixel() * CAMERA_STILL_PIXELS) {
      return true
    }
    // 四元数の内積は向きの近さ(1で一致)。符号は向きに影響しないので絶対値で見る。
    if (Math.abs(camera.quaternion.dot(lastCameraQuaternion)) < 1 - CAMERA_STILL_ANGLE) return true
    return Math.abs(camera.zoom - lastCameraZoom) > Math.abs(lastCameraZoom) * RESOLUTION_STILL_EPSILON
  }

  /** 「ここで止まっている」を記録する(次のフレームの `cameraMoved` の基準)。 */
  function rememberCamera(): void {
    lastCameraPosition.copy(camera.position)
    lastCameraQuaternion.copy(camera.quaternion)
    lastCameraZoom = camera.zoom
  }

  function focusEqual(
    a: Record<string, number[] | null> | null,
    b: Record<string, number[] | null> | null,
  ): boolean {
    if (a === b) return true
    if (a === null || b === null) return false
    const keysA = Object.keys(a).sort()
    const keysB = Object.keys(b).sort()
    if (keysA.length !== keysB.length) return false
    for (let i = 0; i < keysA.length; i++) {
      if (keysA[i] !== keysB[i]) return false
      const va = a[keysA[i]]
      const vb = b[keysB[i]]
      if (va === null && vb === null) continue
      if (va === null || vb === null) return false
      if (va.length !== vb.length) return false
      for (let j = 0; j < va.length; j++) {
        if (va[j] !== vb[j]) return false
      }
    }
    return true
  }

  /** 視錐台とバックフェイスから、サーバへ送る `focus` を組み立てる(落ち着いた後だけ呼ぶ)。 */
  function computeFocus(): Record<string, number[] | null> | null {
    // 切ってあるときは**何も指定しない** = 全Body全面が細かい側(導入前と同じ)。
    if (!faceCullingEnabled || faceSamples.size === 0) return null
    const frustum = new THREE.Frustum()
    camera.updateMatrixWorld()
    frustum.setFromProjectionMatrix(
      new THREE.Matrix4().multiplyMatrices(camera.projectionMatrix, camera.matrixWorldInverse),
    )
    const viewDirection = camera.getWorldDirection(new THREE.Vector3())
    const viewpoint = (camera as THREE.PerspectiveCamera).isPerspectiveCamera
      ? camera.getWorldPosition(new THREE.Vector3())
      : null
    const hysteresis = worldPerPixel() * FOCUS_HYSTERESIS_PIXELS
    const result: Record<string, number[] | null> = {}
    let anySpecified = false
    for (const [bodyId, samples] of faceSamples) {
      const selected = selectFineFaces(samples, frustum, viewpoint, viewDirection, hysteresis)
      if (selected !== null) {
        result[bodyId] = selected
        anySpecified = true
      }
    }
    return anySpecified ? result : null
  }

  /**
   * カメラが落ち着いていれば解像度を採り直す(毎フレーム `tick` から呼ばれる)。
   *
   * **門**はズームに加え**姿勢**も見る —— 寄りだけでは `worldPerPixel` が動かず、
   * 視錐台が変わっても気づけない。落ち着いたあと `computeFocus` は2835面を走査するので、
   * 姿勢と `focusGeneration` が同じなら再走査しない。
   *
   * **`moved` の分岐に `updateClipPlanes()` を相乗りさせてある**(2026-08-30)。near/farは
   * カメラ距離が変わるたびに引き直すべきだが、毎フレーム呼ぶ必要は無い —— この分岐は
   * 既存の画素基準の閾値判定(`cameraMoved()`)で門が閉じているので、オービット/パン/ドリーの
   * 減衰が画素以下に収まった時点で自然に呼ばれなくなる(動いている間は毎フレーム呼ばれるが、
   * `updateProjectionMatrix()` は毎フレームでも安い)。
   */
  function updateResolution(now: number): void {
    const perPixel = worldPerPixel()
    const moved =
      lastWorldPerPixel < 0 ||
      Math.abs(perPixel - lastWorldPerPixel) > lastWorldPerPixel * RESOLUTION_STILL_EPSILON ||
      contentDiagonal !== lastSizeHint ||
      cameraMoved()
    if (moved) {
      lastWorldPerPixel = perPixel
      lastSizeHint = contentDiagonal
      rememberCamera()
      cameraEpoch += 1
      steadySince = now
      updateClipPlanes()
      return
    }
    if (now - steadySince < RESOLUTION_SETTLE_MS) return
    // **同じカメラ・同じ形なら判定もやり直さない**(`computeFocus` は2835面を走査する)。
    if (cameraEpoch === lastFocusComputeEpoch && focusGeneration === lastFocusComputeGeneration) {
      return
    }

    const focus = computeFocus()
    lastFocusComputeEpoch = cameraEpoch
    lastFocusComputeGeneration = focusGeneration

    const target = viewResolution(perPixel, contentDiagonal)
    const ratio = target.deviation / appliedResolution.deviation
    const focusChanged = !focusEqual(focus, lastAppliedFocus)
    if (
      resolutionApplied &&
      ratio < RESOLUTION_CHANGE_RATIO &&
      ratio > 1 / RESOLUTION_CHANGE_RATIO &&
      !focusChanged
    ) {
      return
    }
    resolutionApplied = true
    lastAppliedFocus = focus
    appliedResolution = { ...target, worldPerPixel: perPixel, sizeHint: contentDiagonal, focus }
    // **スケッチはここで描き直す**(ブラウザ内で完結する経路。サーバ往復が要らない)。
    redrawSketches()
    resolutionListener?.(appliedResolution)
  }

  function edgeAt(offsetX: number, offsetY: number): EdgeRef | null {
    if (edgeLines.length === 0) return null
    const { width, height } = size()
    const ndc = new THREE.Vector2((offsetX / width) * 2 - 1, -(offsetY / height) * 2 + 1)
    raycaster.setFromCamera(ndc, camera)
    // 線の当たり判定の太さは**世界座標**で指定する。画素で決めないと、寄れば寄るほど
    // 掴みにくくなる(20mmの立方体と2835面の筐体で同じ操作感にならない)。
    const previous = raycaster.params.Line?.threshold
    raycaster.params.Line = { threshold: worldPerPixel() * EDGE_PICK_PIXELS }
    const hits = raycaster.intersectObjects(edgeLines, false)
    if (previous !== undefined) raycaster.params.Line = { threshold: previous }
    if (hits.length === 0) return null
    const bodyId = hits[0].object.userData.bodyId
    const edgeIndex = hits[0].object.userData.edgeIndex
    if (typeof bodyId !== 'string' || typeof edgeIndex !== 'number') return null
    return { bodyId, edgeIndex }
  }

  /** 折れ線の道のりの `fraction` の位置(0=始点 1=終点)。 */
  function pointAlong(line: THREE.Line, fraction: number): THREE.Vector3 | null {
    const position = line.geometry.getAttribute('position')
    if (!position || position.count < 2) return null
    const at = fraction * (position.count - 1)
    const first = Math.min(Math.floor(at), position.count - 2)
    const t = at - first
    const a = new THREE.Vector3(position.getX(first), position.getY(first), position.getZ(first))
    const b = new THREE.Vector3(
      position.getX(first + 1),
      position.getY(first + 1),
      position.getZ(first + 1),
    )
    return a.lerp(b, t)
  }

  function projectEdge(bodyId: string, edgeIndex: number): { x: number; y: number } | null {
    if (edgeBodyId !== bodyId) return null
    const line = edgeLines.find((candidate) => candidate.userData.edgeIndex === edgeIndex)
    if (!line) return null
    // **端点は避ける**(隣の辺と共有しているので、そこを狙うと隣が取れることがある)。
    // 中ほどから順に試し、**実際にその辺へ当たる画素**だけを返す(面の `project` と同じ規律)。
    for (const fraction of [0.5, 0.35, 0.65, 0.2, 0.8, 0.1, 0.9]) {
      const point = pointAlong(line, fraction)
      if (point === null) continue
      const screen = toScreen(point)
      if (screen === null) continue
      const hit = edgeAt(screen.x, screen.y)
      if (hit && hit.bodyId === bodyId && hit.edgeIndex === edgeIndex) return screen
    }
    return null
  }

  // ------------------------------------------------------------------ 頂点(選択中のものだけ)
  //
  // **辺と違い、候補を全部出すことはしない。** 頂点を引数に取るverbが無いので
  // 「候補を見せて選ばせる」場面が無く、要るのは「AIが選んだ頂点が今どれか」を人が見て
  // 外せることだけである(`duocad/server.py` の `Session.vertices` と同じ理由)。
  //
  // **点(`THREE.Points`)で描き、大きさは画素で固定する**(`sizeAttenuation: false`)。
  // 世界座標の球にすると、描いた時のカメラ距離で大きさが決まってしまい、**寄れば巨大な玉、
  // 引けば1画素未満**になる —— 頂点マーカーは選択中の頂点を見る唯一の手段なので、
  // 27部品を引きで見ているときに消えるのは機能が無いのと同じである。
  const vertexRoot = new THREE.Group()
  scene.add(vertexRoot)

  const vertexMaterial = new THREE.PointsMaterial({
    color: HIGHLIGHT_COLOR,
    size: VERTEX_PIXELS * 2,
    sizeAttenuation: false,
    // 辺と同じく**深度検査をしない**(奥のマーカーも見え、奥のマーカーも指せる)。
    // 見た目だけ隠すと「見えない頂点が外れる」になる。
    depthTest: false,
  })
  const vertexProposedMaterial = new THREE.PointsMaterial({
    color: PROPOSED_COLOR,
    size: VERTEX_PIXELS * 2,
    sizeAttenuation: false,
    depthTest: false,
  })

  /** 今出している頂点マーカー。空なら選択に頂点が入っていない。 */
  let vertexMeshes: THREE.Points[] = []
  const vertexDisposables: Array<{ dispose(): void }> = []
  /** 直前に描いた印(**同じものを渡されたら作り直さない**ため)。 */
  let vertexKey = ''

  function clearVertices(): void {
    for (const mesh of vertexMeshes) vertexRoot.remove(mesh)
    for (const item of vertexDisposables) item.dispose()
    vertexDisposables.length = 0
    vertexMeshes = []
    vertexKey = ''
  }

  function showVertices(markers: VertexMarker[], origin: 'user' | 'proposed'): void {
    const key = `${origin}|${markers
      .map((marker) => `${marker.bodyId}|${marker.vertexIndex}|${marker.point.join(',')}`)
      .join(';')}`
    // 呼び出し側(App.svelte)は選択が変わるたびに呼び直すので、毎回作り直すと
    // ジオメトリを捨てて作り直し続けることになる(`showEdges` と同じ番人)。
    if (key === vertexKey) return
    clearVertices()
    vertexKey = key
    pickChanged() // 頂点マーカーは面より先に取られるので、増減で「指せるもの」が変わる
    const material = origin === 'proposed' ? vertexProposedMaterial : vertexMaterial
    highlightColor.vertex = markers.length === 0 ? null : material.color.getHex()
    if (markers.length === 0) return
    for (const marker of markers) {
      if (marker.point.length < 3) continue
      // **1マーカー1オブジェクト**(辺が1本1 `THREE.Line` なのと同じ) —— まとめると
      // 当たった点からコアの添字へ戻れない。数は選択中のものだけなので費用は無視できる。
      const geometry = new THREE.BufferGeometry()
      geometry.setAttribute(
        'position',
        new THREE.BufferAttribute(new Float32Array(marker.point.slice(0, 3)), 3),
      )
      geometry.computeBoundingSphere()
      const mesh = new THREE.Points(geometry, material)
      mesh.renderOrder = LAYER.region // 深度を書かないぶん、描く順序で辺より手前に出す
      mesh.userData.bodyId = marker.bodyId
      mesh.userData.vertexIndex = marker.vertexIndex
      mesh.userData.point = marker.point
      vertexRoot.add(mesh)
      vertexMeshes.push(mesh)
      vertexDisposables.push(geometry)
    }
  }

  function vertexAt(offsetX: number, offsetY: number): VertexRef | null {
    if (vertexMeshes.length === 0) return null
    const { width, height } = size()
    const ndc = new THREE.Vector2((offsetX / width) * 2 - 1, -(offsetY / height) * 2 + 1)
    raycaster.setFromCamera(ndc, camera)
    // 点の当たり判定も**画素で決める**(辺の `edgeAt` と同じ理由 —— 寄るほど掴みにくく
    // なるのを避ける)。描いている大きさと揃えておく。
    const previous = raycaster.params.Points?.threshold
    raycaster.params.Points = { threshold: worldPerPixel() * VERTEX_PIXELS }
    const hits = raycaster.intersectObjects(vertexMeshes, false)
    if (previous !== undefined) raycaster.params.Points = { threshold: previous }
    if (hits.length === 0) return null
    const bodyId = hits[0].object.userData.bodyId
    const vertexIndex = hits[0].object.userData.vertexIndex
    if (typeof bodyId !== 'string' || typeof vertexIndex !== 'number') return null
    return { bodyId, vertexIndex }
  }

  function projectVertex(bodyId: string, vertexIndex: number): { x: number; y: number } | null {
    const mesh = vertexMeshes.find(
      (candidate) =>
        candidate.userData.bodyId === bodyId && candidate.userData.vertexIndex === vertexIndex,
    )
    if (!mesh) return null
    const point = mesh.userData.point as number[]
    const screen = toScreen(new THREE.Vector3(point[0], point[1], point[2]))
    if (screen === null) return null
    // **本当にその頂点が取れる画素か**を確かめてから返す(面の `project` と同じ流儀)。
    const hit = vertexAt(screen.x, screen.y)
    if (hit === null || hit.bodyId !== bodyId || hit.vertexIndex !== vertexIndex) return null
    return screen
  }

  // ------------------------------------------------------------------ C: スケッチ
  //
  // **描くのは線と領域だけ**で、スケッチ平面そのもの(無限に広がる面)は描かない ——
  // 面を描くと奥のソリッドが隠れ、「既存の部品の上に描く」が見えなくなる。
  //
  // ソリッドとは別のグループ(`sketchRoot`)に持つ。`update()`/`prune()` はソリッド側の
  // `root` しか触らないので、押し出しでスケッチが消えることがない。
  // **`sketchRoot` の宣言は `root` の隣へ上げてある**(初期化中の `applyGridPlanes()` が
  // 読むため。あちらのコメント参照)。

  const sketchMaterial = new THREE.LineBasicMaterial({ color: SKETCH_COLOR })
  const sketchGhostMaterial = new THREE.LineBasicMaterial({ color: SKETCH_GHOST_COLOR })
  // 選択中の図形の線。**線の材質を作り分けるだけ**で、形の作り方は未選択と同じ
  // (`buildEntityLine` に材質を渡す形になっているのはこのため)。
  const sketchSelectedMaterial = new THREE.LineBasicMaterial({ color: SKETCH_SELECTED_COLOR })
  // 点マーカー。**深度を書かない**(`vertexMaterial` と同じ) —— 線の上に必ず出す。
  const sketchHandleMaterial = new THREE.PointsMaterial({
    color: SKETCH_HANDLE_COLOR,
    size: SKETCH_HANDLE_PIXELS * 2,
    sizeAttenuation: false,
    depthTest: false,
  })
  const sketchHandleSelectedMaterial = new THREE.PointsMaterial({
    color: SKETCH_SELECTED_COLOR,
    size: SKETCH_HANDLE_PIXELS * 2,
    sizeAttenuation: false,
    depthTest: false,
  })
  const regionMaterial = new THREE.MeshBasicMaterial({
    color: REGION_COLOR,
    side: THREE.DoubleSide,
    transparent: true,
    opacity: 0.35,
    depthWrite: false,
  })
  const regionSelectedMaterial = new THREE.MeshBasicMaterial({
    color: REGION_SELECTED_COLOR,
    side: THREE.DoubleSide,
    transparent: true,
    opacity: 0.55,
    depthWrite: false,
  })
  // ホバー中の領域。**3Dに領域の色を新しく作らない**(2026-08-27本人決定) ——
  // 面・辺と同じで、既にここにある色に明度を足すだけ。
  const regionHoverMaterial = new THREE.MeshBasicMaterial({
    color: lighten(REGION_COLOR, HOVER_LIGHTEN),
    side: THREE.DoubleSide,
    transparent: true,
    opacity: 0.35,
    depthWrite: false,
  })
  const regionSelectedHoverMaterial = new THREE.MeshBasicMaterial({
    color: lighten(REGION_SELECTED_COLOR, HOVER_LIGHTEN),
    side: THREE.DoubleSide,
    transparent: true,
    opacity: 0.55,
    depthWrite: false,
  })

  interface SketchScene {
    frame: SketchDraw['frame']
    plane: SketchDraw['plane']
    objects: THREE.Object3D[]
    disposables: Array<{ dispose(): void }>
    /** 領域のメッシュ(ピック対象)。 */
    regionMeshes: THREE.Mesh[]
    /** 図形の (u,v) の折れ線(視点合わせの中心を出すのに使う)。 */
    points: Array<[number, number]>
    /** 図形1本ごとの折れ線の点数(実機検査の `sketchPolylineCounts` が読む)。 */
    polylineCounts: number[]
    /**
     * 点のマーカー(**1点1オブジェクト**)。当たった点からコアの参照へ戻すために
     * `userData` に `entityId` と `point`(xキー)を持つ ——
     * まとめて1つの `Points` にすると戻れない(頂点マーカーと同じ理由)。
     */
    handleMeshes: THREE.Points[]
    /** 図形の折れ線(**元の図形だけ**。`userData.entityId` を持つ)。辺のピック対象。 */
    handleLines: THREE.Line[]
  }
  const sketches = new Map<string, SketchScene>()
  let ghost: { objects: THREE.Object3D[]; disposables: Array<{ dispose(): void }> } | null = null
  /** いま描けているスケッチの幽霊の中身(`ghostInfo` が返す。実機検査用の穴)。 */
  let sketchGhostInfo: Array<{ kind: string; params: Record<string, number> }> = []
  let activeSketchId: string | null = null
  /**
   * 通常モードで領域を指せるか(`pickRegions`)。**スケッチモード中は関係ない** ——
   * モード中は常に「そのスケッチの領域」が対象で、このゲートは見ない。
   */
  let regionPickWanted = false

  /** スケッチ平面上の (u,v) を3Dの点へ(コアの `kernel._to_pnt` と同じ規則)。 */
  function toWorld(frame: SketchDraw['frame'], u: number, v: number): THREE.Vector3 {
    return new THREE.Vector3(
      frame.origin[0] + u * frame.u[0] + v * frame.v[0],
      frame.origin[1] + u * frame.u[1] + v * frame.v[1],
      frame.origin[2] + u * frame.u[2] + v * frame.v[2],
    )
  }

  function buildEntityLine(
    frame: SketchDraw['frame'],
    entity: { kind: string; params: Record<string, number> },
    material: THREE.LineBasicMaterial,
  ): { object: THREE.Object3D; geometry: THREE.BufferGeometry; points: Array<[number, number]> } | null {
    // **今の画面の粗さで折る**(`appliedResolution`)。固定分割だった頃は、寄ると
    // 円が多角形に見え、引くと使われない点をGPUへ送っていた。
    const points = entityPoints(entity.kind, entity.params, appliedResolution)
    if (points.length < 2) return null
    const geometry = new THREE.BufferGeometry().setFromPoints(
      points.map(([u, v]) => toWorld(frame, u, v)),
    )
    return { object: new THREE.Line(geometry, material), geometry, points }
  }

  function clearSketch(sketchId: string): void {
    const existing = sketches.get(sketchId)
    if (!existing) return
    for (const object of existing.objects) sketchRoot.remove(object)
    for (const item of existing.disposables) item.dispose()
    sketches.delete(sketchId)
  }

  /**
   * 直近に受け取った描画指示。**解像度が変わったときに折り直すために控える**
   * (折れ線はここで作るので、指示そのものを持っていないと粗さだけを変えられない)。
   */
  let lastSketchDraws: SketchDraw[] = []

  /** 控えた指示で描き直す(`updateResolution` が呼ぶ)。中身が無ければ何もしない。 */
  function redrawSketches(): void {
    if (lastSketchDraws.length > 0) showSketches(lastSketchDraws)
  }

  function showSketches(entries: SketchDraw[]): void {
    lastSketchDraws = entries
    const keep = new Set(entries.map((entry) => entry.sketchId))
    for (const sketchId of [...sketches.keys()]) if (!keep.has(sketchId)) clearSketch(sketchId)
    for (const entry of entries) {
      clearSketch(entry.sketchId)
      const objects: THREE.Object3D[] = []
      const disposables: Array<{ dispose(): void }> = []
      const regionMeshes: THREE.Mesh[] = []
      const points: Array<[number, number]> = []
      const polylineCounts: number[] = []
      const handleMeshes: THREE.Points[] = []
      const handleLines: THREE.Line[] = []
      // 選ばれている図形・点の索き(`entityId` -> 選ばれた点のxキーの集合。図形全体は `null`)。
      const pickedWhole = new Set(
        entry.selectedHandles.filter((item) => item.point === null).map((item) => item.entityId),
      )
      const pickedPoints = new Set(
        entry.selectedHandles
          .filter((item) => item.point !== null)
          .map((item) => `${item.entityId}|${item.point}`),
      )

      // 領域を先に置く(線が領域の上に見えるように。奥行きは書かないので順序で決まる)
      const selected = new Set(entry.selectedRegions)
      for (const region of entry.regions) {
        if (region.indices.length === 0) continue
        const geometry = new THREE.BufferGeometry()
        geometry.setAttribute(
          'position',
          new THREE.BufferAttribute(new Float32Array(region.positions), 3),
        )
        geometry.setIndex(new THREE.BufferAttribute(new Uint32Array(region.indices), 1))
        geometry.computeBoundingSphere()
        const mesh = new THREE.Mesh(
          geometry,
          selected.has(region.index) ? regionSelectedMaterial : regionMaterial,
        )
        mesh.userData.sketchId = entry.sketchId
        mesh.userData.regionIndex = region.index
        // ホバーとの組み合わせを後から決められるように、選択かどうかを印として残す
        // (材質から読み戻すと、ホバー色が付いた瞬間に選択が分からなくなる)。
        mesh.userData.selected = selected.has(region.index)
        sketchRoot.add(mesh)
        objects.push(mesh)
        regionMeshes.push(mesh)
        disposables.push(geometry)
      }

      for (const entity of entry.entities) {
        const built = buildEntityLine(entry.frame, entity, sketchMaterial)
        if (!built) continue
        sketchRoot.add(built.object)
        objects.push(built.object)
        disposables.push(built.geometry)
        points.push(...built.points)
        polylineCounts.push(built.points.length)
      }

      // **元の図形(`handles`)を、展開した描画の上に重ねて置く。** ここに置くのは
      // 「指せるもの」であって「見えるもの」ではない —— 反復で増えた複製は `entities`
      // 側だけに出て、指す対象にはならない(複製は導出物で `entity_id` を持たない)。
      for (const handle of entry.handles) {
        const selected = pickedWhole.has(handle.entityId)
        const built = buildEntityLine(
          entry.frame,
          handle,
          selected ? sketchSelectedMaterial : sketchMaterial,
        )
        if (built) {
          built.object.renderOrder = LAYER.sketchLine
          built.object.userData.entityId = handle.entityId
          sketchRoot.add(built.object)
          objects.push(built.object)
          disposables.push(built.geometry)
          handleLines.push(built.object as THREE.Line)
        }
        for (const point of entityHandles(handle.kind, handle.params)) {
          if (!Number.isFinite(point.u) || !Number.isFinite(point.v)) continue
          const world = toWorld(entry.frame, point.u, point.v)
          const geometry = new THREE.BufferGeometry()
          geometry.setAttribute(
            'position',
            new THREE.BufferAttribute(new Float32Array([world.x, world.y, world.z]), 3),
          )
          geometry.computeBoundingSphere()
          const picked = selected || pickedPoints.has(`${handle.entityId}|${point.key}`)
          const mesh = new THREE.Points(
            geometry,
            picked ? sketchHandleSelectedMaterial : sketchHandleMaterial,
          )
          mesh.renderOrder = LAYER.sketchPoint // 深度を書かないので、描く順序で線より手前に出す
          mesh.userData.entityId = handle.entityId
          mesh.userData.point = point.key
          sketchRoot.add(mesh)
          objects.push(mesh)
          handleMeshes.push(mesh)
          disposables.push(geometry)
        }
      }
      sketches.set(entry.sketchId, {
        frame: entry.frame,
        plane: entry.plane,
        objects,
        disposables,
        regionMeshes,
        points,
        polylineCounts,
        handleMeshes,
        handleLines,
      })
    }
    invalidateHover() // 領域のメッシュを作り直したので印を捨てる
    refreshContentDiagonal() // 見えているものの大きさが変わった(解像度の上下限に効く)
    updateClipPlanes() // 内容が変わった → 視錐台を引き直す
    refreshGridSketchFrame() // 編集中スケッチのframeがここで初めて届くことがある
  }

  /** 領域の材質を決める。**選択とホバーの両方を見る唯一の場所**(面・辺と同じ規律)。 */
  function applyRegionMaterials(): void {
    for (const [sketchId, entry] of sketches) {
      for (const mesh of entry.regionMeshes) {
        const regionIndex = mesh.userData.regionIndex as number
        const isHit = mesh.userData.selected === true
        const isHovered =
          hoverRegion !== null &&
          hoverRegion.sketchId === sketchId &&
          hoverRegion.index === regionIndex
        mesh.material = isHit
          ? isHovered
            ? regionSelectedHoverMaterial
            : regionSelectedMaterial
          : isHovered
            ? regionHoverMaterial
            : regionMaterial
      }
    }
  }

  /**
   * いま編集中のスケッチ平面のframeをグリッドへ渡す(モード外は null)。
   *
   * **`setActiveSketch` と `showSketches` の両方から呼ぶ** —— モードに入った時点では
   * そのスケッチの描画指示がまだ届いておらず、frameを知らないことがある。届いた時点で
   * 呼び直さないと、基準平面へのスケッチ中もXY床が出たままになる。
   */
  function refreshGridSketchFrame(): void {
    const target = activeSketchId === null ? undefined : sketches.get(activeSketchId)
    const frame = target?.frame ?? null
    const isFacePlane = target !== undefined && target.plane.kind === 'face'
    const bounds =
      frame !== null && target !== undefined && target.plane.kind === 'face'
        ? faceUvBounds(target.plane.bodyId, target.plane.faceIndex, frame)
        : null
    setGridSketchFrame(frame, bounds, isFacePlane)
  }

  function clearGhost(): void {
    sketchGhostInfo = []
    if (!ghost) return
    for (const object of ghost.objects) sketchRoot.remove(object)
    for (const item of ghost.disposables) item.dispose()
    ghost = null
  }

  function showSketchGhosts(
    sketchId: string,
    entities: Array<{ kind: string; params: Record<string, number> }>,
  ): void {
    clearGhost()
    const target = sketches.get(sketchId)
    if (entities.length === 0 || !target) return
    const objects: THREE.Object3D[] = []
    const disposables: Array<{ dispose(): void }> = []
    for (const entity of entities) {
      const built = buildEntityLine(target.frame, entity, sketchGhostMaterial)
      if (!built) continue
      sketchRoot.add(built.object)
      objects.push(built.object)
      disposables.push(built.geometry)
      // **実際に描けたものだけ**を控える(`ghostInfo`)。折れ線化できなかった図形を
      // 数に入れると、実機検査が「出ている」と判断したまま画面には何も無い状態になる。
      sketchGhostInfo.push({ kind: entity.kind, params: { ...entity.params } })
    }
    if (objects.length === 0) return
    ghost = { objects, disposables }
  }

  function showSketchGhost(
    sketchId: string,
    entity: { kind: string; params: Record<string, number> } | null,
  ): void {
    showSketchGhosts(sketchId, entity ? [entity] : [])
  }

  /**
   * スケッチに入る直前の視点(抜けるときに戻す)。**モード外では null**。
   *
   * `lookAtSketch` は描いている図形の大きさに寄るので、半径4mmの円を描いて抜けると
   * **40mmの板が画面の外に残る**。視点を全体へ戻すボタンは `7363c76`(下段バーの削除)で
   * 落としてあり、人に残る手は手動のオービット/ズームしかない —— **抜けたら入る前へ戻す**。
   * 「全体が入る視点」ではなく「入る前の視点」なのは、スケッチに入る前に自分で寄せていた
   * 視点まで毎回捨てないため(本人裁定・2026-08-26)。
   *
   * `framedAtEntry` は入った時点で**もう何か描かれていたか**を覚える。**最初の板は
   * スケッチの中で生まれる**ので(`update` の初回 `frameToContent`)、そのときの「入る前」は
   * 何も無い文書の視点であり、戻すと生まれたばかりの形から離れる。その1回だけは戻さない。
   */
  let cameraBeforeSketch:
    | {
        position: THREE.Vector3
        target: THREE.Vector3
        up: THREE.Vector3
        /**
         * 入る前の見かけの大きさ(`viewHalfHeight`)。**位置だけでは戻せない** ——
         * 正投影では寄り引きが視錐台の大きさであってカメラの位置ではないので、
         * 位置を戻しても `lookAtSketch` がスケッチに合わせた倍率のまま残る。
         */
        halfHeight: number
        framedAtEntry: boolean
      }
    | null = null

  function setActiveSketch(sketchId: string | null): void {
    const wasActive = activeSketchId !== null
    if (activeSketchId !== sketchId) pickChanged() // モードの出入りで優先順位ごと変わる
    activeSketchId = sketchId
    refreshGridSketchFrame() // 出すグリッドの平面がモードの出入りで変わる
    if (sketchId !== null) {
      // **入る「とき」だけ控える**。`lookAtSketch` の側で控えないのは、あれが
      // 「正面から見る」でも呼ばれるから —— そこで控えると戻り先が寄った視点に化ける。
      // モード中に別のスケッチへ移ったときも、戻り先は最初に入る前のままにする。
      if (!wasActive) {
        cameraBeforeSketch = {
          position: camera.position.clone(),
          target: controls.target.clone(),
          up: camera.up.clone(),
          halfHeight: viewHalfHeight(),
          framedAtEntry: framed,
        }
      }
      return
    }
    clearGhost()
    // 上方向を既定へ戻す。`lookAtSketch` が平面のv軸へ倒したままだと、モードを抜けた後の
    // オービットが入る前と違う傾きになる。
    camera.up.copy(WORLD_UP)
    const previous = cameraBeforeSketch
    cameraBeforeSketch = null
    if (previous !== null && previous.framedAtEntry) {
      camera.position.copy(previous.position)
      camera.up.copy(previous.up)
      controls.target.copy(previous.target)
      // **見かけの大きさを戻すのは正投影のときだけ。** 正投影では寄り引きがカメラの位置
      // ではないので、位置を戻しても `lookAtSketch` が作った倍率が残る。透視は上の位置の
      // 復元で戻り終わっているので、ここを通しても同じ値を計算し直すだけである。
      if (camera === orthographic) setViewHalfHeight(previous.halfHeight)
      camera.updateProjectionMatrix()
    }
    controls.update()
  }

  /** スケッチ平面と視線の交点を (u,v) で返す(平面がカメラの裏なら null)。 */
  function planePointAt(offsetX: number, offsetY: number): { u: number; v: number } | null {
    if (activeSketchId === null) return null
    const target = sketches.get(activeSketchId)
    if (!target) return null
    const { width, height } = size()
    const ndc = new THREE.Vector2((offsetX / width) * 2 - 1, -(offsetY / height) * 2 + 1)
    raycaster.setFromCamera(ndc, camera)
    const frame = target.frame
    const origin = new THREE.Vector3(...(frame.origin as [number, number, number]))
    const normal = new THREE.Vector3(...(frame.normal as [number, number, number])).normalize()
    const plane = new THREE.Plane().setFromNormalAndCoplanarPoint(normal, origin)
    const hit = raycaster.ray.intersectPlane(plane, new THREE.Vector3())
    if (hit === null) return null
    const delta = hit.sub(origin)
    return {
      u: delta.dot(new THREE.Vector3(...(frame.u as [number, number, number]))),
      v: delta.dot(new THREE.Vector3(...(frame.v as [number, number, number]))),
    }
  }

  /**
   * その画素で当たるスケッチの図形(**アクティブなスケッチだけ**)。
   *
   * **点が先・辺が後**。点は必ず辺の上に乗っているので、辺を先に見ると端点を掴めない
   * (頂点マーカーを辺より先に見るのと同じ理由)。当たり判定の太さは画素で決める
   * (`edgeAt` / `vertexAt` と同じ —— 寄るほど掴みにくくなるのを避ける)。
   */
  function sketchEntityAt(offsetX: number, offsetY: number): SketchEntityHit | null {
    if (activeSketchId === null) return null
    const target = sketches.get(activeSketchId)
    if (!target) return null
    const { width, height } = size()
    const ndc = new THREE.Vector2((offsetX / width) * 2 - 1, -(offsetY / height) * 2 + 1)
    raycaster.setFromCamera(ndc, camera)

    if (target.handleMeshes.length > 0) {
      const previous = raycaster.params.Points?.threshold
      raycaster.params.Points = { threshold: worldPerPixel() * SKETCH_HANDLE_PIXELS }
      const hits = raycaster.intersectObjects(target.handleMeshes, false)
      if (previous !== undefined) raycaster.params.Points = { threshold: previous }
      if (hits.length > 0) {
        const { entityId, point } = hits[0].object.userData
        if (typeof entityId === 'string' && typeof point === 'string') {
          return { sketchId: activeSketchId, entityId, point }
        }
      }
    }

    if (target.handleLines.length > 0) {
      const previous = raycaster.params.Line?.threshold
      raycaster.params.Line = { threshold: worldPerPixel() * EDGE_PICK_PIXELS }
      const hits = raycaster.intersectObjects(target.handleLines, false)
      if (previous !== undefined) raycaster.params.Line = { threshold: previous }
      if (hits.length > 0) {
        const { entityId } = hits[0].object.userData
        if (typeof entityId === 'string') {
          return { sketchId: activeSketchId, entityId, point: null }
        }
      }
    }
    return null
  }

  function pickRegions(on: boolean): void {
    if (regionPickWanted === on) return
    regionPickWanted = on
    pickChanged() // ゲートの開閉で「指せるもの」が変わる(止まったままでも解決し直す)
  }

  /**
   * 今クリックで当たってよい領域メッシュ。
   *
   * - **スケッチモード中**: そのスケッチの領域だけ(描いている最中に別のスケッチの
   *   領域を掴まない)。ゲートは見ない —— モードに入っている時点で領域が対象である。
   * - **通常モード**: `pickRegions(true)` の間だけ、**全スケッチの領域**。
   *   出していない間は空配列なので、面のピックはそのまま動く(辺の `edgeAt` と同じ形)。
   */
  function regionPickTargets(): THREE.Mesh[] {
    if (activeSketchId !== null) return sketches.get(activeSketchId)?.regionMeshes ?? []
    if (!regionPickWanted) return []
    const meshes: THREE.Mesh[] = []
    for (const target of sketches.values()) meshes.push(...target.regionMeshes)
    return meshes
  }

  function regionAt(offsetX: number, offsetY: number): { sketchId: string; index: number } | null {
    const targets = regionPickTargets()
    if (targets.length === 0) return null
    const { width, height } = size()
    const ndc = new THREE.Vector2((offsetX / width) * 2 - 1, -(offsetY / height) * 2 + 1)
    raycaster.setFromCamera(ndc, camera)
    // **近い方が勝つ**(`intersectObjects` は距離順)。複数スケッチの領域が重なっても
    // 手前のものが取れる。
    const hits = raycaster.intersectObjects(targets, false)
    if (hits.length === 0) return null
    const { sketchId, regionIndex } = hits[0].object.userData
    if (typeof sketchId !== 'string' || typeof regionIndex !== 'number') return null
    return { sketchId, index: regionIndex }
  }

  function lookAtSketch(sketchId: string): void {
    const target = sketches.get(sketchId)
    if (!target) return
    const frame = target.frame
    const normal = new THREE.Vector3(...(frame.normal as [number, number, number])).normalize()
    // 中心は描いてあるものの真ん中(何も無ければ平面の原点)
    let center = toWorld(frame, 0, 0)
    let radius = 0
    if (target.points.length > 0) {
      const us = target.points.map(([u]) => u)
      const vs = target.points.map(([, v]) => v)
      const midU = (Math.min(...us) + Math.max(...us)) / 2
      const midV = (Math.min(...vs) + Math.max(...vs)) / 2
      center = toWorld(frame, midU, midV)
      radius = Math.max(Math.max(...us) - Math.min(...us), Math.max(...vs) - Math.min(...vs)) / 2
    }
    const previous = camera.position.distanceTo(controls.target)
    const distance = radius > 0 ? Math.max(fitDistance(radius * SKETCH_FIT_MARGIN), 1) : Math.max(previous, 1)
    // 上方向はビューキューブと同じ規則(`upForDirection`)で決める。ほとんどの平面では
    // WORLD_UPのまま変わらない —— `OrbitControls`は生成時に焼いたZ軸を回転の極として
    // 使い続ける(`WORLD_UP`の説明参照)ので、camera.upをそこから逸らすとキューブの見た目
    // (`cubeActive.up`がcamera.upをそのまま写す)と実際の回転軸がずれ、スケッチ中だけ
    // ビューキューブの動きがおかしくなっていた。`frame.v`を直接使っていたのが原因
    // (基準平面のXZ/YZはたまたまv=Zで無害だったが、任意面やXY平面ではvがZからずれる)。
    // 真上・真下に近い面(cube側の上面図/底面図と同じ閾値)だけ±Yへ差し替わる。
    camera.up.set(...upForDirection([normal.x, normal.y, normal.z]))
    camera.position.copy(center).add(normal.multiplyScalar(distance))
    controls.target.copy(center)
    // 正投影は距離では寄れない(`frameToContent` と同じ理由)。描いてあるものが無いときは
    // 今の見かけの大きさのまま —— 透視側が `previous` の距離を保つのと同じ扱いにする。
    if (camera === orthographic && radius > 0) setViewHalfHeight(fitHalfHeight(radius * SKETCH_FIT_MARGIN))
    camera.updateProjectionMatrix()
    controls.update()
  }

  /**
   * その画素が**本当に押せるか**(canvasの上に浮いているDOMに覆われていないか)。
   *
   * 3Dのレイ検査(`pickAt` / `faceHitAt` / `basePlaneAt`)は**canvasの中しか見ない**ので、
   * ビューポートに浮かせたパネル —— `BrowserPanel.svelte` のブラウザ —— に覆われた画素も
   * 「取れる」と答えてしまう。実際にそこを押すとクリックはパネルに吸われ、**指したはずの
   * ものが指されない**。`project*` は「その画素が本当にそれを取れる」ことを確かめてから
   * 返す約束なので、覆われた画素は候補から落とす(呼び出し側の探索が次の候補へ進む)。
   *
   * `Toast.svelte` の札が引っかからないのは、あちらが `pointer-events: none` で
   * クリックを吸わないから —— 覆っているかどうかではなく、**吸うかどうか**が判定基準であり、
   * それを直接答えるのが `elementFromPoint` である。
   */
  function clickable(x: number, y: number): boolean {
    // **キューブは同一canvasの第2パスなので `elementFromPoint` だけでは判定できない**
    // ——常にcanvasを返し続けるので、ここで明示的に矩形を除外する(タスク仕様の落とし穴1)。
    if (cube.inside(x, y)) return false
    const rect = container.getBoundingClientRect()
    return document.elementFromPoint(rect.left + x, rect.top + y) === renderer.domElement
  }

  function toScreen(point: THREE.Vector3): { x: number; y: number } | null {
    const { width, height } = size()
    const projected = point.clone().project(camera)
    if (projected.z < -1 || projected.z > 1) return null
    const x = ((projected.x + 1) / 2) * width
    const y = ((1 - projected.y) / 2) * height
    if (x < 0 || y < 0 || x > width || y > height) return null
    if (!clickable(x, y)) return null
    return { x, y }
  }

  function projectSketchPoint(sketchId: string, u: number, v: number): { x: number; y: number } | null {
    const target = sketches.get(sketchId)
    if (!target) return null
    return toScreen(toWorld(target.frame, u, v))
  }

  function projectRegion(sketchId: string, index: number): { x: number; y: number } | null {
    const target = sketches.get(sketchId)
    const mesh = target?.regionMeshes.find((candidate) => candidate.userData.regionIndex === index)
    if (!target || !mesh) return null
    const position = mesh.geometry.getAttribute('position')
    const triangles = mesh.geometry.getIndex()
    if (!position || !triangles) return null
    // 面の `project` と同じ流儀: 三角形の内側の点を狙い、**実際にその領域へ当たる点**を返す。
    //
    // **1つの三角形につき4点試す**(重心 + 各頂点へ寄せた3点)。重心1点だけだと、
    // スケッチモードで**点・辺のほうが手前で拾われる**画素に当たったときに候補が尽きる ——
    // ベジエの制御点は曲線から離れた所(＝領域の内側)にも立つので、細長い三角形の重心が
    // その近くに来ることが実際にある(2026-08-30、`smoke-svg` の丘の領域で踏んだ)。
    const weights: ReadonlyArray<readonly [number, number, number]> = [
      [1 / 3, 1 / 3, 1 / 3],
      [0.5, 0.25, 0.25],
      [0.25, 0.5, 0.25],
      [0.25, 0.25, 0.5],
    ]
    const point = new THREE.Vector3()
    const corner = new THREE.Vector3()
    for (let sample = 0; sample < triangles.count / 3 * weights.length; sample += 1) {
      const triangle = Math.floor(sample / weights.length)
      const weight = weights[sample % weights.length]
      point.set(0, 0, 0)
      for (let vertex = 0; vertex < 3; vertex += 1) {
        const at = triangles.getX(triangle * 3 + vertex)
        corner.set(position.getX(at), position.getY(at), position.getZ(at))
        point.addScaledVector(corner, weight[vertex])
      }
      const screen = toScreen(point.clone())
      if (screen === null) continue
      // **スケッチも一致していること**を見る —— 通常モードでは全スケッチの領域が対象に
      // なるので、添字だけ見ると別のスケッチの同じ添字を「当たった」と読む。
      const hit = regionAt(screen.x, screen.y)
      if (!hit || hit.sketchId !== sketchId || hit.index !== index) continue
      // **スケッチの図形が勝つ画素は返さない**(2026-08-30)。スケッチモード中は点・辺が
      // 領域より手前で拾われるので、`regionAt` だけで確かめると「領域に当たるが、実際に
      // クリックすると線が選ばれる」画素を返しうる —— `project*` の約束(その画素で
      // 本当にそれが取れる)が破れる。判定は実際のクリックと同じ `sketchEntityAt` で行う。
      if (sketchEntityAt(screen.x, screen.y) !== null) continue
      return screen
    }
    return null
  }

  // ------------------------------------------------------------------ ウィンドウ選択(左ドラッグ)
  //
  // **矩形に完全包含されたものだけを選ぶ**(交差判定はしない。本人明言。向きも見ない)。
  // 判定はすべて**画面座標**で行う —— 「囲った」は人が画面で見たとおりの意味であって、
  // 世界座標の箱ではない(奥行きは関係ない)。
  //
  // 何を候補にするかは**クリックのゲートと同じ**にしてある(辺は辺を出している間だけ、
  // 領域は要求している間だけ、スケッチの図形はスケッチモード中だけ)。ここだけ別の規則に
  // すると、「クリックでは指せないものがドラッグでだけ選べる」ができてしまう。
  const bandElement = document.createElement('div')
  bandElement.style.position = 'absolute'
  bandElement.style.border = '1px solid #ffb454'
  bandElement.style.background = 'rgba(255, 180, 84, 0.12)'
  // **クリックを吸わない**。吸うと `clickable()`(elementFromPoint)が canvas を見失い、
  // 射影の穴が「その画素は取れない」と答え始める。
  bandElement.style.pointerEvents = 'none'
  bandElement.style.display = 'none'
  // **大きさには影響しない**(position:absolute なのでフローの外)。親を基準にするために
  // だけ `relative` を入れる —— `.viewport` の寸法指定そのものは動かさない(styles.css の不変条件)。
  if (getComputedStyle(container).position === 'static') container.style.position = 'relative'
  container.appendChild(bandElement)

  /** 矩形を引いている最中か(引いていなければ、離したときの扱いはクリック)。 */
  let boxActive = false

  function boxRect(x: number, y: number): { left: number; top: number; right: number; bottom: number } {
    return {
      left: Math.min(downX, x),
      right: Math.max(downX, x),
      top: Math.min(downY, y),
      bottom: Math.max(downY, y),
    }
  }

  function showBand(x: number, y: number): void {
    const rect = boxRect(x, y)
    bandElement.style.display = 'block'
    bandElement.style.left = `${rect.left}px`
    bandElement.style.top = `${rect.top}px`
    bandElement.style.width = `${rect.right - rect.left}px`
    bandElement.style.height = `${rect.bottom - rect.top}px`
  }

  function hideBand(): void {
    boxActive = false
    bandElement.style.display = 'none'
  }

  const projectScratch = new THREE.Vector3()

  /** 世界座標を**コンテナ内の画素**へ(視錐台の外・カメラの裏は null)。
   *  `width`/`height` は呼び出し元から渡す —— `geometryInside` の頂点ループから
   *  頂点ごとに `size()`(= DOM の `clientWidth`/`clientHeight` 読み取り)を呼ぶと、
   *  矩形選択の実測で射影時間の92%を占めるほど遅い(2026-09-04計測)。
   *  ここでモジュール側にキャッシュしないこと —— リサイズで寸法が変わるため、
   *  キャッシュすると「同じ画素が別のものを指す」落とし穴に直結する
   *  ([[viewport-resize-silently-changes-what-a-click-grabs]])。呼び出し元が
   *  1回の矩形選択の中でだけ `size()` を読んで使い回す。 */
  function projectRaw(x: number, y: number, z: number, width: number, height: number): { x: number; y: number } | null {
    projectScratch.set(x, y, z).project(camera)
    if (projectScratch.z < -1 || projectScratch.z > 1) return null
    return {
      x: ((projectScratch.x + 1) / 2) * width,
      y: ((1 - projectScratch.y) / 2) * height,
    }
  }

  function inside(rect: ReturnType<typeof boxRect>, point: { x: number; y: number }): boolean {
    return point.x >= rect.left && point.x <= rect.right && point.y >= rect.top && point.y <= rect.bottom
  }

  /**
   * そのジオメトリの**全頂点**が矩形の中にあるか。1つでも外れたらその時点で false。
   *
   * **早期に打ち切るのが要点**である。27部品(2835面)の全三角形を毎回最後まで見ると
   * ドラッグを離すたびに数十万点を射影することになるが、実際には矩形の外にある面が
   * ほとんどで、1点目で落ちる。
   */
  function geometryInside(
    geometry: THREE.BufferGeometry,
    matrix: THREE.Matrix4,
    rect: ReturnType<typeof boxRect>,
    width: number,
    height: number,
  ): boolean {
    const position = geometry.getAttribute('position')
    if (!position || position.count === 0) return false
    const world = new THREE.Vector3()
    for (let at = 0; at < position.count; at += 1) {
      world.set(position.getX(at), position.getY(at), position.getZ(at)).applyMatrix4(matrix)
      const screen = projectRaw(world.x, world.y, world.z, width, height)
      if (screen === null || !inside(rect, screen)) return false
    }
    return true
  }

  function objectInside(
    object: THREE.Object3D & { geometry: THREE.BufferGeometry },
    rect: ReturnType<typeof boxRect>,
    width: number,
    height: number,
  ): boolean {
    object.updateWorldMatrix(true, false)
    return geometryInside(object.geometry, object.matrixWorld, rect, width, height)
  }

  function collectInBox(rect: ReturnType<typeof boxRect>): Omit<BoxSelectEvent, 'mode'> {
    const faces: FaceRef[] = []
    const edges: EdgeRef[] = []
    const vertices: VertexRef[] = []
    const regions: Array<{ sketchId: string; index: number }> = []
    const sketchEntities: SketchEntityHit[] = []

    // ビューポートの寸法はこの呼び出し1回の間ずっと不変なので、頂点ごとに読まず
    // ここで1回だけ読んで使い回す(モジュール側にはキャッシュしない —
    // `projectRaw` のコメント参照)。
    const { width, height } = size()

    if (activeSketchId === null) {
      // **面は常に指せる**(クリックと同じ)。隠したBodyのメッシュは `visible=false` なので外す。
      for (const [bodyId, entry] of bodies) {
        for (const mesh of entry.faceMeshes) {
          if (!mesh.visible) continue
          if (!objectInside(mesh as THREE.Mesh & { geometry: THREE.BufferGeometry }, rect, width, height)) continue
          const faceIndex = mesh.userData.faceIndex
          if (typeof faceIndex === 'number') faces.push({ bodyId, faceIndex })
        }
      }
      // 辺・頂点は「出している間だけ」(クリックの `edgeAt` / `vertexAt` と同じゲート)
      for (const line of edgeLines) {
        if (!objectInside(line as THREE.Line & { geometry: THREE.BufferGeometry }, rect, width, height)) continue
        const { bodyId, edgeIndex } = line.userData
        if (typeof bodyId === 'string' && typeof edgeIndex === 'number') edges.push({ bodyId, edgeIndex })
      }
      for (const mesh of vertexMeshes) {
        if (!objectInside(mesh as THREE.Points & { geometry: THREE.BufferGeometry }, rect, width, height)) continue
        const { bodyId, vertexIndex } = mesh.userData
        if (typeof bodyId === 'string' && typeof vertexIndex === 'number') vertices.push({ bodyId, vertexIndex })
      }
    } else {
      // **スケッチモードでは図形だけ**。図形全体が入っていればその図形を、
      // 入っていなければ矩形に入った点だけを返す(点と図形全体の二重取りを作らない)。
      const target = sketches.get(activeSketchId)
      const whole = new Set<string>()
      for (const line of target?.handleLines ?? []) {
        if (!objectInside(line as THREE.Line & { geometry: THREE.BufferGeometry }, rect, width, height)) continue
        const entityId = line.userData.entityId
        if (typeof entityId !== 'string') continue
        whole.add(entityId)
        sketchEntities.push({ sketchId: activeSketchId, entityId, point: null })
      }
      for (const mesh of target?.handleMeshes ?? []) {
        const entityId = mesh.userData.entityId
        const point = mesh.userData.point
        if (typeof entityId !== 'string' || typeof point !== 'string') continue
        if (whole.has(entityId)) continue
        if (!objectInside(mesh as THREE.Points & { geometry: THREE.BufferGeometry }, rect, width, height)) continue
        sketchEntities.push({ sketchId: activeSketchId, entityId, point })
      }
    }

    // 領域は**クリックと同じゲート**(`regionPickTargets`)。スケッチモード中はそのスケッチだけ。
    for (const mesh of regionPickTargets()) {
      if (!objectInside(mesh as THREE.Mesh & { geometry: THREE.BufferGeometry }, rect, width, height)) continue
      const { sketchId, regionIndex } = mesh.userData
      if (typeof sketchId === 'string' && typeof regionIndex === 'number') {
        regions.push({ sketchId, index: regionIndex })
      }
    }
    return { faces, edges, vertices, regions, sketchEntities }
  }

  // クリック(押した位置からほとんど動いていない)だけを選択として扱う。
  // 動いた場合は OrbitControls の視点回転であって選択ではない。
  let downAt = 0
  let downX = 0
  let downY = 0
  let downValid = false

  function onPointerDown(event: PointerEvent): void {
    // 新しいポインタ操作が来たら、進行中のキューブ視点アニメーションは中断してよい(タスク仕様)。
    cube.cancelSnap()
    if (event.button !== 0) return
    // **キューブの内側で始まった左ボタン押下は、キューブが引き取る**(モデルのピック・
    // 矩形選択の開始へ漏らさない。タスク仕様の落とし穴1)。
    if (cube.pointerDown(event)) return
    downAt = performance.now()
    downX = event.offsetX
    downY = event.offsetY
    downValid = true
    hideBand()
  }

  function onPointerUp(event: PointerEvent): void {
    // **ボタンの種類より先に解除する**。右/中ボタンで視点を回して離した場合、ここで
    // 解除しないと次の `pointermove` が来るまでホバーが止まったままになる。
    orbiting = false
    if (cube.pointerUp(event)) return
    if (event.button !== 0 || !downValid) {
      if (event.button === 0) hideBand()
      return
    }
    downValid = false
    const moved = Math.hypot(event.offsetX - downX, event.offsetY - downY)
    const elapsed = performance.now() - downAt
    // **矩形を引いていたならウィンドウ選択**(クリックにはしない)。引いた向きは見ない。
    if (boxActive) {
      const rect = boxRect(event.offsetX, event.offsetY)
      hideBand()
      boxSelectListener?.({ ...collectInBox(rect), mode: modeOf(event) })
      return
    }
    if (moved > CLICK_MOVE_LIMIT_PX || elapsed > CLICK_TIME_LIMIT_MS) return
    pickListener?.({ ...resolveHit(event.offsetX, event.offsetY), mode: modeOf(event) })
  }

  /**
   * 修飾キーから選択の扱いを決める。**クリックもウィンドウ選択も同じ規則**
   * (2箇所に書くと片方だけ直したときに操作が食い違う)。
   *
   * **Shift も Ctrl(Mac は Meta)も同じトグル**(2026-08-30本人決定)。除外専用のキーは
   * 持たない —— 間違えたらもう一度クリック(またはドラッグ)すればよい。
   */
  function modeOf(event: { shiftKey: boolean; ctrlKey: boolean; metaKey: boolean }): PickMode {
    return event.shiftKey || event.ctrlKey || event.metaKey ? 'toggle' : 'replace'
  }

  /**
   * その画素をクリックしたら**何が取れるか**(修飾キー以外の `PickEvent` そのもの)。
   *
   * **クリックとホバー強調はこの1つの関数だけを見る**。優先順位をクリック側と
   * ホバー側の2箇所に書くと、片方だけ直したときにプレビューが黙って嘘をつく ——
   * それはこの機能の存在意義そのものを消す。**副作用はここに置かない**
   * (`pickListener` を呼ぶのは `onPointerUp` だけ)。
   */
  function resolveHit(offsetX: number, offsetY: number): Omit<PickEvent, 'mode'> {
    const nothing: Omit<PickEvent, 'mode'> = {
      hit: null,
      edge: null,
      vertex: null,
      point: null,
      region: null,
      plane: null,
      sketchEntity: null,
    }
    // **スケッチモード中は面を拾わない**。描いている最中に奥のソリッドの面を掴むと、
    // 線を引くつもりのクリックが選択の置き換えになる。領域が手前・平面の点がその次。
    if (activeSketchId !== null) {
      const region = regionAt(offsetX, offsetY)
      const plane = planePointAt(offsetX, offsetY)
      // **図形は領域より手前**(点・辺は領域の縁に乗っているので、領域が勝つと
      // 端点を掴めない)。ただし `point`(平面上の座標)は常に一緒に返す ——
      // 描くコマンドが起動している間はそちらが優先で、判断は `App.svelte` が持つ。
      return {
        ...nothing,
        region,
        sketchEntity: sketchEntityAt(offsetX, offsetY),
        point: plane === null ? null : { sketchId: activeSketchId, ...plane },
      }
    }
    // **辺を出しているときは辺が先**。辺は面の上に重なっているので、面より後に見ると
    // 必ず面が勝つ。出していないときは `edgeAt` が即 null を返すので、面のピックは
    // そのまま動く。**この順序は変えない**(基準平面を足しても辺→面の優先順位は同じ)。
    const edge = edgeAt(offsetX, offsetY)
    if (edge !== null) return { ...nothing, edge }
    // **領域を要求しているときは領域が先**(辺と同じ「要求中のものが勝つ」)。領域は
    // スケッチ平面の上に浮いており、押し出した板の底面と重なるので、面より後に見ると
    // 視点によって面が勝ってしまう。`pickRegions(false)` の間は `regionAt` が即 null を
    // 返すので、面のピックはそのまま動く。
    const region = regionAt(offsetX, offsetY)
    if (region !== null) return { ...nothing, region }
    // **頂点マーカーは面より先**(最小の的なので、当たったならそれが狙いである)。
    // 辺・領域より後なのは、頂点が必ず辺の端点に乗っているから —— 先に見ると
    // フィレットを起動している間に辺の端が掴めなくなる。出していない間は
    // `vertexAt` が即 null を返すので、面のピックはそのまま動く。
    const vertex = vertexAt(offsetX, offsetY)
    if (vertex !== null) return { ...nothing, vertex }
    // **面と基準平面は、レイ距離が近い方**(通常のオクルージョン。辺のような優先順位は
    // 付けない —— 平面は部品と同じ空間に居るので、手前に見えているものが取れるのが自然)。
    // 基準平面は `showBasePlanes(true)` の間しか当たらない。
    //
    // **ただし同着は面が勝つ。** 基準平面に描いて押し出すと、その面は平面とぴったり重なり、
    // 2つのレイ距離が同着になる —— 素の `<` では**どちらが手前かが浮動小数の最後の桁で
    // 決まり**、見た目が同じなのに指すたび結果が変わる(2026-08-28、無関係な式の書き換えで
    // カメラが 7e-14 動いただけで面→XZ平面へ反転した)。そこで**画面で1画素にも満たない差を
    // 「手前」と見なさない** —— 見分けが付かないなら、実体のある面のほうが狙いである。
    // しきい値はシーンの寸法に依らないよう `worldPerPixel()`(注視点で1画素ぶんの世界長さ)
    // から作る。この不変は `smoke-files.mjs` の(d)が画角を振って固定している。
    const face = faceHitAt(offsetX, offsetY)
    const basePlane = basePlaneAt(offsetX, offsetY)
    if (
      basePlane !== null &&
      (face === null || basePlane.distance < face.distance - worldPerPixel())
    ) {
      return { ...nothing, plane: basePlane.name }
    }
    return { ...nothing, hit: face?.ref ?? null }
  }

  function onPointerMove(event: PointerEvent): void {
    // **キューブが引き取ったポインタはメインの経路を一切通らない**(タスク仕様の落とし穴1)。
    if (cube.capturing()) {
      cube.pointerMove(event)
      return
    }
    // ポインタがキューブの矩形の内側にあるあいだは、メインのホバー予告・矩形選択開始の
    // どちらも早期に打ち切る(まだ何も押していない=ホバーのみの場合)。
    const overCube = cube.inside(event.offsetX, event.offsetY)
    cube.setHoverAt(overCube ? { x: event.offsetX, y: event.offsetY } : null)
    if (overCube) {
      hoverAt = null
      return
    }
    // **座標を控えるだけ**。当たり判定は `tick` が1フレームに1回だけ回す
    // (pointermove は1フレームに何度も飛ぶので、ここで解決すると同じ絵のために
    // 何度もレイキャストすることになる)。
    hoverAt = { x: event.offsetX, y: event.offsetY }
    // 視点回転中は取れるものの予告を出さない(**これはクリックにならない**)。境目は
    // `onPointerUp` の moved 判定と揃える。**`downValid` ではなく `buttons` で見る** ——
    // canvasの外でボタンを離されると `pointerup` が来ず、`downValid` は立ったままになる。
    const dragged = Math.hypot(event.offsetX - downX, event.offsetY - downY)
    orbiting = event.buttons !== 0 && dragged > CLICK_MOVE_LIMIT_PX
    // **左ボタンを押したまま一定距離を越えたらウィンドウ選択**。`downValid` ではなく
    // `buttons` も見るのは、canvasの外で離されたときに帯が出たままになるのを防ぐため。
    if (downValid && (event.buttons & 1) !== 0 && dragged > BOX_SELECT_MIN_PX) {
      boxActive = true
      showBand(event.offsetX, event.offsetY)
    }
    if (planeMoveListener === null) return
    planeMoveListener(activeSketchId === null ? null : planePointAt(event.offsetX, event.offsetY))
  }

  /** ポインタがcanvasから出た。**強調を残さない**(戻ってくるまで指せるものは無い)。 */
  function onPointerLeave(): void {
    hoverAt = null
    orbiting = false
    // **引きかけの矩形は捨てる**(帯だけが残って画面に貼り付くのを防ぐ)。
    hideBand()
    downValid = false
    // キューブの操作中(クリック判定待ち・ドラッグ中)は無視する —— `setPointerCapture` で
    // 押下中は座標がどこへ行ってもこの要素が受け取り続けるので、`pointerleave` が来ても
    // 操作を打ち切らない(ロック中はそもそも座標が凍結して動かない)。
    if (!cube.capturing()) cube.setHoverAt(null)
  }

  renderer.domElement.addEventListener('pointerdown', onPointerDown)
  renderer.domElement.addEventListener('pointerup', onPointerUp)
  renderer.domElement.addEventListener('pointermove', onPointerMove)
  renderer.domElement.addEventListener('pointerleave', onPointerLeave)
  // ドラッグがOSに横取りされた場合(`pointerup` が来ない)。`pointerleave` と同じ後始末でよい。
  renderer.domElement.addEventListener('pointercancel', onPointerLeave)

  const observer = new ResizeObserver(() => applySize())
  observer.observe(container)
  applySize()

  /**
   * ホバー強調を1フレームぶん更新する。
   *
   * **毎フレーム解決し直す** —— 視点を回すとポインタが動かなくても当たるものが変わるので、
   * 「動いたときだけ」では足りない。材質の書き換えは**変わったときだけ**行う
   * (全メッシュ走査を毎フレームやらない)。
   */
  function updateHover(): void {
    const point = orbiting ? null : hoverAt
    const probe =
      point === null
        ? ''
        : `${pickGeneration},${point.x},${point.y},` +
          `${camera.position.toArray().join(',')},${camera.quaternion.toArray().join(',')},` +
          // **`zoom` も入れる** —— 正投影のドリーは `zoom` だけを動かしてカメラを動かさない
          // ので、位置と姿勢だけのキーでは「ホイールで寄ったのに当たるものが変わらない」になる。
          `${camera.zoom}`
    if (probe === hoverProbe) return
    hoverProbe = probe
    const next = point === null ? null : resolveHit(point.x, point.y)
    // **頂点は出さない**(2026-08-27本人決定)。頂点が取れる画素では面も辺も光らない ——
    // そこをクリックしても面は選ばれないのだから、光らせないほうが正しい。
    //
    // **スケッチの点・辺も同じ扱い**(2026-08-30)。予告の色は付けないが、
    // **そこが取れるときは領域を光らせない** —— 光らせると「クリックすれば取れるもの」の
    // 予告が嘘になり、この機能の存在意義そのものが消える(点・辺は領域より手前で拾われる)。
    const face = next?.hit ?? null
    const edge = next?.edge ?? null
    const region = next?.sketchEntity ? null : (next?.region ?? null)
    const plane = next?.plane ?? null

    const nextFaceKey = face === null ? '' : refKey(face.bodyId, face.faceIndex)
    if (nextFaceKey !== hoverFaceKey) {
      hoverFaceKey = nextFaceKey
      hoverFace = face
      applyFaceMaterials()
    }
    const nextEdgeKey = edge === null ? '' : refKey(edge.bodyId, edge.edgeIndex)
    if (nextEdgeKey !== hoverEdgeKey) {
      hoverEdgeKey = nextEdgeKey
      hoverEdge = edge
      applyEdgeMaterials()
    }
    const nextRegionKey = region === null ? '' : refKey(region.sketchId, region.index)
    if (nextRegionKey !== hoverRegionKey) {
      hoverRegionKey = nextRegionKey
      hoverRegion = region
      applyRegionMaterials()
    }
    if (plane !== hoverPlane) {
      hoverPlane = plane
      applyBasePlaneHover()
    }
  }

  /**
   * 視線が極(真上/真下)から離れたら、`camera.up`をWORLD_UPへ戻す。
   *
   * `OrbitControls`は生成時に焼いた軸(`WORLD_UP`)だけを回転の極として使うので、
   * 位置そのものの計算は`camera.up`の値に影響されない —— が、`controls.update()`が
   * 最後に呼ぶ`object.lookAt()`は**そのときの`camera.up`**でロール(傾き)を決める。
   * ビューキューブの上面図/底面図クリック(`upForDirection`)も`lookAtSketch`も、
   * 極付近の縮退(upが視線と平行になり`lookAt`の直交化が壊れる)を避けるために
   * 一時的に`camera.up`をYへ傾けるが、従来はそこから離れても誰も戻していなかった ——
   * 傾いたままの`up`で`lookAt`し続け、「キューブの上面をクリックしてから回すと
   * 絵が転がる」形で見えた(2026-09-03本人報告)。
   *
   * **極に近い間(`|z| > POLE_Z`)は何もしない**(2026-09-03改訂)。最初の実装は
   * 毎フレーム`upForDirection(今の向き)`を焼き直していたが、あの関数は極付近で
   * 定数`(0,±1,0)`を返す階段関数なので、**通常のドラッグで極をまたぐだけで
   * 上が固定されたように(勝手に上下が揃うように)見えた**(本人報告その2)。
   * 普段のドラッグはそもそも`camera.up`を一切書き換えなくても、`controls`側の
   * 回転計算(常にZ軸が極)だけで滑らかに繋がる —— `upForDirection`のGram-Schmidt射影は
   * 素の`(0,0,1)`を渡した場合と`lookAt`内部の直交化後は同じ絵になる(`upForDirection`の
   * docstring参照)ので、**極から離れているときはWORLD_UPのまま放っておけばよい**。
   * 直したいのは「極ぴったりへ明示的にスナップした直後、そこから離れても誰も戻さない」
   * ことだけなので、**極から外れた瞬間にWORLD_UPへ戻す片方向の掃除**に留める。
   */
  function syncCameraUp(): void {
    if (cube.snapping()) return
    const offset = camera.position.clone().sub(controls.target)
    const length = offset.length()
    if (length < 1e-6) return // 退化(注視点に重なる)
    if (Math.abs(offset.z / length) > POLE_Z) return // 極付近では触らない(上の理由)
    if (camera.up.distanceToSquared(WORLD_UP) < 1e-12) return // 既に合っているなら何もしない
    camera.up.copy(WORLD_UP)
    camera.lookAt(controls.target)
  }

  function tick(): void {
    if (!alive) return
    cube.stepSnap(performance.now())
    controls.update()
    syncCameraUp()
    // **描画ループで見る**(`OrbitControls` の 'change' ではない) —— 減衰つきのドリーは
    // イベントが止まった後も数十フレーム動き続けるので、「止まったか」はフレーム側でしか
    // 分からない。中身は算術数本で、当たったときだけ作り直しが走る。
    updateResolution(performance.now())
    updateHover()
    cube.updateHover()
    // グリッドは毎フレームここだけを更新する(段の不透明度とカメラ行列。ジオメトリは無い)。
    updateGridUniforms()
    renderer.render(scene, camera)
    cube.render()
    frameHandle = requestAnimationFrame(tick)
  }
  frameHandle = requestAnimationFrame(tick)

  return {
    update,
    getGeometry(bodyId) {
      return rawGeometry.get(bodyId) ?? null
    },
    prune,
    onPick(listener) {
      pickListener = listener
    },
    onBoxSelect(listener) {
      boxSelectListener = listener
    },
    onPlaneMove(listener) {
      planeMoveListener = listener
    },
    viewResolution() {
      return { ...appliedResolution }
    },
    setFaceCulling(enabled) {
      if (faceCullingEnabled === enabled) return
      faceCullingEnabled = enabled
      // **次のフレームで必ず採り直す**。カメラが止まったままでも判定が変わるので、
      // 直前に計算した印を捨てないと「姿勢も世代も同じ」で早期returnしてしまう。
      lastFocusComputeEpoch = -1
      lastFocusComputeGeneration = -1
    },
    sketchPolylineCounts() {
      const counts: Record<string, number[]> = {}
      for (const [sketchId, entry] of sketches) counts[sketchId] = [...entry.polylineCounts]
      return counts
    },
    onResolutionChange(listener) {
      resolutionListener = listener
    },
    highlight,
    highlightBodies,
    setOptimizeState,
    setOptimizeDebug,
    optimizeDebugCounts() {
      return { ...debugCounts, hiddenBodyId: debugHiddenBodyId }
    },
    showEdges,
    showBasePlanes,
    pickRegions,
    highlightEdges,
    projectEdge,
    showVertices,
    highlightColors() {
      return { ...highlightColor }
    },
    faceColor(ref) {
      const mesh = allFaceMeshes.find(
        (candidate) => candidate.userData.bodyId === ref.bodyId && candidate.userData.faceIndex === ref.faceIndex,
      )
      const material = mesh?.material as THREE.MeshLambertMaterial | undefined
      return material?.color ? material.color.getHex() : null
    },
    optimizeAnnotationCount() {
      return optimizeRoot.children.length
    },
    optimizeAnnotations() {
      return {
        fixtures: optimizeAnnotationInfo.fixtures.map((ref) => ({ ...ref })),
        loads: optimizeAnnotationInfo.loads.map((item) => ({
          ...item,
          direction: [...item.direction] as [number, number, number],
        })),
      }
    },
    hoveredRef() {
      return {
        face: hoverFace,
        edge: hoverEdge,
        region: hoverRegion,
        plane: hoverPlane,
        color: hoverColorNow(),
      }
    },
    projectVertex,
    showPreview,
    clearPreview,
    showBodyGhosts,
    clearBodyGhosts,
    ghostInfo() {
      return {
        sketch: sketchGhostInfo.map((item) => ({ kind: item.kind, params: { ...item.params } })),
        bodies: bodyGhostInfo.map((item) => ({
          bodyId: item.bodyId,
          offset: [...item.offset] as [number, number, number],
          rotate: item.rotate ?? null,
          // **グループに乗っている行列そのもの**を読む(`bodyGhostMatrix` を呼び直さない)
          // —— 組んだ行列を実際に掛け忘れていても気づけるようにするため。
          matrix: bodyGhosts.get(item.bodyId)?.group.matrix.toArray() ?? [],
        })),
      }
    },
    project,
    showSketches,
    showSketchGhost,
    showSketchGhosts,
    setActiveSketch,
    lookAtSketch,
    frameAll() {
      camera.up.copy(WORLD_UP) // スケッチの v 軸へ倒したままだと、戻したのに傾いて見える
      frameToContent()
    },
    resetGrid() {
      applyGridScale(contentBoxForGrid(), true)
    },
    gridScale() {
      return { extent: gridExtent }
    },
    gridTiers() {
      return {
        mmPerPixel: gridMmPerPixel,
        tiers: TIERS.map((step, index) => ({ step, alpha: gridTierAlphaValues[index] })),
        planes: [...gridPlanesShown],
        axis: gridAxisShown,
        // `sketchGridIsFacePlane` が判定の正(`plane.kind`ベース。2026-09-03修正 —— 以前は
        // frame一致で再判定しており、基準平面と数値的に一致する実ジオメトリの面を見誤っていた)。
        sketchFaceExtent:
          sketchGridFrame !== null && sketchGridIsFacePlane ? (sketchGridBounds?.half ?? null) : null,
        depthBiasLayers: [...gridDepthBiasLayers],
        planeBounds: gridPlaneBounds.map((bounds) => (bounds === null ? null : { ...bounds })),
      }
    },
    setFov,
    cameraPose,
    projectPoint,
    projectSketchPoint,
    projectBasePlane,
    projectRegion,
    viewCubeRect: cube.rect,
    dispose() {
      alive = false
      cancelAnimationFrame(frameHandle)
      observer.disconnect()
      renderer.domElement.removeEventListener('pointerdown', onPointerDown)
      renderer.domElement.removeEventListener('pointerup', onPointerUp)
      renderer.domElement.removeEventListener('pointermove', onPointerMove)
      renderer.domElement.removeEventListener('pointerleave', onPointerLeave)
      renderer.domElement.removeEventListener('pointercancel', onPointerLeave)
      cube.dispose()
      clearPreview()
      clearGhost()
      clearEdges()
      clearVertices()
      vertexMaterial.dispose()
      vertexProposedMaterial.dispose()
      pickEdgeMaterial.dispose()
      pickEdgeSelectedMaterial.dispose()
      pickEdgeProposedMaterial.dispose()
      pickEdgeHoverMaterial.dispose()
      pickEdgeSelectedHoverMaterial.dispose()
      pickEdgeProposedHoverMaterial.dispose()
      for (const sketchId of [...sketches.keys()]) clearSketch(sketchId)
      bandElement.remove()
      sketchMaterial.dispose()
      sketchGhostMaterial.dispose()
      sketchSelectedMaterial.dispose()
      sketchHandleMaterial.dispose()
      sketchHandleSelectedMaterial.dispose()
      regionMaterial.dispose()
      regionSelectedMaterial.dispose()
      regionHoverMaterial.dispose()
      regionSelectedHoverMaterial.dispose()
      for (const bodyId of [...bodies.keys()]) clearBody(bodyId)
      for (const item of basePlaneHoverDisposables) item.dispose()
      for (const grid of planeGrids) grid.dispose()
      gridAxisLine?.geometry.dispose()
      gridAxisMaterial.dispose()
      gridRoot.clear()
      controls.dispose()
      for (const material of faceMaterialCache.values()) material.dispose()
      faceMaterialCache.clear()
      previewMaterial.dispose()
      edgeMaterial.dispose()
      clearOptimizeAnnotations()
      clearOptimizeDebug()
      renderer.dispose()
      renderer.domElement.remove()
    },
  }
}
