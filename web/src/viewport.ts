/**
 * ビューポート — three.js をここに閉じ込める(実装計画書 §4.2 予防2)。
 *
 * **three.js の型を外へ漏らさない**。公開するのは下の `Viewport` インタフェースだけで、
 * three.js のオブジェクトは1つも戻り値・引数に出さない。three.js は semver を持たず
 * r番号でほぼ毎月リリースし非推奨APIを定期的に削除するため、**このスタックで最も churn が
 * 激しい依存**である。1ファイルに閉じておけば破壊的変更の影響がここで止まる。
 *
 * **canvas はフレームワークの外**(実装計画書 §4「守るべき唯一のルール」)。Svelte から
 * 受け取るのは空の `HTMLElement` 1つだけで、canvas の生成・破棄・再描画は全部この中で行う。
 * Svelte はこのコンテナの中身を二度と触らない。
 *
 * **M3.5(2026-08-11)でBodyごとの差分更新に対応した。** M1は「1個の形状を丸ごと差し替える」
 * `load()` だけを持っていたが、STEPがソリッドごとに分かれた(パーツ一覧UI)ことで、
 * 「変わったBodyだけ差し替える」`update()` が要る。**ここが27部品ぶんを毎回全部作り直すと、
 * サーバ側を速くした意味(差分送信)が描画で消える**(モジュールの利用側 `App.svelte` 参照)。
 * 面の当たり判定・強調表示・射影は body_id + face_index の組で指す(1個のBodyの中でしか
 * 一意でなかったM1の `faceIndex` 単独指定から変更)。
 */
import * as THREE from 'three'
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'

/** kernel.tessellate() が返す1面ぶん(サーバのJSONそのまま)。 */
export interface FaceGeometry {
  index: number
  kind: string
  area: number
  centroid: number[]
  normal: number[]
  positions: number[]
  indices: number[]
}

export interface GeometryPayload {
  deviation: number
  face_count: number
  bbox: { min: number[]; max: number[]; center: number[]; diagonal: number }
  faces: FaceGeometry[]
  untriangulated: number[]
}

/** 面を一意に指す(1個のBodyの中でしか一意でない `faceIndex` 単独指定はしない)。 */
export interface FaceRef {
  bodyId: string
  faceIndex: number
}

/** 1Bodyぶんの形状の差し替え。`geometry: null` は非表示・削除(メッシュを取り除く)。 */
export interface BodyGeometryUpdate {
  bodyId: string
  geometry: GeometryPayload | null
}

/** クリックが選択集合に対して何をするか。修飾キーから決まる(B・2026-08-13)。 */
export type PickMode = 'replace' | 'add' | 'remove'

export interface PickEvent {
  /** 当たった面(Body+添字)。外したら null。**スケッチモード中は常に null**(下記)。 */
  hit: FaceRef | null
  /** Shift=追加 / Ctrl(Mac は Meta)=除去 / 修飾なし=置き換え。 */
  mode: PickMode
  /**
   * スケッチモード中の、クリックした点のスケッチ平面上の座標(C・2026-08-13)。
   * 平面がカメラの裏なら null。**これが「手で描く」の入力そのもの**。
   */
  point: { sketchId: string; u: number; v: number } | null
  /** スケッチモード中に領域へ当たったらその添字(領域は面より手前で拾う)。 */
  region: { sketchId: string; index: number } | null
}

/** 1枚のスケッチの描画指示(three.js の型は出さない)。 */
export interface SketchDraw {
  sketchId: string
  /** `p = origin + u*uAxis + v*vAxis`(コアが配る枠。session.ts の `SketchFrame`)。 */
  frame: { origin: number[]; u: number[]; v: number[]; normal: number[] }
  /** 反復を展開した描画対象。`params` はコアの `Entity.params` と同じキー。 */
  entities: Array<{ kind: string; params: Record<string, number> }>
  regions: Array<{ index: number; positions: number[]; indices: number[] }>
  /** 選択中の領域の添字(強調して描く)。 */
  selectedRegions: number[]
}

