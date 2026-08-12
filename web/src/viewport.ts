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

export interface PickEvent {
  /** 当たった面(Body+添字)。外したら null。 */
  hit: FaceRef | null
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
  /** 選択中の面を強調する(nullで解除)。 */
  highlight(hit: FaceRef | null): void
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
  resize(): void
  dispose(): void
}

const BASE_COLOR = 0x9aa6b2
const HIGHLIGHT_COLOR = 0xffb454
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
  const edgeMaterial = new THREE.LineBasicMaterial({ color: EDGE_COLOR })

  const raycaster = new THREE.Raycaster()
  /** body_id -> そのBodyが画面に持つメッシュ。非表示・未読込のBodyはキーごと無い。 */
  const bodies = new Map<string, BodyMeshes>()
  /** 全Body分の面メッシュを1本にまとめたもの(ピック用)。update() のたびに作り直す。 */
  let allFaceMeshes: THREE.Mesh[] = []
  let pickListener: ((event: PickEvent) => void) | null = null
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

  function prune(validBodyIds: string[]): void {
    const valid = new Set(validBodyIds)
    const stale = [...bodies.keys()].filter((bodyId) => !valid.has(bodyId))
    if (stale.length === 0) return
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

  function highlight(hit: FaceRef | null): void {
    for (const mesh of allFaceMeshes) {
      const isHit =
        hit !== null &&
        mesh.userData.bodyId === hit.bodyId &&
        mesh.userData.faceIndex === hit.faceIndex
      mesh.material = isHit ? highlightMaterial : baseMaterial
    }
  }

  function pickAt(offsetX: number, offsetY: number): FaceRef | null {
    const { width, height } = size()
    const ndc = new THREE.Vector2((offsetX / width) * 2 - 1, -(offsetY / height) * 2 + 1)
    raycaster.setFromCamera(ndc, camera)
    const hits = raycaster.intersectObjects(allFaceMeshes, false)
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
    if (!mesh) return null
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
    const hit = pickAt(event.offsetX, event.offsetY)
    pickListener?.({ hit })
  }

  renderer.domElement.addEventListener('pointerdown', onPointerDown)
  renderer.domElement.addEventListener('pointerup', onPointerUp)

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
    highlight,
    project,
    resize: applySize,
    dispose() {
      alive = false
      cancelAnimationFrame(frameHandle)
      observer.disconnect()
      renderer.domElement.removeEventListener('pointerdown', onPointerDown)
      renderer.domElement.removeEventListener('pointerup', onPointerUp)
      for (const bodyId of [...bodies.keys()]) clearBody(bodyId)
      controls.dispose()
      baseMaterial.dispose()
      highlightMaterial.dispose()
      edgeMaterial.dispose()
      renderer.dispose()
      renderer.domElement.remove()
    },
  }
}
