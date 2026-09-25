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
import type { BasePlaneName, EdgeGeometry } from './session'
import {
  type BodyGeometryUpdate, type BodyGhostRotation, type BoxSelectEvent, type EdgeRef, type FaceGeometry,
  type FaceRef, type GeometryPayload, type OptimizeVisualState, type PickEvent, type PickMode, type SectionState,
  type SketchDraw, type SketchEntityHit, type VertexMarker, type VertexRef, type Viewport,
} from './viewportTypes'
import {
  BACKGROUND_COLOR, BASE_COLOR, BASE_PLANES, BASE_PLANE_PROBES, BOX_SELECT_MIN_PX, CLICK_MOVE_LIMIT_PX, CLICK_TIME_LIMIT_MS,
  EDGE_COLOR, EDGE_PICK_PIXELS, FOV_DEFAULT, FOV_MAX, FOV_MIN, FOV_ORTHO, HIGHLIGHT_COLOR, HOME_VIEW_OFFSET, HOVER_LIGHTEN, LAYER,
  OPTIMIZE_EXCLUDE_COLOR, OPTIMIZE_FIXTURE_COLOR, OPTIMIZE_PRESERVE_COLOR, PICK_EDGE_COLOR, PREVIEW_COLOR, PROPOSED_COLOR,
  REGION_COLOR, REGION_SELECTED_COLOR, SKETCH_COLOR, SKETCH_FIT_MARGIN, SKETCH_GHOST_COLOR, SKETCH_HANDLE_COLOR,
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
  // **`BASE_EXTENT` を使う**(可変の `grid.extent()` ではない) —— ここは起動直後の1回きりで、
  // `grid` はまだ作られておらず(下の平面グリッドの節より前)、その初期値は
  // 常に `BASE_EXTENT` だから同じ値になる。
  //
  // **距離は `fitDistance` で求める**。決め打ちの `diagonal * 1.2` のような定数は
  // FOV_DEFAULT を変えると式とずれる(45度では差0.6%で気づきにくいが、30度では55%もずれ、
  // 起動直後の空の文書やまだ内容の無いスケッチが想定より大きく寄って見える事故になる ——
  // `smoke-move.mjs` の辺クリックが実際にこれで外れた)。`frameToContent` と同じ式を
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
  // **マウスの割り当て**: 左=ウィンドウ選択(このファイルが自前で
  // 実装する)/ 中=パン / 右=カメラ回転。
  //
  // `OrbitControls` の既定は LEFT=ROTATE / MIDDLE=DOLLY / RIGHT=PAN なので、**3つとも
  // 変える**。左を `null` にするのは、
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
  // 初期化中の `createGridPlanes()` が既にこれを読む。宣言をC節に置いたままにすると
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
  const annotations = createOptimizeAnnotations({
    faceMeshes: () => allFaceMeshes,
    contentDiagonal: () => contentDiagonal,
  })
  annotations.object.renderOrder = LAYER.optimize
  scene.add(annotations.object)

  // 測定の線と札(issue #10)。装飾で指せない(`root` にも入れない —— 上と同じ理由)。
  // `showMeasurement` が唯一の書き手。
  const measureAnnotation = createMeasureAnnotation({ contentDiagonal: () => contentDiagonal })
  measureAnnotation.object.renderOrder = LAYER.measure
  scene.add(measureAnnotation.object)

  // ------------------------------------------------------------------ 開発用のデバッグ表示
  //
  // 塊(`?debug=space`)・平滑化前のボクセル(`?voxel=1`)・面アンカーの節点
  // (`?debug=anchors`)。**`root` には入れない**(注記と同じ理由 —— 外接直方体に混ざると
  // 初回フレーミングやnear/far、グリッドの範囲まで歪む)。ピックの対象にも足さない。
  // `setOptimizeDebug` が唯一の書き手。
  const optimizeDebug = createOptimizeDebug({ setBodyVisible })
  optimizeDebug.object.renderOrder = LAYER.debug
  scene.add(optimizeDebug.object)

  // ------------------------------------------------------------------ 平面グリッド(3段固定・無限)
  //
  // 描く平面・基準平面のピック範囲・ホバー板・面外の軸線は `gridPlanes.ts`。
  // `root` には入れない —— 入れると初回の `frameToContent` がグリッドの箱に引っ張られる。
  // ピック対象にもしない(判定は `basePlaneAt` の解析交差が別に持ち、範囲の半幅は `grid.extent()`)。
  // **`root` / `sketchRoot` を作った後で呼ぶ**(最初の描画で `contentBoxForGrid` を読む)。
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
  // 原点・軸・平面の7項目を1つのGroupにまとめた装飾(`originGizmo.ts`)。**`grid.object` には
  // 入れない** —— あちらは `groupOrder` を -1 に固定しており、中に入れると個々の
  // `LAYER.originGizmo` が効かなくなる。理由は `root` にも入れない(初回フレーミングを
  // 歪めない、既存装飾群と同じ規律)。既定は表示。
  const originGizmo = createOriginGizmo()
  scene.add(originGizmo.object)

  // ------------------------------------------------------------------ 断面表示(issue #6)
  //
  // 渡す関数はどれも後で呼ばれる閉包なので、後方で宣言する `size` / `projectRaw` を読んでも TDZ にかからない。
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

  /**
   * 面の材質は **(役割 × 選択状態 × ホバー × 陰影)** をキーにした遅延生成のキャッシュから取る。
   *
   * 役割は形状最適化(第3のモード)の保持/除外/拘束で、素の面(`'none'`)を含めて4種。
   * 選択状態は3種(未選択 `'plain'` / 確定 `'user'` / AI提案 `'proposed'`)、ホバーは2種、
   * 陰影は2種(平滑 / メッシュボディのフラット)。組み合わせは最大48だが、**使った組だけ**を
   * この中で作る(全部を手で書き下さない) —— 役割が無かった頃の6個(base/highlight/proposed
   * × 通常/ホバー)から増える軸を、静的な定数ではなく関数1つに閉じるための形。
   *
   * **選択は役割に勝つ**。`state !== 'plain'` のときは役割を見ない ——
   * 保持ソリッド(緑)を選ぶとハイライト色になり、緑は一時的に見えなくなる。
   */
  type OptimizeFaceRole = 'none' | 'preserve' | 'exclude' | 'fixture'
  type FaceMaterialState = 'plain' | 'user' | 'proposed'

  function roleBaseColor(role: OptimizeFaceRole): number {
    if (role === 'preserve') return OPTIMIZE_PRESERVE_COLOR
    if (role === 'exclude') return OPTIMIZE_EXCLUDE_COLOR
    if (role === 'fixture') return OPTIMIZE_FIXTURE_COLOR
    return BASE_COLOR
  }

  /**
   * 面の材質は (役割 × 状態 × ホバー × 平面陰影) の遅延生成のキャッシュから取る(`variantCache`)。
   * `state !== 'plain'` のときは役割(保持・除外・拘束の色)を見ない —— 選択が役割に勝つ。
   */
  const faceMaterials = variantCache(
    (role: OptimizeFaceRole, state: FaceMaterialState, hovered: boolean, flat: boolean) =>
      `${state === 'plain' ? role : 'none'}:${state}:${hovered ? 1 : 0}:${flat ? 1 : 0}`,
    (role, state, hovered, flat) => {
      const base =
        state === 'user'
          ? HIGHLIGHT_COLOR
          : state === 'proposed'
            ? PROPOSED_COLOR
            : roleBaseColor(state === 'plain' ? role : 'none')
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
    // 半透明どうしが互いを隠して真っ黒な塊にならないようにする(奥行きは書かない)
    depthWrite: false,
    clippingPlanes: section.clipPlanes,
  })
  const edgeMaterial = new THREE.LineBasicMaterial({ color: EDGE_COLOR, clippingPlanes: section.clipPlanes })

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
  /** `rawGeometry` と寿命を揃える可視判定用サンプル(`resolutionTracker` の `computeFocus` が2835面を毎フレーム走査しない)。 */
  const faceSamples = new Map<string, BodySamples>()
  /** `faceSamples` が変わった印。姿勢が止まっていても形が届けば `computeFocus` をやり直す。 */
  let focusGeneration = 0
  /** 全Body分の面メッシュを1本にまとめたもの(ピック用)。update() のたびに作り直す。 */
  let allFaceMeshes: THREE.Mesh[] = []
  /** プレビュー(幽霊)のメッシュ。**ピックの対象にしない**ので `bodies` とは別に持つ。 */
  let pickListener: ((event: PickEvent) => void) | null = null
  let boxSelectListener: ((event: BoxSelectEvent) => void) | null = null
  let sectionDragListener: ((position: number) => void) | null = null
  let planeMoveListener:
    | ((point: { u: number; v: number } | null, sketchEntity: SketchEntityHit | null) => void)
    | null = null

  // ---------------------------------------------------------------- ホバー強調
  //
  // **「そのままクリックしたら取れるもの」を1つだけ明るくする**。
  // 対象は面・辺・領域・基準平面・スケッチの図形/点で、**頂点は出さない** ——
  // 選択に入っている頂点しかマーカーが無く、「クリックすれば選択されるもの」を出せる
  // 状態にない(スケッチの点マーカーは常に描かれているので、頂点と違って対象にできる)。
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
  let hoverSketchEntity: SketchEntityHit | null = null
  // 「変わったときだけ材質を書き換える」ための印(毎フレーム全メッシュを走査しない)。
  let hoverFaceKey = ''
  let hoverEdgeKey = ''
  let hoverRegionKey = ''
  let hoverSketchKey = ''
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
    hoverSketchEntity = null
    hoverFaceKey = ''
    hoverEdgeKey = ''
    hoverRegionKey = ''
    hoverSketchKey = ''
    hoverPlane = null
    pickChanged()
    // **材質まで戻す**。参照と印を捨てるだけだと、作り直していない対象に付けた明るい材質が
    // そのまま残る(次の解決が「なし」や別種別だと、誰も戻さないまま画面に居座る)。
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
    // **正投影は縦(`top`/`bottom`)を正として横を作り直す。** 透視の `aspect` と同じで、
    // 縦の見え方を寸法に依存させない(`smoke-features.mjs` がv方向を基準に置いている)。
    // **`zoom` には触らない** —— あれは OrbitControls のドリーの持ち物である。
    orthographic.right = orthographic.top * aspect
    orthographic.left = -orthographic.top * aspect
    orthographic.updateProjectionMatrix()
    // 使っていない側も同時に整えてある(切り替えた瞬間に1フレーム歪むのを防ぐ)。
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
   * 1面ぶんの `BufferGeometry` を作る(本物と幽霊で**同じ形の作り方**)。
   *
   * 本物(`buildBody`)と確定前の幽霊(`ghostLayer.ts`)はどちらも同じ三角形群から作るので、
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
    const faceGeometries: THREE.BufferGeometry[] = []
    const sceneObjects: THREE.Object3D[] = []
    const disposables: Array<{ dispose(): void }> = []
    for (const face of payload.faces) {
      if (face.indices.length === 0) continue // 三角形が取れなかった面(サーバが警告を出す)
      const geometry = faceGeometry(face)
      faceGeometries.push(geometry)
      // メッシュボディは**面1つ・`kind: "mesh"`** で届く(`duocad/meshbody.py` の
      // `tessellate`。B-repの面種別にこの値は無い)。三角形がそのまま形なので、
      // 頂点法線を平均すると元のメッシュに無い滑らかさが出る。
      const flat = face.kind === 'mesh'
      // **素の材質**で生む(役割・選択・ホバーは `applyFaceMaterials()` が
      // `rebuildPickIndex()`(→ `invalidateHover()`)経由ですぐに塗り直す)。
      const mesh = new THREE.Mesh(geometry, faceMaterialFor('none', 'plain', false, flat))
      mesh.userData.bodyId = bodyId
      mesh.userData.faceIndex = face.index
      // 塗り直す側(`applyFaceMaterials`)は `GeometryPayload` を見ないので、ここで印を残す。
      mesh.userData.flatShaded = flat
      root.add(mesh)
      faceMeshes.push(mesh)
      sceneObjects.push(mesh)
      disposables.push(geometry)

      // 面の境界線。装飾ではなく「どこが1つの面か」を見せるためで、
      // これが無いと隣り合う同色の面を狙い分けられず、狙いにくさの計測が意味を失う。
      // **メッシュボディには出さない** —— 面が1つしか無いので狙い分けの役に立たず、
      // 三角形の稜線がそのまま線になって形が潰れる。
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
      // 形が変わったBodyの辺は**その場で消す**。古い添字の線を残すと、指した線と
      // 違う辺が丸まる(§7の「添字は形状変更で壊れる」の辺版)。要るなら呼び出し側が
      // 版の変化を見て取り直す(`App.svelte`)。
      if (entry.bodyId === edgeBodyId) clearEdges()
    }
    rebuildPickIndex()
    refreshContentDiagonal() // 見えているものの大きさが変わった(解像度の上下限に効く)
    updateClipPlanes() // 内容が変わった → 視錐台を引き直す(姿勢は初回のみ下で決める)

    // **`frameToContent()` が実際に合わせたときだけ機会を消費する**。
    // 空振り(箱が空)で `framed = true` にすると、以後どんなに大きな内容が届いても
    // 初回フレーミングが二度と起きない(上の関数docstring参照)。
    if (!framed && frameToContent()) {
      framed = true
    }

    // **作り直したボディに、デバッグ表示の「隠す」を当て直す。** `?voxel=1` は結果ボディを
    // 隠してボクセルを見せる —— ここで当て直さないと、結果が届いた直後の状態配信で
    // ボディが生まれ直し、平滑化メッシュとボクセルが重なって両方読めなくなる。
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
   * 半径 `radius` の球が画面に収まる、注視点からの距離。FOVとアスペクトの狭い方で決まる。
   *
   * **FOVを可変にした以上、収まる距離を定数で決め打ちできない** —— 既定の30度・
   * アスペクト1以上では `radius / tan(15°) = radius * 3.7321`(`frameToContent` は
   * `radius = diagonal / 2` で呼ぶので `diagonal * 1.8660`)。45度なら
   * `radius / tan(22.5°) = radius * 2.4142`(`diagonal * 1.2071`)—— FOVが変われば
   * この係数も変わり、30度は45度よりパースが弱く見えるぶんカメラは約1.55倍遠くに置かれる。
   * 縦長のビューポート(aspect<1)では狭い方(横)に合わせるため距離がさらに伸びる。
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
   * で、「見かけの大きさを保つ」処理(`setFov` の切り替え)と画面1画素ぶんの世界長さ
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
   * **`frameToContent`(姿勢)から独立させてある**。姿勢はページ読み込みで
   * 1回決めれば十分だが、near/farは「最初に届いた形の大きさ」で固定してよいものではない
   * —— 後から大きなSTEPが届けばfarを追い越して手前が切り取られ(farはズーム・オービットでは
   * 一切広がらない)、逆に小さな細部へ深く寄ればnearが大きいまま手前が切れる。**内容が変わる
   * たび**(`refreshContentDiagonal` の呼び出し元)**とカメラが意味のある幅で動くたび**
   * (`resolutionTracker` の `cameraMoved` 判定に相乗り)の両方で呼ぶことで、視錐台を常時追随させる。
   *
   * near は**距離側からも**導く —— コンテンツの大きさだけで決めると、大きなモデルの中の
   * 小さな面へ深く寄ったときに手前が切れる(near は「いま見ている距離」に対しても十分
   * 小さくなければならない)。far は既存の `対角 × 20` の保守性をそのまま保ちつつ、
   * 距離が伸びた場合の下限も持つ(`frameToContent` の元のガードコメントの理由と同じ)。
   *
   * `span` の下限に `grid.extent() * 2` を置くのは、空の文書やグリッドしか無い場面でも
   * 視錐台がグリッド全体を収める大きさを保つため(`contentDiagonal` はボディが無いと0)。
   * **ピック範囲の半幅は固定ではない**(`grid.rescale`)ので、グリッドが
   * 広がればここの下限も一緒に広がる —— 取りこぼすと、グリッドは見えているのに
   * その外側でnear/farが詰まる、という遠い壊れ方をする。
   */
  function updateClipPlanes(): void {
    const span = Math.max(contentDiagonal, grid.extent() * 2)
    const distance = camera.position.distanceTo(controls.target)
    camera.near = Math.max(Math.min(span, distance) / 1000, 0.01)
    camera.far = Math.max(span * 20, distance * 4)
    camera.updateProjectionMatrix()
  }

  /**
   * グリッド計算専用の外接直方体。**`root` + `sketchRoot` だけ**から作る —— `grid.object`
   * (グリッド自身)を含めると「グリッドが伸びる→箱が大きくなる→グリッドが伸びる」の
   * 発散フィードバックループになる(`grid.object` は `root` に入れていない。平面グリッドの節の
   * コメント参照)。`refreshContentDiagonal` の箱と同じもの(そちらは対角の長さだけを取り出す)。
   */
  function contentBoxForGrid(): THREE.Box3 {
    const box = new THREE.Box3().setFromObject(root)
    box.union(new THREE.Box3().setFromObject(sketchRoot))
    return box
  }

  /** シーンに今ある全メッシュの外接直方体にカメラを合わせる(初回だけ)。
   *
   * 複数Bodyでは初回に届く順序や本数が変わりうるので、**実際に置いたメッシュから計算する**方が
   * 頑丈(サーバのbbox形式に依存しない)。
   *
   * **姿勢(位置・向き)だけを決める。** near/farは `updateClipPlanes` の仕事(上記コメント)。
   *
   * **実際に合わせたかを boolean で返す**。箱が空(=描くものがまだ無い)なら
   * 何もせず `false` を返す —— 呼び出し側の `update()` はこれを見て初めて「一度きりの機会」
   * (`framed` フラグ)を消費する。戻り値を見ずに常に `framed = true` にすると、
   * `geometry: null` のBodyだけが届いた1通(本体の形状がまだ無い等)で空振りのまま
   * 機会が焼き切れ、以後どれだけ内容が届いても初回フレーミングが起きなくなる。
   */
  function frameToContent(): boolean {
    const box = new THREE.Box3().setFromObject(root)
    // **姿勢を合わせられるか(box空)に関わらず、グリッドの縮小許可は必ず1回通す**。
    // `frameAll()`はここ(`frameToContent`)を呼ぶだけで、
    // 縮小の許可はここにしか無い —— 下の `box.isEmpty()` の早期returnより前に置かないと、
    // モデルを全部消してから`frameAll()`しても、広がったグリッドが二度と縮まなかった
    // (`contentBoxForGrid()`は`sketchRoot`も見るので、ボディだけ消してスケッチが残る
    // 場合も同じ早期returnを通る)。**姿勢(戻り値・カメラの位置/向き)の扱いは変えない**
    // —— これは「実際に合わせたか」を表す契約のままで、グリッドの縮小はそれとは独立に
    // 判定する(`grid.rescale`はその一手だけを担う)。
    grid.rescale(contentBoxForGrid(), true)
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
    // (実測: 93.6px → 143.7px)。同じ係数を掛けて、どちらの投影でも
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
      mesh.material = faceMaterialFor(role, state, isHovered, mesh.userData.flatShaded === true)
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

  /**
   * 形状最適化(第3のモード)の見た目を作り直す。**`null` は全部消すだけ**
   * (モードを抜けた・そもそも入っていない)。
   *
   * `preserveBodies` / `excludeBodies` は `applyFaceMaterials()` が読む役割の表を差し替える
   * だけ(ボディの色は選択と同じ経路)。拘束・荷重は**装飾**(`optimizeAnnotations.ts`)を
   * 作り直す。
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
   * 面のピック。**レイ距離も返す** —— 基準平面と面のどちらが手前かを比べるのに要る
   * (`onPointerUp`)。`pickAt` はこれの薄い覆いで、距離を要らない呼び出し側のためにある。
   *
   * **`ref` が null の当たりは断面の切り口** —— 指せないが奥のものを隠す実体なので、
   * 距離だけ返す(基準平面との勝負に使う)。
   *
   * 断面が ON のとき、レイキャストはクリップ平面を見ないので、平面をよぎる位置(`section.cut`)
   * を境に当たりを捨てる。切られた側からレイが入る場合は、よぎる点が中身かどうかを「同じ
   * ボディの表面をそれまでに奇数回またいだか」で決める(描画のステンシルの偶奇と同じ規則。
   * `sectionView.ts`)。
   */
  function faceHitAt(
    offsetX: number,
    offsetY: number,
  ): { ref: FaceRef | null; distance: number } | null {
    const ray = rayAt(offsetX, offsetY)
    // **隠しているメッシュは指せない**(プレビュー中の本物。three.js の Raycaster は
    // `visible` を見ないので、ここで明示的に外す)。
    const hits = raycaster.intersectObjects(
      allFaceMeshes.filter((mesh) => mesh.visible),
      false,
    )
    const cut = section.cut(ray)
    if (cut === 'cut') return null
    if (cut === 'kept') {
      const ref = hits.length === 0 ? null : faceRefOf(hits[0])
      return ref === null ? null : { ref, distance: hits[0].distance }
    }
    if (cut.originKept) {
      // 残る側から切られた側へ向かうレイ: 平面の手前までしか見えない(切り口は裏から見えない)
      const first = hits[0]
      if (!first || first.distance >= cut.t) return null
      const ref = faceRefOf(first)
      return ref === null ? null : { ref, distance: first.distance }
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
    const ref = faceRefOf(first)
    return ref === null ? null : { ref, distance: first.distance }
  }

  function pickAt(offsetX: number, offsetY: number): FaceRef | null {
    return faceHitAt(offsetX, offsetY)?.ref ?? null
  }

  // ------------------------------------------------------------------ 基準3平面(スケッチを貼る先)
  //
  // 「指せるか」の旗はグリッドが持つ(`grid.armed()`) —— **どの平面を出すかがその旗で決まる**ので、
  // 表示側と同じ場所に置く。

  function showBasePlanes(on: boolean): void {
    if (grid.armed() === on) return
    grid.setArmed(on)
    pickChanged()
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
   * の外(|u| か |v| が `grid.extent()` 超)は捨て、**レイ距離が最小の1枚**を採る。
   *
   * **描画が無限になってもピック範囲は無限にしない**。
   * ホバー板(`gridPlanes.ts`)も同じ `grid.extent()` から作る —— ここだけずれると
   * 「板は見えているのに指せない」という遠い壊れ方をする。
   */
  function basePlaneAt(
    offsetX: number,
    offsetY: number,
  ): { name: BasePlaneName; distance: number } | null {
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
    return best
  }

  function projectBasePlane(name: BasePlaneName): { x: number; y: number } | null {
    const spec = BASE_PLANES.find((candidate) => candidate.name === name)
    if (!spec || !grid.armed()) return null
    for (const [u, v] of BASE_PLANE_PROBES) {
      const screen = toScreen(planePoint(null, spec.u, spec.v, u, v))
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
      const screen = screenOf(point, width, height) // 画面の外・浮いているパネルの下は次の三角形を試す
      if (screen === null) continue
      const hit = pickAt(screen.x, screen.y)
      if (hit && hit.bodyId === bodyId && hit.faceIndex === faceIndex) return screen
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

  type EdgeMaterialState = 'plain' | 'user' | 'proposed'
  /** 辺の材質は (状態 × ホバー) のキャッシュから取る。ホバーは面と同じで明度を足すだけ。深度検査はしない
   *  (奥の辺も見え、指せる)。 */
  const edgeMaterials = variantCache(
    (state: EdgeMaterialState, hovered: boolean) => `${state}:${hovered ? 1 : 0}`,
    (state, hovered) => {
      const base = state === 'user' ? HIGHLIGHT_COLOR : state === 'proposed' ? PROPOSED_COLOR : PICK_EDGE_COLOR
      return new THREE.LineBasicMaterial({
        color: hovered ? lighten(base, HOVER_LIGHTEN) : base,
        depthTest: false,
        clippingPlanes: section.clipPlanes,
      })
    },
  )

  /** 今出している辺(**1つのBodyぶんだけ**)。空なら辺モードではない。 */
  let edgeLines: THREE.Line[] = []
  let edgeBodyId: string | null = null
  /** 今出している辺の元になった配列(**同じものを渡されたら作り直さない**ための印)。 */
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
      const line = new THREE.Line(geometry, edgeMaterials.get('plain', false))
      line.renderOrder = LAYER.edge // 深度を書かないぶん、描く順序で面より手前に出す
      line.userData.bodyId = bodyId
      line.userData.edgeIndex = edge.index
      edgeLines.push(edgeBatch.add(line, geometry))
    }
  }

  /** 今出している辺のうち選択に入っているもの(面の `selectedFaceKeys` と同じ役目)。 */
  let selectedEdgeIndices = new Set<number>()
  let edgeOrigin: 'user' | 'proposed' = 'user'

  /** 辺の材質を決める。**選択とホバーの両方を見る唯一の場所**(面と同じ規律)。 */
  function applyEdgeMaterials(): void {
    // 色は材質から読み戻す(`highlightColors()` の検査がこの値を読む)。
    highlightColor.edge = selectedEdgeIndices.size === 0 ? null : edgeMaterials.get(edgeOrigin, false).color.getHex()
    for (const line of edgeLines) {
      const edgeIndex = line.userData.edgeIndex as number
      const isHit = selectedEdgeIndices.has(edgeIndex)
      const isHovered =
        hoverEdge !== null &&
        hoverEdge.bodyId === (line.userData.bodyId as string) &&
        hoverEdge.edgeIndex === edgeIndex
      line.material = edgeMaterials.get(isHit ? edgeOrigin : 'plain', isHovered)
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
   * 画面1画素ぶんの世界座標の長さ。**式は `gridScale.ts` の `mmPerPixel` 1つに寄せてある**
   * —— グリッドの段の判定・可変解像度・視点が止まったかの判定が同じ物差しで動く。
   */
  function worldPerPixel(): number {
    const { height } = size()
    return Math.max(mmPerPixel(cameraProjection(), height), 1e-9)
  }

  // ------------------------------------------------------------------ 可変解像度(ズーム連動)
  /**
   * いま見えているものの外接対角[mm]。**毎フレーム測らない** —— `Box3.setFromObject` は
   * 全メッシュを走査するので、中身が変わったとき(`update` / `prune` / `showSketches`)に
   * だけ測り直す。上下限の丸めにしか使わないので、1フレーム古くても害が無い。
   */
  let contentDiagonal = 0

  function refreshContentDiagonal(): void {
    const box = contentBoxForGrid()
    // 断面の枠・切り口はボディ(と幽霊)だけの外接箱で決める(スケッチは切らないので入れない)
    section.setContent(new THREE.Box3().setFromObject(root))
    contentDiagonal = box.isEmpty() ? 0 : box.getSize(new THREE.Vector3()).length()
    // **広げるのは即座、縮めない**(`allowShrink: false`) —— ふつうの編集で内容がしきい値
    // 付近で揺れても、グリッドが縮んで点滅することはない(`gridPlanes.ts` の `rescale` のdocstring)。
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
  // 当たり半径の定数(`EDGE_PICK_PIXELS` / `SKETCH_HANDLE_PIXELS` / `VERTEX_PIXELS`)は
  // **画面上の半径[CSS px]**として使う。Raycaster の world threshold
  // (`worldPerPixel() * 定数`)では、注視点と対象の奥行きが違うだけで実際の screen 半径が
  // 変わり、斜めから見た線は 3D 垂線距離ぶん楕円状に当たる。
  //
  // **`worldPerPixel()` はここから外れた**。残っているのはグリッド・可変解像度・clip 用。

  const pickScratch = new THREE.Vector3()

  /** 折れ線1本ぶんの world 座標の読み口。**配列は作らない**(`polylineScreenDistSq`)。 */
  interface PickPolyline {
    count: number
    at(index: number, out: THREE.Vector3): void
  }

  /**
   * 折れ線を screen 上で歩き、カーソルまでの距離²[px²]の最小を返す。**射影できた隣り合う
   * 2点だけ**を線分として見るので、1本も見えなければ null。
   */
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
   * `polylineScreenDistSq` の断面つき版。`clip`(残る側が正の符号付き距離)で切られた線分は
   * 落とし、平面をまたぐ線分は交点で切り詰めてから測る —— 端点を1つずつ落とすだけだと、
   * 切り口のすぐ手前の見えている部分が指せなくなる。
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
   * スケッチの図形(`Line2`)の折れ線。**`geometry` からは読めない** ——
   * `LineGeometry` の `position` 属性は太線を描くための箱テンプレートであって折れ線ではない。
   * world 座標の点列は `buildEntityLine` が `userData.polylineWorld` へ入れてある
   * (矩形選択の `collectInBox` も同じものを読む)。
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
  // **描くのは線と領域だけ**で、スケッチ平面そのもの(無限に広がる面)は描かない ——
  // 面を描くと奥のソリッドが隠れ、「既存の部品の上に描く」が見えなくなる。
  //
  // ソリッドとは別のグループ(`sketchRoot`)に持つ。`update()`/`prune()` はソリッド側の
  // `root` しか触らないので、押し出しでスケッチが消えることがない。
  // **`sketchRoot` の宣言は `root` の隣へ上げてある**(初期化中の `createGridPlanes()` が
  // 読むため。あちらのコメント参照)。

  const sketchLineResolution = new THREE.Vector2(1, 1)
  const sketchMaterial = createSketchLineMaterial(SKETCH_COLOR, sketchLineResolution)
  const sketchGhostMaterial = createSketchLineMaterial(SKETCH_GHOST_COLOR, sketchLineResolution)

  /**
   * スケッチの線・点の材質は **(選択状態 × ホバー)** をキーにした遅延生成のキャッシュから取る
   * (`faceMaterialFor` と同じ形)。**反復を展開した `entities` 側の描画は通らない** ——
   * あちらは `sketchMaterial` を直接使う(ピック対象外なので選択もホバーも起きない)。
   */
  type SketchMaterialState = 'plain' | 'selected'
  type SketchLineMaterial = ReturnType<typeof createSketchLineMaterial>
  const sketchStateKey = (state: SketchMaterialState, hovered: boolean): string => `${state}:${hovered ? 1 : 0}`
  // `plain` の非ホバーは既存の `sketchMaterial` を種として使う(2つの独立インスタンスに分裂させない。
  // 捨てるのはキャッシュの `dispose()` の1回だけ)。
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

  // **深度を書かない**(頂点マーカーと同じ) —— 線の上に必ず出す。
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
  /**
   * 領域の材質は (選択 × ホバー) のキャッシュから取る。不透明度は選択で変わる(非選択 0.35 / 選択 0.55)。
   * ホバーは**3Dに領域の色を新しく作らない** —— 面・辺と同じで、既にある色に明度を足すだけ。
   */
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
  /**
   * 通常モードで領域を指せるか(`pickRegions`)。**スケッチモード中は関係ない** ——
   * モード中は常に「そのスケッチの領域」が対象で、このゲートは見ない。
   */
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
    // **今の画面の粗さで折る**(`resolution.applied()`)。固定分割だった頃は、寄ると
    // 円が多角形に見え、引くと使われない点をGPUへ送っていた。
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

  /**
   * 直近に受け取った描画指示。**解像度が変わったときに折り直すために控える**
   * (折れ線はここで作るので、指示そのものを持っていないと粗さだけを変えられない)。
   */
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
        // ホバーとの組み合わせを後から決められるように、選択かどうかを印として残す
        // (材質から読み戻すと、ホバー色が付いた瞬間に選択が分からなくなる)。
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

      // **元の図形(`handles`)を、展開した描画の上に重ねて置く。** ここに置くのは
      // 「指せるもの」であって「見えるもの」ではない —— 反復で増えた複製は `entities`
      // 側だけに出て、指す対象にはならない(複製は導出物で `entity_id` を持たない)。
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
          // ホバーとの組み合わせを後から決められるように、選択かどうかを印として残す
          // (`applySketchHoverMaterials` が材質から読み戻すと、ホバー色が付いた瞬間に
          // 選択が分からなくなる。領域の `userData.selected` と同じ理由)。
          built.object.userData.selected = selected
          handleLines.push(batch.add(built.object, built.geometry))
        }
        // **点が互いに独立して動く種別だけ常時表示する**(entityHasIndependentPoints)。
        // rectangle/circle/arcは点が1個で、その点だけ動かすのと図形全体を動かすのが
        // move_sketch_entities内で同じ処理になる(entityHasIndependentPointsのdocstring
        // 参照) —— 常時出すと「なぜこの1点だけ特別か」という見た目の非対称になるので、
        // ホバー/選択のときだけ見せる。ピック可能な座標そのものは変えない
        // (`pickPointsAt` はmesh.visibleを見ない)。
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
        mesh.material = regionMaterials.get(isHit, isHovered)
      }
    }
  }

  /**
   * スケッチの線・点の材質を決める。**選択とホバーの両方を見る唯一の場所**
   * (面・辺・領域と同じ規律)。作り直さず、`handleLines`/`handleMeshes` を歩いて
   * `.material` を差し替えるだけにする(面・辺と同じ)。
   *
   * 点は必ず辺の上に乗っているので、**点がホバーされているときは線を光らせない**
   * (`hoverSketchEntity.point === null` を線側の条件に入れて排他にする)。
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
   * いま編集中のスケッチ平面のframeをグリッドへ渡す(モード外は null)。
   *
   * **`setActiveSketch` と `showSketches` の両方から呼ぶ** —— モードに入った時点では
   * そのスケッチの描画指示がまだ届いておらず、frameを知らないことがある。届いた時点で
   * 呼び直さないと、基準平面へのスケッチ中もXY床が出たままになる。
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
  ): void {
    clearGhost()
    const target = sketches.get(sketchId)
    if (entities.length === 0 || !target) return
    const batch = createBatch(sketchRoot)
    for (const entity of entities) {
      const built = buildEntityLine(target.frame, entity, sketchGhostMaterial)
      if (!built) continue
      batch.add(built.object, built.geometry)
      // **実際に描けたものだけ**を控える(`ghostInfo`)。折れ線化できなかった図形を
      // 数に入れると、実機検査が「出ている」と判断したまま画面には何も無い状態になる。
      sketchGhostInfo.push({ kind: entity.kind, params: { ...entity.params } })
    }
    if (batch.objects.length === 0) return
    ghost = batch
  }

  /**
   * スケッチに入る直前の視点(抜けるときに戻す)。**モード外では null**。
   *
   * `lookAtSketch` は描いている図形の大きさに寄るので、半径4mmの円を描いて抜けると
   * **40mmの板が画面の外に残る**。視点を全体へ戻すボタンは `7363c76`(下段バーの削除)で
   * 落としてあり、人に残る手は手動のオービット/ズームしかない —— **抜けたら入る前へ戻す**。
   * 「全体が入る視点」ではなく「入る前の視点」なのは、スケッチに入る前に自分で寄せていた
   * 視点まで毎回捨てないため。
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
    // **`hoverAt`(画面座標)を捨てる** —— 入る/出る/切り替えのどれでもカメラが飛ぶことがある
    // (`lookAtSketch` の正対・退出時の視点復元)。飛んだ直後は同じ画面座標が指す3D点が
    // 別物になるので、そのままカーソル追従(`grid.followCursor` の毎フレーム経路)
    // に食わせると、切り替え前の位置が新しい平面の外側へ再投影されて縁のグリッドが
    // 意味なく広がる(実測: 面スケッチへの切替直後、直前のクリック位置が新しい面の外へ
    // 再投影されて内容だけの範囲より広い値が一瞬出た)。次の実際のポインタ移動まで
    // 「いま指している場所は無い」も同じ扱いにする(`onPointerLeave` と同じ考え方)。
    hoverAt = null
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
   * **見た目だけの角**(`entityVisualCorners`。長方形の残り3隅など)と**直線どうしの交点**
   * (`target.intersections`)を、本物の点と**同じ半径・同じ優先度**で拾う。`point` は
   * 常に null —— 独立した参照は持たない(`pointKeys` の点ではない)ので、当たった結果は
   * 「この図形の、たまたま角(または交点)の位置」であり、辺のどこかに当たったのと
   * 同じ図形全体の扱いになる(`move_sketch_entities` の的にはならない)。交点は2つの
   * 図形にまたがるが、参照は片方(`entityIds[0]`)だけを返す —— どちらも辺として選べば
   * 同じ位置なので、線が重なったときの「近い方が勝つ」既存の規則と同じ扱いでよい。
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
   * **直線区間の中点**(`entityMidpoints`)を、本物の点と**同じ半径**(`SKETCH_HANDLE_PIXELS`)
   * だが**1段低い優先度**(点・角・交点より下、辺より上)で拾う。本人指定
   * 「吸着優先順位は点と線の間」。`snapDirection` はカーソル強調(垂直な短いティック)が
   * 画面角度を出すのに使う —— 呼び出し側(`sketchEntityAt`)の点パス・角パスの後、
   * 辺パスの前からだけ呼ぶこと。
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
   * その画素で当たるスケッチの図形(**アクティブなスケッチだけ**)。
   *
   * **点が先・角/交点が次・中点がその次・辺が最後**。点は必ず辺の上に乗っているので、
   * 辺を先に見ると端点を掴めない(頂点マーカーを辺より先に見るのと同じ理由)。角・交点
   * (`sketchCornerAt`)も同じ理由で辺より先 —— 角・交点は必ずどれかの辺の上に乗っている。
   * 中点(`sketchMidpointAt`)は角・交点より1段低い優先度(本人指定「点と線の間」)。
   * 当たり半径は画面上の画素で決める(`edgeAt` / `vertexAt` と同じ `pickPointsAt` /
   * `pickLinesAt`)。
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

  /** **頂点を辺より先に見るか**(`preferVertices`)。既定は辺が先(`resolveHit` の順序)。 */
  let vertexPickWanted = false

  function preferVertices(on: boolean): void {
    if (vertexPickWanted === on) return
    vertexPickWanted = on
    pickChanged() // ゲートの開閉で「指せるもの」が変わる(`pickRegions` と同じ)
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
    rayAt(offsetX, offsetY)
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
    // 上方向はビューキューブと同じ規則(`upForDirection`)で決める。ほとんどの平面では
    // WORLD_UPのまま変わらない —— `OrbitControls`は生成時に焼いたZ軸を回転の極として
    // 使い続ける(`WORLD_UP`の説明参照)ので、camera.upをそこから逸らすとキューブの見た目
    // (`cubeActive.up`がcamera.upをそのまま写す)と実際の回転軸がずれ、スケッチ中だけ
    // ビューキューブの動きがおかしくなっていた。`frame.v` を直接使うと任意面や XY 平面で
    // camera.up が回転軸とずれる。真上・真下に近い面(cube側の上面図/底面図と同じ閾値)だけ
    // ±Yへ差し替わる。
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
    // **角・交点かどうかは `hit.snapAt` の有無(中点を除く)で決める**(`sketchCornerAt` だけが
    // 中点でないほうを立てる)。以前は確定後の座標を自分自身への射影と比べていたが、交点は
    // 2つの図形にまたがるため、その比較では検出できない(`SketchEntityHit.snapAt` のdocstring)。
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
   * `snapDirection`((u,v)空間の単位ベクトル)を画面上の角度(度)へ変換する。
   * カーソル強調のティック(`sketchCursorOverlay.ts`)専用 —— 実際に表示する位置
   * (`snapScreenAt`)とは別に、方向だけを知りたいので `projectForPick` を使う
   * (`toScreen` はDOM被覆を見るため、方向の補助点がたまたま覆われた画素に落ちると
   * 角度だけ消えてしまう)。
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
    // 面の `project` と同じ流儀: 三角形の内側の点を狙い、**実際にその領域へ当たる点**を返す。
    //
    // **1つの三角形につき4点試す**(重心 + 各頂点へ寄せた3点)。重心1点だけだと、
    // スケッチモードで**点・辺のほうが手前で拾われる**画素に当たったときに候補が尽きる ——
    // ベジエの制御点は曲線から離れた所(＝領域の内側)にも立つので、細長い三角形の重心が
    // その近くに来ることが実際にある(`smoke-svg` の丘の領域で実際に踏んだ)。
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
      // **スケッチの図形が勝つ画素は返さない**。スケッチモード中は点・辺が
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
  // 帯の DOM と包含判定は `selectionBand.ts`。何を候補にするか(`collectInBox`)は
  // **クリックのゲートと同じ**にしてある(辺は辺を出している間だけ、領域は要求している間だけ、
  // スケッチの図形はスケッチモード中だけ)。ここだけ別の規則にすると、
  // 「クリックでは指せないものがドラッグでだけ選べる」ができてしまう。
  const band = createSelectionBand({ container, projectRaw })

  const projectScratch = new THREE.Vector3()

  /** 世界座標を**コンテナ内の画素**へ(カメラの裏 —— z が clip の外 —— だけ null)。**当たり判定の射影はこれ** ——
   *  viewport の矩形でも `clickable()` でも切らない(画面の外へ出た端点も、線分までの距離を測るには要る。切るのは `screenOf`)。
   *  `width`/`height` は呼び出し元から渡す —— `selectionBand.ts` の頂点ループから
   *  頂点ごとに `size()`(= DOM の `clientWidth`/`clientHeight` 読み取り)を呼ぶと、
   *  矩形選択の実測で射影時間の92%を占めるほど遅い。
   *  ここでモジュール側にキャッシュしないこと —— リサイズで寸法が変わるため、
   *  キャッシュすると「同じ画素が別のものを指す」落とし穴に直結する。
   *  呼び出し元が
   *  1回の矩形選択の中でだけ `size()` を読んで使い回す。 */
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

    // ビューポートの寸法はこの呼び出し1回の間ずっと不変なので、頂点ごとに読まず
    // ここで1回だけ読んで使い回す(モジュール側にはキャッシュしない —
    // `projectRaw` のコメント参照)。
    const { width, height } = size()

    if (activeSketchId === null) {
      const keep = section.keepsFilter()
      // **面は常に指せる**(クリックと同じ)。隠したBodyのメッシュは `visible=false` なので外す。
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
      // **スケッチモードでは図形だけ**。図形全体が入っていればその図形を、
      // 入っていなければ矩形に入った点だけを返す(点と図形全体の二重取りを作らない)。
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

    // 領域は**クリックと同じゲート**(`regionPickTargets`)。スケッチモード中はそのスケッチだけ。
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
    // **キューブの内側で始まった左ボタン押下は、キューブが引き取る**(モデルのピック・
    // 矩形選択の開始へ漏らさない。タスク仕様の落とし穴1)。
    if (cube.pointerDown(event)) return
    // **断面のハンドルの上で始まった左ボタン押下は、ハンドルが引き取る**(キューブと同じ規律)。
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
    // **ボタンの種類より先に解除する**。右/中ボタンで視点を回して離した場合、ここで
    // 解除しないと次の `pointermove` が来るまでホバーが止まったままになる。
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
    // **矩形を引いていたならウィンドウ選択**(クリックにはしない)。引いた向きは見ない。
    if (band.active()) {
      const rect = band.rect(downX, downY, event.offsetX, event.offsetY)
      band.hide()
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
   * **Shift も Ctrl(Mac は Meta)も同じトグル**。除外専用のキーは
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
    // **頂点を直接ピックさせている間だけ、頂点が辺より先**(`preferVertices`)。
    // **既定の順序のままでは角の頂点は絶対に取れない** —— 頂点は必ず辺の端点に乗って
    // いるので、辺を出しているコマンド(`move_body` は対象を面/辺/頂点で指すので常に
    // 出ている)では下の `edgeAt` が必ず先に当たる。ゲートを閉じている間は既定の順序で、
    // フィレット中に辺の端を掴めることは変わらない。
    if (vertexPickWanted) {
      const wanted = vertexMarkers.at(offsetX, offsetY)
      if (wanted !== null) return { ...nothing, vertex: wanted }
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
    const vertex = vertexMarkers.at(offsetX, offsetY)
    if (vertex !== null) return { ...nothing, vertex }
    // **面と基準平面は、レイ距離が近い方**(通常のオクルージョン。辺のような優先順位は
    // 付けない —— 平面は部品と同じ空間に居るので、手前に見えているものが取れるのが自然)。
    // 基準平面は `showBasePlanes(true)` の間しか当たらない。
    //
    // **ただし同着は面が勝つ。** 基準平面に描いて押し出すと、その面は平面とぴったり重なり、
    // 2つのレイ距離が同着になる —— 素の `<` では**どちらが手前かが浮動小数の最後の桁で
    // 決まり**、見た目が同じなのに指すたび結果が変わる(無関係な式の書き換えでカメラが
    // 7e-14 動いただけで面→XZ平面へ反転したことが実際にあった)。そこで**画面で1画素にも満たない差を
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
    // ポインタがキューブの矩形の内側にあるあいだは、メインのホバー予告・矩形選択開始の
    // どちらも早期に打ち切る(まだ何も押していない=ホバーのみの場合)。
    const overCube = cube.inside(event.offsetX, event.offsetY)
    cube.setHoverAt(overCube ? { x: event.offsetX, y: event.offsetY } : null)
    if (overCube) {
      hoverAt = null
      updateSketchCursor(event.offsetX, event.offsetY, null, null)
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
      band.show(downX, downY, event.offsetX, event.offsetY)
    }
    let planePoint: { u: number; v: number } | null = null
    let sketchHit: SketchEntityHit | null = null
    if (planeMoveListener !== null) {
      if (activeSketchId === null) {
        planeMoveListener(null, null)
      } else {
        // **`sketchEntityAt` は `resolveHit` と同じ関数**(クリックの `PickEvent.sketchEntity` と
        // 同一)。同じ画素で呼ぶことで、幽霊のスナップ先とクリック確定時のスナップ先が
        // 同じ入力を通る(App.svelte の不変条件)。
        planePoint = planePointAt(event.offsetX, event.offsetY)
        sketchHit = sketchEntityAt(event.offsetX, event.offsetY)
        planeMoveListener(planePoint, sketchHit)
        grid.followCursor(planePoint)
      }
    }
    updateSketchCursor(event.offsetX, event.offsetY, planePoint, sketchHit)
  }

  /** ポインタがcanvasから出た。**強調を残さない**(戻ってくるまで指せるものは無い)。 */
  function onPointerLeave(event: PointerEvent): void {
    hoverAt = null
    orbiting = false
    section.setHover(false)
    // ハンドルのドラッグは `pointercancel`(OS の横取り)でだけ打ち切る —— `pointerleave` は
    // `setPointerCapture` 中には来ないし、来ても座標は届き続ける(キューブと同じ規律)。
    if (event.type === 'pointercancel') section.pointerUp()
    if (!section.dragging()) renderer.domElement.style.cursor = ''
    sketchCursor.update(null, 'free', null)
    // カーソルがcanvas外に出た=いま追従すべき位置が無い。`sketchCursor` と同じく
    // 「カーソル無し」(`point: null`)へ戻す —— そのままだと、キャンバスの外へ出ている間
    // ずっと直前の拡大状態が残る。
    grid.followCursor(null)
    // **引きかけの矩形は捨てる**(帯だけが残って画面に貼り付くのを防ぐ)。
    band.hide()
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
    // **頂点は出さない**。頂点が取れる画素では面も辺も光らない ——
    // そこをクリックしても面は選ばれないのだから、光らせないほうが正しい。
    //
    // **スケッチの点・辺も同じ扱い**。予告の色は付けないが、
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
   * 視線が極(真上/真下)から離れたら、`camera.up`をWORLD_UPへ戻す。
   *
   * `OrbitControls`は生成時に焼いた軸(`WORLD_UP`)だけを回転の極として使うので、
   * 位置そのものの計算は`camera.up`の値に影響されない —— が、`controls.update()`が
   * 最後に呼ぶ`object.lookAt()`は**そのときの`camera.up`**でロール(傾き)を決める。
   * ビューキューブの上面図/底面図クリック(`upForDirection`)も`lookAtSketch`も、
   * 極付近の縮退(upが視線と平行になり`lookAt`の直交化が壊れる)を避けるために
   * 一時的に`camera.up`をYへ傾ける。**傾けたまま戻さないと**、「キューブの上面を
   * クリックしてから回すと絵が転がる」形で壊れる —— 極から離れたら必ずここで戻すこと。
   *
   * **極に近い間(`|z| > POLE_Z`)は何もしない。** 毎フレーム`upForDirection(今の向き)`を
   * 焼き直す実装は、あの関数が極付近で定数`(0,±1,0)`を返す階段関数であるため、
   * **通常のドラッグで極をまたぐだけで上が固定されたように(勝手に上下が揃うように)
   * 見える**壊れ方になる。普段のドラッグはそもそも`camera.up`を一切書き換えなくても、`controls`側の
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
    resolution.update(performance.now())
    updateHover()
    cube.updateHover()
    // **カーソル追従もフレームごとに引き直す**(`onPointerMove` だけに任せない)。
    // ホイールでズームすると `hoverAt`(画面座標)は動かないが、レイと平面frameの交点
    // (`planePointAt`)はカメラが動いた分だけ変わる —— `onPointerMove` 経由だけでは
    // 「カーソルを動かさずホイールで寄せる/引く」だけの操作でグリッドが追従しない
    // (本人報告2026-09-11)。`planePointAt`/`grid.followCursor` は
    // どちらもスケッチ非活性なら即 `null`/no-opで返るので、素通りの費用は無視できる。
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
      sketchCursor.dispose()
      ghostLayer.clearPreview()
      clearGhost()
      clearEdges()
      vertexMarkers.dispose()
      edgeMaterials.dispose()
      for (const sketchId of [...sketches.keys()]) clearSketch(sketchId)
      band.dispose()
      sketchGhostMaterial.dispose()
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