export interface Viewport {
  /**
   * Bodyの形状をまとめて反映する。**初回(まだ何も描いていない状態)だけ、反映後の
   * シーン全体にカメラを合わせる**。以後の呼び出しは視点を保ったまま差分だけ描き直す
   * (M1の `load()` の「初回だけフレームする」を複数Body対応に一般化したもの)。
   * 変更のないBodyには触れない — 27部品のうち1つだけ変わった押し出しでは、
   * 呼び出し側(App.svelte)はその1件だけを渡せばよい。
   */
  update(bodies: BodyGeometryUpdate[]): void
  /**
   * `validBodyIds` に無いBodyの描画を取り除く(文書から消えたBodyのメッシュを掃除する)。
   * `duocad/server.py` の `Session.delta` の契約(「知っているのに `bodies` にも
   * `unchanged` にも現れないIDは消えた」)に対応する側。**M3.5で実際に呼べるverbからは
   * 到達しない**(undoは文書の土台より前へ戻せない)が、呼び出し側(App.svelte)は
   * 契約どおりに毎回呼ぶ — 到達しない経路のためのコードを書かないと、次にBody削除が
   * 起きるverbが増えたときに同じ「画面から消えない」不具合が再発する
   * (2026-08-11のQA指摘)。
   */
  prune(validBodyIds: string[]): void
  /** 面のクリック(当たり・外れとも)を受け取る。 */
  onPick(listener: (event: PickEvent) => void): void
  /**
   * スケッチモード中、ポインタがスケッチ平面のどこを指しているか(C)。
   * モード外・平面の裏では null が来る。**確定前の線を見せるためだけ**に使う。
   */
  onPlaneMove(listener: (point: { u: number; v: number } | null) => void): void
  /**
   * 選択中の面を強調する(空配列で解除)。
   *
   * **B(2026-08-13)で複数選択と出所に対応した**。`origin` が `"proposed"`(AIの提案)なら
   * 確定した選択と**明確に違う色**で描く —— これが「交互操作が人間側から見える」唯一の
   * 場所である(実装計画書§8b B)。
   *
   * **辺・頂点・領域は3Dでは描かない**。ここが持っているのは面ごとの三角形メッシュだけで、
   * 個々の位相辺のジオメトリを持っていないため(面の境界線は `EdgesGeometry` による
   * 見た目であって、コアの辺の添字とは対応しない)。辺の選択は `SelectionPanel` が
   * 一覧と件数で見せる。3Dで辺を色分けするのは、面ごとに辺を送る作りが要るので D の債務。
   */
  highlight(refs: FaceRef[], origin: 'user' | 'proposed'): void
  /**
   * 確定前の形(プレビュー)を重ねる。**本物のBodyは隠し、幽霊を代わりに描く**。
   *
   * 幽霊は**ピックの対象にしない**(まだ存在しない形は指せない)。本物の状態が
   * 1つでも届いたら(`update`)プレビューは無効になるので、自動的に消える。
   */
  showPreview(bodies: BodyGeometryUpdate[]): void
  /** プレビューを消して本物のBodyを出し直す。 */
  clearPreview(): void
  /**
   * 面の表面上の点の画面座標(コンテナ内のCSSピクセル)。無理なら null。
   *
   * これは自動テスト(`scripts/smoke.mjs`)が**本物のクリックで面を選ぶ**ために必要な穴で、
   * 「動くはず」で納品しないための道具。重心ではなく三角形の上の点を返し、
   * さらに**その画素からのレイが実際にこの面へ当たることを確かめてから**返す
   * (重心は円環や円筒では面の外にあり、隠れている面もあるため)。**他のBodyに隠れている
   * 面は、そのBodyを隠さない限り null になる**(それ自体が可視性=選択機構の一部であることの
   * 検査になる)。
   */
  project(bodyId: string, faceIndex: number): { x: number; y: number } | null
  // ------------------------------------------------------------------ C: スケッチ
  /**
   * スケッチを描き直す(渡さなかったスケッチの描画は消える)。**図形は線、領域は面**で
   * 描く。ソリッドの描画(`update`)とは別のグループに持つので、押し出しで形が変わっても
   * スケッチは消えない。
   */
  showSketches(sketches: SketchDraw[]): void
  /**
   * 確定前の図形(スケッチの幽霊)を描く。`entity` が null で消える。
   *
   * **サーバへ投げない**。図形の形はブラウザ側で分かっており、コアで投機実行すると
   * 文書に一度は入る(戻すとはいえ)。Escで中断しても文書が変わらないことを、
   * 「そもそも送らない」で成立させる。
   */
  showSketchGhost(sketchId: string, entity: { kind: string; params: Record<string, number> } | null): void
  /**
   * スケッチモードの対象を決める。`null` でモードを抜ける。
   *
   * モード中のクリックは**面ではなく**「領域」または「平面上の点」になる
   * (`PickEvent.point` / `PickEvent.region`)。描いている最中に奥のソリッドの面を
   * 掴んでしまうと、線を引くつもりのクリックが選択の置き換えになる。
   */
  setActiveSketch(sketchId: string | null): void
  /** スケッチ平面を正面から見る視点へ移す(無いと描きにくい)。 */
  lookAtSketch(sketchId: string): void
  /** スケッチ平面上の (u,v) の画面座標。実機検査が**本物のクリックで描く**ために要る。 */
  projectSketchPoint(sketchId: string, u: number, v: number): { x: number; y: number } | null
  /** 領域の画面座標(実機検査が領域を本物のクリックで選ぶために要る)。 */
  projectRegion(sketchId: string, index: number): { x: number; y: number } | null
  resize(): void
  dispose(): void
}

