/**
 * ビューポート — three.js をここに閉じ込める(docs/DESIGN_DECISIONS.md「技術選定の根拠」)。
 *
 * three.js の型を外へ漏らさない: 公開するのは `viewportTypes.ts` の `Viewport` だけ。
 * three.js を触るのはこのファイルと兄弟モジュール(`planeGrid.ts` / `faceCulling.ts` /
 * `viewCubeOverlay.ts` など)だけで、破壊的変更の影響を Svelte 層へ出さない。
 *
 * canvas はフレームワークの外(docs/DESIGN_DECISIONS.md「技術選定の根拠」の「frontend で守るべき唯一のルール」)。
 * Svelte から受け取るのは空の `HTMLElement` 1つだけで、canvas の生成・破棄・再描画は全部この中で行う。
 *
 * Bodyごとの差分更新に対応する(`update()` は変わったBodyだけ差し替える)。毎回全部作り直すと
 * サーバ側の差分送信の意味が描画で消える。面の当たり判定・強調表示・射影は
 * body_id + face_index の組で指す(`faceIndex` は1個のBodyの中でしか一意でない)。
 */
import * as THREE from 'three'
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'

// 辺の折れ線の形は `session.ts` の `EdgeGeometry` が正(サーバが配る形そのもの)。
import {
  entityHandles, entityHasIndependentPoints, entityMidpoints, entityPoints, entityVisualCorners, sketchIntersections,
  snapSketchPoint,
} from './entityKinds'
import { classifySketchSnap, createSketchCursorOverlay } from './sketchCursorOverlay'
import { sampleFaces, type BodySamples } from './faceCulling'
import { startFrameLoop } from './frameLoop'
import { createHeadlight } from './headlight'
import { cappedPixelRatio, nextPixelRatio } from './pixelRatio'
import { createResolutionTracker } from './resolutionTracker'
import { BASE_EXTENT, mmPerPixel, uvBoxOfPoints, type CameraProjection } from './gridScale'
import { createOriginGizmo } from './originGizmo'
import { createGhostLayer } from './ghostLayer'
import { createSectionView } from './sectionView'
import { createSelectionBand, type BandRect } from './selectionBand'
import { createGridPlanes } from './gridPlanes'
import { frameVector, planePoint } from './planeMath'
import { createBatch, type SceneBatch } from './sceneBatch'
import { distSq, perspectiveParam, pickNearestWithinRadius, rankByOverlap, type RankedCandidate, type ScreenPickCandidate } from './screenPick'
import { variantCache } from './viewportMaterials'
import { buildSketchLine, createSketchLineMaterial, withLineDepthBias, type SketchFatLine } from './sketchLine'
import { POLE_Z, upForDirection } from './viewCube'
import { createOptimizeAnnotations } from './optimizeAnnotations'
import { createOptimizeDebug } from './optimizeDebug'
import { createViewCubeOverlay } from './viewCubeOverlay'
import { createInterferenceLayer } from './interferenceLayer'
import { createMeasureAnnotation } from './measureAnnotation'
import { createThumbnailer } from './thumbnail'
import { buildBodyMesh } from './bodyMesh'
import { OCCLUDER_LAYER, createOcclusion } from './occlusion'
import { closestOnPolyline, createProjector, screenHitOfPoint, type PickPolyline, type ScreenProjector } from './polylinePick'
import { createSolidFeatures, type BodyEdges, type BodyVertices } from './solidFeatures'
import type { BasePlaneName } from './session'
import {
  type BodyGeometryUpdate, type BoxSelectEvent, type EdgeRef, type FaceGeometry,
  type FaceRef, type GeometryPayload, type OptimizeVisualState, type PickEvent, type PickKind, type PickMode, type SectionState,
  type SketchDraw, type SketchEntityHit, type VertexRef, type Viewport,
} from './viewportTypes'
import {
  BACKGROUND_COLOR, BASE_COLOR, BASE_PLANES, BASE_PLANE_PROBES, BOX_SELECT_MIN_PX, CLICK_MOVE_LIMIT_PX, CLICK_TIME_LIMIT_MS,
  BOX_SEEN_STEP_PX, DEPTH_BIAS_LAYER, EDGE_COLOR, EDGE_PICK_PIXELS, FOV_DEFAULT, FOV_MAX, FOV_MIN, FOV_ORTHO, HIGHLIGHT_COLOR, HOME_VIEW_OFFSET,
  HOVER_LIGHTEN, LAYER, OPTIMIZE_EXCLUDE_COLOR, OPTIMIZE_FIXTURE_COLOR, OPTIMIZE_PRESERVE_COLOR, PICK_OVERLAP_PIXELS, PREVIEW_COLOR,
  REGION_COLOR, REGION_SELECTED_COLOR, SKETCH_COLOR, SKETCH_FIT_MARGIN, SKETCH_GHOST_COLOR, SKETCH_GHOST_WARN_COLOR, SKETCH_HANDLE_COLOR,
  SKETCH_HANDLE_PIXELS, SKETCH_SELECTED_COLOR, VERTEX_PIXELS, WORLD_UP, isSelectableFov, lighten, withDepthBias, type BodyMeshes,
} from './viewportConstants'

export { FOV_DEFAULT, FOV_ORTHO, isSelectableFov }
export type {
  BoxSelectEvent, GeometryPayload, PickEvent, SectionState, SketchEntityHit,
  Viewport,
}

