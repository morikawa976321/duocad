/**
 * ビューキューブ(常設・26領域)の描画と操作。形と向きの計算は `viewCube.ts` の純関数が持つ。
 *
 * 同一canvasへの第2パス(`render`。`viewport.ts` の `tick` が毎フレーム呼ぶ)。別のDOM要素で
 * 浮かせない。同じcanvasに描くぶん `elementFromPoint` は常にcanvasを返すので、メインの
 * ポインタ経路(`clickable`)の側でキューブの矩形を明示的に除外する。
 *
 * 独立した `THREE.Scene`+専用の光を持つ。キューブ自体はワールドに固定し、カメラだけを
 * メインカメラの向きに追従させる。
 *
 * カメラは本編(`activateCamera`)と同じ2台持ち(`cubePerspective`/`cubeOrthographic`)。
 */
import * as THREE from 'three'
import type { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js'

import { EDGE_T, cellBox, cellDirection, regionCells, upForDirection } from './viewCube'
import { BASE_COLOR, FOV_DEFAULT, HOME_VIEW_OFFSET, HOVER_LIGHTEN, lighten } from './viewportConstants'

/**
 * ビューキューブ(常設・26領域)の一辺[画素]と右上の余白[画素]。
 * クリックを吸う矩形でもある(`viewCubeRect` / `insideCubeRect`)ので、広げるほど右上で
 * モデルを指せる領域が減る。
 */
export const CUBE_SIZE_PX = 144
export const CUBE_MARGIN_PX = 12
/** `pointerdown` からこれだけ動いたらドラッグ(未満で `pointerup` ならクリック=スナップ)。 */
export const CUBE_DRAG_THRESHOLD_PX = 4
/** クリックでスナップするときの視点アニメーション時間[ms]。 */
export const CUBE_SNAP_MS = 300
/** キューブ用正投影カメラの視錐台半幅(角が切れない程度)。
 *  透視のときの距離式 `CUBE_ORTHO_HALF / sin(fov/2)` の分子も兼ねる(fov→0で正投影の見え方へ連続的に近づく)。 */
export const CUBE_ORTHO_HALF = 1.8
/** 正投影のときのキューブ用カメラ距離(near/farに収まればよい)。透視のときは `updateCubeCamera` が毎フレームfovから導く。 */
export const CUBE_CAMERA_DISTANCE = 4
/** キューブ(半幅1の立方体)の外接球の半径。透視のときのnear/farの導出に使う。 */
export const CUBE_CIRCUMRADIUS = Math.sqrt(3)
/** 透視のときのnear平面の安全率(1未満)。手前の角がnearに食われて消えるのを防ぐ。 */
export const CUBE_NEAR_SAFETY = 0.5
/** 透視のときのfar平面の余裕率(1超)。 */
export const CUBE_FAR_MARGIN = 1.5
/** 面セルの日本語ラベル。キーは `"i,j,k"`。Z-up右手系。 */
export const CUBE_FACE_LABELS: Readonly<Record<string, string>> = {
  '1,0,0': '右',
  '-1,0,0': '左',
  '0,1,0': '後',
  '0,-1,0': '前',
  '0,0,1': '上',
  '0,0,-1': '下',
}

/** 本編(`viewport.ts`)から借りるもの。 */
export interface ViewCubeDeps {
  /** 第2パスを描く先。本編と同じ renderer。 */
  renderer: THREE.WebGLRenderer
  /** 本編がいま使っているカメラ。投影の切替で差し替わるので毎回読み直す。 */
  camera(): THREE.PerspectiveCamera | THREE.OrthographicCamera
  perspective: THREE.PerspectiveCamera
  orthographic: THREE.OrthographicCamera
  controls: OrbitControls<THREE.PerspectiveCamera | THREE.OrthographicCamera>
  /** コンテナのCSSピクセル寸法。 */
  size(): { width: number; height: number }
  /** verb引数overlay(`CommandHost` の `.toolbar-overlay`)がいま覆っている高さ[px](閉じていれば0)。
   *  `.viewport` 自体は縮まずoverlayがcanvasに重なるので、キューブの矩形をこの分だけ下げる(GitHub issue #2)。 */
  overlayOffset(): number
}

export interface ViewCubeOverlay {
  /** ビューポート右上の矩形(コンテナ内のCSSピクセル、左上原点)。 */
  rect(): { x: number; y: number; w: number; h: number }
  /** その画素が矩形の内側か。 */
  inside(x: number, y: number): boolean
  /** ポインタを引き取っている(クリックかドラッグか未確定・またはドラッグ中)。 */
  capturing(): boolean
  /** 視点アニメーションが進行中。 */
  snapping(): boolean
  /** 視点アニメーションを打ち切る。 */
  cancelSnap(): void
  /** キューブが引き取ったら `true`(メインのピック・矩形選択へ流さない)。 */
  pointerDown(event: PointerEvent): boolean
  /** キューブが処理したら `true`。 */
  pointerUp(event: PointerEvent): boolean
  pointerMove(event: PointerEvent): void
  /** ポインタがキューブの上にある位置(外なら `null`)。 */
  setHoverAt(point: { x: number; y: number } | null): void
  /** 視点アニメーションを1フレームぶん進める。 */
  stepSnap(now: number): void
  updateHover(): void
  /** 第2パスを描く(本編の描画のあと)。 */
  render(): void
  dispose(): void
}

export function createViewCubeOverlay(deps: ViewCubeDeps): ViewCubeOverlay {
  const { renderer, controls, perspective, orthographic, size, overlayOffset } = deps

  const cubeScene = new THREE.Scene()
  const cubeOrthographic = new THREE.OrthographicCamera(
    -CUBE_ORTHO_HALF, CUBE_ORTHO_HALF, CUBE_ORTHO_HALF, -CUBE_ORTHO_HALF, 0.1, 10,
  )
  // near/farは初期値。透視のときは `updateCubeCamera` が毎フレーム引き直す。
  const cubePerspective = new THREE.PerspectiveCamera(FOV_DEFAULT, 1, 0.1, 10)
  cubeScene.add(cubeOrthographic, cubePerspective)
  let cubeActive: THREE.PerspectiveCamera | THREE.OrthographicCamera = cubeOrthographic
  // 専用の光1つ(本編の照明は共有しない)。アクティブなカメラの子にして(本編の `headlight` と同じ)
  // どの向きでも視点側から当てる。
  const cubeLight = new THREE.DirectionalLight(0xffffff, 1.6)
  cubeLight.position.set(0.4, 0.7, 1)
  cubeActive.add(cubeLight)

  const cubeGroup = new THREE.Group()
  const cubeLabelGroup = new THREE.Group()
  cubeScene.add(cubeGroup, cubeLabelGroup)

  const cubeDisposables: Array<{ dispose(): void }> = []
  /** `"i,j,k"` -> そのセルの材質(ホバーで明度を変えるためインスタンスを分ける)。 */
  const cubeMaterials = new Map<string, THREE.MeshLambertMaterial>()

  for (const [i, j, k] of regionCells()) {
    const { center, size } = cellBox(i, j, k)
    const geometry = new THREE.BoxGeometry(size[0], size[1], size[2])
    const material = new THREE.MeshLambertMaterial({ color: BASE_COLOR })
    const mesh = new THREE.Mesh(geometry, material)
    mesh.position.set(center[0], center[1], center[2])
    mesh.userData = { i, j, k }
    cubeGroup.add(mesh)
    cubeMaterials.set(`${i},${j},${k}`, material)
    cubeDisposables.push(geometry, material)
  }

  /** 面セルの外側に立てる、指せない日本語ラベル板(ピック対象はセル側の `cubeGroup` だけ)。 */
  function buildCubeLabel(i: number, j: number, k: number, text: string): void {
    const normal = new THREE.Vector3(i, j, k) // 面セルなので既に単位ベクトル
    const up = upForDirection([i, j, k])
    const basis = new THREE.Matrix4().lookAt(normal, new THREE.Vector3(0, 0, 0), new THREE.Vector3(...up))
    const canvas = document.createElement('canvas')
    canvas.width = 128
    canvas.height = 128
    const ctx = canvas.getContext('2d')
    if (ctx) {
      ctx.fillStyle = '#e8eef6'
      ctx.font = 'bold 84px sans-serif'
      ctx.textAlign = 'center'
      ctx.textBaseline = 'middle'
      ctx.fillText(text, canvas.width / 2, canvas.height / 2 + 4)
    }
    const texture = new THREE.CanvasTexture(canvas)
    const material = new THREE.MeshBasicMaterial({ map: texture, transparent: true })
    // 面セルの一辺に対する比で持つ(EDGE_T を変えても面と文字の比が崩れない)。
    const labelSize = 2 * (1 - 2 * EDGE_T) * 0.7
    const geometry = new THREE.PlaneGeometry(labelSize, labelSize)
    const mesh = new THREE.Mesh(geometry, material)
    mesh.quaternion.setFromRotationMatrix(basis)
    // 面セルの外側の面は必ず立方体の表面(±1)に一致する(`cellBox` の不変条件。
    // `tests/eval_view_cube.mjs` が検査)ので、+0.001(z-fighting回避)だけ出す。
    // `cellBox` の `size` から出すと薄い寸法と取り違えて立方体の中に埋もれる。
    mesh.position.copy(normal).multiplyScalar(1.001)
    mesh.raycast = () => {} // ピックされるのは面セル(cubeGroup)の側
    cubeLabelGroup.add(mesh)
    cubeDisposables.push(geometry, material, texture)
  }
  for (const [i, j, k] of regionCells()) {
    const label = CUBE_FACE_LABELS[`${i},${j},${k}`]
    if (label !== undefined) buildCubeLabel(i, j, k, label)
  }

  /** ビューポート右上のキューブの矩形(コンテナ内のCSSピクセル、左上原点。`offsetX`/`offsetY` と同じ)。
   *  verb引数overlayが開いている間はその高さぶん下げる(`overlayOffset` 参照)。 */
  function viewCubeRect(): { x: number; y: number; w: number; h: number } {
    const { width } = size()
    return {
      x: width - CUBE_MARGIN_PX - CUBE_SIZE_PX,
      y: CUBE_MARGIN_PX + overlayOffset(),
      w: CUBE_SIZE_PX,
      h: CUBE_SIZE_PX,
    }
  }

  /** その画素がキューブの矩形の内側か。 */
  function insideCubeRect(x: number, y: number): boolean {
    const rect = viewCubeRect()
    return x >= rect.x && x <= rect.x + rect.w && y >= rect.y && y <= rect.y + rect.h
  }

  const cubeRaycaster = new THREE.Raycaster()
  const cubeNdc = new THREE.Vector2()

  /** 画面座標(キューブ矩形の内側)から、当たった領域の `(i,j,k)`(外れなら null)。
   *  描画と同じ `cubeActive` でレイを作る(別カメラだとFOV 90などで見えている角をクリックしても隣の辺が取れる)。 */
  function cubeHitAt(x: number, y: number): { i: number; j: number; k: number } | null {
    const rect = viewCubeRect()
    if (x < rect.x || x > rect.x + rect.w || y < rect.y || y > rect.y + rect.h) return null
    cubeNdc.set(((x - rect.x) / rect.w) * 2 - 1, -(((y - rect.y) / rect.h) * 2 - 1))
    cubeRaycaster.setFromCamera(cubeNdc, cubeActive)
    const hits = cubeRaycaster.intersectObjects(cubeGroup.children, false)
    if (hits.length === 0) return null
    const data = hits[0].object.userData as { i: number; j: number; k: number }
    return { i: data.i, j: data.j, k: data.k }
  }

  /**
   * キューブ用カメラを、いまのメインカメラの向き・投影へ追従させる(`up` も写すのでロールも一致)。
   * 毎フレーム `renderCubePass` 経由で呼ばれるので、FOVの値は保持せず毎回読む。
   *
   * - 正投影: 距離は `CUBE_CAMERA_DISTANCE` 固定。
   * - 透視: 距離は `CUBE_ORTHO_HALF / sin(fov/2)`。FOVが大きいほどカメラが近づくので、
   *   near/farも距離から引き直す(定数だと手前の角がnearに食われて丸ごと消える)。
   */
  function updateCubeCamera(): void {
    const camera = deps.camera()
    const wantOrtho = camera === orthographic
    const next = wantOrtho ? cubeOrthographic : cubePerspective
    if (next !== cubeActive) {
      // 光(カメラの子)を付け替える。忘れるとキューブが真っ暗になる。
      cubeActive.remove(cubeLight)
      next.add(cubeLight)
      cubeActive = next
    }
    const offset = camera.position.clone().sub(controls.target)
    if (offset.lengthSq() < 1e-12) offset.copy(HOME_VIEW_OFFSET) // 退化(注視点に重なる)の逃げ場
    const dir = offset.normalize()
    let distance = CUBE_CAMERA_DISTANCE
    if (!wantOrtho) {
      cubePerspective.fov = perspective.fov
      distance = CUBE_ORTHO_HALF / Math.sin(THREE.MathUtils.degToRad(perspective.fov) / 2)
      // 手前の角までの距離は `distance - CUBE_CIRCUMRADIUS`。
      cubePerspective.near = Math.max((distance - CUBE_CIRCUMRADIUS) * CUBE_NEAR_SAFETY, 0.01)
      cubePerspective.far = (distance + CUBE_CIRCUMRADIUS) * CUBE_FAR_MARGIN
      cubePerspective.updateProjectionMatrix()
    }
    cubeActive.position.copy(dir).multiplyScalar(distance)
    cubeActive.up.copy(camera.up)
    cubeActive.lookAt(0, 0, 0)
  }

  // ---------------------------------------------------------------- ホバー(明るくなる)
  /** ポインタがキューブの矩形の内側にあるときだけ非null。 */
  let cubeHoverAt: { x: number; y: number } | null = null
  let cubeHoverKey = ''

  function applyCubeMaterials(): void {
    for (const [key, material] of cubeMaterials) {
      material.color.setHex(key === cubeHoverKey ? lighten(BASE_COLOR, HOVER_LIGHTEN) : BASE_COLOR)
    }
  }

  function updateCubeHover(): void {
    const hit = cubeHoverAt === null ? null : cubeHitAt(cubeHoverAt.x, cubeHoverAt.y)
    const key = hit === null ? '' : `${hit.i},${hit.j},${hit.k}`
    if (key === cubeHoverKey) return
    cubeHoverKey = key
    applyCubeMaterials()
  }

  // ---------------------------------------------------------------- クリック=スナップ(視点アニメーション)
  interface CubeSnap {
    start: number
    fromDir: THREE.Vector3
    fromUp: THREE.Vector3
    dirQuat: THREE.Quaternion
    upQuat: THREE.Quaternion
    distance: number
  }
  let cubeSnap: CubeSnap | null = null

  /**
   * カメラを `dir` 方向(注視点からの距離は保つ)・`up` 姿勢へアニメーションで移す。
   * 方向を球面上で補間して距離を掛ける(直線補間だと注視点の近くを一瞬めり込む)。
   * キューブ固有の状態(`cubeSnap` 以外)には触れず、他から呼べる形にしてある。
   */
  function animateCameraTo(dir: THREE.Vector3, up: THREE.Vector3): void {
    const camera = deps.camera()
    const distance = Math.max(camera.position.distanceTo(controls.target), 1e-6)
    const fromDir = camera.position.clone().sub(controls.target).normalize()
    const fromUp = camera.up.clone().normalize()
    const toDir = dir.clone().normalize()
    const toUp = up.clone().normalize()
    cubeSnap = {
      start: performance.now(),
      fromDir,
      fromUp,
      // `setFromUnitVectors` は反対方向(dot≈-1)も安全に扱うので対蹠点への遷移でも壊れない。
      dirQuat: new THREE.Quaternion().setFromUnitVectors(fromDir, toDir),
      upQuat: new THREE.Quaternion().setFromUnitVectors(fromUp, toUp),
      distance,
    }
  }

  /** アニメーションを1フレームぶん進める(`tick` が毎フレーム呼ぶ。動いていなければ何もしない)。 */
  function stepCubeSnap(now: number): void {
    const camera = deps.camera()
    if (cubeSnap === null) return
    const t = Math.min(1, (now - cubeSnap.start) / CUBE_SNAP_MS)
    const eased = t < 0.5 ? 2 * t * t : 1 - ((-2 * t + 2) ** 2) / 2
    const dirQ = new THREE.Quaternion().slerp(cubeSnap.dirQuat, eased)
    const upQ = new THREE.Quaternion().slerp(cubeSnap.upQuat, eased)
    const dir = cubeSnap.fromDir.clone().applyQuaternion(dirQ).normalize()
    const up = cubeSnap.fromUp.clone().applyQuaternion(upQ).normalize()
    camera.position.copy(controls.target).addScaledVector(dir, cubeSnap.distance)
    camera.up.copy(up)
    camera.lookAt(controls.target)
    if (t >= 1) cubeSnap = null
  }

  function handleCubeClick(x: number, y: number): void {
    const hit = cubeHitAt(x, y)
    if (hit === null) return
    const dir = cellDirection(hit.i, hit.j, hit.k)
    const up = upForDirection(dir)
    animateCameraTo(new THREE.Vector3(...dir), new THREE.Vector3(...up))
  }

  // ---------------------------------------------------------------- ドラッグ=自由回転(Pointer Lock)
  let cubeDownX = 0
  let cubeDownY = 0
  /** `setPointerCapture` したポインタID(していなければ -1)。 */
  let cubePointerId = -1
  /** `pointerdown` はキューブの内側で起きたが、まだクリックかドラッグか未確定。 */
  let cubePointerActive = false
  /** しきい値を超えてドラッグと確定した。 */
  let cubeDragging = false
  /** Pointer Lockが実際に掛かっている。 */
  let cubeLocked = false

  function releaseCubePointerCapture(): void {
    if (cubePointerId === -1) return
    try {
      renderer.domElement.releasePointerCapture(cubePointerId)
    } catch {
      // 既に外れていても実害は無い。
    }
    cubePointerId = -1
  }

  /**
   * Pointer Lockを要求する。Promiseで失敗したら `unadjustedMovement` が理由の可能性があるので、
   * オプション無しで1回だけ再試行する。どちらも失敗したときのフォールバック(カーソルを出した
   * ままの通常ドラッグ)は `pointerlockerror` 側。古い実装はPromiseを返さずイベントだけで知らせる。
   */
  function requestCubeLock(): void {
    let request: Promise<void> | undefined
    try {
      request = renderer.domElement.requestPointerLock({ unadjustedMovement: true })
    } catch {
      request = undefined
    }
    request?.catch(() => {
      try {
        renderer.domElement.requestPointerLock()?.catch(() => {
          /* `pointerlockerror` がフォールバックへの切替を行う */
        })
      } catch {
        /* 同上 */
      }
    })
  }

  function beginCubeDrag(): void {
    cubeDragging = true
    cubeHoverAt = null
    if (cubeHoverKey !== '') { cubeHoverKey = ''; applyCubeMaterials() }
    requestCubeLock()
  }

  function endCubeDrag(): void {
    cubeDragging = false
    cubePointerActive = false
    releaseCubePointerCapture()
    if (document.pointerLockElement === renderer.domElement) document.exitPointerLock()
  }

  function handleCubePointerLockChange(): void {
    const locked = document.pointerLockElement === renderer.domElement
    if (locked) {
      cubeLocked = true
      return
    }
    if (cubeLocked) {
      // ロックが外れたらドラッグ終了(Esc・フォーカス移動など)。
      cubeLocked = false
      if (cubeDragging) endCubeDrag()
    }
  }
  function handleCubePointerLockError(): void {
    // ロックが取れなくても、カーソルを出したままのドラッグにフォールバックする
    // (`movementX/Y` は非ロック時の `pointermove` でも運ばれる)。
    cubeLocked = false
  }
  document.addEventListener('pointerlockchange', handleCubePointerLockChange)
  document.addEventListener('pointerlockerror', handleCubePointerLockError)

  /**
   * キューブのドラッグ中の `pointermove`。ロック中は `clientX/clientY` が凍結し
   * `movementX/movementY` だけが動くので、回転量はそれで計算する(ロック不可でも同じ)。
   * 式は `OrbitControls._handleMouseMoveRotate` と同じ(`rotateLeft`/`rotateUp` は公開API)。
   * 上下の可動域は制限しない(`OrbitControls` が元々制限を持たない)。
   */
  function handleCubePointerMove(event: PointerEvent): void {
    if (!cubeDragging) {
      const dx = event.offsetX - cubeDownX
      const dy = event.offsetY - cubeDownY
      if (Math.hypot(dx, dy) < CUBE_DRAG_THRESHOLD_PX) return
      beginCubeDrag()
    }
    cubeSnap = null
    const height = renderer.domElement.clientHeight || 1
    controls.rotateLeft((2 * Math.PI * event.movementX * controls.rotateSpeed) / height)
    controls.rotateUp((2 * Math.PI * event.movementY * controls.rotateSpeed) / height)
  }

  /**
   * ビューキューブを同一canvasへ第2パスとして描く(メインの描画のあと)。
   * 終わったら必ずフルサイズのviewportと `setScissorTest(false)` に戻す(戻し忘れると次フレームの本編が隅に描かれる)。
   * `setViewport`/`setScissor` はCSSピクセル(three.js側が`pixelRatio`を掛ける)だが、
   * y軸はWebGLネイティブの下原点なので `height - rect.y - rect.h` で変換する。
   */
  function renderCubePass(): void {
    updateCubeCamera()
    const rect = viewCubeRect()
    const { width, height } = size()
    const glY = height - rect.y - rect.h
    renderer.autoClear = false
    renderer.clearDepth()
    renderer.setViewport(rect.x, glY, rect.w, rect.h)
    renderer.setScissor(rect.x, glY, rect.w, rect.h)
    renderer.setScissorTest(true)
    renderer.render(cubeScene, cubeActive)
    renderer.setScissorTest(false)
    renderer.setViewport(0, 0, width, height)
    renderer.autoClear = true
  }

  return {
    rect: viewCubeRect,
    inside: insideCubeRect,
    capturing() {
      return cubePointerActive || cubeDragging
    },
    snapping() {
      return cubeSnap !== null
    },
    cancelSnap() {
      cubeSnap = null
    },
    pointerDown(event) {
      if (!insideCubeRect(event.offsetX, event.offsetY)) return false
      cubeDownX = event.offsetX
      cubeDownY = event.offsetY
      cubePointerActive = true
      cubeDragging = false
      cubePointerId = event.pointerId
      try {
        renderer.domElement.setPointerCapture(event.pointerId)
      } catch {
        // 非対応環境でもフォールバックのドラッグは成立する。
      }
      return true
    },
    pointerUp(event) {
      if (event.button !== 0 || !(cubePointerActive || cubeDragging)) return false
      const wasClick = cubePointerActive && !cubeDragging
      const x = event.offsetX
      const y = event.offsetY
      if (cubeDragging) endCubeDrag()
      else {
        cubePointerActive = false
        releaseCubePointerCapture()
      }
      if (wasClick) handleCubeClick(x, y)
      return true
    },
    pointerMove: handleCubePointerMove,
    setHoverAt(point) {
      cubeHoverAt = point
    },
    stepSnap: stepCubeSnap,
    updateHover: updateCubeHover,
    render: renderCubePass,
    dispose() {
      document.removeEventListener('pointerlockchange', handleCubePointerLockChange)
      document.removeEventListener('pointerlockerror', handleCubePointerLockError)
      if (document.pointerLockElement === renderer.domElement) document.exitPointerLock()
      releaseCubePointerCapture()
      // `THREE.Light` に `dispose()` は無い。
      for (const item of cubeDisposables) item.dispose()
      cubeGroup.clear()
      cubeLabelGroup.clear()
    },
  }
}