const BASE_COLOR = 0x9aa6b2
const HIGHLIGHT_COLOR = 0xffb454
/** スケッチの線・確定前の線・領域(C・2026-08-13)。ソリッドの色と混ざらない色にする。 */
const SKETCH_COLOR = 0xe8eef6
const SKETCH_GHOST_COLOR = 0x7ee081
const REGION_COLOR = 0x4d7ea8
const REGION_SELECTED_COLOR = 0xffb454
/** 円・円弧を折れ線で描くときの分割数(見た目の作り込みではなく、狙える程度の滑らかさ)。 */
const ARC_SEGMENTS = 64
/** AIの提案(`origin === "proposed"`)。確定(橙)と**色相で**離す。 */
const PROPOSED_COLOR = 0x36d1ff
/** 確定前の形。半透明で「まだ無いもの」に見せる。 */
const PREVIEW_COLOR = 0x7ee081
const EDGE_COLOR = 0x20262d
const BACKGROUND_COLOR = 0x161a1f

/** クリックとドラッグ(視点回転)の境目。これ以上動いたら選択ではなく回転とみなす。 */
const CLICK_MOVE_LIMIT_PX = 6
const CLICK_TIME_LIMIT_MS = 500

/** 1個のBodyが画面に持つメッシュ(面ごとの三角形メッシュ+境界線)。 */
interface BodyMeshes {
  /** ピック判定の対象(面の三角形メッシュだけ)。 */
  faceMeshes: THREE.Mesh[]
  /** `root` に足した全オブジェクト(面メッシュ+境界線)。clearBodyで**これを**取り除く
   * (面メッシュだけを外すと境界線(LineSegments)がシーンに残り続け、dispose済みの
   * ジオメトリを参照するゴースト線としてメモリリークする。2026-08-11のQA指摘)。 */
  sceneObjects: THREE.Object3D[]
  disposables: Array<{ dispose(): void }>
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

  const camera = new THREE.PerspectiveCamera(45, 1, 0.1, 10000)
  camera.position.set(1, 1, 1)
  scene.add(camera)
  // 光をカメラに付ける(視点を回しても面が真っ黒にならない。見た目の作り込みではなく可視性)
  const headlight = new THREE.DirectionalLight(0xffffff, 1.6)
  headlight.position.set(0.4, 0.7, 1)
  camera.add(headlight)
  scene.add(new THREE.HemisphereLight(0xdfe7f2, 0x191c21, 1.0))

  const controls = new OrbitControls(camera, renderer.domElement)
  controls.enableDamping = true
  controls.dampingFactor = 0.15

  const root = new THREE.Group()
  scene.add(root)