export function createViewport(container: HTMLElement): Viewport {
  // `stencil` は three r163 以降の既定が false で、無いと断面の切り口が例外も出さずに描かれない
  const renderer = new THREE.WebGLRenderer({ antialias: true, stencil: true })
  // 断面は材質ごとのクリップ平面で切る(`renderer.clippingPlanes` はグリッド・スケッチ・キューブまで切る)
  renderer.localClippingEnabled = true
  renderer.setPixelRatio(cappedPixelRatio(window.devicePixelRatio))
  renderer.domElement.style.display = 'block'
  renderer.domElement.style.width = '100%'
  renderer.domElement.style.height = '100%'
  renderer.domElement.style.touchAction = 'none'
  container.appendChild(renderer.domElement)

  const scene = new THREE.Scene()
  scene.background = new THREE.Color(BACKGROUND_COLOR)

  /**
   * カメラは2つあり、`camera` が今使っている方を指す(正投影は別種の投影で1つのカメラでは
   * 表せない。`FOV_ORTHO` 参照)。姿勢(position / up / near / far)は切り替えのたびに写す。
   * `camera` を直接見てよいのは姿勢と投影の共通部分だけ: `fov` / `aspect` は透視、
   * `left`/`right`/`top`/`bottom`/`zoom` は正投影にしか無いので、触る処理は
   * `perspective` / `orthographic` を名指しすること。
   */
  const perspective = new THREE.PerspectiveCamera(FOV_DEFAULT, 1, 0.1, 10000)
  const orthographic = new THREE.OrthographicCamera(-1, 1, 1, -1, 0.1, 10000)
  let camera: THREE.PerspectiveCamera | THREE.OrthographicCamera = perspective
  // 空の文書でも原点まわりが見える位置。部品が届いたら `frameToContent` が上書きする。
  // `grid` はまだ無く、その初期値は常に `BASE_EXTENT` なので `grid.extent()` の代わりにこれを使う。
  // 距離は `fitDistance` で求める: 定数(`diagonal * 1.2` など)は FOV_DEFAULT を変えると
  // ずれる(45度で0.6%、30度で55%)。
  {
    const diagonal = Math.hypot(BASE_EXTENT * 2, BASE_EXTENT * 2, BASE_EXTENT * 2)
    const distance = fitDistance(diagonal / 2)
    camera.position.copy(HOME_VIEW_OFFSET).multiplyScalar(distance)
  }
  scene.add(camera)
  const headlightRig = createHeadlight()
  const headlight = headlightRig.light
  camera.add(headlight)
  scene.add(headlightRig.aim)
  scene.add(new THREE.HemisphereLight(0xdfe7f2, 0x191c21, 1.0))

  // 型引数を明示する: 省くと `perspective` で固まり、正投影へ差し替えるとき
  // `controls.object` が受け取れない(`activateCamera`)。
  const controls: OrbitControls<THREE.PerspectiveCamera | THREE.OrthographicCamera> =
    new OrbitControls(camera, renderer.domElement)
  controls.enableDamping = true
  controls.dampingFactor = 0.15
  // 左=ウィンドウ選択(自前実装)/ 中=パン / 右=回転。左を `null` にするのは左ドラッグを
  // 丸ごとこちらへ渡すため(割り当てたままだと矩形を引く間にカメラが動く)。
  controls.mouseButtons = {
    LEFT: null,
    MIDDLE: THREE.MOUSE.PAN,
    RIGHT: THREE.MOUSE.ROTATE,
  }

  const root = new THREE.Group()
  scene.add(root)

  // スケッチのグループはここで作る(使うのは下の「C: スケッチ」節)。初期化中の
  // `createGridPlanes()` が `contentBoxForGrid` 経由で読むため、C節に置くとTDZで落ち
  // (`Cannot access 'sketchRoot' before initialization`)、`window.__duocad.ready` が
  // 立たず「画面が出ない」という原因から遠い症状になる。
  const sketchRoot = new THREE.Group()
  scene.add(sketchRoot)

  // ------------------------------------------------------------------ 必要なときだけ描く(issue #171)
  //
  // 描くのは、公開口が呼ばれた(`invalidating`)・ポインタが動いた・ホバーや姿勢の解決が何かを変えた・カメラが
  // 動いた(減衰・ビューキューブの回転を含む。`cameraMovedSinceRender`)フレームだけ。何もしていない間は描かない
  // (常時描くと、27 部品で静止中も主スレッドが 12 ms/フレーム埋まり、ラップトップの電池も減る)。
  // 公開口を通らずに見た目を変える経路を足したら、そこで `invalidate()` を呼ぶこと。
  let needsRender = true
  function invalidate(): void {
    needsRender = true
  }

  // ------------------------------------------------------------------ 形状最適化(第3のモード)の注記
  //
  // 拘束の固定記号・荷重の矢印+ラベル。`root` に入れない(外接直方体に混ざると初回フレーミングや
  // near/far計算を歪める)。装飾なのでピック対象にも足さない。`setOptimizeState` が唯一の書き手。
  const annotations = createOptimizeAnnotations({
    faceVertices: (bodyId, faceIndex) => {
      const body = bodies.get(bodyId)?.body
      const positions = body?.facePositions(faceIndex) ?? null
      return body === undefined || positions === null ? null : { positions, normals: body.faceNormals(faceIndex) }
    },
    contentDiagonal: () => contentDiagonal,
  })
  annotations.object.renderOrder = LAYER.optimize
  scene.add(annotations.object)

  // 測定の線と札(issue #10)。`root` に入れず、ピック対象にもしない。`showMeasurement` が唯一の書き手。
  const measureAnnotation = createMeasureAnnotation({ contentDiagonal: () => contentDiagonal })
  measureAnnotation.object.renderOrder = LAYER.measure
  scene.add(measureAnnotation.object)

  // 干渉チェックの装飾(issue #186)。測定と同じく指せない。`showInterference` が唯一の書き手。
  const interferenceLayer = createInterferenceLayer({ contentDiagonal: () => contentDiagonal })
  interferenceLayer.object.renderOrder = LAYER.measure
  scene.add(interferenceLayer.object)

  // ------------------------------------------------------------------ 開発用のデバッグ表示
  //
  // 塊(`?debug=space`)・平滑化前のボクセル(`?voxel=1`)・面アンカーの節点(`?debug=anchors`)。
  // `root` に入れず、ピック対象にもしない。`setOptimizeDebug` が唯一の書き手。
  const optimizeDebug = createOptimizeDebug({ setBodyVisible })
  optimizeDebug.object.renderOrder = LAYER.debug
  scene.add(optimizeDebug.object)

  // ------------------------------------------------------------------ 平面グリッド(3段固定・無限)
  //
  // `root` に入れない(初回の `frameToContent` がグリッドの箱に引っ張られる)。
  // ピック対象にもしない(判定は `basePlaneAt` の解析交差、範囲の半幅は `grid.extent()`)。
  // `root` / `sketchRoot` を作った後で呼ぶ(最初の描画で `contentBoxForGrid` を読む)。
  const grid = createGridPlanes({
    contentBox: contentBoxForGrid,
    facePositions: (bodyId, faceIndex) => bodies.get(bodyId)?.body.facePositions(faceIndex) ?? null,
    worldPerPixel,
    camera: () => camera,
    onExtentChange: updateClipPlanes,
  })
  scene.add(grid.object)

  // ------------------------------------------------------------------ 常設の原点ギズモ(issue #8)
  //
  // `grid.object` には入れない(あちらは `groupOrder` を -1 に固定しており、
  // 個々の `LAYER.originGizmo` が効かなくなる)。`root` にも入れない。
  const originGizmo = createOriginGizmo()
  scene.add(originGizmo.object)

  // ------------------------------------------------------------------ 断面表示(issue #6)
  //
  const section = createSectionView({
    rayAt,
    camera: () => camera,
    project(point) {
      const { width, height } = size()
      return projectRaw(point, width, height)
    },
    onDrag(position) {
      sectionDragListener?.(position)
    },
    pickChanged,
  })
  scene.add(section.object)

  const cube = createViewCubeOverlay({
    renderer,
    camera: () => camera,
    perspective,
    orthographic,
    controls,
    size,
    overlayOffset: toolbarOverlayOffsetPx,
  })

  const sketchCursor = createSketchCursorOverlay(container)
  let sketchCursorWanted = false

  function applyBasePlaneHover(): void {
    grid.setHoverPlane(hoverPlane)
  }

  /** 今ホバーで当てている色(実機検査フック専用)。ホバーは明度だけを上げるので、材質から読み戻す。 */
  function hoverColorNow(): number | null {
    const pick = (object: THREE.Object3D | undefined): number | null => {
      const material = (object as { material?: { color?: THREE.Color } } | undefined)?.material
      return material?.color ? material.color.getHex() : null
    }
    const face = hoverFace
    if (face !== null) return paintedFaceColor(face)
    if (hoverEdge !== null) return features.drawnColor(hoverEdge)
    if (hoverVertex !== null) return features.drawnColor(hoverVertex)
    const region = hoverRegion
    if (region !== null) {
      return pick(
        sketches
          .get(region.sketchId)
          ?.regionMeshes.find((mesh) => mesh.userData.regionIndex === region.index),
      )
    }
    const sketchEntity = hoverSketchEntity
    if (sketchEntity !== null) {
      const entry = sketches.get(sketchEntity.sketchId)
      if (sketchEntity.point !== null) {
        // 編集していないスケッチの点は、ホバー中の点だけを印(`markHovered`)で描く
        if (entry !== undefined && !entry.editing) return pick(entry.markHovered ?? undefined)
        return pick(
          entry?.handleMeshes.find(
            (mesh) => mesh.userData.entityId === sketchEntity.entityId && mesh.userData.point === sketchEntity.point,
          ),
        )
      }
      return pick(entry?.handleLines.find((line) => line.userData.entityId === sketchEntity.entityId))
    }
    if (hoverPlane !== null) return pick(grid.hoverMesh(hoverPlane))
    return null
  }

  /** 形状最適化(第3のモード)の保持/除外/拘束の役割。素の面は `'none'`。 */
  type OptimizeFaceRole = 'none' | 'preserve' | 'exclude' | 'fixture'
  type FaceMaterialState = 'plain' | 'user'

  function roleBaseColor(role: OptimizeFaceRole): number {
    if (role === 'preserve') return OPTIMIZE_PRESERVE_COLOR
    if (role === 'exclude') return OPTIMIZE_EXCLUDE_COLOR
    if (role === 'fixture') return OPTIMIZE_FIXTURE_COLOR
    return BASE_COLOR
  }

  /**
   * ボディの材質は2つだけ(面の色は頂点色。`bodyMesh.ts`)。平らな陰影はメッシュボディ用(三角形がそのまま形で、
   * 頂点法線を平均すると元に無い滑らかさが出る)。
   */
  const bodyMaterials = {
    smooth: new THREE.MeshLambertMaterial({
      color: 0xffffff,
      vertexColors: true,
      side: THREE.DoubleSide,
      clippingPlanes: section.clipPlanes,
    }),
    flat: new THREE.MeshLambertMaterial({
      color: 0xffffff,
      vertexColors: true,
      side: THREE.DoubleSide,
      flatShading: true,
      clippingPlanes: section.clipPlanes,
    }),
  }
  const lightenedColors = new Map<number, number>()
  /**
   * 面の色(16進)。選択は役割に勝つ: `state !== 'plain'` のときは役割の色を見ない(保持ソリッドを選ぶとハイライト色になる)。
   * ホバーは明度だけを上げる。
   */
  function faceColorFor(role: OptimizeFaceRole, state: FaceMaterialState, hovered: boolean): number {
    const base = state === 'user' ? HIGHLIGHT_COLOR : roleBaseColor(role)
    if (!hovered) return base
    let light = lightenedColors.get(base)
    if (light === undefined) {
      light = lighten(base, HOVER_LIGHTEN)
      lightenedColors.set(base, light)
    }
    return light
  }
  /** 塗っている色を、材質の色を読み戻したのと同じ値で返す(sRGB の16進 → 線形 → 16進。実機検査のフックが比べる)。 */
  function paintedFaceColor(ref: FaceRef): number | null {
    const hex = bodies.get(ref.bodyId)?.body.faceColor(ref.faceIndex)
    return hex === undefined ? null : new THREE.Color(hex).getHex()
  }
  const previewMaterial = new THREE.MeshLambertMaterial({
    color: PREVIEW_COLOR,
    side: THREE.DoubleSide,
    transparent: true,
    opacity: 0.55,
    // 半透明どうしが互いを隠して真っ黒な塊にならないよう、奥行きは書かない
    depthWrite: false,
    clippingPlanes: section.clipPlanes,
  })
  const edgeMaterial = new THREE.LineBasicMaterial({ color: EDGE_COLOR, clippingPlanes: section.clipPlanes })

  const raycaster = new THREE.Raycaster()
  /** body_id -> そのBodyが画面に持つメッシュ。非表示・未読込のBodyはキーごと無い。 */
  const bodies = new Map<string, BodyMeshes>()
  const thumbnailer = createThumbnailer(renderer)
  /** body_id -> サーバから届いた生の `GeometryPayload`(`getGeometry()` 用)。`bodies` と寿命を揃える。 */
  const rawGeometry = new Map<string, GeometryPayload>()
  /** `rawGeometry` と寿命を揃える可視判定用サンプル(`resolutionTracker` の `computeFocus` が2835面を毎フレーム走査しない)。 */
  const faceSamples = new Map<string, BodySamples>()
  /** `faceSamples` が変わった印。姿勢が止まっていても形が届けば `computeFocus` をやり直す。 */
  let focusGeneration = 0
  /** 全Bodyのメッシュ(1ボディ1つ。ピック用)。update() のたびに作り直す。 */
  let pickMeshes: THREE.Mesh[] = []
  let pickListener: ((event: PickEvent) => void) | null = null
  let boxSelectListener: ((event: BoxSelectEvent) => void) | null = null
  let sectionDragListener: ((position: number) => void) | null = null
  let pickDataWantedListener: ((bodyIds: string[]) => void) | null = null
  let planeMoveListener:
    | ((point: { u: number; v: number } | null, sketchEntity: SketchEntityHit | null) => void)
    | null = null

  // ---------------------------------------------------------------- ホバー強調
  //
  // 「そのままクリックしたら取れるもの」を1つだけ明るくする。対象は面・辺・頂点・領域・基準平面・
  // スケッチの図形/点(部品ごと。辺・頂点・編集していないスケッチの点は、ホバー中だけ描く)。
  // 当たり判定は `resolveHit` をクリックと共有する(優先順位を2箇所に書くとプレビューが嘘をつく)。
  /** ポインタの現在位置(canvasの外なら null)。解決はフレームに1回(`tick`)。 */
  let hoverAt: { x: number; y: number } | null = null
  /** 視点回転中(=そのポインタの動きはクリックではない)。回している間は解決しない。 */
  let orbiting = false
  let hoverFace: FaceRef | null = null
  let hoverEdge: EdgeRef | null = null
  let hoverVertex: VertexRef | null = null
  let hoverPlane: BasePlaneName | null = null
  let hoverRegion: { sketchId: string; index: number } | null = null
  let hoverSketchEntity: SketchEntityHit | null = null
  // 「変わったときだけ材質を書き換える」ための印(毎フレーム全メッシュを走査しない)。
  let hoverFaceKey = ''
  let hoverRegionKey = ''
  let hoverSketchKey = ''
  /** 直近にホバーを解決したときの「ポインタ位置+視点+`pickGeneration`」。同じならレイキャストごと省く。 */
  let hoverProbe = ''
  /**
   * 「何が指せるか」が変わるたびに増やす番号。`hoverProbe` に混ぜてあり、ポインタも視点も
   * 動いていなくても次のフレームで解決し直す(無いと、領域をホバーしたまま Esc でコマンドを
   * 解除しても明るいまま残る)。頂点マーカーの増減・プレビューで本物を隠したとき・寸法変更も同じ。
   */
  let pickGeneration = 0

  /** 「何が指せるか」を変えた側が呼ぶ。 */
  function pickChanged(): void {
    pickGeneration += 1
  }

  /** ホバーの印を捨てる。描画対象を作り直した側が呼ぶ(作り直したメッシュは素の材質で生まれるため)。 */
  function invalidateHover(): void {
    hoverFace = null
    hoverEdge = null
    hoverVertex = null
    hoverRegion = null
    hoverSketchEntity = null
    hoverFaceKey = ''
    hoverRegionKey = ''
    hoverSketchKey = ''
    hoverPlane = null
    pickChanged()
    // 材質まで戻す(印を捨てるだけだと、作り直していない対象の明るい材質が居座る)
    applyFaceMaterials()
    features.setHover(null, null)
    applyRegionMaterials()
    applyBasePlaneHover()
    applySketchHoverMaterials()
  }
  let framed = false

  function size(): { width: number; height: number } {
    return {
      width: Math.max(1, container.clientWidth),
      height: Math.max(1, container.clientHeight),
    }
  }

  /** `CommandHost` が `.app` へ書く `--toolbar-overlay-h` を読み直す(`ViewCubeDeps.overlayOffset`参照)。
   *  `BrowserPanel`/`SettingsDialog` と同じCSS変数を、canvas側はJSで読むしかない。 */
  function toolbarOverlayOffsetPx(): number {
    const app = container.closest('.app')
    if (!(app instanceof HTMLElement)) return 0
    const px = parseFloat(getComputedStyle(app).getPropertyValue('--toolbar-overlay-h'))
    return Number.isFinite(px) ? px : 0
  }

  /** 画素比が変わっていたら描画バッファへ反映する(`applySize` と毎フレームの `tick` が呼ぶ)。変えたら true。 */
  function syncPixelRatio(): boolean {
    const next = nextPixelRatio(renderer.getPixelRatio(), window.devicePixelRatio)
    if (next === null) return false
    renderer.setPixelRatio(next)
    return true
  }

  function applySize(): void {
    pickChanged() // 寸法が変わると同じ画素が別のものを指す
    const { width, height } = size()
    // ブラウザのズーム・別モニタへの移動で画素比が変わったら追従する(作成時の1回だけだと、バッファの解像度が起動時のまま引き伸ばされる)。
    syncPixelRatio()
    renderer.setSize(width, height, false)
    const aspect = width / height
    perspective.aspect = aspect
    perspective.updateProjectionMatrix()
    // 正投影は縦(`top`/`bottom`)を正として横を作り直す。
    // `zoom` は OrbitControls のドリーの持ち物なので触らない。
    orthographic.right = orthographic.top * aspect
    orthographic.left = -orthographic.top * aspect
    orthographic.updateProjectionMatrix()
    sketchLineResolution.set(width, height)
  }

  function clearBody(bodyId: string): void {
    const existing = bodies.get(bodyId)
    if (!existing) return
    for (const object of existing.sceneObjects) root.remove(object)
    existing.body.dispose()
    section.removeBody(bodyId)
    bodies.delete(bodyId)
    rawGeometry.delete(bodyId)
    if (faceSamples.delete(bodyId)) focusGeneration += 1
  }

  /**
   * 1面ぶんの `BufferGeometry` を作る。本物(`buildBody`)と幽霊(`ghostLayer.ts`)で共有する
   * (ずれるとプレビューと確定結果が違う形に見える)。メッシュの組み立ては共通化しない:
   * 本物は `userData` と面の境界線を持ち、幽霊は持たない。
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
    // 素の色で生む(役割・選択・ホバーは `rebuildPickIndex()` 経由の `applyFaceMaterials()` が塗り直す)。
    // 面の境界線は隣り合う同色の面を狙い分けるため。メッシュボディには出さない(`bodyMesh.ts`)。
    const body = buildBodyMesh(bodyId, payload, bodyMaterials, edgeMaterial, BASE_COLOR)
    const sceneObjects: THREE.Object3D[] = [body.mesh]
    body.mesh.layers.enable(OCCLUDER_LAYER) // 矩形選択の遮蔽(`occlusion.ts` の奥行きの読み出し)が描くのはこれだけ
    root.add(body.mesh)
    if (body.lines !== null) {
      root.add(body.lines)
      sceneObjects.push(body.lines)
    }
    section.setBody(bodyId, [body.mesh.geometry])
    bodies.set(bodyId, { body, sceneObjects })
    rawGeometry.set(bodyId, payload)
    faceSamples.set(bodyId, sampleFaces(payload))
    focusGeneration += 1
  }

  function rebuildPickIndex(): void {
    pickMeshes = [...bodies.values()].map((entry) => entry.body.mesh)
    invalidateHover() // 作り直した面は素の材質で生まれる(`invalidateHover` のコメント)
  }

  function update(entries: BodyGeometryUpdate[], options: { keepPreview?: boolean } = {}): void {
    if (entries.length === 0) return
    // 確定前の幽霊を消して本物を作り直し、ローカル幽霊は届いた形で組み直す(`rebuildAround`)。
    ghostLayer.rebuildAround(() => rebuildBodies(entries), options.keepPreview === true)
  }

  function rebuildBodies(entries: BodyGeometryUpdate[]): void {
    // 辺・頂点は捨てない: 粗さだけ変わった配信(カメラ由来)でも作り直しが走る。形が変わったら呼び手
    // (`App.svelte`)が形の版を見て差し替える(古い添字の線は違う辺を指す)。
    for (const entry of entries) {
      clearBody(entry.bodyId)
      if (entry.geometry) buildBody(entry.bodyId, entry.geometry)
    }
    rebuildPickIndex()
    refreshContentDiagonal() // 見えているものの大きさが変わった(解像度の上下限に効く)
    updateClipPlanes() // 内容が変わった → 視錐台を引き直す(姿勢は初回のみ下で決める)

    // `frameToContent()` が実際に合わせたときだけ初回フレーミングの機会を消費する
    // (箱が空のまま `framed = true` にすると以後二度と起きない)。
    if (!framed && frameToContent()) {
      framed = true
    }

    // 作り直したボディにデバッグ表示の「隠す」を当て直す(`?voxel=1` は結果ボディを隠す。
    // 当て直さないと生まれ直したボディとボクセルが重なる)。
    optimizeDebug.applyHidden()
  }

  // ------------------------------------------------------------------ プレビュー(幽霊。中身は ghostLayer.ts)
  function setBodyVisible(bodyId: string, visible: boolean): void {
    const entry = bodies.get(bodyId)
    if (!entry) return
    for (const object of entry.sceneObjects) object.visible = visible
    section.setBodyVisible(bodyId, visible) // 隠した本物の切り口も一緒に消す
  }

  const ghostLayer = createGhostLayer({
    parent: root,
    material: previewMaterial,
    faceGeometry,
    rawGeometry: (bodyId) => rawGeometry.get(bodyId),
    setBodyVisible,
    pickChanged,
  })

  function prune(validBodyIds: string[]): void {
    const valid = new Set(validBodyIds)
    const stale = [...bodies.keys()].filter((bodyId) => !valid.has(bodyId))
    if (stale.length === 0) return
    ghostLayer.clearPreview() // 消えるBodyの幽霊が取り残されないように(update と同じ理由)
    for (const bodyId of stale) {
      features.setEdges(bodyId, null)
      features.setVertices(bodyId, null)
      clearBody(bodyId)
    }
    rebuildPickIndex()
    refreshContentDiagonal() // 見えているものの大きさが変わった(解像度の上下限に効く)
    updateClipPlanes() // 内容が変わった → 視錐台を引き直す
  }

  /**
   * 半径 `radius` の球が画面に収まる注視点からの距離(FOVとアスペクトの狭い方で決まる)。
   * 既定30度・aspect>=1 では `diagonal * 1.8660`、45度なら `diagonal * 1.2071`。
   * 正投影のときも透視の画角で評価する(返すのは透視と同じ場所にカメラを置く距離でしかなく、
   * 収めるのは `fitHalfHeight` + `setViewHalfHeight`)。収める量は `fitHalfHeight` が持ち、ここは距離へ直すだけ。
   */
  function fitDistance(radius: number): number {
    return fitHalfHeight(radius) / Math.tan(THREE.MathUtils.degToRad(perspective.fov) / 2)
  }

  /**
   * 半径 `radius` の球が画面に収まる、注視点の面での縦の半分の世界長さ(縦横の狭い方に合わせる)。
   * `fitDistance` と対: 透視は距離で、正投影は視錐台の大きさで収める。
   */
  function fitHalfHeight(radius: number): number {
    return perspective.aspect >= 1 ? radius : radius / perspective.aspect
  }

  /**
   * 注視点の面で画面の縦に収まる世界長さの半分(`setFov` の切り替えと `worldPerPixel` が通る)。
   * 正投影はドリーが `zoom` だけを動かすので `zoom` で割る。
   */
  function viewHalfHeight(): number {
    if (camera === orthographic) return orthographic.top / orthographic.zoom
    const distance = camera.position.distanceTo(controls.target)
    return distance * Math.tan(THREE.MathUtils.degToRad(perspective.fov) / 2)
  }

  /**
   * 見かけの大きさ(`viewHalfHeight`)を `half` にする。透視は注視点からの距離、正投影は
   * 視錐台の大きさで作る(カメラを前後させても大きさが変わらない)。正投影は `zoom` を1へ戻して
   * 視錐台へ焼き込む(`zoom` は OrbitControls のドリーの持ち物)。
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
   * 使うカメラを差し替える(透視 ⇄ 正投影)。姿勢・クリップ範囲・ヘッドライト・シーン所属・
   * `OrbitControls` の操作対象を移す。`OrbitControls` の極(`_quat`)は生成時の `up` から焼かれた
   * まま動かないが、両カメラの上方向は同じワールド+Z(`WORLD_UP` 参照)なので問題ない。
   */
  function activateCamera(next: THREE.PerspectiveCamera | THREE.OrthographicCamera): void {
    if (next === camera) return
    next.position.copy(camera.position)
    next.up.copy(camera.up)
    next.quaternion.copy(camera.quaternion)
    next.near = camera.near
    next.far = camera.far
    camera.remove(headlight)
    scene.remove(camera)
    next.add(headlight)
    scene.add(next)
    camera = next
    controls.object = next
    next.updateProjectionMatrix()
  }

  /**
   * near/far を、いまの内容とカメラ距離から引き直す。姿勢(`frameToContent`)とは独立:
   * 後から大きなSTEPが届くとfarを追い越して手前が切れ(farはズーム・オービットでは広がらない)、
   * 小さな細部へ深く寄るとnearが大きいまま手前が切れる。内容が変わるたび
   * (`refreshContentDiagonal` の呼び出し元)とカメラが意味のある幅で動くたび
   * (`resolutionTracker` の `cameraMoved` 判定)に呼ぶ。near は距離側からも導く。
   * `span` の下限 `grid.extent() * 2` は、空の文書やグリッドだけの場面でも視錐台がグリッドを
   * 収めるため(ピック範囲の半幅は `grid.rescale` で変わる)。
   */
  function updateClipPlanes(): void {
    const span = Math.max(contentDiagonal, grid.extent() * 2)
    const distance = camera.position.distanceTo(controls.target)
    camera.near = Math.max(Math.min(span, distance) / 1000, 0.01)
    camera.far = Math.max(span * 20, distance * 4)
    camera.updateProjectionMatrix()
  }

  /**
   * グリッド計算専用の外接直方体。`root` + `sketchRoot` だけから作る(`grid.object` を含めると
   * 「グリッドが伸びる→箱が大きくなる→グリッドが伸びる」の発散ループになる)。
   */
  function contentBoxForGrid(): THREE.Box3 {
    const box = new THREE.Box3().setFromObject(root)
    box.union(new THREE.Box3().setFromObject(sketchRoot))
    return box
  }

  /**
   * シーンに今ある全メッシュの外接直方体にカメラを合わせる(初回だけ。姿勢のみで、near/farは
   * `updateClipPlanes`)。実際に合わせたかを返す: 箱が空なら何もせず `false`。呼び出し側の
   * `update()` はこれを見て初めて一度きりの機会(`framed`)を消費する
   * (`geometry: null` だけの1通で焼き切れるのを防ぐ)。
   */
  function frameToContent(): boolean {
    const box = new THREE.Box3().setFromObject(root)
    // 箱が空でも、グリッドの縮小許可は必ず1回通す(許可はここにしか無い。早期returnより前に
    // 置かないと、全部消して `frameAll()` してもグリッドが縮まない)。戻り値(姿勢を合わせたか)とは独立。
    grid.rescale(contentBoxForGrid(), true)
    if (box.isEmpty()) return false
    const center = new THREE.Vector3()
    box.getCenter(center)
    const diagonal = Math.max(box.getSize(new THREE.Vector3()).length(), 1e-6)
    const distance = fitDistance(diagonal / 2)
    camera.position.copy(center).addScaledVector(HOME_VIEW_OFFSET, distance)
    controls.target.copy(center)
    // 正投影は視錐台で収める。`HOME_VIEW_OFFSET` の長さが1ではなく、透視は `fitHalfHeight` の
    // 1.5362倍ぶん引いた絵になるので、同じ係数を掛けて揃える(掛けないと `frameAll` のたびに
    // 正投影だけ1.54倍に寄る。実測 93.6px → 143.7px)。
    if (camera === orthographic) {
      setViewHalfHeight(fitHalfHeight(diagonal / 2) * HOME_VIEW_OFFSET.length())
    }
    updateClipPlanes()
    controls.update()
    return true
  }

  function setFov(deg: number): void {
    if (!Number.isFinite(deg)) return
    // 0だけが正投影で、それ以外は 30〜90 へ丸める。
    const next = deg === FOV_ORTHO ? FOV_ORTHO : Math.min(FOV_MAX, Math.max(FOV_MIN, deg))
    const wantOrtho = next === FOV_ORTHO
    const already = wantOrtho
      ? camera === orthographic
      : camera === perspective && Math.abs(next - perspective.fov) < 1e-6
    if (already) return
    // 切り替えの前に見かけの大きさを測り、後で同じ値へ戻す(しないとパースと一緒に拡大率が変わる)。
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

  function viewSignature(): string {
    camera.updateMatrixWorld()
    const { width, height } = size()
    return [...camera.matrixWorld.elements, ...camera.projectionMatrix.elements, width, height].join(',')
  }

  function projectPoint(point: readonly [number, number, number]): { x: number; y: number } | null {
    const { width, height } = size()
    return projectRaw(frameVector(point), width, height)
  }

  /** 「持ち主+添字」の1本の鍵(面・辺・領域で共通)。 */
  function refKey(owner: string, index: number): string {
    return `${owner}\u0000${index}`
  }

  /** 今の選択(`highlight` が受け取ったもの)。 */
  let selectedFaceKeys = new Set<string>()
  /** ボディ単位の選択(`highlightBodies`。保持/除外を決めているあいだだけ埋まる)。 */
  let selectedBodyIds = new Set<string>()

  /** 形状最適化(第3のモード)の役割。モードを抜けたら空にする(`setOptimizeState(null)`)。 */
  let bodyRoles = new Map<string, 'preserve' | 'exclude'>()
  let fixtureFaceKeys = new Set<string>()

  /**
   * 面の材質を決める。選択・ホバー・役割の全部を見る唯一の場所
   * (別々に `mesh.material` を書くと次の `highlight()` の全走査に消される)。
   */
  function applyFaceMaterials(): void {
    // 塗るのは色が変わった面の頂点だけ(`BodyMesh.setFaceColor`)。全面を舐めるのは鍵の計算だけ。
    for (const [bodyId, entry] of bodies) {
      const hitAsBody = selectedBodyIds.has(bodyId)
      const bodyRole: OptimizeFaceRole = bodyRoles.get(bodyId) ?? 'none'
      for (const face of entry.body.faces) {
        const key = refKey(bodyId, face.index)
        const state: FaceMaterialState = hitAsBody || selectedFaceKeys.has(key) ? 'user' : 'plain'
        const role: OptimizeFaceRole = fixtureFaceKeys.has(key) ? 'fixture' : bodyRole
        const isHovered = hoverFace !== null && hoverFace.bodyId === bodyId && hoverFace.faceIndex === face.index
        if (entry.body.setFaceColor(face.index, faceColorFor(role, state, isHovered))) invalidate()
      }
    }
  }

  function highlight(refs: FaceRef[]): void {
    selectedFaceKeys = new Set(refs.map((ref) => refKey(ref.bodyId, ref.faceIndex)))
    applyFaceMaterials()
  }

  /** 選択中のボディを強調する(`highlight` のボディ版)。選択は役割色に勝つので全面がハイライト色になる。 */
  function highlightBodies(bodyIds: string[]): void {
    selectedBodyIds = new Set(bodyIds)
    applyFaceMaterials()
  }

  /**
   * 形状最適化(第3のモード)の見た目を作り直す。`null` は全部消すだけ。
   * 保持/除外ボディは `applyFaceMaterials()` の役割表、拘束・荷重は装飾(`optimizeAnnotations.ts`)。
   */
  function setOptimizeState(state: OptimizeVisualState | null): void {
    annotations.clear()
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
    annotations.draw(state)
  }

  /** その画素を通るレイ(`raycaster.ray` を今のカメラで作り直して返す。断面のハンドル判定も借りる)。 */
  function rayAt(offsetX: number, offsetY: number): THREE.Ray {
    const { width, height } = size()
    const ndc = new THREE.Vector2((offsetX / width) * 2 - 1, -(offsetY / height) * 2 + 1)
    raycaster.setFromCamera(ndc, camera)
    return raycaster.ray
  }

  function faceRefOf(hit: THREE.Intersection): FaceRef | null {
    const bodyId = hit.object.userData.bodyId
    // 当たりの `faceIndex` は三角形の通し番号。面の添字へは三角形ごとの表で戻す(`bodyMesh.ts`)。
    const triangleFaces = hit.object.userData.triangleFaces as Int32Array | undefined
    const faceIndex = triangleFaces !== undefined && typeof hit.faceIndex === 'number' ? triangleFaces[hit.faceIndex] : undefined
    if (typeof bodyId !== 'string' || typeof faceIndex !== 'number') return null
    return { bodyId, faceIndex }
  }

  /**
   * 面のピック。`ref` が null の当たりは断面の切り口: 指せないが実体なので、その画素の基準平面と
   * 奥の領域を塞ぐ(`resolveHit`)。
   * レイキャストはクリップ平面を見ないので、断面が ON のときは平面をよぎる位置(`section.cut`)を
   * 境に当たりを捨てる。切られた側からレイが入る場合は、同じボディの表面をそれまでに奇数回
   * またいだかで中身かどうかを決める(描画のステンシルの偶奇と同じ。`sectionView.ts`)。
   */
  function faceHitAt(
    offsetX: number,
    offsetY: number,
  ): { ref: FaceRef | null; distance: number } | null {
    return surfaceAlong(rayAt(offsetX, offsetY))
  }

  /**
   * 視線で最初に当たる面(`faceHitAt` の中身)。遮蔽の判定(`occlusion.ts`)も同じ関数で引く: 面を指せないコマンドの
   * 起動中も、切り口(`ref: null`)も、遮るものであることは変わらない。`raycaster.ray` を書き換える。
   */
  function surfaceAlong(ray: THREE.Ray): { ref: FaceRef | null; distance: number } | null {
    if (raycaster.ray !== ray) raycaster.ray.copy(ray)
    // 隠しているメッシュは指せない(Raycaster は `visible` を見ないので明示的に外す)。
    const hits = raycaster.intersectObjects(
      pickMeshes.filter((mesh) => mesh.visible),
      false,
    )
    const cut = section.cut(raycaster.ray)
    if (cut === 'cut') return null
    if (cut === 'kept') {
      if (hits.length === 0) return null
      return { ref: faceRefOf(hits[0]), distance: hits[0].distance }
    }
    if (cut.originKept) {
      // 残る側から切られた側へ向かうレイ: 平面の手前までしか見えない(切り口は裏から見えない)
      const first = hits[0]
      if (!first || first.distance >= cut.t) return null
      return { ref: faceRefOf(first), distance: first.distance }
    }
    const crossings = new Map<string, number>()
    let first: THREE.Intersection | null = null
    for (const hit of hits) {
      if (hit.distance >= cut.t) {
        first = hit
        break
      }
      const bodyId = hit.object.userData.bodyId
      if (typeof bodyId === 'string') crossings.set(bodyId, (crossings.get(bodyId) ?? 0) + 1)
    }
    for (const count of crossings.values()) {
      if (count % 2 === 1) return { ref: null, distance: cut.t }
    }
    if (first === null) return null
    return { ref: faceRefOf(first), distance: first.distance }
  }

  // ------------------------------------------------------------------ 基準3平面(スケッチを貼る先)
  //
  // 「指せるか」の旗はグリッドが持つ(`grid.armed()`)。

  function showBasePlanes(on: boolean): void {
    if (grid.armed() === on) return
    grid.setArmed(on)
    pickChanged()
    if (!on) {
      hoverPlane = null
      applyBasePlaneHover()
    }
  }

  /**
   * 基準3平面のうち当たった1枚(当たらなければ null)。描いているグリッドは判定に使わない
   * (格子はシェーダーが無限に描き、当たれるジオメトリが無い)。`planePointAt` と同じ解析交差を
   * 3枚ぶん回し、ピック範囲(|u| か |v| が `grid.extent()` 超)の外を捨て、レイ距離が最小の1枚を採る。
   * ホバー板(`gridPlanes.ts`)も同じ `grid.extent()` から作る(ずれると「板は見えているのに指せない」)。
   */
  function basePlaneAt(
    offsetX: number,
    offsetY: number,
  ): BasePlaneName | null {
    if (!grid.armed()) return null
    const ray = rayAt(offsetX, offsetY)
    const extent = grid.extent()
    let best: { name: BasePlaneName; distance: number } | null = null
    const hit = new THREE.Vector3()
    for (const spec of BASE_PLANES) {
      const plane = new THREE.Plane(frameVector(spec.normal), 0)
      if (ray.intersectPlane(plane, hit) === null) continue // 平行 / カメラの裏
      const u = hit.dot(frameVector(spec.u))
      const v = hit.dot(frameVector(spec.v))
      if (Math.abs(u) > extent || Math.abs(v) > extent) continue
      const distance = ray.origin.distanceTo(hit)
      if (best === null || distance < best.distance) best = { name: spec.name, distance }
    }
    return best?.name ?? null
  }

  function projectBasePlane(name: BasePlaneName): { x: number; y: number } | null {
    const spec = BASE_PLANES.find((candidate) => candidate.name === name)
    if (!spec || !grid.armed()) return null
    for (const [u, v] of BASE_PLANE_PROBES) {
      const screen = toScreen(planePoint(null, spec.u, spec.v, u, v))
      if (screen === null) continue
      // クリックしたら本当にその平面が取れる画素かを `resolveHit` で確かめる(優先順位を書き写さない)
      if (resolveHit(screen.x, screen.y).plane !== name) continue
      return screen
    }
    return null
  }

  function project(bodyId: string, faceIndex: number): { x: number; y: number } | null {
    const entry = bodies.get(bodyId)
    const span = entry?.body.faceOf(faceIndex)
    if (!entry || !span || !entry.body.mesh.visible) return null
    const mesh = entry.body.mesh
    const position = mesh.geometry.getAttribute('position')
    const index = mesh.geometry.getIndex()
    if (!position || !index) return null
    const { width, height } = size()
    const point = new THREE.Vector3()
    const corner = new THREE.Vector3()

    for (let triangle = span.triangleStart; triangle < span.triangleStart + span.triangleCount; triangle += 1) {
      point.set(0, 0, 0)
      for (let vertex = 0; vertex < 3; vertex += 1) {
        const at = index.getX(triangle * 3 + vertex)
        corner.set(position.getX(at), position.getY(at), position.getZ(at))
        point.add(corner)
      }
      point.multiplyScalar(1 / 3)
      mesh.localToWorld(point)
      const screen = screenOf(point, width, height) // 画面の外・浮いているパネルの下は次の三角形を試す
      if (screen === null) continue
      // 判定はクリックと同じ `resolveHit`(辺・頂点・スケッチの線が近くて取られる画素は返さない)
      const hit = resolveHit(screen.x, screen.y).hit
      if (hit && hit.bodyId === bodyId && hit.faceIndex === faceIndex) return screen
    }
    return null
  }

  // ------------------------------------------------------------------ D: 辺・頂点(`solidFeatures.ts`)と遮蔽(`occlusion.ts`)
  //
  // 辺・頂点はサーバから取り寄せたボディの分だけ指せる(`onPickDataWanted` で頼む)。描くのはホバー中・選択中だけ。
  // ソリッドともスケッチとも別のグループに持つ(`update()`/`prune()` は `root` しか触らない)。
  const features = createSolidFeatures({
    clipPlanes: section.clipPlanes,
    sphereNear: (sphere, x, y, radiusPx, project) => occlusion.sphereNear(sphere, x, y, radiusPx, project),
    invalidate,
  })
  scene.add(features.object)

  const occlusion = createOcclusion({
    camera: () => camera,
    renderer,
    scene,
    size,
    surfaceDistance: (ray) => surfaceAlong(ray)?.distance ?? null,
    contentDiagonal: () => contentDiagonal,
    clipPlanes: section.clipPlanes,
  })

  /** いまのカメラを投影の種類ごとの最小限の形へ落とす(`gridScale.ts` の純関数へ渡す用)。 */
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

  /** 画面1画素ぶんの世界座標の長さ。式は `gridScale.ts` の `mmPerPixel` 1つ(グリッドの段・可変解像度・視点静止判定が同じ物差し)。 */
  function worldPerPixel(): number {
    const { height } = size()
    return Math.max(mmPerPixel(cameraProjection(), height), 1e-9)
  }

  // ------------------------------------------------------------------ 可変解像度(ズーム連動)
  /**
   * いま見えているものの外接対角[mm]。`Box3.setFromObject` は全メッシュを走査するので、
   * 中身が変わったとき(`update` / `prune` / `showSketches`)だけ測り直す(1フレーム古くても害が無い)。
   */
  let contentDiagonal = 0

  function refreshContentDiagonal(): void {
    const box = contentBoxForGrid()
    // 断面の枠・切り口はボディ(と幽霊)だけの外接箱で決める(スケッチは切らないので入れない)
    section.setContent(new THREE.Box3().setFromObject(root))
    contentDiagonal = box.isEmpty() ? 0 : box.getSize(new THREE.Vector3()).length()
    // 広げるのは即座、縮めない(`allowShrink: false`)。内容がしきい値付近で揺れてもグリッドが点滅しない。
    grid.rescale(box, false)
  }

  // 「カメラが止まったか」と採用した解像度は `resolutionTracker.ts`(式と定数は `adaptiveResolution.ts`)。
  const resolution = createResolutionTracker({
    camera: () => camera,
    worldPerPixel,
    contentDiagonal: () => contentDiagonal,
    faceSamples,
    focusGeneration: () => focusGeneration,
    onMoved: updateClipPlanes,
    onApplied: redrawSketches,
  })

  // ------------------------------------------------ 当たり判定の共有部品(screen pick)
  //
  // 当たり半径の定数(`EDGE_PICK_PIXELS` / `SKETCH_HANDLE_PIXELS` / `VERTEX_PIXELS`)は画面上の
  // 半径[CSS px]として使う。Raycaster の world threshold だと注視点と対象の奥行き差で
  // 実際の screen 半径が変わり、斜めから見た線は 3D 垂線距離ぶん楕円状に当たる。
  // 折れ線の歩き方と、狙われた3Dの点(遮蔽の判定に使う)は `polylinePick.ts`。

  const pickScratch = new THREE.Vector3()

  /** 今のカメラと寸法の射影(1回の当たり判定のあいだ使い回す)。 */
  function projector(): ScreenProjector {
    const { width, height } = size()
    camera.updateMatrixWorld()
    return createProjector(camera, width, height)
  }

  /**
   * スケッチの図形(`Line2`)の折れ線。`geometry` の `position` は太線を描く箱テンプレートなので読めない。
   * `buildEntityLine` が `userData.polylineWorld` に入れた world 座標の点列を読む(矩形選択の `collectInBox` も同じ)。
   */
  function sketchPolyline(line: SketchFatLine): PickPolyline | null {
    const points = line.userData.polylineWorld as readonly THREE.Vector3[] | undefined
    if (!points || points.length < 2) return null
    return {
      count: points.length,
      at(index, out) {
        out.copy(points[index])
      },
    }
  }

  /**
   * その画素で取れる点。`readItem` が null を返したものは候補にしない(`userData` の形が
   * 違うものを黙って拾わない)。
   */
  function pickPointsAt<T>(
    offsetX: number,
    offsetY: number,
    radiusPx: number,
    meshes: readonly THREE.Points[],
    readItem: (mesh: THREE.Points) => T | null,
    keep: ((point: THREE.Vector3) => boolean) | null = null,
  ): T | null {
    if (meshes.length === 0) return null
    const { width, height } = size()
    const candidates: Array<ScreenPickCandidate<T>> = []
    for (const mesh of meshes) {
      const item = readItem(mesh)
      if (item === null) continue
      const position = mesh.geometry.getAttribute('position')
      if (!position || position.count === 0) continue
      mesh.updateWorldMatrix(true, false)
      let best = Infinity
      for (let at = 0; at < position.count; at += 1) {
        pickScratch
          .set(position.getX(at), position.getY(at), position.getZ(at))
          .applyMatrix4(mesh.matrixWorld)
        if (keep !== null && !keep(pickScratch)) continue
        const screen = projectRaw(pickScratch, width, height)
        if (screen === null) continue
        const d = distSq(screen.x, screen.y, offsetX, offsetY)
        if (d < best) best = d
      }
      if (best !== Infinity) candidates.push({ item, distSq: best })
    }
    return pickNearestWithinRadius(radiusPx, candidates)
  }

  /** その画素で取れる折れ線。`readItem` / `polylineOf` の約束は `pickPointsAt` と同じ。 */
  function pickLinesAt<L extends THREE.Object3D, T>(
    offsetX: number,
    offsetY: number,
    radiusPx: number,
    lines: readonly L[],
    readItem: (line: L) => T | null,
    polylineOf: (line: L) => PickPolyline | null,
  ): T | null {
    if (lines.length === 0) return null
    const project = projector()
    const candidates: Array<ScreenPickCandidate<T>> = []
    for (const line of lines) {
      const item = readItem(line)
      if (item === null) continue
      const polyline = polylineOf(line)
      if (polyline === null) continue
      const hit = closestOnPolyline(offsetX, offsetY, polyline, project)
      if (hit === null) continue
      candidates.push({ item, distSq: hit.distSq })
    }
    return pickNearestWithinRadius(radiusPx, candidates)
  }

  /** 辺の上の点の画面座標(実際にその画素でその辺が取れるものだけ)。 */
  function projectEdge(bodyId: string, edgeIndex: number): { x: number; y: number } | null {
    // 端点は避ける(隣の辺と共有していて隣が取れることがある)。中ほどから順に試す。
    for (const fraction of [0.5, 0.35, 0.65, 0.2, 0.8, 0.1, 0.9]) {
      const point = features.edgePoint({ bodyId, edgeIndex }, fraction)
      if (point === null) return null
      const screen = toScreen(point)
      if (screen === null) continue
      const hit = resolveHit(screen.x, screen.y).edge
      if (hit && hit.bodyId === bodyId && hit.edgeIndex === edgeIndex) return screen
    }
    return null
  }

  /** 頂点の画面座標(実際にその画素でその頂点が取れるときだけ)。 */
  function projectVertex(bodyId: string, vertexIndex: number): { x: number; y: number } | null {
    const point = features.vertexPoint({ bodyId, vertexIndex })
    if (point === null) return null
    const screen = toScreen(point)
    if (screen === null) return null
    const hit = resolveHit(screen.x, screen.y).vertex
    return hit && hit.bodyId === bodyId && hit.vertexIndex === vertexIndex ? screen : null
  }

  // ------------------------------------------------------------------ C: スケッチ
  //
  // 描くのは線と領域だけ。スケッチ平面そのもの(無限に広がる面)を描くと奥のソリッドが隠れる。
  // ソリッドとは別のグループ(`sketchRoot`。宣言は `root` の隣)に持つ。`update()`/`prune()` は `root` しか触らない。

  const sketchLineResolution = new THREE.Vector2(1, 1)
  const sketchMaterial = createSketchLineMaterial(SKETCH_COLOR, sketchLineResolution)
  const sketchGhostMaterial = createSketchLineMaterial(SKETCH_GHOST_COLOR, sketchLineResolution)
  const sketchGhostWarnMaterial = createSketchLineMaterial(SKETCH_GHOST_WARN_COLOR, sketchLineResolution)
  // 点の図形の幽霊(折れ線を持たないので取っ手と同じ大きさの印で描く)
  const ghostPointMaterialOf = (color: number) =>
    new THREE.PointsMaterial({ color, size: SKETCH_HANDLE_PIXELS * 2, sizeAttenuation: false, depthTest: false })
  const sketchGhostPointMaterial = ghostPointMaterialOf(SKETCH_GHOST_COLOR)
  const sketchGhostWarnPointMaterial = ghostPointMaterialOf(SKETCH_GHOST_WARN_COLOR)

  /**
   * スケッチの線・点の材質は (選択状態 × ホバー × 描き方) のキャッシュから取る(`faceMaterialFor` と同じ形)。
   * 描き方: 編集中のスケッチ(作図のため)と選択中の図形(AI が選んだ裏のものも人に見える)は立体に透かす(`'xray'`)。
   * それ以外は立体の裏に回ると隠れる(`'depth'`。docs/DESIGN_DECISIONS.md「見えるものはすべてさわれる」。
   * 指せるかの判定 `occlusion.ts` と揃う)。
   */
  type SketchMaterialState = 'plain' | 'selected'
  type SketchDepth = 'xray' | 'depth'
  type SketchLineMaterial = ReturnType<typeof createSketchLineMaterial>
  const sketchStateKey = (state: SketchMaterialState, hovered: boolean): string => `${state}:${hovered ? 1 : 0}`
  // `plain` の非ホバーの透かしは既存の `sketchMaterial` を種にする(インスタンスを分裂させない)。
  const sketchLineMaterials = variantCache(
    (state: SketchMaterialState, hovered: boolean, depth: SketchDepth) => `${sketchStateKey(state, hovered)}:${depth}`,
    (state, hovered, depth) => {
      const base = state === 'selected' ? SKETCH_SELECTED_COLOR : SKETCH_COLOR
      const material = createSketchLineMaterial(hovered ? lighten(base, HOVER_LIGHTEN) : base, sketchLineResolution)
      return depth === 'depth' ? withLineDepthBias(material, DEPTH_BIAS_LAYER.sketch) : material
    },
    [['plain:0:xray', sketchMaterial]],
  )
  function sketchLineMaterialFor(state: SketchMaterialState, hovered: boolean, depth: SketchDepth = 'xray'): SketchLineMaterial {
    return sketchLineMaterials.get(state, hovered, depth)
  }

  // 深度を書かない(頂点マーカーと同じ。線の上に必ず出す)。
  const sketchHandleMaterials = variantCache(sketchStateKey, (state, hovered) => {
    const base = state === 'selected' ? SKETCH_SELECTED_COLOR : SKETCH_HANDLE_COLOR
    return new THREE.PointsMaterial({
      color: hovered ? lighten(base, HOVER_LIGHTEN) : base,
      size: SKETCH_HANDLE_PIXELS * 2,
      sizeAttenuation: false,
      depthTest: false,
    })
  })
  function sketchHandleMaterialFor(state: SketchMaterialState, hovered: boolean): THREE.PointsMaterial {
    return sketchHandleMaterials.get(state, hovered)
  }
  // 編集していないスケッチの点の図形: 取っ手と同じ見た目で、立体の裏に回ると隠れる(線と同じ深度バイアス)
  const sketchPointEntityMaterial = withDepthBias(
    new THREE.PointsMaterial({
      color: SKETCH_HANDLE_COLOR,
      size: SKETCH_HANDLE_PIXELS * 2,
      sizeAttenuation: false,
      depthWrite: false,
    }),
    DEPTH_BIAS_LAYER.sketch,
  )
  /**
   * 領域の材質は (選択 × ホバー) のキャッシュから取る。不透明度は選択で変わる(非選択 0.35 / 選択 0.55)。
   * 選択中は立体に透かし、それ以外は立体の裏に回ると隠れる(線と同じ。押し出した元の領域は押し出した面と
   * 厳密に同一平面なので、深度バイアスで手前へ寄せる)。
   */
  const regionMaterials = variantCache(
    (selected: boolean, hovered: boolean) => `${selected ? 1 : 0}:${hovered ? 1 : 0}`,
    (selected, hovered) => {
      const base = selected ? REGION_SELECTED_COLOR : REGION_COLOR
      const material = new THREE.MeshBasicMaterial({
        color: hovered ? lighten(base, HOVER_LIGHTEN) : base,
        side: THREE.DoubleSide,
        transparent: true,
        opacity: selected ? 0.55 : 0.35,
        depthTest: !selected,
        depthWrite: false,
      })
      return selected ? material : withDepthBias(material, DEPTH_BIAS_LAYER.sketch)
    },
  )

  interface SketchScene {
    /** 作ったときの鍵(`sketchBuildKey`)。同じ鍵の指示が来たら作り直さず、選択の印だけ付け直す。 */
    builtKey: string
    frame: SketchDraw['frame']
    plane: SketchDraw['plane']
    /** シーンに足したもの(除去と破棄のため。ピック用の配列は別に持つ)。 */
    batch: SceneBatch
    regionMeshes: THREE.Mesh[]
    /** 図形の (u,v) の折れ線と点の図形の位置(視点合わせの中心を出すのに使う)。 */
    points: Array<[number, number]>
    /** 図形1本ごとの折れ線の点数(実機検査の `sketchPolylineCounts` が読む)。 */
    polylineCounts: number[]
    /** 編集中のスケッチとして作ったか(`SketchDraw.editing`)。 */
    editing: boolean
    /**
     * 点のマーカー(編集中のスケッチだけ。1点1オブジェクト)。当たった点からコアの参照へ戻すため `userData` に `entityId` と
     * `point`(xキー)を持つ(まとめて1つの `Points` にすると戻れない)。
     */
    handleMeshes: THREE.Points[]
    /**
     * 編集していないスケッチの点(ピックの候補だけ。オブジェクトは作らない: 取り込んだ SVG はベジエの制御点が数千あり、
     * 全スケッチに1点1オブジェクトを置くと毎フレームの走査が重い)。描くのは選択中(`markSelected`)とホバー中(`markHovered`)だけ。
     */
    pickPoints: Array<{ point: THREE.Vector3; entityId: string; key: string }>
    /** 選択中の点(`entityId|xキー`)。 */
    selectedPointKeys: Set<string>
    markSelected: THREE.Points | null
    markHovered: THREE.Points | null
    /** 図形の折れ線(文書の図形だけ。`userData.entityId` を持つ)。辺のピック対象。 */
    handleLines: SketchFatLine[]
    /** 文書の図形(`handles`)。スナップ座標の解決に使う。 */
    handles: SketchDraw['handles']
    /**
     * 直線区間(line/rectangleの辺)どうしの交点(`sketchIntersections`。曲線は対象外)。
     * `sketchCornerAt` が本物の点・`visualCorners` と同じ優先度で拾うために持つ。
     */
    intersections: ReturnType<typeof sketchIntersections>
    /** 線・点・領域の外接球(世界座標。画面でカーソルから遠いスケッチを歩かない)。 */
    sphere: THREE.Sphere
  }
  const sketches = new Map<string, SketchScene>()
  let ghost: SceneBatch | null = null
  let activeSketchId: string | null = null

  /** スケッチモードの外で指せる種類(`setPickKinds`)。既定は全部。 */
  let pickKinds = new Set<PickKind>(['face', 'edge', 'vertex', 'region', 'sketchPoint', 'sketchLine'])

  function setPickKinds(kinds: readonly PickKind[]): void {
    const next = new Set(kinds)
    if (next.size === pickKinds.size && [...next].every((kind) => pickKinds.has(kind))) return
    pickKinds = next
    pickChanged() // ゲートの開閉で「指せるもの」が変わる(止まったままでも解決し直す)
  }

  /** スケッチ平面上の (u,v) を3Dの点へ(コアの `kernel_sketch._to_pnt` と同じ規則)。 */
  function toWorld(frame: SketchDraw['frame'], u: number, v: number): THREE.Vector3 {
    return planePoint(frame.origin, frame.u, frame.v, u, v)
  }

  function buildEntityLine(
    frame: SketchDraw['frame'],
    entity: { kind: string; params: Record<string, number> },
    material: SketchLineMaterial,
  ): { object: SketchFatLine; geometry: THREE.BufferGeometry; points: Array<[number, number]> } | null {
    // 今の画面の粗さで折る(`resolution.applied()`)
    const points = entityPoints(entity.kind, entity.params, resolution.applied())
    if (points.length < 2) return null
    const worldPoints = points.map(([u, v]) => toWorld(frame, u, v))
    const built = buildSketchLine(worldPoints, material)
    if (!built) return null
    built.object.userData.polylineWorld = worldPoints
    return { object: built.object, geometry: built.geometry, points }
  }

  function clearSketch(sketchId: string): void {
    const existing = sketches.get(sketchId)
    if (!existing) return
    existing.batch.clear()
    existing.markSelected = replaceMark(existing, existing.markSelected, new Set(), sketchHandleMaterialFor('selected', false))
    existing.markHovered = replaceMark(existing, existing.markHovered, new Set(), sketchHandleMaterialFor('plain', true))
    sketches.delete(sketchId)
  }

  /** 直近に受け取った描画指示。解像度が変わったとき折り直すために控える。 */
  let lastSketchDraws: SketchDraw[] = []

  /** 控えた指示で描き直す(`resolutionTracker` が解像度を採り直したときに呼ぶ)。中身が無ければ何もしない。 */
  function redrawSketches(): void {
    invalidate()
    if (lastSketchDraws.length > 0) showSketches(lastSketchDraws, true)
  }

  /**
   * スケッチを作り直すかの鍵: コアの版(描くものが変わると上がる)と、編集中か(描き方と点のマーカーが変わる)。
   * 選択はここに入れない(作り直さずに `restyleSketch` で塗り直す)。
   */
  function sketchBuildKey(entry: SketchDraw): string {
    return `${entry.revision}|${entry.editing ? 'edit' : ''}`
  }

  /** 作り直さずに選択の印(`userData.selected` と点の見え方)だけを付け直す。材質は呼び手が塗る。 */
  function restyleSketch(scene: SketchScene, entry: SketchDraw): void {
    const selected = new Set(entry.selectedRegions)
    for (const mesh of scene.regionMeshes) mesh.userData.selected = selected.has(mesh.userData.regionIndex as number)
    const pickedWhole = new Set(entry.selectedHandles.filter((item) => item.point === null).map((item) => item.entityId))
    const pickedPoints = new Set(
      entry.selectedHandles.filter((item) => item.point !== null).map((item) => `${item.entityId}|${item.point}`),
    )
    for (const line of scene.handleLines) line.userData.selected = pickedWhole.has(line.userData.entityId as string)
    for (const mesh of scene.handleMeshes) {
      const entityId = mesh.userData.entityId as string
      mesh.userData.selected = pickedWhole.has(entityId) || pickedPoints.has(`${entityId}|${mesh.userData.point as string}`)
    }
    scene.selectedPointKeys = pickedPoints
    if (!scene.editing) {
      scene.markSelected = replaceMark(scene, scene.markSelected, pickedPoints, sketchHandleMaterialFor('selected', false))
    }
  }

  /**
   * 編集していないスケッチの点の印を差し替える(`keys` は `entityId|xキー`)。空なら消す。印は立体に透かす
   * (選択中の点は裏でも見え、ホバー中の点は見えている点にしか乗らない)。
   */
  function replaceMark(
    scene: SketchScene,
    previous: THREE.Points | null,
    keys: ReadonlySet<string>,
    material: THREE.PointsMaterial,
  ): THREE.Points | null {
    if (previous !== null) {
      sketchRoot.remove(previous)
      previous.geometry.dispose()
    }
    const positions: number[] = []
    for (const item of scene.pickPoints) {
      if (keys.has(`${item.entityId}|${item.key}`)) positions.push(item.point.x, item.point.y, item.point.z)
    }
    if (positions.length === 0) return null
    const geometry = new THREE.BufferGeometry()
    geometry.setAttribute('position', new THREE.BufferAttribute(new Float32Array(positions), 3))
    geometry.computeBoundingSphere()
    const mark = new THREE.Points(geometry, material)
    mark.renderOrder = LAYER.sketchPoint
    sketchRoot.add(mark)
    return mark
  }

  /** 実機検査の数え上げ(`sketchRebuilds`): スケッチを作り直した回数。選択だけの配信で増えないことを見る。 */
  let sketchRebuildCount = 0

  /** `force` は版が同じでも作り直す(画面の粗さが変わって折り直すとき)。 */
  function showSketches(entries: SketchDraw[], force = false): void {
    lastSketchDraws = entries
    const keep = new Set(entries.map((entry) => entry.sketchId))
    // 消した・隠したスケッチも変化に数える(ホバーの印・内容の大きさ・視錐台を引き直す)。
    let rebuilt = false
    for (const sketchId of [...sketches.keys()]) {
      if (keep.has(sketchId)) continue
      clearSketch(sketchId)
      rebuilt = true
    }
    for (const entry of entries) {
      const existing = sketches.get(entry.sketchId)
      if (!force && existing !== undefined && existing.builtKey === sketchBuildKey(entry)) {
        restyleSketch(existing, entry)
        continue
      }
      rebuilt = true
      sketchRebuildCount += 1
      clearSketch(entry.sketchId)
      const batch = createBatch(sketchRoot)
      const regionMeshes: THREE.Mesh[] = []
      const points: Array<[number, number]> = []
      const polylineCounts: number[] = []
      const handleMeshes: THREE.Points[] = []
      const handleLines: SketchFatLine[] = []
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
        const mesh = new THREE.Mesh(geometry, regionMaterials.get(selected.has(region.index), false))
        mesh.userData.sketchId = entry.sketchId
        mesh.userData.regionIndex = region.index
        // 選択かどうかを印として残す(材質から読み戻すとホバー色が付いた瞬間に選択が分からなくなる)
        mesh.userData.selected = selected.has(region.index)
        regionMeshes.push(batch.add(mesh, geometry))
      }

      const editing = entry.editing
      // 旧形式の反復の複製は `entities` にしか無い(持つときだけ描く。`SketchDraw.livePatterns`)
      if (entry.livePatterns) {
        const material = editing ? sketchMaterial : sketchLineMaterialFor('plain', false, 'depth')
        for (const entity of entry.entities) {
          const built = buildEntityLine(entry.frame, entity, material)
          if (!built) continue
          batch.add(built.object, built.geometry)
          points.push(...built.points)
          polylineCounts.push(built.points.length)
        }
      }

      // 文書の図形(`handles`)。ここに置くのは「指せるもの」(旧形式の反復の複製は `entity_id` を持たず指せない)。
      const pickPoints: SketchScene['pickPoints'] = []
      const pointEntityPositions: number[] = []
      for (const handle of entry.handles) {
        const selected = pickedWhole.has(handle.entityId)
        const built = buildEntityLine(
          entry.frame,
          handle,
          sketchLineMaterialFor(selected ? 'selected' : 'plain', false, editing || selected ? 'xray' : 'depth'),
        )
        if (built) {
          built.object.renderOrder = LAYER.sketchLine
          built.object.userData.entityId = handle.entityId
          // 選択かどうかの印(領域の `userData.selected` と同じ理由)
          built.object.userData.selected = selected
          handleLines.push(batch.add(built.object, built.geometry))
          if (!entry.livePatterns) {
            points.push(...built.points)
            polylineCounts.push(built.points.length)
          }
        }
        // 点の図形は折れ線を持たないので、視点合わせの範囲へ位置そのものを入れる
        if (handle.kind === 'point') points.push([handle.params.cx, handle.params.cy])
        if (!editing) {
          // 点は普段描かない(ホバー中・選択中だけ `replaceMark`)。点の図形だけは編集していないスケッチでも常に描く
          // (本人決定 2026-10-18。docs/DESIGN_DECISIONS.md)。
          for (const point of entityHandles(handle.kind, handle.params)) {
            if (!Number.isFinite(point.u) || !Number.isFinite(point.v)) continue
            const world = toWorld(entry.frame, point.u, point.v)
            pickPoints.push({ point: world, entityId: handle.entityId, key: point.key })
            if (handle.kind === 'point') pointEntityPositions.push(world.x, world.y, world.z)
          }
          continue
        }
        // 点が互いに独立して動く種別だけ常時表示する(entityHasIndependentPoints)。
        // rectangle/circle/arc は点が1個で、その点だけ動かすのと図形全体を動かすのが
        // move_sketch_entities 内で同じ処理になるため、ホバー/選択のときだけ見せる
        // (ピック可能な座標は変えない。`pickPointsAt` は mesh.visible を見ない)。
        // 点の図形は点の印のほかに描くものが無いので常に見せる。
        const showPointsAtRest = handle.kind === 'point' || entityHasIndependentPoints(handle.kind)
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
            sketchHandleMaterialFor(picked ? 'selected' : 'plain', false),
          )
          mesh.renderOrder = LAYER.sketchPoint // 深度を書かないので、描く順序で線より手前に出す
          mesh.visible = showPointsAtRest || picked
          mesh.userData.entityId = handle.entityId
          mesh.userData.point = point.key
          mesh.userData.selected = picked
          mesh.userData.showAtRest = showPointsAtRest
          handleMeshes.push(batch.add(mesh, geometry))
        }
      }
      if (pointEntityPositions.length > 0) {
        const geometry = new THREE.BufferGeometry()
        geometry.setAttribute('position', new THREE.BufferAttribute(new Float32Array(pointEntityPositions), 3))
        geometry.computeBoundingSphere()
        const mesh = new THREE.Points(geometry, sketchPointEntityMaterial)
        mesh.renderOrder = LAYER.sketchPoint
        batch.add(mesh, geometry)
      }
      const bounds = new THREE.Box3()
      for (const line of handleLines) {
        for (const point of line.userData.polylineWorld as THREE.Vector3[]) bounds.expandByPoint(point)
      }
      for (const mesh of regionMeshes) bounds.expandByObject(mesh)
      for (const item of pickPoints) bounds.expandByPoint(item.point)
      const scene: SketchScene = {
        builtKey: sketchBuildKey(entry),
        frame: entry.frame,
        plane: entry.plane,
        batch,
        regionMeshes,
        points,
        polylineCounts,
        editing,
        handleMeshes,
        pickPoints,
        selectedPointKeys: pickedPoints,
        markSelected: null,
        markHovered: null,
        handleLines,
        handles: entry.handles,
        intersections: sketchIntersections(entry.handles),
        sphere: bounds.isEmpty() ? new THREE.Sphere(new THREE.Vector3(), -1) : bounds.getBoundingSphere(new THREE.Sphere()),
      }
      if (!editing) scene.markSelected = replaceMark(scene, null, pickedPoints, sketchHandleMaterialFor('selected', false))
      sketches.set(entry.sketchId, scene)
    }
    if (!rebuilt) {
      applyRegionMaterials()
      applySketchHoverMaterials()
      return
    }
    invalidateHover() // 領域のメッシュを作り直したので印を捨てる
    refreshContentDiagonal() // 見えているものの大きさが変わった(解像度の上下限に効く)
    updateClipPlanes() // 内容が変わった → 視錐台を引き直す
    refreshGridSketchFrame() // 編集中スケッチのframeがここで初めて届くことがある
  }

  /** 領域の材質を決める。選択とホバーの両方を見る唯一の場所。 */
  function applyRegionMaterials(): void {
    for (const [sketchId, entry] of sketches) {
      for (const mesh of entry.regionMeshes) {
        const regionIndex = mesh.userData.regionIndex as number
        const isHit = mesh.userData.selected === true
        const isHovered =
          hoverRegion !== null &&
          hoverRegion.sketchId === sketchId &&
          hoverRegion.index === regionIndex
        mesh.material = regionMaterials.get(isHit, isHovered)
      }
    }
  }

  /**
   * スケッチの線・点の材質を決める。選択とホバーの両方を見る唯一の場所。`handleLines`/`handleMeshes` を
   * 歩いて `.material` を差し替えるだけ。点は必ず辺の上に乗るので、点がホバーされているときは
   * 線を光らせない(`hoverSketchEntity.point === null` を線側の条件に入れて排他にする)。
   */
  function applySketchHoverMaterials(): void {
    for (const [sketchId, entry] of sketches) {
      for (const line of entry.handleLines) {
        const entityId = line.userData.entityId as string
        const selected = line.userData.selected === true
        const hovered =
          hoverSketchEntity !== null &&
          hoverSketchEntity.sketchId === sketchId &&
          hoverSketchEntity.entityId === entityId &&
          hoverSketchEntity.point === null
        line.material = sketchLineMaterialFor(
          selected ? 'selected' : 'plain',
          hovered,
          entry.editing || selected ? 'xray' : 'depth',
        )
      }
      if (!entry.editing) {
        const hoveredPoint =
          hoverSketchEntity !== null && hoverSketchEntity.sketchId === sketchId && hoverSketchEntity.point !== null
            ? new Set([`${hoverSketchEntity.entityId}|${hoverSketchEntity.point}`])
            : new Set<string>()
        if (hoveredPoint.size > 0 || entry.markHovered !== null) {
          const selected = [...hoveredPoint].some((key) => entry.selectedPointKeys.has(key))
          entry.markHovered = replaceMark(
            entry,
            entry.markHovered,
            hoveredPoint,
            sketchHandleMaterialFor(selected ? 'selected' : 'plain', true),
          )
        }
      }
      for (const mesh of entry.handleMeshes) {
        const entityId = mesh.userData.entityId as string
        const point = mesh.userData.point as string
        const selected = mesh.userData.selected === true
        const showAtRest = mesh.userData.showAtRest === true
        const hovered =
          hoverSketchEntity !== null &&
          hoverSketchEntity.sketchId === sketchId &&
          hoverSketchEntity.entityId === entityId &&
          hoverSketchEntity.point === point
        mesh.material = sketchHandleMaterialFor(selected ? 'selected' : 'plain', hovered)
        mesh.visible = showAtRest || selected || hovered
      }
    }
  }

  /**
   * いま編集中のスケッチ平面のframeをグリッドへ渡す(モード外は null)。`setActiveSketch` と
   * `showSketches` の両方から呼ぶ: モードに入った時点では描画指示がまだ届かずframeを知らないことがあり、
   * 届いた時点で呼び直さないと基準平面へのスケッチ中もXY床が出たままになる。
   */
  function refreshGridSketchFrame(): void {
    const target = activeSketchId === null ? undefined : sketches.get(activeSketchId)
    const face =
      target !== undefined && target.plane.kind === 'face'
        ? { bodyId: target.plane.bodyId, faceIndex: target.plane.faceIndex }
        : null
    grid.setSketchFrame(target?.frame ?? null, face)
  }

  function clearGhost(): void {
    if (!ghost) return
    ghost.clear()
    ghost = null
  }

  function showSketchGhosts(
    sketchId: string,
    entities: Array<{ kind: string; params: Record<string, number> }>,
    tone: 'normal' | 'warn' = 'normal',
  ): void {
    clearGhost()
    const target = sketches.get(sketchId)
    if (entities.length === 0 || !target) return
    const batch = createBatch(sketchRoot)
    const material = tone === 'warn' ? sketchGhostWarnMaterial : sketchGhostMaterial
    const pointPositions: number[] = []
    for (const entity of entities) {
      if (entity.kind === 'point') {
        const world = toWorld(target.frame, entity.params.cx, entity.params.cy)
        pointPositions.push(world.x, world.y, world.z)
        continue
      }
      const built = buildEntityLine(target.frame, entity, material)
      if (!built) continue
      batch.add(built.object, built.geometry)
    }
    if (pointPositions.length > 0) {
      const geometry = new THREE.BufferGeometry()
      geometry.setAttribute('position', new THREE.BufferAttribute(new Float32Array(pointPositions), 3))
      geometry.computeBoundingSphere()
      const mesh = new THREE.Points(geometry, tone === 'warn' ? sketchGhostWarnPointMaterial : sketchGhostPointMaterial)
      mesh.renderOrder = LAYER.sketchPoint
      batch.add(mesh, geometry)
    }
    if (batch.objects.length === 0) return
    ghost = batch
  }

  /**
   * スケッチに入る直前の視点(抜けるときに戻す)。モード外では null。
   * `lookAtSketch` は図形の大きさに寄るので、戻さないと小さな図形のとき大きな板が画面外に残る。
   * 「入る前の視点」へ戻すのは、入る前に自分で寄せた視点を捨てないため。
   * `framedAtEntry` は入った時点でもう何か描かれていたかを覚える: 最初の板はスケッチの中で生まれる
   * (`update` の初回 `frameToContent`)ので、そのときの「入る前」は何も無い文書の視点で、
   * 戻すと形から離れる。その1回だけは戻さない。
   */
  let cameraBeforeSketch:
    | {
        position: THREE.Vector3
        target: THREE.Vector3
        up: THREE.Vector3
        /** 入る前の見かけの大きさ(`viewHalfHeight`)。正投影では寄り引きが視錐台の大きさなので、位置だけでは戻せない。 */
        halfHeight: number
        framedAtEntry: boolean
      }
    | null = null

  function setActiveSketch(sketchId: string | null): void {
    const wasActive = activeSketchId !== null
    if (activeSketchId !== sketchId) pickChanged() // モードの出入りで優先順位ごと変わる
    // `hoverAt` を捨てる: 入る/出る/切り替えでカメラが飛ぶと同じ画面座標が別の3D点を指し、
    // `grid.followCursor` に食わせると旧位置が新しい平面の外へ再投影されて縁のグリッドが広がる
    // (実測: 面スケッチへの切替直後、内容だけの範囲より広い値が一瞬出た)。`onPointerLeave` と同じ扱い。
    hoverAt = null
    activeSketchId = sketchId
    refreshGridSketchFrame() // 出すグリッドの平面がモードの出入りで変わる
    if (sketchId !== null) {
      // 入る「とき」だけ控える(`lookAtSketch` は「正面から見る」でも呼ばれ、そこで控えると戻り先が
      // 寄った視点になる)。モード中に別のスケッチへ移っても戻り先は最初のまま。
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
    // 上方向を既定へ戻す(`lookAtSketch` がv軸へ倒したままだと、抜けた後のオービットの傾きが変わる)。
    camera.up.copy(WORLD_UP)
    const previous = cameraBeforeSketch
    cameraBeforeSketch = null
    if (previous !== null && previous.framedAtEntry) {
      camera.position.copy(previous.position)
      camera.up.copy(previous.up)
      controls.target.copy(previous.target)
      // 見かけの大きさを戻すのは正投影のときだけ(透視は上の位置の復元で戻り終わっている)。
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
    const ray = rayAt(offsetX, offsetY)
    const frame = target.frame
    const origin = frameVector(frame.origin)
    const normal = frameVector(frame.normal).normalize()
    const plane = new THREE.Plane().setFromNormalAndCoplanarPoint(normal, origin)
    const hit = ray.intersectPlane(plane, new THREE.Vector3())
    if (hit === null) return null
    const delta = hit.sub(origin)
    // `frame.u` / `frame.v` は正規化しない(`toWorld` と同じ扱い)。
    return { u: delta.dot(frameVector(frame.u)), v: delta.dot(frameVector(frame.v)) }
  }

  /**
   * 見た目だけの角(`entityVisualCorners`。長方形の残り3隅など)と直線どうしの交点(`target.intersections`)を、
   * 本物の点と同じ半径・同じ優先度で拾う。`point` は常に null: 独立した参照を持たないので、辺に当たったのと
   * 同じ図形全体の扱い(`move_sketch_entities` の的にならない)。交点の参照は片方(`entityIds[0]`)だけ返す。
   * `sketchEntityAt` の点パスと辺パスの間だけから呼ぶこと。
   */
  function sketchCornerAt(offsetX: number, offsetY: number, target: SketchScene): SketchEntityHit | null {
    const sketchId = activeSketchId
    if (sketchId === null) return null
    const { width, height } = size()
    const candidates: Array<ScreenPickCandidate<SketchEntityHit>> = []
    for (const entry of target.handles) {
      for (const [u, v] of entityVisualCorners(entry.kind, entry.params)) {
        const screen = projectRaw(toWorld(target.frame, u, v), width, height)
        if (screen === null) continue
        candidates.push({
          item: { sketchId, entityId: entry.entityId, point: null, snapAt: [u, v] },
          distSq: distSq(screen.x, screen.y, offsetX, offsetY),
        })
      }
    }
    for (const intersection of target.intersections) {
      const screen = projectRaw(toWorld(target.frame, intersection.u, intersection.v), width, height)
      if (screen === null) continue
      candidates.push({
        item: {
          sketchId,
          entityId: intersection.entityIds[0],
          point: null,
          snapAt: [intersection.u, intersection.v],
        },
        distSq: distSq(screen.x, screen.y, offsetX, offsetY),
      })
    }
    return pickNearestWithinRadius(SKETCH_HANDLE_PIXELS, candidates)
  }

  /**
   * 直線区間の中点(`entityMidpoints`)を、本物の点と同じ半径(`SKETCH_HANDLE_PIXELS`)で1段低い優先度
   * (点・角・交点より下、辺より上)で拾う。`snapDirection` は
   * カーソル強調が画面角度を出すのに使う。`sketchEntityAt` の点パス・角パスの後、辺パスの前からだけ呼ぶこと。
   */
  function sketchMidpointAt(offsetX: number, offsetY: number, target: SketchScene): SketchEntityHit | null {
    const sketchId = activeSketchId
    if (sketchId === null) return null
    const { width, height } = size()
    const candidates: Array<ScreenPickCandidate<SketchEntityHit>> = []
    for (const entry of target.handles) {
      for (const midpoint of entityMidpoints(entry.kind, entry.params)) {
        const screen = projectRaw(toWorld(target.frame, midpoint.u, midpoint.v), width, height)
        if (screen === null) continue
        candidates.push({
          item: {
            sketchId,
            entityId: entry.entityId,
            point: null,
            snapAt: [midpoint.u, midpoint.v],
            midpoint: true,
            snapDirection: midpoint.direction,
          },
          distSq: distSq(screen.x, screen.y, offsetX, offsetY),
        })
      }
    }
    return pickNearestWithinRadius(SKETCH_HANDLE_PIXELS, candidates)
  }

  /**
   * その画素で当たるスケッチの図形(アクティブなスケッチだけ)。点が先・角/交点が次・中点がその次・辺が最後:
   * 点・角・交点は必ずどれかの辺の上に乗っているので、辺を先に見ると掴めない。当たり半径は画面上の画素
   * (`edgeAt` / `vertexAt` と同じ `pickPointsAt` / `pickLinesAt`)。
   */
  function sketchEntityAt(offsetX: number, offsetY: number): SketchEntityHit | null {
    if (activeSketchId === null) return null
    const target = sketches.get(activeSketchId)
    if (!target) return null
    const sketchId = activeSketchId
    const handle = pickPointsAt(
      offsetX,
      offsetY,
      SKETCH_HANDLE_PIXELS,
      target.handleMeshes,
      (mesh): SketchEntityHit | null => {
        const { entityId, point } = mesh.userData
        if (typeof entityId !== 'string' || typeof point !== 'string') return null
        return { sketchId, entityId, point }
      },
    )
    if (handle !== null) return handle
    const corner = sketchCornerAt(offsetX, offsetY, target)
    if (corner !== null) return corner
    const midpoint = sketchMidpointAt(offsetX, offsetY, target)
    if (midpoint !== null) return midpoint
    return pickLinesAt(
      offsetX,
      offsetY,
      EDGE_PICK_PIXELS,
      target.handleLines,
      (line): SketchEntityHit | null => {
        const entityId = line.userData.entityId
        if (typeof entityId !== 'string') return null
        return { sketchId, entityId, point: null }
      },
      sketchPolyline,
    )
  }

  /**
   * 今クリックで当たってよい領域メッシュ。スケッチモード中はそのスケッチの領域だけ、通常モードは指せる間
   * (`setPickKinds` に `'region'`)だけ見えている全スケッチの領域。
   */
  function regionPickTargets(): THREE.Mesh[] {
    if (activeSketchId !== null) return sketches.get(activeSketchId)?.regionMeshes ?? []
    if (!pickKinds.has('region')) return []
    const meshes: THREE.Mesh[] = []
    for (const target of sketches.values()) meshes.push(...target.regionMeshes)
    return meshes
  }

  /** その画素の視線が当たる領域(近い順)と、当たった点・距離。 */
  function regionHitsAt(
    offsetX: number,
    offsetY: number,
  ): Array<{ region: { sketchId: string; index: number }; point: THREE.Vector3; distance: number }> {
    const targets = regionPickTargets()
    if (targets.length === 0) return []
    rayAt(offsetX, offsetY)
    const found = []
    for (const hit of raycaster.intersectObjects(targets, false)) {
      const { sketchId, regionIndex } = hit.object.userData
      if (typeof sketchId !== 'string' || typeof regionIndex !== 'number') continue
      found.push({ region: { sketchId, index: regionIndex }, point: hit.point, distance: hit.distance })
    }
    return found
  }

  function regionAt(offsetX: number, offsetY: number): { sketchId: string; index: number } | null {
    // 近い方が勝つ(`intersectObjects` は距離順)
    return regionHitsAt(offsetX, offsetY)[0]?.region ?? null
  }

  /** 通常モードの的の候補1件(`resolveHit` の点・線の段)。 */
  interface PickCandidate {
    hit: Partial<Omit<PickEvent, 'mode'>>
    /** 狙われている3Dの点(遮蔽の判定がここへ視線を引く)。 */
    point: THREE.Vector3
    /** 遮蔽の許し[mm](`occlusion.slack`)。 */
    slack: number
  }

  /** 編集していないスケッチの点・線のうち、当たり半径に入るもの(通常モード)。 */
  function sketchPickCandidates(
    x: number,
    y: number,
    project: ScreenProjector,
  ): { points: Array<RankedCandidate<PickCandidate>>; lines: Array<RankedCandidate<PickCandidate>> } {
    const points: Array<RankedCandidate<PickCandidate>> = []
    const lines: Array<RankedCandidate<PickCandidate>> = []
    const wantPoints = pickKinds.has('sketchPoint')
    const wantLines = pickKinds.has('sketchLine')
    if (!wantPoints && !wantLines) return { points, lines }
    const pointLimit = SKETCH_HANDLE_PIXELS * SKETCH_HANDLE_PIXELS
    const lineLimit = EDGE_PICK_PIXELS * EDGE_PICK_PIXELS
    for (const [sketchId, scene] of sketches) {
      if (scene.editing) continue
      if (!occlusion.sphereNear(scene.sphere, x, y, SKETCH_HANDLE_PIXELS, project)) continue
      if (wantPoints) {
        for (const item of scene.pickPoints) {
          const hit = screenHitOfPoint(x, y, item.point, project)
          if (hit === null || hit.distSq > pointLimit) continue
          points.push({
            item: {
              hit: { sketchEntity: { sketchId, entityId: item.entityId, point: item.key } },
              point: hit.point,
              slack: occlusion.slack(hit.point, project.height),
            },
            distSq: hit.distSq,
            solid: false,
            key: `s|${sketchId}|${item.entityId}|${item.key}`,
          })
        }
      }
      if (wantLines) {
        for (const line of scene.handleLines) {
          const entityId = line.userData.entityId
          const polyline = sketchPolyline(line)
          if (typeof entityId !== 'string' || polyline === null) continue
          const hit = closestOnPolyline(x, y, polyline, project)
          if (hit === null || hit.distSq > lineLimit) continue
          lines.push({
            item: {
              hit: { sketchEntity: { sketchId, entityId, point: null } },
              point: hit.point,
              slack: occlusion.slack(hit.point, project.height),
            },
            distSq: hit.distSq,
            solid: false,
            key: `s|${sketchId}|${entityId}`,
          })
        }
      }
    }
    return { points, lines }
  }

  /** 並べた候補のうち最初の見えているもの(`occlusion.ts`)。 */
  function firstVisible(ranked: Array<RankedCandidate<PickCandidate>>): PickCandidate | null {
    for (const candidate of ranked) {
      if (occlusion.visible(candidate.item.point, candidate.item.slack)) return candidate.item
    }
    return null
  }

  /** カーソルの近くにある見えているボディ(辺・頂点の取り寄せの合図に使う)。 */
  function bodiesNearCursor(x: number, y: number, project: ScreenProjector): string[] {
    const near: string[] = []
    const sphere = new THREE.Sphere()
    for (const [bodyId, entry] of bodies) {
      const mesh = entry.body.mesh
      if (!mesh.visible || mesh.geometry.boundingSphere === null) continue
      sphere.copy(mesh.geometry.boundingSphere).applyMatrix4(mesh.matrixWorld)
      if (occlusion.sphereNear(sphere, x, y, EDGE_PICK_PIXELS, project)) near.push(bodyId)
    }
    return near
  }

  function lookAtSketch(sketchId: string): void {
    const target = sketches.get(sketchId)
    if (!target) return
    const frame = target.frame
    const normal = frameVector(frame.normal).normalize()
    // 中心は描いてあるものの真ん中(何も無ければ平面の原点)
    let center = toWorld(frame, 0, 0)
    let radius = 0
    const box = uvBoxOfPoints(target.points)
    if (box !== null) {
      center = toWorld(frame, (box.uMin + box.uMax) / 2, (box.vMin + box.vMax) / 2)
      radius = Math.max(box.uMax - box.uMin, box.vMax - box.vMin) / 2
    }
    const previous = camera.position.distanceTo(controls.target)
    const distance = radius > 0 ? Math.max(fitDistance(radius * SKETCH_FIT_MARGIN), 1) : Math.max(previous, 1)
    // 上方向はビューキューブと同じ規則(`upForDirection`)で決める。`OrbitControls` は生成時に焼いた
    // Z軸を回転の極として使い続ける(`WORLD_UP` 参照)ので、camera.up をそこから逸らすとキューブの見た目
    // (`cubeActive.up` が camera.up を写す)と実際の回転軸がずれる。`frame.v` を直接使うと任意面や XY 平面で
    // ずれる。真上・真下に近い面(cube側の上面図/底面図と同じ閾値)だけ ±Y へ差し替わる。
    camera.up.set(...upForDirection([normal.x, normal.y, normal.z]))
    camera.position.copy(center).add(normal.multiplyScalar(distance))
    controls.target.copy(center)
    // 正投影は距離では寄れない(`frameToContent` と同じ理由)。描いてあるものが無いときは今の見かけの大きさのまま。
    if (camera === orthographic && radius > 0) setViewHalfHeight(fitHalfHeight(radius * SKETCH_FIT_MARGIN))
    camera.updateProjectionMatrix()
    controls.update()
  }

  /**
   * その画素が本当に押せるか(canvasの上に浮いているDOMに覆われていないか)。3Dのレイ検査
   * (`pickAt` / `faceHitAt` / `basePlaneAt`)はcanvasの中しか見ないので、浮かせたパネル
   * (`BrowserPanel.svelte`)に覆われた画素も「取れる」と答えてしまう。`project*` は押せることを確かめて
   * 返す約束なので、覆われた画素は候補から落とす。判定基準は覆っているかではなく「クリックを吸うか」
   * (`Toast.svelte` は `pointer-events: none`)で、`elementFromPoint` がそれを答える。
   */
  function clickable(x: number, y: number): boolean {
    // キューブは同一canvasの第2パスで `elementFromPoint` は常にcanvasを返すので、矩形を明示的に除外する
    if (cube.inside(x, y)) return false
    if (section.handleAt(x, y)) return false // ハンドルが押下を引き取る画素(`onPointerDown`)
    const rect = container.getBoundingClientRect()
    return document.elementFromPoint(rect.left + x, rect.top + y) === renderer.domElement
  }

  function toScreen(point: THREE.Vector3): { x: number; y: number } | null {
    const { width, height } = size()
    return screenOf(point, width, height)
  }

  /**
   * 世界座標を**指せる画素**へ: 射影 → 画面の外なら null → `clickable()` でなければ null。
   * 寸法を引数で受けるのは `project` が三角形ごとに `size()`(DOM 読み取り)を呼ばないため(`projectRaw` の注意)。
   * 当たり判定の射影は画面の矩形でも `clickable()` でも切らない `projectRaw` のほう(線分までの距離には画面外の端点も要る)。
   */
  function screenOf(point: THREE.Vector3, width: number, height: number): { x: number; y: number } | null {
    const screen = projectRaw(point, width, height)
    if (screen === null) return null
    if (screen.x < 0 || screen.y < 0 || screen.x > width || screen.y > height) return null
    if (!clickable(screen.x, screen.y)) return null
    return screen
  }

  function projectSketchPoint(sketchId: string, u: number, v: number): { x: number; y: number } | null {
    const target = sketches.get(sketchId)
    if (!target) return null
    return toScreen(toWorld(target.frame, u, v))
  }

  /** カーソル強調の位置。段の規則はクリックと同じ `snapSketchPoint`(図形は編集中スケッチの `handles` から引く)。 */
  function snappedSketchUv(
    rawU: number,
    rawV: number,
    hit: SketchEntityHit | null,
    sketch: SketchScene,
  ): [number, number] {
    return snapSketchPoint(
      rawU,
      rawV,
      hit,
      (entityId) => sketch.handles.find((item) => item.entityId === entityId),
      resolution.applied(),
      true,
    )
  }

  function updateSketchCursor(
    offsetX: number,
    offsetY: number,
    planePoint: { u: number; v: number } | null,
    hit: SketchEntityHit | null,
  ): void {
    if (!sketchCursorWanted || activeSketchId === null || orbiting || planePoint === null) {
      sketchCursor.update(null, 'free', null)
      return
    }
    if (cube.inside(offsetX, offsetY)) {
      sketchCursor.update(null, 'free', null)
      return
    }
    const sketch = sketches.get(activeSketchId)
    if (!sketch) {
      sketchCursor.update(null, 'free', null)
      return
    }
    const [su, sv] = snappedSketchUv(planePoint.u, planePoint.v, hit, sketch)
    // 角・交点かどうかは `hit.snapAt` の有無(中点を除く)で決める(交点は2つの図形にまたがり、
    // 確定後の座標と自分自身への射影の比較では検出できない。`SketchEntityHit.snapAt` 参照)。
    const isCorner = hit !== null && hit.snapAt !== undefined && !hit.midpoint
    const isMidpoint = hit !== null && hit.midpoint === true
    const snap = classifySketchSnap(hit, planePoint.u, planePoint.v, isCorner, isMidpoint)
    const snapScreenAt =
      snap === 'free' ? null : projectSketchPoint(activeSketchId, su, sv)
    const tickAngleDeg =
      isMidpoint && hit?.snapDirection ? tickAngleForDirection(sketch, su, sv, hit.snapDirection) : undefined
    sketchCursor.update({ x: offsetX, y: offsetY }, snap, snapScreenAt, tickAngleDeg)
  }

  /**
   * `snapDirection`((u,v)空間の単位ベクトル)を画面上の角度(度)へ変換する。カーソル強調のティック
   * (`sketchCursorOverlay.ts`)専用。方向だけ要るので `projectRaw` を使う(`toScreen` はDOM被覆を見るため、
   * 補助点が覆われた画素に落ちると角度だけ消える)。
   */
  function tickAngleForDirection(
    sketch: SketchScene,
    u: number,
    v: number,
    direction: readonly [number, number],
  ): number | undefined {
    const { width, height } = size()
    const p1 = projectRaw(toWorld(sketch.frame, u, v), width, height)
    const p2 = projectRaw(toWorld(sketch.frame, u + direction[0], v + direction[1]), width, height)
    if (p1 === null || p2 === null) return undefined
    return (Math.atan2(p2.y - p1.y, p2.x - p1.x) * 180) / Math.PI
  }

  function projectRegion(sketchId: string, index: number): { x: number; y: number } | null {
    const target = sketches.get(sketchId)
    const mesh = target?.regionMeshes.find((candidate) => candidate.userData.regionIndex === index)
    if (!target || !mesh) return null
    const position = mesh.geometry.getAttribute('position')
    const triangles = mesh.geometry.getIndex()
    if (!position || !triangles) return null
    // 面の `project` と同じ流儀: 三角形の内側の点を狙い、実際にその領域へ当たる点を返す。
    // 1つの三角形につき4点試す(重心 + 各頂点へ寄せた3点): 重心1点だと、スケッチモードで点・辺が手前で
    // 拾われる画素に当たったとき候補が尽きる(ベジエの制御点は領域の内側にも立ち、細長い三角形の
    // 重心がその近くに来る)。
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
      // 判定はクリックと同じ `resolveHit`。スケッチも一致を見る(通常モードは全スケッチの領域が対象で、添字だけだと
      // 別スケッチの同じ添字を当たりと読む)
      const resolved = resolveHit(screen.x, screen.y)
      const hit = resolved.region
      if (!hit || hit.sketchId !== sketchId || hit.index !== index) continue
      // スケッチの図形が勝つ画素は返さない(スケッチモードでは点・辺が領域より手前で拾われ、`project*` の約束が破れる)
      if (resolved.sketchEntity !== null) continue
      return screen
    }
    return null
  }

  // ------------------------------------------------------------------ ウィンドウ選択(左ドラッグ)
  //
  // 帯の DOM と包含判定は `selectionBand.ts`。候補(`collectInBox`)はクリックのゲートと同じ
  // (辺は辺を出している間だけ、領域は要求している間だけ、スケッチの図形はスケッチモード中だけ)。
  // 違うと「クリックでは指せないものがドラッグでだけ選べる」ができる。
  const band = createSelectionBand({ container, projectRaw })

  const projectScratch = new THREE.Vector3()

  /**
   * 世界座標をコンテナ内の画素へ(カメラの裏だけ null)。当たり判定の射影はこれ: viewport の矩形でも
   * `clickable()` でも切らない(画面外の端点も線分までの距離を測るのに要る。切るのは `screenOf`)。
   * `width`/`height` は呼び出し元から渡す: 頂点ごとに `size()`(DOM 読み取り)を呼ぶと矩形選択の
   * 射影時間の92%を占める(実測)。リサイズで寸法が変わるので、モジュール側にキャッシュしないこと。
   */
  function projectRaw(point: THREE.Vector3, width: number, height: number): { x: number; y: number } | null {
    projectScratch.copy(point).project(camera)
    if (projectScratch.z < -1 || projectScratch.z > 1) return null
    return {
      x: ((projectScratch.x + 1) / 2) * width,
      y: ((1 - projectScratch.y) / 2) * height,
    }
  }

  /**
   * 矩形に完全に入り、一部でも見えているもの(docs/DESIGN_DECISIONS.md「見えるものはすべてさわれる」:
   * 矩形選択は全種別を集める・一部でも見えていれば入れる)。通常モードの種類はクリックと同じ(`pickKinds`)。
   * 見えているかは遮るものの奥行きを1回描いて引く(`occlusion.snapshot`)。辺・頂点がまだ届いていないボディが
   * 矩形に掛かれば `pendingBodies` に入れて取り寄せを頼む(呼び手は届いてから集め直す)。
   */
  function collectInBox(rect: BandRect): Omit<BoxSelectEvent, 'mode'> {
    const faces: FaceRef[] = []
    const edges: EdgeRef[] = []
    const vertices: VertexRef[] = []
    const regions: Array<{ sketchId: string; index: number }> = []
    const sketchEntities: SketchEntityHit[] = []
    const pendingBodies: string[] = []

    // 寸法はこの呼び出しの間不変なので1回だけ読んで使い回す(`projectRaw` 参照)
    const { width, height } = size()

    if (activeSketchId === null) {
      const keep = section.keepsFilter()
      camera.updateMatrixWorld()
      const depth = occlusion.snapshot()
      // 断面の ON の間は、奥行きの読み出しで見えた点を視線でも確かめる(読み出しは切り口の蓋を描かず、蓋の奥の裏の面で
      // 遮る。蓋に隠れた中身を見えていると読む。視線の判定 `surfaceAlong` は蓋で遮る)
      const sectionOn = keep !== null
      const seen = (point: THREE.Vector3, deviation = 0): boolean => {
        const slack = occlusion.slack(point, height, deviation)
        if (depth !== null && !depth.visible(point, slack)) return false
        return !sectionOn || occlusion.visible(point, slack)
      }
      // 立体の点は断面で切り落とした側なら見えていない(描かれていない。切り落とした側から見ると蓋より手前にあり、
      // 奥行きでも視線でも見えていると読む)。スケッチは断面で切らない
      const seenOnSolid = (point: THREE.Vector3, deviation = 0): boolean =>
        (keep === null || keep(point)) && seen(point, deviation)
      const identity = new THREE.Matrix4()
      const projector = createProjector(camera, width, height)
      // 線分1本を見る点の数の上限。画面に収まる線分なら画面の対角を超えない(超えるのは画面の外へはみ出す線分だけ)
      const maxSteps = Math.ceil(Math.hypot(width, height) / BOX_SEEN_STEP_PX)
      /**
       * 折れ線の上を画面で `BOX_SEEN_STEP_PX` おきに見て、どこか1点でも見えているか(頂点だけだと、両端が隠れて中ほどが
       * 見える線を落とす)。画面での等間隔を透視補正で3Dの位置へ戻す(3Dで等間隔に取ると手前で間隔が開く)。
       */
      const polylineSeen = (
        count: number,
        at: (index: number, out: THREE.Vector3) => void,
        test: (point: THREE.Vector3, deviation?: number) => boolean,
        deviation = 0,
      ): boolean => {
        const a = new THREE.Vector3()
        const b = new THREE.Vector3()
        const along = new THREE.Vector3()
        const sa = { x: 0, y: 0, w: 1 }
        const sb = { x: 0, y: 0, w: 1 }
        for (let index = 0; index < count; index += 1) {
          at(index, b)
          if (index === 0) {
            if (test(b, deviation)) return true
            a.copy(b)
            continue
          }
          const projected = projector.project(a, sa) && projector.project(b, sb)
          const length = projected ? Math.hypot(sb.x - sa.x, sb.y - sa.y) : 0
          const steps = Math.min(maxSteps, Math.max(1, Math.ceil(length / BOX_SEEN_STEP_PX)))
          for (let step = 1; step <= steps; step += 1) {
            const s = step / steps
            const t = projected ? perspectiveParam(s, sa.w, sb.w) : s
            if (test(along.lerpVectors(a, b, t), deviation)) return true
          }
          a.copy(b)
        }
        return false
      }

      // 面。隠したBodyのメッシュは `visible=false` なので外す。
      for (const [bodyId, entry] of pickKinds.has('face') ? bodies : []) {
        const mesh = entry.body.mesh
        if (!mesh.visible) continue
        mesh.updateWorldMatrix(true, false)
        for (const face of entry.body.faces) {
          const positions = entry.body.facePositions(face.index)
          if (positions === null || !band.positionsInside(positions, mesh.matrixWorld, rect, width, height, keep)) continue
          if (!faceSeen(entry.body.mesh, face, seenOnSolid)) continue
          faces.push({ bodyId, faceIndex: face.index })
        }
      }
      // 辺・頂点(届いたボディの分)
      if (pickKinds.has('edge')) {
        features.forEachEdge((ref, points, deviation) => {
          if (!band.positionsInside(points, identity, rect, width, height, keep)) return
          const visible = polylineSeen(
            Math.floor(points.length / 3),
            (index, out) => out.set(points[index * 3], points[index * 3 + 1], points[index * 3 + 2]),
            seenOnSolid,
            deviation,
          )
          if (visible) edges.push(ref)
        })
      }
      if (pickKinds.has('vertex')) {
        features.forEachVertex((ref, point) => {
          if (keep !== null && !keep(point)) return
          const screen = projectRaw(point, width, height)
          if (screen === null || !band.pointInside(screen, rect)) return
          if (seen(point)) vertices.push(ref)
        })
      }
      if (pickKinds.has('edge') || pickKinds.has('vertex')) {
        for (const [bodyId, entry] of bodies) {
          const lacking =
            (pickKinds.has('edge') && !features.hasEdges(bodyId)) || (pickKinds.has('vertex') && !features.hasVertices(bodyId))
          if (lacking && entry.body.mesh.visible && bodyTouchesRect(entry.body.mesh, rect, width, height)) pendingBodies.push(bodyId)
        }
        if (pendingBodies.length > 0) pickDataWantedListener?.(pendingBodies)
      }
      // 編集していないスケッチの図形。図形全体が入っていればその図形を、入っていなければ矩形に入った点だけを返す。
      for (const [sketchId, scene] of sketches) {
        if (scene.editing) continue
        const whole = new Set<string>()
        if (pickKinds.has('sketchLine')) {
          for (const line of scene.handleLines) {
            const worldPoints = line.userData.polylineWorld as readonly THREE.Vector3[] | undefined
            const entityId = line.userData.entityId
            if (!worldPoints || typeof entityId !== 'string') continue
            if (!band.polylineWorldInside(worldPoints, rect, width, height)) continue
            if (!polylineSeen(worldPoints.length, (index, out) => out.copy(worldPoints[index]), seen)) continue
            whole.add(entityId)
            sketchEntities.push({ sketchId, entityId, point: null })
          }
        }
        if (pickKinds.has('sketchPoint')) {
          for (const item of scene.pickPoints) {
            if (whole.has(item.entityId)) continue
            const screen = projectRaw(item.point, width, height)
            if (screen === null || !band.pointInside(screen, rect) || !seen(item.point)) continue
            sketchEntities.push({ sketchId, entityId: item.entityId, point: item.key })
          }
        }
      }
      // 領域
      for (const mesh of regionPickTargets()) {
        if (!band.objectInside(mesh as THREE.Mesh & { geometry: THREE.BufferGeometry }, rect, width, height)) continue
        const triangles = (mesh.geometry.getIndex()?.count ?? 0) / 3
        const visible = faceSeen(mesh, { triangleStart: 0, triangleCount: triangles }, seen)
        const { sketchId, regionIndex } = mesh.userData
        if (visible && typeof sketchId === 'string' && typeof regionIndex === 'number') {
          regions.push({ sketchId, index: regionIndex })
        }
      }
    } else {
      // スケッチモードでは図形だけ。図形全体が入っていればその図形を、入っていなければ矩形に入った点だけを返す。
      const target = sketches.get(activeSketchId)
      const whole = new Set<string>()
      for (const line of target?.handleLines ?? []) {
        const worldPoints = line.userData.polylineWorld as readonly THREE.Vector3[] | undefined
        if (!worldPoints || !band.polylineWorldInside(worldPoints, rect, width, height)) continue
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
        if (!band.objectInside(mesh as THREE.Points & { geometry: THREE.BufferGeometry }, rect, width, height)) continue
        sketchEntities.push({ sketchId: activeSketchId, entityId, point })
      }
      // 領域はクリックと同じ(そのスケッチの領域だけ)
      for (const mesh of regionPickTargets()) {
        if (!band.objectInside(mesh as THREE.Mesh & { geometry: THREE.BufferGeometry }, rect, width, height)) continue
        const { sketchId, regionIndex } = mesh.userData
        if (typeof sketchId === 'string' && typeof regionIndex === 'number') {
          regions.push({ sketchId, index: regionIndex })
        }
      }
    }
    return { faces, edges, vertices, regions, sketchEntities, rect, pendingBodies }
  }

  /** 面の三角形の重心と頂点のどれかが見えているか(一部でも見えていれば入れる)。 */
  function faceSeen(
    mesh: THREE.Mesh,
    face: { triangleStart: number; triangleCount: number },
    seen: (point: THREE.Vector3) => boolean,
  ): boolean {
    const position = mesh.geometry.getAttribute('position')
    const index = mesh.geometry.getIndex()
    if (!position || !index) return true
    const corner = new THREE.Vector3()
    const centroid = new THREE.Vector3()
    for (let triangle = face.triangleStart; triangle < face.triangleStart + face.triangleCount; triangle += 1) {
      centroid.set(0, 0, 0)
      for (let vertex = 0; vertex < 3; vertex += 1) {
        const at = index.getX(triangle * 3 + vertex)
        corner.set(position.getX(at), position.getY(at), position.getZ(at)).applyMatrix4(mesh.matrixWorld)
        if (seen(corner)) return true
        centroid.addScaledVector(corner, 1 / 3)
      }
      if (seen(centroid)) return true
    }
    return false
  }

  /** そのボディの外接球が画面で矩形に掛かるか(辺・頂点の取り寄せを頼むかの判定)。 */
  function bodyTouchesRect(mesh: THREE.Mesh, rect: BandRect, width: number, height: number): boolean {
    if (mesh.geometry.boundingSphere === null) return false
    const sphere = mesh.geometry.boundingSphere.clone().applyMatrix4(mesh.matrixWorld)
    const center = projectRaw(sphere.center, width, height)
    if (center === null) return true
    const radius = occlusion.screenRadius(sphere, height)
    const nearestX = Math.min(Math.max(center.x, rect.left), rect.right)
    const nearestY = Math.min(Math.max(center.y, rect.top), rect.bottom)
    return (nearestX - center.x) ** 2 + (nearestY - center.y) ** 2 <= radius * radius
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
    // キューブの内側で始まった左ボタン押下はキューブが引き取る(ピック・矩形選択の開始へ漏らさない)
    if (cube.pointerDown(event)) return
    // 断面のハンドルの上で始まった左ボタン押下はハンドルが引き取る
    if (section.pointerDown(event.offsetX, event.offsetY)) {
      renderer.domElement.setPointerCapture(event.pointerId)
      renderer.domElement.style.cursor = 'grabbing'
      return
    }
    downAt = performance.now()
    downX = event.offsetX
    downY = event.offsetY
    downValid = true
    band.hide()
  }

  function onPointerUp(event: PointerEvent): void {
    // ボタンの種類より先に解除する(右/中ボタンで回して離したとき、解除しないと次の `pointermove` までホバーが止まる)
    orbiting = false
    if (section.dragging()) {
      section.pointerUp()
      if (renderer.domElement.hasPointerCapture(event.pointerId)) {
        renderer.domElement.releasePointerCapture(event.pointerId)
      }
      renderer.domElement.style.cursor = section.handleAt(event.offsetX, event.offsetY) ? 'grab' : ''
      return
    }
    if (cube.pointerUp(event)) return
    if (event.button !== 0 || !downValid) {
      if (event.button === 0) band.hide()
      return
    }
    downValid = false
    const moved = Math.hypot(event.offsetX - downX, event.offsetY - downY)
    const elapsed = performance.now() - downAt
    // 矩形を引いていたならウィンドウ選択(クリックにはしない)。引いた向きは見ない。
    if (band.active()) {
      const rect = band.rect(downX, downY, event.offsetX, event.offsetY)
      band.hide()
      boxSelectListener?.({ ...collectInBox(rect), mode: modeOf(event) })
      return
    }
    if (moved > CLICK_MOVE_LIMIT_PX || elapsed > CLICK_TIME_LIMIT_MS) return
    pickListener?.({ ...resolveHit(event.offsetX, event.offsetY), mode: modeOf(event) })
  }

  /** 修飾キーから選択の扱いを決める。クリックもウィンドウ選択も同じ規則。Shift も Ctrl(Mac は Meta)も同じトグルで、除外専用のキーは持たない。 */
  function modeOf(event: { shiftKey: boolean; ctrlKey: boolean; metaKey: boolean }): PickMode {
    return event.shiftKey || event.ctrlKey || event.metaKey ? 'toggle' : 'replace'
  }

  /**
   * その画素をクリックしたら何が取れるか(修飾キー以外の `PickEvent` そのもの)。
   * クリックとホバー強調はこの関数だけを見る(優先順位を2箇所に書くとプレビューが嘘をつく)。
   * 副作用はここに置かない(`pickListener` を呼ぶのは `onPointerUp` だけ)。
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
    // スケッチモード中は面を拾わない(線を引くつもりのクリックが選択の置き換えになる)。領域が手前・平面の点がその次。
    if (activeSketchId !== null) {
      const region = regionAt(offsetX, offsetY)
      const plane = planePointAt(offsetX, offsetY)
      // 図形は領域より手前(点・辺は領域の縁に乗っており、領域が勝つと端点を掴めない)。`point` は常に一緒に返し、
      // 描くコマンドが起動している間はそちらが優先(判断は `App.svelte`)。
      return {
        ...nothing,
        region,
        sketchEntity: sketchEntityAt(offsetX, offsetY),
        point: plane === null ? null : { sketchId: activeSketchId, ...plane },
      }
    }
    // 通常モード: 細かいものから(点 → 線 → 面・領域)。同じ細かさで重なって見えるときはソリッドが先
    // (`rankByOverlap`)。見えないもの(立体の裏)は飛ばして次の候補へ(`occlusion.ts`)。
    // 細かさを先に見るのは、点は線の端に、線は面の縁に必ず乗っていて、広いものを先に見ると細かいものが一生取れないため
    // (docs/VIEWPORT.md「固定の優先順位の下では…」)。docs/DESIGN_DECISIONS.md「見えるものはすべてさわれる」。
    const project = projector()
    const sketchCandidates = sketchPickCandidates(offsetX, offsetY, project)
    const points: Array<RankedCandidate<PickCandidate>> = [...sketchCandidates.points]
    if (pickKinds.has('vertex')) {
      for (const candidate of features.vertexCandidates(offsetX, offsetY, VERTEX_PIXELS, project, section.keepsFilter())) {
        points.push({
          item: { hit: { vertex: candidate.ref }, point: candidate.point, slack: occlusion.slack(candidate.point, project.height) },
          distSq: candidate.distSq,
          solid: true,
          key: `v|${candidate.ref.bodyId}|${candidate.ref.vertexIndex}`,
        })
      }
    }
    const pointHit = firstVisible(rankByOverlap(Math.max(VERTEX_PIXELS, SKETCH_HANDLE_PIXELS), PICK_OVERLAP_PIXELS, points))
    if (pointHit !== null) return { ...nothing, ...pointHit.hit }

    const lines: Array<RankedCandidate<PickCandidate>> = [...sketchCandidates.lines]
    if (pickKinds.has('edge')) {
      for (const candidate of features.edgeCandidates(offsetX, offsetY, EDGE_PICK_PIXELS, project, section.signedDistance())) {
        lines.push({
          item: {
            hit: { edge: candidate.ref },
            point: candidate.point,
            slack: occlusion.slack(candidate.point, project.height, candidate.deviation),
          },
          distSq: candidate.distSq,
          solid: true,
          key: `e|${candidate.ref.bodyId}|${candidate.ref.edgeIndex}`,
        })
      }
    }
    const lineHit = firstVisible(rankByOverlap(EDGE_PICK_PIXELS, PICK_OVERLAP_PIXELS, lines))
    if (lineHit !== null) return { ...nothing, ...lineHit.hit }

    // 面と領域。面に当たる画素では面が勝つ(同じ細かさならソリッドが先。押し出した元のスケッチの領域は押し出した面と
    // 厳密に同一平面で重なる)。面を指せない間(領域だけを受け付けるコマンドの起動中)も、面は遮るものとして残る。
    // 断面の切り口(`ref: null`)も遮る。
    const surface = faceHitAt(offsetX, offsetY)
    if (surface !== null && surface.ref !== null && pickKinds.has('face')) return { ...nothing, hit: surface.ref }
    for (const candidate of regionHitsAt(offsetX, offsetY)) {
      if (surface !== null && candidate.distance > surface.distance + occlusion.slack(candidate.point, project.height)) break
      return { ...nothing, region: candidate.region }
    }
    // 基準平面は面の無い画素でだけ取れる(レイ距離では競わせない。わざと: 部品のシルエットに重なる部分の基準平面は
    // 指せない。#109、本人判断 2026-10-07、docs/VIEWPORT.md「ピック」)。`showBasePlanes(true)` の間しか当たらない。
    if (surface !== null) return nothing
    return { ...nothing, plane: basePlaneAt(offsetX, offsetY) }
  }

  function onPointerMove(event: PointerEvent): void {
    // キューブが引き取ったポインタはメインの経路を一切通らない
    if (cube.capturing()) {
      cube.pointerMove(event)
      return
    }
    if (section.dragging()) {
      section.pointerMove(event.offsetX, event.offsetY)
      return
    }
    // ハンドルの上では本編のホバー予告を出さない(押せばハンドルが取るので、面が光ると嘘になる)
    const overHandle = section.handleAt(event.offsetX, event.offsetY)
    section.setHover(overHandle)
    renderer.domElement.style.cursor = overHandle ? 'grab' : ''
    if (overHandle) {
      hoverAt = null
      cube.setHoverAt(null)
      updateSketchCursor(event.offsetX, event.offsetY, null, null)
      return
    }
    // キューブの矩形の内側では、メインのホバー予告・矩形選択開始を早期に打ち切る(ホバーのみの場合)。
    const overCube = cube.inside(event.offsetX, event.offsetY)
    cube.setHoverAt(overCube ? { x: event.offsetX, y: event.offsetY } : null)
    if (overCube) {
      hoverAt = null
      updateSketchCursor(event.offsetX, event.offsetY, null, null)
      return
    }
    // 座標を控えるだけ。当たり判定は `tick` が1フレームに1回回す(pointermove は1フレームに何度も飛ぶ)。
    hoverAt = { x: event.offsetX, y: event.offsetY }
    // 視点回転中は取れるものの予告を出さない。境目は `onPointerUp` の moved 判定と揃える。`downValid` ではなく
    // `buttons` で見る(canvasの外でボタンを離されると `pointerup` が来ず、`downValid` が立ったままになる)。
    const dragged = Math.hypot(event.offsetX - downX, event.offsetY - downY)
    orbiting = event.buttons !== 0 && dragged > CLICK_MOVE_LIMIT_PX
    // 左ボタンを押したまま一定距離を越えたらウィンドウ選択。`buttons` も見るのは、canvasの外で離されたとき帯が出たままになるのを防ぐため。
    if (downValid && (event.buttons & 1) !== 0 && dragged > BOX_SELECT_MIN_PX) {
      band.show(downX, downY, event.offsetX, event.offsetY)
    }
    let planePoint: { u: number; v: number } | null = null
    let sketchHit: SketchEntityHit | null = null
    if (planeMoveListener !== null) {
      if (activeSketchId === null) {
        planeMoveListener(null, null)
      } else {
        // `sketchEntityAt` は `resolveHit` と同じ関数: 幽霊のスナップ先とクリック確定時のスナップ先が同じ入力を通る(App.svelte の不変条件)。
        planePoint = planePointAt(event.offsetX, event.offsetY)
        sketchHit = sketchEntityAt(event.offsetX, event.offsetY)
        planeMoveListener(planePoint, sketchHit)
        grid.followCursor(planePoint)
      }
    }
    updateSketchCursor(event.offsetX, event.offsetY, planePoint, sketchHit)
  }

  /** ポインタがcanvasから出た。強調を残さない(戻ってくるまで指せるものは無い)。 */
  function onPointerLeave(event: PointerEvent): void {
    hoverAt = null
    orbiting = false
    section.setHover(false)
    // ハンドルとキューブのドラッグは `pointercancel` でだけ打ち切る(`pointerleave` は `setPointerCapture` 中には来ず、来ても座標は届き続ける)。
    if (event.type === 'pointercancel') {
      section.pointerUp()
      cube.cancel()
    }
    if (!section.dragging()) renderer.domElement.style.cursor = ''
    sketchCursor.update(null, 'free', null)
    // 追従すべき位置が無いので「カーソル無し」(`point: null`)へ戻す(`sketchCursor` と同じ。さもないと直前の拡大状態が残る)。
    grid.followCursor(null)
    // 引きかけの矩形は捨てる(帯が画面に残るのを防ぐ)
    band.hide()
    downValid = false
    // キューブの操作中は無視する(`setPointerCapture` で押下中は座標がどこへ行ってもこの要素が受け取り続けるので、`pointerleave` で打ち切らない)。
    if (!cube.capturing()) cube.setHoverAt(null)
  }

  renderer.domElement.addEventListener('pointerdown', onPointerDown)
  renderer.domElement.addEventListener('pointerup', onPointerUp)
  renderer.domElement.addEventListener('pointermove', onPointerMove)
  renderer.domElement.addEventListener('pointerleave', onPointerLeave)
  // ドラッグがOSに横取りされた場合(`pointerup` が来ない)。`pointerleave` と同じ後始末でよい。
  renderer.domElement.addEventListener('pointercancel', onPointerLeave)

  // ポインタ・ホイールは公開口を通らずに見た目(ホバー・ビューキューブ・断面のつまみ・グリッド)を変える
  for (const type of ['pointerdown', 'pointerup', 'pointermove', 'pointerleave', 'pointercancel', 'wheel'] as const) {
    renderer.domElement.addEventListener(type, invalidate, { passive: true })
  }
  controls.addEventListener('change', invalidate)
  // 描画バッファが失われた後(GPU のリセット・タブの復帰)は、何も変わっていなくても描き直す
  renderer.domElement.addEventListener('webglcontextrestored', invalidate)
  document.addEventListener('visibilitychange', invalidate)
  // ビューキューブはツールバーの引数欄の高さ(`CommandHost` が `.app` の style へ書く `--toolbar-overlay-h`)だけ下がる。
  // 公開口を通らずに変わるので、書き換えを見張って描き直す(描かないとキューブの絵と当たり判定がずれる)。
  const overlayWatcher = new MutationObserver(invalidate)
  const appElement = container.closest('.app')
  if (appElement instanceof HTMLElement) overlayWatcher.observe(appElement, { attributes: true, attributeFilter: ['style'] })

  const observer = new ResizeObserver(() => {
    invalidate()
    applySize()
  })
  observer.observe(container)
  applySize()

  /**
   * ホバー強調を1フレームぶん更新する。毎フレーム解決し直す(視点を回すとポインタが動かなくても
   * 当たるものが変わる)。材質の書き換えは変わったときだけ。
   */
  /** カーソルの近くのボディのうち辺・頂点を頼むもの(辺・頂点を指せる間だけ。古い版かは呼び手が決める)。 */
  function requestPickData(x: number, y: number): void {
    if (pickDataWantedListener === null || !(pickKinds.has('edge') || pickKinds.has('vertex'))) return
    const near = bodiesNearCursor(x, y, projector())
    if (near.length > 0) pickDataWantedListener(near)
  }

  function updateHover(): void {
    const point = orbiting ? null : hoverAt
    // カメラの値は丸めて鍵にする: 真上から見る極の近く(スケッチ中)では `OrbitControls` が毎フレーム位置と姿勢を
    // 1e-8 ほど揺らし続け、そのままだと止まっていても毎フレーム当たりを解き直す(`cameraMovedSinceRender` と同じ揺れ)。
    const fixed = (values: number[]) => values.map((value) => value.toFixed(6)).join(',')
    const probe =
      point === null
        ? ''
        : `${pickGeneration},${point.x},${point.y},` +
          `${fixed(camera.position.toArray())},${fixed(camera.quaternion.toArray())},` +
          // `zoom` も入れる: 正投影のドリーは `zoom` だけを動かすので、無いとホイールで寄っても当たるものが変わらない。
          `${camera.zoom}`
    if (probe === hoverProbe) return
    hoverProbe = probe
    if (point !== null && activeSketchId === null) requestPickData(point.x, point.y)
    const next = point === null ? null : resolveHit(point.x, point.y)
    // スケッチモードでは図形と領域が一緒に返る: 図形が取れるときは領域を光らせない
    // (点・辺は領域より手前で拾われ、光らせると予告が嘘になる)。
    const face = next?.hit ?? null
    const edge = next?.edge ?? null
    const vertex = next?.vertex ?? null
    const region = next?.sketchEntity ? null : (next?.region ?? null)
    const plane = next?.plane ?? null

    const nextFaceKey = face === null ? '' : refKey(face.bodyId, face.faceIndex)
    if (nextFaceKey !== hoverFaceKey) {
      hoverFaceKey = nextFaceKey
      hoverFace = face
      applyFaceMaterials()
    }
    // 面は `applyFaceMaterials` が色を変えたときに描き直しを頼む。辺・頂点は `features` が頼む。
    hoverEdge = edge
    hoverVertex = vertex
    features.setHover(edge, vertex)
    const nextRegionKey = region === null ? '' : refKey(region.sketchId, region.index)
    if (nextRegionKey !== hoverRegionKey) {
      hoverRegionKey = nextRegionKey
      hoverRegion = region
      applyRegionMaterials()
      invalidate()
    }
    const sketchEntity = next?.sketchEntity ?? null
    const nextSketchKey =
      sketchEntity === null ? '' : `${sketchEntity.sketchId}|${sketchEntity.entityId}|${sketchEntity.point ?? ''}`
    if (nextSketchKey !== hoverSketchKey) {
      hoverSketchKey = nextSketchKey
      hoverSketchEntity = sketchEntity
      applySketchHoverMaterials()
      invalidate()
    }
    if (plane !== hoverPlane) {
      hoverPlane = plane
      applyBasePlaneHover()
      invalidate()
    }
  }

  /**
   * 視線が極(真上/真下)から離れたら、`camera.up` を WORLD_UP へ戻す。`controls.update()` が最後に呼ぶ
   * `object.lookAt()` は `camera.up` でロールを決める。ビューキューブの上面図/底面図クリック(`upForDirection`)も
   * `lookAtSketch` も、極付近の縮退を避けるため一時的に `camera.up` をYへ傾けるので、戻さないと
   * 「キューブの上面をクリックしてから回すと絵が転がる」。
   * 極に近い間(`|z| > POLE_Z`)は何もしない: 毎フレーム `upForDirection(今の向き)` を焼き直すと、あれは
   * 極付近で定数 `(0,±1,0)` を返す階段関数なので、通常のドラッグで極をまたぐだけで上が固定されたように見える。
   * 極から離れているときは WORLD_UP のままでよい(`upForDirection` の docstring 参照)ので、
   * 極から外れた瞬間に WORLD_UP へ戻す片方向の掃除に留める。
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

  /** 直近の本描画(ビューキューブを除く)の描画呼び出しの数と、本描画の累計回数(物差し用。issue #171)。 */
  let lastDrawCalls = 0
  let renderCount = 0
  /** 最後に描いたときのカメラ(`cameraMovedSinceRender`)。 */
  let renderedCamera: THREE.Camera | null = null
  const renderedView = new THREE.Matrix4()
  const renderedProjection = new THREE.Matrix4()

  /**
   * 最後に描いた後にカメラが動いたか(減衰・ビューキューブの回転・投影の切り替え・寸法の変更を含む)。
   * 完全一致では比べない: 真上から見る極の近く(スケッチ中)では `OrbitControls` が毎フレーム姿勢を計算し直し、
   * 行列が 1e-8 ほど揺れ続ける(実測。画素には出ない)。
   */
  function cameraMovedSinceRender(): boolean {
    camera.updateMatrixWorld()
    return (
      camera !== renderedCamera ||
      !matricesClose(camera.matrixWorld, renderedView) ||
      !matricesClose(camera.projectionMatrix, renderedProjection)
    )
  }

  function matricesClose(a: THREE.Matrix4, b: THREE.Matrix4): boolean {
    for (let i = 0; i < 16; i += 1) {
      if (Math.abs(a.elements[i] - b.elements[i]) > 1e-6) return false
    }
    return true
  }

  function tick(): void {
    // 画素比はイベントで待たず毎フレーム見る(同じ CSS 寸法のまま別モニタへ移ったときは `ResizeObserver` も発火しない)。
    if (syncPixelRatio()) {
      invalidate()
      applySize()
    }
    cube.stepSnap(performance.now())
    controls.update()
    syncCameraUp()
    // 描画ループで見る(`OrbitControls` の 'change' ではない): 減衰つきのドリーはイベントが止まった後も
    // 動き続けるので、「止まったか」はフレーム側でしか分からない。
    resolution.update(performance.now())
    updateHover()
    if (cube.updateHover()) invalidate()
    // カーソル追従もフレームごとに引き直す(`onPointerMove` だけに任せない): ホイールでズームすると `hoverAt` は
    // 動かないがレイと平面frameの交点(`planePointAt`)は変わり、カーソルを動かさず寄せ引きするだけではグリッドが
    // 追従しない(本人報告2026-09-11)。スケッチ非活性なら `planePointAt`/`grid.followCursor` は即 `null`/no-op。
    grid.followCursor(hoverAt === null ? null : planePointAt(hoverAt.x, hoverAt.y))
    // グリッドは毎フレームここだけを更新する(段の不透明度とカメラ行列。ジオメトリは無い)。
    grid.updateUniforms()
    section.update()
    if (needsRender || cameraMovedSinceRender()) {
      needsRender = false
      headlightRig.follow(controls.target)
      renderer.render(scene, camera)
      lastDrawCalls = renderer.info.render.calls
      renderCount += 1
      // ビューキューブは本描画の後に同じ描画バッファへ重ねる(色を消さない)ので、本描画と同じフレームでだけ描く
      cube.render()
      renderedCamera = camera
      renderedView.copy(camera.matrixWorld)
      renderedProjection.copy(camera.projectionMatrix)
    }
  }
  const frameLoop = startFrameLoop(tick, {
    request: (callback) => requestAnimationFrame(callback),
    cancel: (handle) => cancelAnimationFrame(handle),
    report: (error) => reportError(error),
  })

  const api: Viewport = {
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
    setSketchCursorWanted(wanted) {
      if (sketchCursorWanted === wanted) return
      sketchCursorWanted = wanted
      sketchCursor.setActive(wanted)
      if (!wanted) sketchCursor.update(null, 'free', null)
    },
    viewResolution() {
      return { ...resolution.applied() }
    },
    setFaceCulling: resolution.setFaceCulling,
    sketchPolylineCounts() {
      const counts: Record<string, number[]> = {}
      for (const [sketchId, entry] of sketches) counts[sketchId] = [...entry.polylineCounts]
      return counts
    },
    onResolutionChange: resolution.onChange,
    highlight,
    highlightBodies,
    setOptimizeState,
    setOptimizeDebug: optimizeDebug.setState,
    setPickKinds,
    setBodyEdges(bodyId, edges) {
      if (features.setEdges(bodyId, edges as BodyEdges | null)) pickChanged()
    },
    setBodyVertices(bodyId, vertices) {
      if (features.setVertices(bodyId, vertices as BodyVertices | null)) pickChanged()
    },
    onPickDataWanted(listener) {
      pickDataWantedListener = listener
    },
    showBasePlanes,
    setOriginVisible: originGizmo.setVisible,
    setSection: section.setState,
    onSectionDrag(listener) {
      sectionDragListener = listener
    },
    contentCenter() {
      const box = new THREE.Box3().setFromObject(root)
      if (box.isEmpty()) return null
      const center = box.getCenter(new THREE.Vector3())
      return [center.x, center.y, center.z]
    },
    groupEdgeChains(on) {
      features.groupChains(on)
    },
    edgeChain: features.chainOf,
    setEdgeChains: features.setChains,
    onEdgeChainWanted: features.onChainWanted,
    highlightEdges: features.highlightEdges,
    highlightVertices: features.highlightVertices,
    collectInBox,
    projectEdge,
    showMeasurement: measureAnnotation.show,
    showInterference: interferenceLayer.show,
    interferenceSolidCount: interferenceLayer.overlapSolidCount,
    sketchRebuilds: () => sketchRebuildCount,
    drawCalls: () => lastDrawCalls,
    renderCount: () => renderCount,
    hoveredRef() {
      return {
        face: hoverFace,
        edge: hoverEdge,
        vertex: hoverVertex,
        region: hoverRegion,
        plane: hoverPlane,
        sketchEntity: hoverSketchEntity,
        color: hoverColorNow(),
      }
    },
    projectVertex,
    showPreview: ghostLayer.showPreview,
    clearPreview: ghostLayer.clearPreview,
    showBodyGhosts: ghostLayer.showBodyGhosts,
    project,
    showSketches,
    showSketchGhosts,
    setActiveSketch,
    lookAtSketch,
    frameAll() {
      camera.up.copy(WORLD_UP) // スケッチの v 軸へ倒したままだと、戻したのに傾いて見える
      frameToContent()
    },
    resetGrid() {
      grid.rescale(contentBoxForGrid(), true)
    },
    setFov,
    cameraPose,
    viewSignature,
    projectPoint,
    projectSketchPoint,
    projectBasePlane,
    projectRegion,
    captureThumbnail() {
      // `object.visible` は見ない: 文書で非表示の立体は `bodies` にキーごと無く、`visible=false` はプレビュー中の
      // 一時的な非表示(`setBodyVisible`)。見るとプレビュー中に撮った絵から立体が欠ける。
      return thumbnailer.capture([...bodies.values()].flatMap((entry) => entry.sceneObjects))
    },
    dispose() {
      frameLoop.stop()
      observer.disconnect()
      thumbnailer.dispose()
      renderer.domElement.removeEventListener('pointerdown', onPointerDown)
      renderer.domElement.removeEventListener('pointerup', onPointerUp)
      renderer.domElement.removeEventListener('pointermove', onPointerMove)
      renderer.domElement.removeEventListener('pointerleave', onPointerLeave)
      renderer.domElement.removeEventListener('pointercancel', onPointerLeave)
      document.removeEventListener('visibilitychange', invalidate)
      overlayWatcher.disconnect()
      cube.dispose()
      sketchCursor.dispose()
      ghostLayer.clearPreview()
      clearGhost()
      features.dispose()
      occlusion.dispose()
      for (const sketchId of [...sketches.keys()]) clearSketch(sketchId)
      band.dispose()
      sketchGhostMaterial.dispose()
      sketchGhostWarnMaterial.dispose()
      sketchLineMaterials.dispose() // `sketchMaterial` は種として入っている(ここで1回だけ捨てる)
      sketchHandleMaterials.dispose()
      regionMaterials.dispose()
      for (const bodyId of [...bodies.keys()]) clearBody(bodyId)
      grid.dispose()
      originGizmo.dispose()
      section.dispose()
      controls.dispose()
      bodyMaterials.smooth.dispose()
      bodyMaterials.flat.dispose()
      previewMaterial.dispose()
      edgeMaterial.dispose()
      annotations.clear()
      measureAnnotation.dispose()
      interferenceLayer.dispose()
      optimizeDebug.clear()
      renderer.dispose()
      renderer.domElement.remove()
    },
  }
  return invalidating(api, invalidate, ['drawCalls', 'renderCount'])
}

/**
 * 公開口のどれかが呼ばれたら次のフレームで描き直させる(必要なときだけ描く。`createViewport` の「必要なときだけ描く」)。
 * 見た目を変える口を1つずつ印すと付け忘れる(付け忘れた口は「画面が変わらない」になる)ので、読むだけの口も含めて全部。
 * 除くのは描画の物差し(`except`)だけ: 毎フレーム読むと、読むこと自体が描き続けさせる。
 */
function invalidating<T extends object>(api: T, invalidate: () => void, except: readonly string[]): T {
  for (const [key, value] of Object.entries(api)) {
    if (typeof value !== 'function' || except.includes(key)) continue
    ;(api as Record<string, unknown>)[key] = (...args: unknown[]) => {
      invalidate()
      return (value as (...rest: unknown[]) => unknown).apply(api, args)
    }
  }
  return api
}
