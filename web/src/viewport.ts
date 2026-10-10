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
import { createResolutionTracker } from './resolutionTracker'
import { BASE_EXTENT, mmPerPixel, type CameraProjection } from './gridScale'
import { createOriginGizmo } from './originGizmo'
import { createGhostLayer } from './ghostLayer'
import { createSectionView } from './sectionView'
import { createSelectionBand, type BandRect } from './selectionBand'
import { createGridPlanes } from './gridPlanes'
import { frameVector, planePoint } from './planeMath'
import { createBatch, type SceneBatch } from './sceneBatch'
import { distSq, distSqPointSegment, pickNearestWithinRadius, type ScreenPickCandidate } from './screenPick'
import { variantCache } from './viewportMaterials'
import { buildSketchLine, createSketchLineMaterial, type SketchFatLine } from './sketchLine'
import { POLE_Z, upForDirection } from './viewCube'
import { createOptimizeAnnotations } from './optimizeAnnotations'
import { createOptimizeDebug } from './optimizeDebug'
import { createViewCubeOverlay } from './viewCubeOverlay'
import { createVertexMarkers } from './vertexMarkers'
import { createMeasureAnnotation } from './measureAnnotation'
import { createThumbnailer } from './thumbnail'
import type { BasePlaneName, EdgeGeometry } from './session'
import {
  type BodyGeometryUpdate, type BodyGhostRotation, type BoxSelectEvent, type EdgeRef, type FaceGeometry,
  type FaceRef, type GeometryPayload, type OptimizeVisualState, type PickEvent, type PickMode, type SectionState,
  type SketchDraw, type SketchEntityHit, type VertexMarker, type VertexRef, type Viewport,
} from './viewportTypes'
import {
  BACKGROUND_COLOR, BASE_COLOR, BASE_PLANES, BASE_PLANE_PROBES, BOX_SELECT_MIN_PX, CLICK_MOVE_LIMIT_PX, CLICK_TIME_LIMIT_MS,
  EDGE_COLOR, EDGE_PICK_PIXELS, FOV_DEFAULT, FOV_MAX, FOV_MIN, FOV_ORTHO, HIGHLIGHT_COLOR, HOME_VIEW_OFFSET, HOVER_LIGHTEN, LAYER,
  OPTIMIZE_EXCLUDE_COLOR, OPTIMIZE_FIXTURE_COLOR, OPTIMIZE_PRESERVE_COLOR, PICK_EDGE_COLOR, PREVIEW_COLOR,
  REGION_COLOR, REGION_SELECTED_COLOR, SKETCH_COLOR, SKETCH_FIT_MARGIN, SKETCH_GHOST_COLOR, SKETCH_GHOST_WARN_COLOR, SKETCH_HANDLE_COLOR,
  SKETCH_HANDLE_PIXELS, SKETCH_SELECTED_COLOR, WORLD_UP, isSelectableFov, lighten, type BodyMeshes,
} from './viewportConstants'

export { FOV_DEFAULT, FOV_ORTHO, isSelectableFov }
export type {
  BodyGhostRotation, BoxSelectEvent, GeometryPayload, PickEvent, SectionState, SketchDraw, SketchEntityHit,
  VertexMarker, Viewport,
}