  const baseMaterial = new THREE.MeshLambertMaterial({
    color: BASE_COLOR,
    side: THREE.DoubleSide,
  })
  const highlightMaterial = new THREE.MeshLambertMaterial({
    color: HIGHLIGHT_COLOR,
    side: THREE.DoubleSide,
  })
  const proposedMaterial = new THREE.MeshLambertMaterial({
    color: PROPOSED_COLOR,
    side: THREE.DoubleSide,
  })
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
  /** 全Body分の面メッシュを1本にまとめたもの(ピック用)。update() のたびに作り直す。 */
  let allFaceMeshes: THREE.Mesh[] = []
  /** プレビュー(幽霊)のメッシュ。**ピックの対象にしない**ので `bodies` とは別に持つ。 */
  const previewMeshes = new Map<string, { objects: THREE.Object3D[]; disposables: Array<{ dispose(): void }> }>()
  let pickListener: ((event: PickEvent) => void) | null = null
  let planeMoveListener: ((point: { u: number; v: number } | null) => void) | null = null
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
    const { width, height } = size()
    renderer.setSize(width, height, false)
    camera.aspect = width / height
    camera.updateProjectionMatrix()
  }

  function clearBody(bodyId: string): void {
    const existing = bodies.get(bodyId)
    if (!existing) return
    for (const object of existing.sceneObjects) root.remove(object)
    for (const item of existing.disposables) item.dispose()
    bodies.delete(bodyId)
  }

  function buildBody(bodyId: string, payload: GeometryPayload): void {
    const faceMeshes: THREE.Mesh[] = []
    const sceneObjects: THREE.Object3D[] = []
    const disposables: Array<{ dispose(): void }> = []
    for (const face of payload.faces) {
      if (face.indices.length === 0) continue // 三角形が取れなかった面(サーバが警告を出す)
      const geometry = new THREE.BufferGeometry()
      geometry.setAttribute(
        'position',
        new THREE.BufferAttribute(new Float32Array(face.positions), 3),
      )
      geometry.setIndex(new THREE.BufferAttribute(new Uint32Array(face.indices), 1))
      geometry.computeVertexNormals()
      geometry.computeBoundingSphere()
      const mesh = new THREE.Mesh(geometry, baseMaterial)
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
  }

  function rebuildPickIndex(): void {
    allFaceMeshes = []
    for (const entry of bodies.values()) allFaceMeshes.push(...entry.faceMeshes)
  }

  function update(entries: BodyGeometryUpdate[]): void {
    if (entries.length === 0) return
    // 本物の形が届いた時点で、確定前の予想は用済み(古い幽霊を残さない)。
    clearPreview()
    for (const entry of entries) {
      clearBody(entry.bodyId)
      if (entry.geometry) buildBody(entry.bodyId, entry.geometry)
    }
    rebuildPickIndex()

    if (!framed) {
      frameToContent()
      framed = true
    }
  }

  // ------------------------------------------------------------------ プレビュー(幽霊)
  function setBodyVisible(bodyId: string, visible: boolean): void {
    const entry = bodies.get(bodyId)
    if (!entry) return
    for (const object of entry.sceneObjects) object.visible = visible
  }

  function showPreview(entries: BodyGeometryUpdate[]): void {
    clearPreview()
    for (const entry of entries) {
      if (!entry.geometry) continue
      const objects: THREE.Object3D[] = []
      const disposables: Array<{ dispose(): void }> = []
      for (const face of entry.geometry.faces) {
        if (face.indices.length === 0) continue
        const geometry = new THREE.BufferGeometry()
        geometry.setAttribute(
          'position',
          new THREE.BufferAttribute(new Float32Array(face.positions), 3),
        )
        geometry.setIndex(new THREE.BufferAttribute(new Uint32Array(face.indices), 1))
        geometry.computeVertexNormals()
        geometry.computeBoundingSphere()
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

  function clearPreview(): void {
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
    for (const bodyId of stale) clearBody(bodyId)
    rebuildPickIndex()
  }

  /** シーンに今ある全メッシュの外接直方体にカメラを合わせる(初回だけ)。
   *
   * M1はサーバが返す単一Bodyの `bbox` をそのまま使っていたが、複数Bodyでは
   * 初回に届く順序や本数が変わりうるので、**実際に置いたメッシュから計算する**方が
   * 頑丈(サーバのbbox形式に依存しない)。
   */
  function frameToContent(): void {
    const box = new THREE.Box3().setFromObject(root)
    if (box.isEmpty()) return
    const center = new THREE.Vector3()
    box.getCenter(center)
    const diagonal = Math.max(box.getSize(new THREE.Vector3()).length(), 1e-6)
    const distance = diagonal * 1.2
    camera.near = Math.max(diagonal / 1000, 0.01)
    camera.far = diagonal * 20
    camera.position.copy(center).add(new THREE.Vector3(distance, distance * 0.6, distance))
    camera.updateProjectionMatrix()
    controls.target.copy(center)
    controls.update()
  }

  function highlight(refs: FaceRef[], origin: 'user' | 'proposed'): void {
    const selectedMaterial = origin === 'proposed' ? proposedMaterial : highlightMaterial
    const keys = new Set(refs.map((ref) => `${ref.bodyId} ${ref.faceIndex}`))
    for (const mesh of allFaceMeshes) {
      const isHit = keys.has(`${mesh.userData.bodyId} ${mesh.userData.faceIndex}`)
      mesh.material = isHit ? selectedMaterial : baseMaterial
    }
  }

  function pickAt(offsetX: number, offsetY: number): FaceRef | null {
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
    return { bodyId, faceIndex }
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
      const hit = pickAt(x, y)
      if (hit && hit.bodyId === bodyId && hit.faceIndex === faceIndex) return { x, y }
    }
    return null
  }

  // ------------------------------------------------------------------ C: スケッチ
  //
  // **描くのは線と領域だけ**で、スケッチ平面そのもの(無限に広がる面)は描かない ——
  // 面を描くと奥のソリッドが隠れ、「既存の部品の上に描く」が見えなくなる。
  //
  // ソリッドとは別のグループ(`sketchRoot`)に持つ。`update()`/`prune()` はソリッド側の
  // `root` しか触らないので、押し出しでスケッチが消えることがない。
  const sketchRoot = new THREE.Group()
  scene.add(sketchRoot)

  const sketchMaterial = new THREE.LineBasicMaterial({ color: SKETCH_COLOR })
  const sketchGhostMaterial = new THREE.LineBasicMaterial({ color: SKETCH_GHOST_COLOR })
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

  interface SketchScene {
    frame: SketchDraw['frame']
    objects: THREE.Object3D[]
    disposables: Array<{ dispose(): void }>
    /** 領域のメッシュ(ピック対象)。 */
    regionMeshes: THREE.Mesh[]
    /** 図形の (u,v) の折れ線(視点合わせの中心を出すのに使う)。 */
    points: Array<[number, number]>
  }
  const sketches = new Map<string, SketchScene>()
  let ghost: { objects: THREE.Object3D[]; disposables: Array<{ dispose(): void }> } | null = null
  let activeSketchId: string | null = null

  /** スケッチ平面上の (u,v) を3Dの点へ(コアの `kernel._to_pnt` と同じ規則)。 */
  function toWorld(frame: SketchDraw['frame'], u: number, v: number): THREE.Vector3 {
    return new THREE.Vector3(
      frame.origin[0] + u * frame.u[0] + v * frame.v[0],
      frame.origin[1] + u * frame.u[1] + v * frame.v[1],
      frame.origin[2] + u * frame.u[2] + v * frame.v[2],
    )
  }

  /**
   * 図形を (u,v) の折れ線にする。**コアの `Entity.params` のキーをそのまま読む**
   * (`duocad/sketch.py` の `Entity` docstring)。円・円弧は折れ線で近似する。
   */
  function entityPoints(kind: string, params: Record<string, number>): Array<[number, number]> {
    if (kind === 'line') {
      return [
        [params.x0, params.y0],
        [params.x1, params.y1],
      ]
    }
    if (kind === 'rectangle') {
      const { x, y, width, height } = params
      return [
        [x, y],
        [x + width, y],
        [x + width, y + height],
        [x, y + height],
        [x, y],
      ]
    }
    if (kind === 'circle' || kind === 'arc') {
      const start = kind === 'circle' ? 0 : params.start_deg
      // 反時計回り・0度をまたいでよい(`add_arc` の契約)。end<=start は一周分足す。
      let end = kind === 'circle' ? 360 : params.end_deg
      if (kind === 'arc' && end <= start) end += 360
      const steps = Math.max(8, Math.round((ARC_SEGMENTS * Math.abs(end - start)) / 360))
      const points: Array<[number, number]> = []
      for (let step = 0; step <= steps; step += 1) {
        const angle = ((start + ((end - start) * step) / steps) * Math.PI) / 180
        points.push([
          params.cx + params.radius * Math.cos(angle),
          params.cy + params.radius * Math.sin(angle),
        ])
      }
      return points
    }
    return []
  }

  function buildEntityLine(
    frame: SketchDraw['frame'],
    entity: { kind: string; params: Record<string, number> },
    material: THREE.LineBasicMaterial,
  ): { object: THREE.Object3D; geometry: THREE.BufferGeometry; points: Array<[number, number]> } | null {
    const points = entityPoints(entity.kind, entity.params)
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

  function showSketches(entries: SketchDraw[]): void {
    const keep = new Set(entries.map((entry) => entry.sketchId))
    for (const sketchId of [...sketches.keys()]) if (!keep.has(sketchId)) clearSketch(sketchId)
    for (const entry of entries) {
      clearSketch(entry.sketchId)
      const objects: THREE.Object3D[] = []
      const disposables: Array<{ dispose(): void }> = []
      const regionMeshes: THREE.Mesh[] = []
      const points: Array<[number, number]> = []

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
      }
      sketches.set(entry.sketchId, {
        frame: entry.frame,
        objects,
        disposables,
        regionMeshes,
        points,
      })
    }
  }

  function clearGhost(): void {
    if (!ghost) return
    for (const object of ghost.objects) sketchRoot.remove(object)
    for (const item of ghost.disposables) item.dispose()
    ghost = null
  }

  function showSketchGhost(
    sketchId: string,
    entity: { kind: string; params: Record<string, number> } | null,
  ): void {
    clearGhost()
    const target = sketches.get(sketchId)
    if (!entity || !target) return
    const built = buildEntityLine(target.frame, entity, sketchGhostMaterial)
    if (!built) return
    sketchRoot.add(built.object)
    ghost = { objects: [built.object], disposables: [built.geometry] }
  }

  function setActiveSketch(sketchId: string | null): void {
    activeSketchId = sketchId
    if (sketchId !== null) return
    clearGhost()
    // 上方向を既定へ戻す。`lookAtSketch` が平面のv軸へ倒したままだと、モードを抜けた後の
    // オービットが入る前と違う傾きになる(QA 2026-08-13)。
    camera.up.set(0, 1, 0)
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

  function regionAt(offsetX: number, offsetY: number): { sketchId: string; index: number } | null {
    if (activeSketchId === null) return null
    const target = sketches.get(activeSketchId)
    if (!target || target.regionMeshes.length === 0) return null
    const { width, height } = size()
    const ndc = new THREE.Vector2((offsetX / width) * 2 - 1, -(offsetY / height) * 2 + 1)
    raycaster.setFromCamera(ndc, camera)
    const hits = raycaster.intersectObjects(target.regionMeshes, false)
    if (hits.length === 0) return null
    const index = hits[0].object.userData.regionIndex
    if (typeof index !== 'number') return null
    return { sketchId: activeSketchId, index }
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
    const distance = radius > 0 ? Math.max(radius * 3, 1) : Math.max(previous, 1)
    // 上方向を平面のv軸に合わせる(そうしないと真上から見たときに回転が不定になる)
    camera.up.set(...(frame.v as [number, number, number]))
    camera.position.copy(center).add(normal.multiplyScalar(distance))
    camera.updateProjectionMatrix()
    controls.target.copy(center)
    controls.update()
  }

  function toScreen(point: THREE.Vector3): { x: number; y: number } | null {
    const { width, height } = size()
    const projected = point.clone().project(camera)
    if (projected.z < -1 || projected.z > 1) return null
    const x = ((projected.x + 1) / 2) * width
    const y = ((1 - projected.y) / 2) * height
    if (x < 0 || y < 0 || x > width || y > height) return null
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
    // 面の `project` と同じ流儀: 三角形の重心を狙い、**実際にその領域へ当たる点**を返す
    const point = new THREE.Vector3()
    const corner = new THREE.Vector3()
    for (let triangle = 0; triangle < triangles.count / 3; triangle += 1) {
      point.set(0, 0, 0)
      for (let vertex = 0; vertex < 3; vertex += 1) {
        const at = triangles.getX(triangle * 3 + vertex)
        corner.set(position.getX(at), position.getY(at), position.getZ(at))
        point.add(corner)
      }
      point.multiplyScalar(1 / 3)
      const screen = toScreen(point.clone())
      if (screen === null) continue
      const hit = regionAt(screen.x, screen.y)
      if (hit && hit.index === index) return screen
    }
    return null
  }

  // クリック(押した位置からほとんど動いていない)だけを選択として扱う。
  // 動いた場合は OrbitControls の視点回転であって選択ではない。
  let downAt = 0
  let downX = 0
  let downY = 0
  let downValid = false

  function onPointerDown(event: PointerEvent): void {
    if (event.button !== 0) return
    downAt = performance.now()
    downX = event.offsetX
    downY = event.offsetY
    downValid = true
  }

  function onPointerUp(event: PointerEvent): void {
    if (event.button !== 0 || !downValid) return
    downValid = false
    const moved = Math.hypot(event.offsetX - downX, event.offsetY - downY)
    const elapsed = performance.now() - downAt
    if (moved > CLICK_MOVE_LIMIT_PX || elapsed > CLICK_TIME_LIMIT_MS) return
    const mode: PickMode = event.shiftKey
      ? 'add'
      : event.ctrlKey || event.metaKey
        ? 'remove'
        : 'replace'
    // **スケッチモード中は面を拾わない**(C)。描いている最中に奥のソリッドの面を掴むと、
    // 線を引くつもりのクリックが選択の置き換えになる。領域が手前・平面の点がその次。
    if (activeSketchId !== null) {
      const region = regionAt(event.offsetX, event.offsetY)
      const plane = planePointAt(event.offsetX, event.offsetY)
      pickListener?.({
        hit: null,
        mode,
        region,
        point: plane === null ? null : { sketchId: activeSketchId, ...plane },
      })
      return
    }
    pickListener?.({ hit: pickAt(event.offsetX, event.offsetY), mode, point: null, region: null })
  }

  function onPointerMove(event: PointerEvent): void {
    if (planeMoveListener === null) return
    planeMoveListener(activeSketchId === null ? null : planePointAt(event.offsetX, event.offsetY))
  }

  renderer.domElement.addEventListener('pointerdown', onPointerDown)
  renderer.domElement.addEventListener('pointerup', onPointerUp)
  renderer.domElement.addEventListener('pointermove', onPointerMove)

  const observer = new ResizeObserver(() => applySize())
  observer.observe(container)
  applySize()

  function tick(): void {
    if (!alive) return
    controls.update()
    renderer.render(scene, camera)
    frameHandle = requestAnimationFrame(tick)
  }
  frameHandle = requestAnimationFrame(tick)

  return {
    update,
    prune,
    onPick(listener) {
      pickListener = listener
    },
    onPlaneMove(listener) {
      planeMoveListener = listener
    },
    highlight,
    showPreview,
    clearPreview,
    project,
    showSketches,
    showSketchGhost,
    setActiveSketch,
    lookAtSketch,
    projectSketchPoint,
    projectRegion,
    resize: applySize,
    dispose() {
      alive = false
      cancelAnimationFrame(frameHandle)
      observer.disconnect()
      renderer.domElement.removeEventListener('pointerdown', onPointerDown)
      renderer.domElement.removeEventListener('pointerup', onPointerUp)
      renderer.domElement.removeEventListener('pointermove', onPointerMove)
      clearPreview()
      clearGhost()
      for (const sketchId of [...sketches.keys()]) clearSketch(sketchId)
      sketchMaterial.dispose()
      sketchGhostMaterial.dispose()
      regionMaterial.dispose()
      regionSelectedMaterial.dispose()
      for (const bodyId of [...bodies.keys()]) clearBody(bodyId)
      controls.dispose()
      baseMaterial.dispose()
      highlightMaterial.dispose()
      proposedMaterial.dispose()
      previewMaterial.dispose()
      edgeMaterial.dispose()
      renderer.dispose()
      renderer.domElement.remove()
    },
  }
}
