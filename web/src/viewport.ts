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

export interface PickEvent {
  /** 当たった面の添字(1始まり)。外したら null。 */
  faceIndex: number | null
  /** ポインタを押した時刻(= 操作の始まり)。`performance.now()`。 */
  tPointerDown: number
  /** 当たり判定が終わった時刻。`performance.now()`。 */
  tResolved: number
}

export interface Viewport {
  /** 形状を差し替える。初回だけカメラを形状に合わせ、以後は視点を保つ。 */
  load(payload: GeometryPayload): void
  /** 面のクリック(当たり・外れとも)を受け取る。 */
  onPick(listener: (event: PickEvent) => void): void
  /** 選択中の面を強調する。 */
  highlight(faceIndices: number[]): void
  /**
   * 面の表面上の点の画面座標(コンテナ内のCSSピクセル)。無理なら null。
   *
   * これは自動テスト(`scripts/smoke.mjs`)が**本物のクリックで面を選ぶ**ために必要な穴で、
   * 「動くはず」で納品しないための道具。重心ではなく三角形の上の点を返し、
   * さらに**その画素からのレイが実際にこの面へ当たることを確かめてから**返す
   * (重心は円環や円筒では面の外にあり、隠れている面もあるため)。
   */
  project(faceIndex: number): { x: number; y: number } | null
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
  let faceMeshes: THREE.Mesh[] = []
  let scratch: Array<{ dispose(): void }> = []
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

  function clear(): void {
    for (const mesh of faceMeshes) root.remove(mesh)
    for (const child of [...root.children]) root.remove(child)
    for (const item of scratch) item.dispose()
    scratch = []
    faceMeshes = []
  }

  function load(payload: GeometryPayload): void {
    clear()
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
      mesh.userData.faceIndex = face.index
      root.add(mesh)
      faceMeshes.push(mesh)
      scratch.push(geometry)

      // 面の境界線。装飾ではなく「どこが1つの面か」を見せるためで、
      // これが無いと隣り合う同色の面を狙い分けられず、狙いにくさの計測が意味を失う。
      const edges = new THREE.EdgesGeometry(geometry, 20)
      root.add(new THREE.LineSegments(edges, edgeMaterial))
      scratch.push(edges)
    }

    if (!framed) {
      frame(payload.bbox)
      framed = true
    }
  }

  function frame(bbox: GeometryPayload['bbox']): void {
    const center = new THREE.Vector3(bbox.center[0], bbox.center[1], bbox.center[2])
    const diagonal = bbox.diagonal > 0 ? bbox.diagonal : 1
    const distance = diagonal * 1.2
    camera.near = Math.max(diagonal / 1000, 0.01)
    camera.far = diagonal * 20
    camera.position.copy(center).add(new THREE.Vector3(distance, distance * 0.6, distance))
    camera.updateProjectionMatrix()
    controls.target.copy(center)
    controls.update()
  }

  function highlight(faceIndices: number[]): void {
    const selected = new Set(faceIndices)
    for (const mesh of faceMeshes) {
      mesh.material = selected.has(mesh.userData.faceIndex as number)
        ? highlightMaterial
        : baseMaterial
    }
  }

  function pickAt(offsetX: number, offsetY: number): number | null {
    const { width, height } = size()
    const ndc = new THREE.Vector2((offsetX / width) * 2 - 1, -(offsetY / height) * 2 + 1)
    raycaster.setFromCamera(ndc, camera)
    const hits = raycaster.intersectObjects(faceMeshes, false)
    if (hits.length === 0) return null
    const faceIndex = hits[0].object.userData.faceIndex
    return typeof faceIndex === 'number' ? faceIndex : null
  }

  function project(faceIndex: number): { x: number; y: number } | null {
    const mesh = faceMeshes.find((candidate) => candidate.userData.faceIndex === faceIndex)
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
      if (pickAt(x, y) === faceIndex) return { x, y }
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
    const faceIndex = pickAt(event.offsetX, event.offsetY)
    pickListener?.({ faceIndex, tPointerDown: downAt, tResolved: performance.now() })
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
    load,
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
      clear()
      controls.dispose()
      baseMaterial.dispose()
      highlightMaterial.dispose()
      edgeMaterial.dispose()
      renderer.dispose()
      renderer.domElement.remove()
    },
  }
}