export function createViewport(container: HTMLElement): Viewport {
  // `stencil` は three r163 以降の既定が false で、無いと断面の切り口が例外も出さずに描かれない
  const renderer = new THREE.WebGLRenderer({ antialias: true, stencil: true })
  // 断面は材質ごとのクリップ平面で切る(`renderer.clippingPlanes` はグリッド・スケッチ・キューブまで切る)
  renderer.localClippingEnabled = true
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2))
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
  const headlight = new THREE.DirectionalLight(0xffffff, 1.6)
  headlight.position.set(0.4, 0.7, 1)
  camera.add(headlight)
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

  // ------------------------------------------------------------------ 形状最適化(第3のモード)の注記
  //
  // 拘束の固定記号・荷重の矢印+ラベル。`root` に入れない(外接直方体に混ざると初回フレーミングや
  // near/far計算を歪める)。装飾なのでピック対象にも足さない。`setOptimizeState` が唯一の書き手。
  const annotations = createOptimizeAnnotations({
    faceMeshes: () => allFaceMeshes,
    contentDiagonal: () => contentDiagonal,
  })
  annotations.object.renderOrder = LAYER.optimize
  scene.add(annotations.object)

  // 測定の線と札(issue #10)。`root` に入れず、ピック対象にもしない。`showMeasurement` が唯一の書き手。
  const measureAnnotation = createMeasureAnnotation({ contentDiagonal: () => contentDiagonal })
  measureAnnotation.object.renderOrder = LAYER.measure
  scene.add(measureAnnotation.object)

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
    faceMesh: (bodyId, faceIndex) =>
      allFaceMeshes.find(
        (candidate) => candidate.userData.bodyId === bodyId && candidate.userData.faceIndex === faceIndex,
      ),
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

  /**
   * 今ホバーで当てている色(実機検査フック専用)。ホバーは明度だけを上げ、
   * `highlightColors` には変化が出ないので、材質から読み戻す。
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
    const sketchEntity = hoverSketchEntity
    if (sketchEntity !== null) {
      const entry = sketches.get(sketchEntity.sketchId)
      if (sketchEntity.point !== null) {
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
   * 面の材質は (役割 × 状態 × ホバー × 平面陰影) の遅延生成のキャッシュから取る(`variantCache`)。
   * 選択は役割に勝つ: `state !== 'plain'` のときは役割の色を見ない(保持ソリッドを選ぶとハイライト色になる)。
   */
  const faceMaterials = variantCache(
    (role: OptimizeFaceRole, state: FaceMaterialState, hovered: boolean, flat: boolean) =>
      `${state === 'plain' ? role : 'none'}:${state}:${hovered ? 1 : 0}:${flat ? 1 : 0}`,
    (role, state, hovered, flat) => {
      const base = state === 'user' ? HIGHLIGHT_COLOR : roleBaseColor(role)
      return new THREE.MeshLambertMaterial({
        color: hovered ? lighten(base, HOVER_LIGHTEN) : base,
        side: THREE.DoubleSide,
        flatShading: flat,
        clippingPlanes: section.clipPlanes,
      })
    },
  )
  function faceMaterialFor(
    role: OptimizeFaceRole,
    state: FaceMaterialState,
    hovered: boolean,
    flat = false,
  ): THREE.MeshLambertMaterial {
    return faceMaterials.get(role, state, hovered, flat)
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
  /** 全Body分の面メッシュを1本にまとめたもの(ピック用)。update() のたびに作り直す。 */
  let allFaceMeshes: THREE.Mesh[] = []
  let pickListener: ((event: PickEvent) => void) | null = null
  let boxSelectListener: ((event: BoxSelectEvent) => void) | null = null
  let sectionDragListener: ((position: number) => void) | null = null
  let planeMoveListener:
    | ((point: { u: number; v: number } | null, sketchEntity: SketchEntityHit | null) => void)
    | null = null

  // ---------------------------------------------------------------- ホバー強調
  //
  // 「そのままクリックしたら取れるもの」を1つだけ明るくする。対象は面・辺・領域・基準平面・
  // スケッチの図形/点。頂点は出さない(選択に入っている頂点しかマーカーが無い)。
  // 当たり判定は `resolveHit` をクリックと共有する(優先順位を2箇所に書くとプレビューが嘘をつく)。
  /** ポインタの現在位置(canvasの外なら null)。解決はフレームに1回(`tick`)。 */
  let hoverAt: { x: number; y: number } | null = null
  /** 視点回転中(=そのポインタの動きはクリックではない)。回している間は解決しない。 */
  let orbiting = false
  let hoverFace: FaceRef | null = null
  let hoverEdge: EdgeRef | null = null
  let hoverPlane: BasePlaneName | null = null
  let hoverRegion: { sketchId: string; index: number } | null = null
  let hoverSketchEntity: SketchEntityHit | null = null
  // 「変わったときだけ材質を書き換える」ための印(毎フレーム全メッシュを走査しない)。
  let hoverFaceKey = ''
  let hoverEdgeKey = ''
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
    hoverRegion = null
    hoverSketchEntity = null
    hoverFaceKey = ''
    hoverEdgeKey = ''
    hoverRegionKey = ''
    hoverSketchKey = ''
    hoverPlane = null
    pickChanged()
    // 材質まで戻す(印を捨てるだけだと、作り直していない対象の明るい材質が居座る)
    applyFaceMaterials()
    applyEdgeMaterials()
    applyRegionMaterials()
    applyBasePlaneHover()
    applySketchHoverMaterials()
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

  /** `CommandHost` が `.app` へ書く `--toolbar-overlay-h` を読み直す(`ViewCubeDeps.overlayOffset`参照)。
   *  `BrowserPanel`/`SettingsDialog` と同じCSS変数を、canvas側はJSで読むしかない。 */
  function toolbarOverlayOffsetPx(): number {
    const app = container.closest('.app')
    if (!(app instanceof HTMLElement)) return 0
    const px = parseFloat(getComputedStyle(app).getPropertyValue('--toolbar-overlay-h'))
    return Number.isFinite(px) ? px : 0
  }

  function applySize(): void {
    pickChanged() // 寸法が変わると同じ画素が別のものを指す
    const { width, height } = size()
    renderer.setSize(width, height, false)
    const aspect = width / height
    perspective.aspect = aspect
    perspective.updateProjectionMatrix()
    // 正投影は縦(`top`/`bottom`)を正として横を作り直す(`smoke-features.mjs` がv方向を基準にする)。
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
    for (const item of existing.disposables) item.dispose()
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
    const faceMeshes: THREE.Mesh[] = []
    const faceGeometries: THREE.BufferGeometry[] = []
    const sceneObjects: THREE.Object3D[] = []
    const disposables: Array<{ dispose(): void }> = []
    for (const face of payload.faces) {
      if (face.indices.length === 0) continue // 三角形が取れなかった面(サーバが警告を出す)
      const geometry = faceGeometry(face)
      faceGeometries.push(geometry)
      // メッシュボディは面1つ・`kind: "mesh"` で届く(`duocad/meshbody.py` の `tessellate`)。
      // 三角形がそのまま形なので、頂点法線を平均すると元に無い滑らかさが出る。
      const flat = face.kind === 'mesh'
      // 素の材質で生む(役割・選択・ホバーは `rebuildPickIndex()` 経由の `applyFaceMaterials()` が塗り直す)
      const mesh = new THREE.Mesh(geometry, faceMaterialFor('none', 'plain', false, flat))
      mesh.userData.bodyId = bodyId
      mesh.userData.faceIndex = face.index
      mesh.userData.flatShaded = flat // `applyFaceMaterials` は `GeometryPayload` を見ない
      root.add(mesh)
      faceMeshes.push(mesh)
      sceneObjects.push(mesh)
      disposables.push(geometry)

      // 面の境界線(隣り合う同色の面を狙い分けるため)。メッシュボディには出さない
      // (面が1つで、三角形の稜線がそのまま線になって形が潰れる)。
      if (!flat) {
        const edges = new THREE.EdgesGeometry(geometry, 20)
        const line = new THREE.LineSegments(edges, edgeMaterial)
        root.add(line)
        sceneObjects.push(line)
        disposables.push(edges)
      }
    }
    section.setBody(bodyId, faceGeometries)
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
    // 確定前の幽霊を消して本物を作り直し、ローカル幽霊は届いた形で組み直す(`rebuildAround`)。
    ghostLayer.rebuildAround(() => rebuildBodies(entries))
  }

  function rebuildBodies(entries: BodyGeometryUpdate[]): void {
    for (const entry of entries) {
      clearBody(entry.bodyId)
      if (entry.geometry) buildBody(entry.bodyId, entry.geometry)
      // 形が変わったBodyの辺はその場で消す(古い添字の線は違う辺を指す)。要るなら
      // 呼び出し側が版の変化を見て取り直す(`App.svelte`)。
      if (entry.bodyId === edgeBodyId) clearEdges()
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
    if (edgeBodyId !== null && stale.includes(edgeBodyId)) clearEdges()
    for (const bodyId of stale) clearBody(bodyId)
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

  function projectPoint(point: readonly [number, number, number]): { x: number; y: number } | null {
    const { width, height } = size()
    return projectRaw(frameVector(point), width, height)
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
    highlightColor.face =
      selectedFaceKeys.size === 0 && selectedBodyIds.size === 0
        ? null
        : faceMaterialFor('none', 'user', false).color.getHex()
    for (const mesh of allFaceMeshes) {
      const bodyId = mesh.userData.bodyId as string
      const faceIndex = mesh.userData.faceIndex as number
      const hitAsFace = selectedFaceKeys.has(refKey(bodyId, faceIndex))
      const hitAsBody = selectedBodyIds.has(bodyId)
      const state: FaceMaterialState = hitAsFace || hitAsBody ? 'user' : 'plain'
      const role: OptimizeFaceRole = fixtureFaceKeys.has(refKey(bodyId, faceIndex))
        ? 'fixture'
        : (bodyRoles.get(bodyId) ?? 'none')
      const isHovered =
        hoverFace !== null && hoverFace.bodyId === bodyId && hoverFace.faceIndex === faceIndex
      mesh.material = faceMaterialFor(role, state, isHovered, mesh.userData.flatShaded === true)
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
    const faceIndex = hit.object.userData.faceIndex
    if (typeof bodyId !== 'string' || typeof faceIndex !== 'number') return null
    return { bodyId, faceIndex }
  }

  /**
   * 面のピック(`pickAt` は切り口を区別しない呼び出し側向けの薄い覆い)。`ref` が null の当たりは
   * 断面の切り口: 指せないが実体なので、その画素の基準平面を塞ぐ(`resolveHit`)。
   * レイキャストはクリップ平面を見ないので、断面が ON のときは平面をよぎる位置(`section.cut`)を
   * 境に当たりを捨てる。切られた側からレイが入る場合は、同じボディの表面をそれまでに奇数回
   * またいだかで中身かどうかを決める(描画のステンシルの偶奇と同じ。`sectionView.ts`)。
   */
  function faceHitAt(
    offsetX: number,
    offsetY: number,
  ): { ref: FaceRef | null } | null {
    const ray = rayAt(offsetX, offsetY)
    // 隠しているメッシュは指せない(Raycaster は `visible` を見ないので明示的に外す)。
    const hits = raycaster.intersectObjects(
      allFaceMeshes.filter((mesh) => mesh.visible),
      false,
    )
    const cut = section.cut(ray)
    if (cut === 'cut') return null
    if (cut === 'kept') {
      const ref = hits.length === 0 ? null : faceRefOf(hits[0])
      return ref === null ? null : { ref }
    }
    if (cut.originKept) {
      // 残る側から切られた側へ向かうレイ: 平面の手前までしか見えない(切り口は裏から見えない)
      const first = hits[0]
      if (!first || first.distance >= cut.t) return null
      const ref = faceRefOf(first)
      return ref === null ? null : { ref }
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
      if (count % 2 === 1) return { ref: null }
    }
    if (first === null) return null
    const ref = faceRefOf(first)
    return ref === null ? null : { ref }
  }

  function pickAt(offsetX: number, offsetY: number): FaceRef | null {
    return faceHitAt(offsetX, offsetY)?.ref ?? null
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
      const screen = screenOf(point, width, height) // 画面の外・浮いているパネルの下は次の三角形を試す
      if (screen === null) continue
      const hit = pickAt(screen.x, screen.y)
      if (hit && hit.bodyId === bodyId && hit.faceIndex === faceIndex) return screen
    }
    return null
  }

  // ------------------------------------------------------------------ D: 辺(選べる線)
  //
  // 輪郭線の `EdgesGeometry`(`buildBody` 参照)はコアの辺の添字と対応しない。ここで描くのは
  // サーバが配る `kernel.tessellate_edges` の折れ線で、1本ごとにコアの添字を持つ。
  // ソリッドともスケッチとも別のグループに持つ(`update()`/`prune()` は `root` しか触らない)。
  const edgeRoot = new THREE.Group()
  scene.add(edgeRoot)

  type EdgeMaterialState = 'plain' | 'user'
  /** 辺の材質は (状態 × ホバー) のキャッシュから取る。深度検査はしない(奥の辺も見え、指せる)。 */
  const edgeMaterials = variantCache(
    (state: EdgeMaterialState, hovered: boolean) => `${state}:${hovered ? 1 : 0}`,
    (state, hovered) => {
      const base = state === 'user' ? HIGHLIGHT_COLOR : PICK_EDGE_COLOR
      return new THREE.LineBasicMaterial({
        color: hovered ? lighten(base, HOVER_LIGHTEN) : base,
        depthTest: false,
        clippingPlanes: section.clipPlanes,
      })
    },
  )

  /** 今出している辺(1つのBodyぶんだけ)。空なら辺モードではない。 */
  let edgeLines: THREE.Line[] = []
  let edgeBodyId: string | null = null
  /** 今出している辺の元になった配列(同じものを渡されたら作り直さない印)。 */
  let edgeSource: EdgeGeometry[] | null = null
  /** 辺の除去と破棄の束(ピックは `edgeLines` から)。 */
  const edgeBatch = createBatch(edgeRoot)

  function clearEdges(): void {
    edgeBatch.clear()
    edgeLines = []
    edgeBodyId = null
    edgeSource = null
    invalidateHover()
  }

  function showEdges(bodyId: string | null, edges: EdgeGeometry[]): void {
    // 同じものを渡されたら何もしない(`App.svelte` は選択が変わるたびに呼び直すので、毎回
    // 作り直すと辺のクリックのたびに数百本の BufferGeometry を作り直す。27部品で1Bodyに600本超)。
    if (bodyId !== null && bodyId === edgeBodyId && edges === edgeSource) return
    clearEdges()
    if (bodyId === null || edges.length === 0) return
    edgeBodyId = bodyId
    edgeSource = edges
    for (const edge of edges) {
      // 2点(=6要素)未満は線にならない。`kernel.tessellate_edges` は失敗時も端点2点を返す契約なので
      // 届かないはずだが、壊れた線を作らないための番人。
      if (edge.points.length < 6) continue
      const geometry = new THREE.BufferGeometry()
      geometry.setAttribute(
        'position',
        new THREE.BufferAttribute(new Float32Array(edge.points), 3),
      )
      geometry.computeBoundingSphere()
      const line = new THREE.Line(geometry, edgeMaterials.get('plain', false))
      line.renderOrder = LAYER.edge // 深度を書かないぶん、描く順序で面より手前に出す
      line.userData.bodyId = bodyId
      line.userData.edgeIndex = edge.index
      edgeLines.push(edgeBatch.add(line, geometry))
    }
  }

  /** 今出している辺のうち選択に入っているもの(面の `selectedFaceKeys` と同じ役目)。 */
  let selectedEdgeIndices = new Set<number>()

  /** 辺の材質を決める。選択とホバーの両方を見る唯一の場所。 */
  function applyEdgeMaterials(): void {
    // 色は材質から読み戻す(`highlightColors()` の検査がこの値を読む)。
    highlightColor.edge = selectedEdgeIndices.size === 0 ? null : edgeMaterials.get('user', false).color.getHex()
    for (const line of edgeLines) {
      const edgeIndex = line.userData.edgeIndex as number
      const isHit = selectedEdgeIndices.has(edgeIndex)
      const isHovered =
        hoverEdge !== null &&
        hoverEdge.bodyId === (line.userData.bodyId as string) &&
        hoverEdge.edgeIndex === edgeIndex
      line.material = edgeMaterials.get(isHit ? 'user' : 'plain', isHovered)
    }
  }

  function highlightEdges(refs: EdgeRef[]): void {
    selectedEdgeIndices = new Set(
      refs.filter((ref) => ref.bodyId === edgeBodyId).map((ref) => ref.edgeIndex),
    )
    applyEdgeMaterials()
  }

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

  const pickScratch = new THREE.Vector3()

  /** 折れ線1本ぶんの world 座標の読み口(配列は作らない)。 */
  interface PickPolyline {
    count: number
    at(index: number, out: THREE.Vector3): void
  }

  /** 折れ線を screen 上で歩き、カーソルまでの距離²[px²]の最小を返す。射影できた隣り合う2点だけを線分と見る。見えなければ null。 */
  function polylineScreenDistSq(
    offsetX: number,
    offsetY: number,
    polyline: PickPolyline,
    width: number,
    height: number,
    clip: ((point: THREE.Vector3) => number) | null = null,
  ): number | null {
    if (clip !== null) return clippedPolylineScreenDistSq(offsetX, offsetY, polyline, width, height, clip)
    let best = Infinity
    let previousX = 0
    let previousY = 0
    let hasPrevious = false
    for (let at = 0; at < polyline.count; at += 1) {
      polyline.at(at, pickScratch)
      const screen = projectRaw(pickScratch, width, height)
      if (screen === null) {
        hasPrevious = false
        continue
      }
      if (hasPrevious) {
        const distSq = distSqPointSegment(offsetX, offsetY, previousX, previousY, screen.x, screen.y)
        if (distSq < best) best = distSq
      }
      previousX = screen.x
      previousY = screen.y
      hasPrevious = true
    }
    return best === Infinity ? null : best
  }

  const clipScratchPrevious = new THREE.Vector3()
  const clipScratchA = new THREE.Vector3()
  const clipScratchB = new THREE.Vector3()

  /**
   * `polylineScreenDistSq` の断面つき版。`clip`(残る側が正の符号付き距離)で切られた線分は落とし、
   * 平面をまたぐ線分は交点で切り詰めてから測る(端点を落とすだけだと切り口手前の見えている部分が指せない)。
   */
  function clippedPolylineScreenDistSq(
    offsetX: number,
    offsetY: number,
    polyline: PickPolyline,
    width: number,
    height: number,
    clip: (point: THREE.Vector3) => number,
  ): number | null {
    let best = Infinity
    let previousDistance = 0
    let hasPrevious = false
    for (let at = 0; at < polyline.count; at += 1) {
      polyline.at(at, pickScratch)
      const distance = clip(pickScratch)
      if (hasPrevious && (previousDistance >= 0 || distance >= 0)) {
        clipScratchA.copy(clipScratchPrevious)
        clipScratchB.copy(pickScratch)
        const crossing = previousDistance / (previousDistance - distance)
        if (previousDistance < 0) clipScratchA.lerpVectors(clipScratchPrevious, pickScratch, crossing)
        else if (distance < 0) clipScratchB.lerpVectors(clipScratchPrevious, pickScratch, crossing)
        const a = projectRaw(clipScratchA, width, height)
        const b = projectRaw(clipScratchB, width, height)
        if (a !== null && b !== null) {
          const distSq = distSqPointSegment(offsetX, offsetY, a.x, a.y, b.x, b.y)
          if (distSq < best) best = distSq
        }
      }
      clipScratchPrevious.copy(pickScratch)
      previousDistance = distance
      hasPrevious = true
    }
    return best === Infinity ? null : best
  }

  /** 素の `THREE.Line`(ソリッドの辺)の折れ線。position 属性 × `matrixWorld`。 */
  function bufferPolyline(line: THREE.Line): PickPolyline | null {
    const position = line.geometry.getAttribute('position')
    if (!position || position.count < 2) return null
    line.updateWorldMatrix(true, false)
    const matrix = line.matrixWorld
    return {
      count: position.count,
      at(index, out) {
        out.set(position.getX(index), position.getY(index), position.getZ(index)).applyMatrix4(matrix)
      },
    }
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
    clip: ((point: THREE.Vector3) => number) | null = null,
  ): T | null {
    if (lines.length === 0) return null
    const { width, height } = size()
    const candidates: Array<ScreenPickCandidate<T>> = []
    for (const line of lines) {
      const item = readItem(line)
      if (item === null) continue
      const polyline = polylineOf(line)
      if (polyline === null) continue
      const distSq = polylineScreenDistSq(offsetX, offsetY, polyline, width, height, clip)
      if (distSq === null) continue
      candidates.push({ item, distSq })
    }
    return pickNearestWithinRadius(radiusPx, candidates)
  }

  function edgeAt(offsetX: number, offsetY: number): EdgeRef | null {
    return pickLinesAt(
      offsetX,
      offsetY,
      EDGE_PICK_PIXELS,
      edgeLines,
      (line) => {
        const { bodyId, edgeIndex } = line.userData
        if (typeof bodyId !== 'string' || typeof edgeIndex !== 'number') return null
        return { bodyId, edgeIndex }
      },
      bufferPolyline,
      section.signedDistance(),
    )
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
    // 端点は避ける(隣の辺と共有していて隣が取れることがある)。中ほどから順に試し、実際にその辺へ当たる画素だけを返す。
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

  const vertexMarkers = createVertexMarkers({
    pickPointsAt,
    toScreen,
    pickChanged,
    highlightColor,
    clipPlanes: section.clipPlanes,
    sectionKeeps: (point) => section.keeps(point),
  })
  scene.add(vertexMarkers.object)

  // ------------------------------------------------------------------ C: スケッチ
  //
  // 描くのは線と領域だけ。スケッチ平面そのもの(無限に広がる面)を描くと奥のソリッドが隠れる。
  // ソリッドとは別のグループ(`sketchRoot`。宣言は `root` の隣)に持つ。`update()`/`prune()` は `root` しか触らない。

  const sketchLineResolution = new THREE.Vector2(1, 1)
  const sketchMaterial = createSketchLineMaterial(SKETCH_COLOR, sketchLineResolution)
  const sketchGhostMaterial = createSketchLineMaterial(SKETCH_GHOST_COLOR, sketchLineResolution)
  const sketchGhostWarnMaterial = createSketchLineMaterial(SKETCH_GHOST_WARN_COLOR, sketchLineResolution)

  /**
   * スケッチの線・点の材質は (選択状態 × ホバー) のキャッシュから取る(`faceMaterialFor` と同じ形)。
   * 反復を展開した `entities` 側の描画は通らない(`sketchMaterial` を直接使う。ピック対象外)。
   */
  type SketchMaterialState = 'plain' | 'selected'
  type SketchLineMaterial = ReturnType<typeof createSketchLineMaterial>
  const sketchStateKey = (state: SketchMaterialState, hovered: boolean): string => `${state}:${hovered ? 1 : 0}`
  // `plain` の非ホバーは既存の `sketchMaterial` を種にする(インスタンスを分裂させない)。
  const sketchLineMaterials = variantCache(
    sketchStateKey,
    (state, hovered) => {
      const base = state === 'selected' ? SKETCH_SELECTED_COLOR : SKETCH_COLOR
      return createSketchLineMaterial(hovered ? lighten(base, HOVER_LIGHTEN) : base, sketchLineResolution)
    },
    [['plain:0', sketchMaterial]],
  )
  function sketchLineMaterialFor(state: SketchMaterialState, hovered: boolean): SketchLineMaterial {
    return sketchLineMaterials.get(state, hovered)
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
  /** 領域の材質は (選択 × ホバー) のキャッシュから取る。不透明度は選択で変わる(非選択 0.35 / 選択 0.55)。 */
  const regionMaterials = variantCache(
    (selected: boolean, hovered: boolean) => `${selected ? 1 : 0}:${hovered ? 1 : 0}`,
    (selected, hovered) => {
      const base = selected ? REGION_SELECTED_COLOR : REGION_COLOR
      return new THREE.MeshBasicMaterial({
        color: hovered ? lighten(base, HOVER_LIGHTEN) : base,
        side: THREE.DoubleSide,
        transparent: true,
        opacity: selected ? 0.55 : 0.35,
        depthWrite: false,
      })
    },
  )

  interface SketchScene {
    frame: SketchDraw['frame']
    plane: SketchDraw['plane']
    /** シーンに足したもの(除去と破棄のため。ピック用の配列は別に持つ)。 */
    batch: SceneBatch
    regionMeshes: THREE.Mesh[]
    /** 図形の (u,v) の折れ線(視点合わせの中心を出すのに使う)。 */
    points: Array<[number, number]>
    /** 図形1本ごとの折れ線の点数(実機検査の `sketchPolylineCounts` が読む)。 */
    polylineCounts: number[]
    /** 点のマーカー(1点1オブジェクト)。当たった点からコアの参照へ戻すため `userData` に `entityId` と `point`(xキー)を持つ(まとめて1つの `Points` にすると戻れない)。 */
    handleMeshes: THREE.Points[]
    /** 図形の折れ線(元の図形だけ。`userData.entityId` を持つ)。辺のピック対象。 */
    handleLines: SketchFatLine[]
    /** 元の図形(`handles`)。スナップ座標の解決に使う。 */
    handles: SketchDraw['handles']
    /**
     * 直線区間(line/rectangleの辺)どうしの交点(`sketchIntersections`。曲線は対象外)。
     * `sketchCornerAt` が本物の点・`visualCorners` と同じ優先度で拾うために持つ。
     */
    intersections: ReturnType<typeof sketchIntersections>
  }
  const sketches = new Map<string, SketchScene>()
  let ghost: SceneBatch | null = null
  /** いま描けているスケッチの幽霊の中身(`ghostInfo` が返す。実機検査用の穴)。 */
  let sketchGhostInfo: Array<{ kind: string; params: Record<string, number> }> = []
  let activeSketchId: string | null = null
  /** 通常モードで領域を指せるか(`pickRegions`)。スケッチモード中は見ない(常にそのスケッチの領域が対象)。 */
  let regionPickWanted = false

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
    sketches.delete(sketchId)
  }

  /** 直近に受け取った描画指示。解像度が変わったとき折り直すために控える。 */
  let lastSketchDraws: SketchDraw[] = []

  /** 控えた指示で描き直す(`resolutionTracker` が解像度を採り直したときに呼ぶ)。中身が無ければ何もしない。 */
  function redrawSketches(): void {
    if (lastSketchDraws.length > 0) showSketches(lastSketchDraws)
  }

  function showSketches(entries: SketchDraw[]): void {
    lastSketchDraws = entries
    const keep = new Set(entries.map((entry) => entry.sketchId))
    for (const sketchId of [...sketches.keys()]) if (!keep.has(sketchId)) clearSketch(sketchId)
    for (const entry of entries) {
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

      for (const entity of entry.entities) {
        const built = buildEntityLine(entry.frame, entity, sketchMaterial)
        if (!built) continue
        batch.add(built.object, built.geometry)
        points.push(...built.points)
        polylineCounts.push(built.points.length)
      }

      // 元の図形(`handles`)を展開した描画の上に重ねる: ここに置くのは「指せるもの」
      // (反復の複製は導出物で `entity_id` を持たず、指す対象にならない)。
      for (const handle of entry.handles) {
        const selected = pickedWhole.has(handle.entityId)
        const built = buildEntityLine(
          entry.frame,
          handle,
          sketchLineMaterialFor(selected ? 'selected' : 'plain', false),
        )
        if (built) {
          built.object.renderOrder = LAYER.sketchLine
          built.object.userData.entityId = handle.entityId
          // 選択かどうかの印(領域の `userData.selected` と同じ理由)
          built.object.userData.selected = selected
          handleLines.push(batch.add(built.object, built.geometry))
        }
        // 点が互いに独立して動く種別だけ常時表示する(entityHasIndependentPoints)。
        // rectangle/circle/arc は点が1個で、その点だけ動かすのと図形全体を動かすのが
        // move_sketch_entities 内で同じ処理になるため、ホバー/選択のときだけ見せる
        // (ピック可能な座標は変えない。`pickPointsAt` は mesh.visible を見ない)。
        const showPointsAtRest = entityHasIndependentPoints(handle.kind)
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
      sketches.set(entry.sketchId, {
        frame: entry.frame,
        plane: entry.plane,
        batch,
        regionMeshes,
        points,
        polylineCounts,
        handleMeshes,
        handleLines,
        handles: entry.handles,
        intersections: sketchIntersections(entry.handles),
      })
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
        line.material = sketchLineMaterialFor(selected ? 'selected' : 'plain', hovered)
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
    sketchGhostInfo = []
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
    for (const entity of entities) {
      const built = buildEntityLine(target.frame, entity, material)
      if (!built) continue
      batch.add(built.object, built.geometry)
      // 実際に描けたものだけ控える(折れ線化できなかった図形を数えると検査が「出ている」と誤判断する)
      sketchGhostInfo.push({ kind: entity.kind, params: { ...entity.params } })
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
   * (点・角・交点より下、辺より上)で拾う(本人指定「吸着優先順位は点と線の間」)。`snapDirection` は
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

  function pickRegions(on: boolean): void {
    if (regionPickWanted === on) return
    regionPickWanted = on
    pickChanged() // ゲートの開閉で「指せるもの」が変わる(止まったままでも解決し直す)
  }

  /** 頂点を辺より先に見るか(`preferVertices`)。既定は辺が先(`resolveHit` の順序)。 */
  let vertexPickWanted = false

  function preferVertices(on: boolean): void {
    if (vertexPickWanted === on) return
    vertexPickWanted = on
    pickChanged() // ゲートの開閉で「指せるもの」が変わる(`pickRegions` と同じ)
  }

  /**
   * 今クリックで当たってよい領域メッシュ。スケッチモード中はそのスケッチの領域だけ(ゲートは見ない)。
   * 通常モードは `pickRegions(true)` の間だけ全スケッチの領域で、出していない間は空配列(面のピックはそのまま動く)。
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
    rayAt(offsetX, offsetY)
    // 近い方が勝つ(`intersectObjects` は距離順)
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
    const normal = frameVector(frame.normal).normalize()
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
   * (`sketchCursorOverlay.ts`)専用。方向だけ要るので `projectForPick` を使う(`toScreen` はDOM被覆を見るため、
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
    // 重心がその近くに来る。`smoke-svg` の丘の領域で実際に踏んだ)。
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
      // スケッチも一致を見る(通常モードは全スケッチの領域が対象で、添字だけだと別スケッチの同じ添字を当たりと読む)
      const hit = regionAt(screen.x, screen.y)
      if (!hit || hit.sketchId !== sketchId || hit.index !== index) continue
      // スケッチの図形が勝つ画素は返さない(スケッチモードでは点・辺が領域より手前で拾われ、`project*` の約束が破れる)。
      // 判定はクリックと同じ `sketchEntityAt`。
      if (sketchEntityAt(screen.x, screen.y) !== null) continue
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

  function collectInBox(rect: BandRect): Omit<BoxSelectEvent, 'mode'> {
    const faces: FaceRef[] = []
    const edges: EdgeRef[] = []
    const vertices: VertexRef[] = []
    const regions: Array<{ sketchId: string; index: number }> = []
    const sketchEntities: SketchEntityHit[] = []

    // 寸法はこの呼び出しの間不変なので1回だけ読んで使い回す(`projectRaw` 参照)
    const { width, height } = size()

    if (activeSketchId === null) {
      const keep = section.keepsFilter()
      // 面は常に指せる(クリックと同じ)。隠したBodyのメッシュは `visible=false` なので外す。
      for (const [bodyId, entry] of bodies) {
        for (const mesh of entry.faceMeshes) {
          if (!mesh.visible) continue
          if (!band.objectInside(mesh as THREE.Mesh & { geometry: THREE.BufferGeometry }, rect, width, height, keep)) continue
          const faceIndex = mesh.userData.faceIndex
          if (typeof faceIndex === 'number') faces.push({ bodyId, faceIndex })
        }
      }
      // 辺・頂点は「出している間だけ」(クリックの `edgeAt` / `vertexAt` と同じゲート)
      for (const line of edgeLines) {
        if (!band.objectInside(line as THREE.Line & { geometry: THREE.BufferGeometry }, rect, width, height, keep)) continue
        const { bodyId, edgeIndex } = line.userData
        if (typeof bodyId === 'string' && typeof edgeIndex === 'number') edges.push({ bodyId, edgeIndex })
      }
      for (const mesh of vertexMarkers.meshes()) {
        if (!band.objectInside(mesh as THREE.Points & { geometry: THREE.BufferGeometry }, rect, width, height, keep)) continue
        const { bodyId, vertexIndex } = mesh.userData
        if (typeof bodyId === 'string' && typeof vertexIndex === 'number') vertices.push({ bodyId, vertexIndex })
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
    }

    // 領域はクリックと同じゲート(`regionPickTargets`)。スケッチモード中はそのスケッチだけ。
    for (const mesh of regionPickTargets()) {
      if (!band.objectInside(mesh as THREE.Mesh & { geometry: THREE.BufferGeometry }, rect, width, height)) continue
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
    // 頂点を直接ピックさせている間だけ、頂点が辺より先(`preferVertices`)。既定の順序では角の頂点は取れない
    // (頂点は辺の端点に乗っており、`move_body` のように辺を出しているコマンドでは `edgeAt` が必ず先に当たる)。
    // ゲートを閉じている間は既定の順序で、フィレット中に辺の端を掴めることは変わらない。
    if (vertexPickWanted) {
      const wanted = vertexMarkers.at(offsetX, offsetY)
      if (wanted !== null) return { ...nothing, vertex: wanted }
    }
    // 辺を出しているときは辺が先(辺は面の上に重なり、面より後に見ると必ず面が勝つ)。出していないときは
    // `edgeAt` が即 null を返す。この順序は変えない(基準平面を足しても辺→面は同じ)。
    const edge = edgeAt(offsetX, offsetY)
    if (edge !== null) return { ...nothing, edge }
    // 領域を要求しているときは領域が先(辺と同じ「要求中のものが勝つ」)。領域はスケッチ平面の上に浮いて
    // 押し出した板の底面と重なるので、面より後だと視点によって面が勝つ。`pickRegions(false)` の間は `regionAt` が即 null。
    const region = regionAt(offsetX, offsetY)
    if (region !== null) return { ...nothing, region }
    // 頂点マーカーは面より先(最小の的なので当たったなら狙い)。辺・領域より後なのは、頂点が辺の端点に
    // 乗っており、先に見るとフィレット起動中に辺の端が掴めなくなるため。出していない間は `vertexAt` が即 null。
    const vertex = vertexMarkers.at(offsetX, offsetY)
    if (vertex !== null) return { ...nothing, vertex }
    // 面に当たる画素では面が勝ち、基準平面は面の無い画素でだけ取れる(レイ距離では競わせない。わざと:
    // 部品のシルエットに重なる部分の基準平面は指せない。#109、本人判断 2026-10-07、docs/VIEWPORT.md「ピック」)。
    // 基準平面は `showBasePlanes(true)` の間しか当たらない。
    const face = faceHitAt(offsetX, offsetY)
    if (face !== null) return { ...nothing, hit: face.ref }
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
    // ハンドルのドラッグは `pointercancel` でだけ打ち切る(`pointerleave` は `setPointerCapture` 中には来ず、来ても座標は届き続ける。キューブと同じ)。
    if (event.type === 'pointercancel') section.pointerUp()
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

  const observer = new ResizeObserver(() => applySize())
  observer.observe(container)
  applySize()

  /**
   * ホバー強調を1フレームぶん更新する。毎フレーム解決し直す(視点を回すとポインタが動かなくても
   * 当たるものが変わる)。材質の書き換えは変わったときだけ。
   */
  function updateHover(): void {
    const point = orbiting ? null : hoverAt
    const probe =
      point === null
        ? ''
        : `${pickGeneration},${point.x},${point.y},` +
          `${camera.position.toArray().join(',')},${camera.quaternion.toArray().join(',')},` +
          // `zoom` も入れる: 正投影のドリーは `zoom` だけを動かすので、無いとホイールで寄っても当たるものが変わらない。
          `${camera.zoom}`
    if (probe === hoverProbe) return
    hoverProbe = probe
    const next = point === null ? null : resolveHit(point.x, point.y)
    // 頂点は出さない: 頂点が取れる画素では面も辺も光らない(クリックしても面は選ばれない)。
    // スケッチの点・辺も同じ扱い: 予告の色は付けないが、そこが取れるときは領域を光らせない
    // (点・辺は領域より手前で拾われ、光らせると予告が嘘になる)。
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
    const sketchEntity = next?.sketchEntity ?? null
    const nextSketchKey =
      sketchEntity === null ? '' : `${sketchEntity.sketchId}|${sketchEntity.entityId}|${sketchEntity.point ?? ''}`
    if (nextSketchKey !== hoverSketchKey) {
      hoverSketchKey = nextSketchKey
      hoverSketchEntity = sketchEntity
      applySketchHoverMaterials()
    }
    if (plane !== hoverPlane) {
      hoverPlane = plane
      applyBasePlaneHover()
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

  function tick(): void {
    if (!alive) return
    cube.stepSnap(performance.now())
    controls.update()
    syncCameraUp()
    // 描画ループで見る(`OrbitControls` の 'change' ではない): 減衰つきのドリーはイベントが止まった後も
    // 動き続けるので、「止まったか」はフレーム側でしか分からない。
    resolution.update(performance.now())
    updateHover()
    cube.updateHover()
    // カーソル追従もフレームごとに引き直す(`onPointerMove` だけに任せない): ホイールでズームすると `hoverAt` は
    // 動かないがレイと平面frameの交点(`planePointAt`)は変わり、カーソルを動かさず寄せ引きするだけではグリッドが
    // 追従しない(本人報告2026-09-11)。スケッチ非活性なら `planePointAt`/`grid.followCursor` は即 `null`/no-op。
    grid.followCursor(hoverAt === null ? null : planePointAt(hoverAt.x, hoverAt.y))
    // グリッドは毎フレームここだけを更新する(段の不透明度とカメラ行列。ジオメトリは無い)。
    grid.updateUniforms()
    section.update()
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
    optimizeDebugCounts: optimizeDebug.counts,
    showEdges,
    showBasePlanes,
    setOriginVisible: originGizmo.setVisible,
    setSection: section.setState,
    onSectionDrag(listener) {
      sectionDragListener = listener
    },
    sectionInfo: section.info,
    contentCenter() {
      const box = new THREE.Box3().setFromObject(root)
      if (box.isEmpty()) return null
      const center = box.getCenter(new THREE.Vector3())
      return [center.x, center.y, center.z]
    },
    pickRegions,
    preferVertices,
    highlightEdges,
    projectEdge,
    showVertices: vertexMarkers.show,
    showMeasurement: measureAnnotation.show,
    measurementInfo: measureAnnotation.info,
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
    optimizeAnnotationCount: annotations.count,
    optimizeAnnotations: annotations.info,
    hoveredRef() {
      return {
        face: hoverFace,
        edge: hoverEdge,
        region: hoverRegion,
        plane: hoverPlane,
        sketchEntity: hoverSketchEntity,
        color: hoverColorNow(),
      }
    },
    projectVertex: vertexMarkers.project,
    showPreview: ghostLayer.showPreview,
    clearPreview: ghostLayer.clearPreview,
    showBodyGhosts: ghostLayer.showBodyGhosts,
    ghostInfo() {
      return {
        sketch: sketchGhostInfo.map((item) => ({ kind: item.kind, params: { ...item.params } })),
        bodies: ghostLayer.info(),
      }
    },
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
    gridScale() {
      return { extent: grid.extent() }
    },
    gridTiers: grid.info,
    setFov,
    cameraPose,
    projectPoint,
    projectSketchPoint,
    projectBasePlane,
    projectRegion,
    viewCubeRect: cube.rect,
    captureThumbnail() {
      // `object.visible` は見ない: 文書で非表示の立体は `bodies` にキーごと無く、`visible=false` はプレビュー中の
      // 一時的な非表示(`setBodyVisible`)。見るとプレビュー中に撮った絵から立体が欠ける。
      return thumbnailer.capture([...bodies.values()].flatMap((entry) => entry.sceneObjects))
    },
    dispose() {
      alive = false
      cancelAnimationFrame(frameHandle)
      observer.disconnect()
      thumbnailer.dispose()
      renderer.domElement.removeEventListener('pointerdown', onPointerDown)
      renderer.domElement.removeEventListener('pointerup', onPointerUp)
      renderer.domElement.removeEventListener('pointermove', onPointerMove)
      renderer.domElement.removeEventListener('pointerleave', onPointerLeave)
      renderer.domElement.removeEventListener('pointercancel', onPointerLeave)
      cube.dispose()
      sketchCursor.dispose()
      ghostLayer.clearPreview()
      clearGhost()
      clearEdges()
      vertexMarkers.dispose()
      edgeMaterials.dispose()
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
      faceMaterials.dispose()
      previewMaterial.dispose()
      edgeMaterial.dispose()
      annotations.clear()
      optimizeDebug.clear()
      renderer.dispose()
      renderer.domElement.remove()
    },
  }
}
