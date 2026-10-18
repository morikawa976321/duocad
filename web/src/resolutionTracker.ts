/**
 * 可変解像度(ズーム連動)の「カメラが止まったか」の判定と、直近に採用した解像度の保持。
 * 目標精度は世界座標のmmではなく画面の画素で決める(式と定数は `adaptiveResolution.ts`、
 * 同じものが `duocad/resolution.py` にある)。
 * 見えているものの大きさ(`contentDiagonal`)と面の見本(`faceSamples`)は本編が書くので getter で借りる。
 */
import * as THREE from 'three'

import { DEFAULT_RESOLUTION, deviationFloored, viewResolution } from './adaptiveResolution'
import { selectFineFaces, type BodySamples } from './faceCulling'
import type { ViewportResolution } from './viewportTypes'

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
 * いま細かく測っている面が粗い側へ落ちるまでの余裕[画素](`faceCulling.ts` の `selectFineFaces`)。
 * 0だと視錐台の縁の面が入/出を往復し、そのたびに27部品ぶんの測り直しがサーバへ飛ぶ。
 * 画素で決めるのは、縁の余裕が寄り引きで変わらないようにするため。
 */
const FOCUS_HYSTERESIS_PIXELS = 16

type Focus = Record<string, number[] | null>

export interface ResolutionTrackerDeps {
  camera(): THREE.PerspectiveCamera | THREE.OrthographicCamera
  /** 画面1画素ぶんの世界座標の長さ(本編の `worldPerPixel`。グリッドと同じ物差し)。 */
  worldPerPixel(): number
  /** いま見えているものの外接対角[mm](注記と near/far も読むので本編が持つ)。 */
  contentDiagonal(): number
  faceSamples: ReadonlyMap<string, BodySamples>
  /** 面の見本が変わるたびに増える番号(同じなら `computeFocus` を再走査しない)。 */
  focusGeneration(): number
  /** カメラが意味のある幅で動いた(near/far を引き直す。`updateClipPlanes`)。 */
  onMoved(): void
  /** 採用した解像度が変わった(ブラウザ内で完結するスケッチの折れ線を描き直す)。 */
  onApplied(): void
}

export interface ResolutionTracker {
  update(now: number): void
  applied(): ViewportResolution
  /** face単位の高精度化を使うか。**切れるのは実機検査のためだけ**(既定は入り)。 */
  setFaceCulling(enabled: boolean): void
  onChange(listener: (resolution: ViewportResolution) => void): void
}

export function createResolutionTracker(deps: ResolutionTrackerDeps): ResolutionTracker {
  const { camera, worldPerPixel, contentDiagonal, faceSamples, focusGeneration, onMoved, onApplied } = deps

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
  const lastCameraPosition = new THREE.Vector3(NaN, NaN, NaN)
  const lastCameraQuaternion = new THREE.Quaternion(0, 0, 0, 1)
  let lastCameraZoom = -1
  /** 投影行列の x の倍率(`elements[0]`)。幅だけが変わるリサイズ(左右パネルの開閉)は位置・姿勢・ズーム・`worldPerPixel` のどれも動かさず、これだけが動く。 */
  let lastProjectionScaleX = NaN
  let cameraEpoch = 0
  let steadySince = 0
  let lastFocusComputeEpoch = -1
  let lastFocusComputeGeneration = -1
  let faceCullingEnabled = true
  let lastAppliedFocus: Focus | null = null
  let lastAppliedFloored = false

  /**
   * カメラが意味のある幅で動いたか(位置・姿勢・正投影の倍率・投影の横倍率)。
   * 一致ではなく閾値で見る: `OrbitControls` の減衰は指数で近づき完全には止まらないので、
   * 一致を条件にすると `steadySince` が毎フレーム振り出しに戻り、解像度が二度と採り直されない
   * (実測: ホイールを回しても目標偏差が動かなくなった)。閾値は画面の画素で決める
   * (mmだと20mmの立方体と600mmの筐体で「止まった」の意味が変わる)。
   */
  function cameraMoved(): boolean {
    const current = camera()
    if (!Number.isFinite(lastCameraPosition.x)) return true
    if (current.position.distanceTo(lastCameraPosition) > worldPerPixel() * CAMERA_STILL_PIXELS) {
      return true
    }
    // 四元数の内積は向きの近さ(1で一致)。符号は向きに影響しないので絶対値で見る。
    if (Math.abs(current.quaternion.dot(lastCameraQuaternion)) < 1 - CAMERA_STILL_ANGLE) return true
    if (Math.abs(current.zoom - lastCameraZoom) > Math.abs(lastCameraZoom) * RESOLUTION_STILL_EPSILON) return true
    // 視錐台が横に広がる/狭まると、新しく画面に入った面を細かく測り直す必要がある。
    const scaleX = current.projectionMatrix.elements[0]
    return Math.abs(scaleX - lastProjectionScaleX) > Math.abs(lastProjectionScaleX) * RESOLUTION_STILL_EPSILON
  }

  function rememberCamera(): void {
    const current = camera()
    lastCameraPosition.copy(current.position)
    lastCameraQuaternion.copy(current.quaternion)
    lastCameraZoom = current.zoom
    lastProjectionScaleX = current.projectionMatrix.elements[0]
  }

  function focusEqual(a: Focus | null, b: Focus | null): boolean {
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

  function computeFocus(): Focus | null {
    // 切ってあるときは何も指定しない(全Body全面が細かい側)。
    if (!faceCullingEnabled || faceSamples.size === 0) return null
    const current = camera()
    const frustum = new THREE.Frustum()
    current.updateMatrixWorld()
    frustum.setFromProjectionMatrix(
      new THREE.Matrix4().multiplyMatrices(current.projectionMatrix, current.matrixWorldInverse),
    )
    const viewDirection = current.getWorldDirection(new THREE.Vector3())
    const viewpoint = (current as THREE.PerspectiveCamera).isPerspectiveCamera
      ? current.getWorldPosition(new THREE.Vector3())
      : null
    const hysteresis = worldPerPixel() * FOCUS_HYSTERESIS_PIXELS
    const result: Focus = {}
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
   * 門はズームに加え姿勢も見る(寄りだけでは `worldPerPixel` が動かず視錐台の変化に気づけない)。
   * 落ち着いた後の `computeFocus` は2835面を走査するので、姿勢と `focusGeneration` が同じなら再走査しない。
   *
   * `moved` の分岐に `onMoved()`(near/far の引き直し)を相乗りさせてある: 画素基準の閾値で門が
   * 閉じているので、減衰が画素以下に収まれば自然に呼ばれなくなる。
   */
  function update(now: number): void {
    const perPixel = worldPerPixel()
    const diagonal = contentDiagonal()
    const moved =
      lastWorldPerPixel < 0 ||
      Math.abs(perPixel - lastWorldPerPixel) > lastWorldPerPixel * RESOLUTION_STILL_EPSILON ||
      diagonal !== lastSizeHint ||
      cameraMoved()
    if (moved) {
      lastWorldPerPixel = perPixel
      lastSizeHint = diagonal
      rememberCamera()
      cameraEpoch += 1
      steadySince = now
      onMoved()
      return
    }
    if (now - steadySince < RESOLUTION_SETTLE_MS) return
    const generation = focusGeneration()
    if (cameraEpoch === lastFocusComputeEpoch && generation === lastFocusComputeGeneration) {
      return
    }

    const focus = computeFocus()
    lastFocusComputeEpoch = cameraEpoch
    lastFocusComputeGeneration = generation

    const target = viewResolution(perPixel, diagonal)
    const ratio = target.deviation / appliedResolution.deviation
    const focusChanged = !focusEqual(focus, lastAppliedFocus)
    // 下限に張り付いた・離れたことは偏差の比に出ない(張り付いた先の偏差が今の偏差の 1.25 倍以内に収まりうる)
    const floored = deviationFloored(perPixel, diagonal)
    if (
      resolutionApplied &&
      ratio < RESOLUTION_CHANGE_RATIO &&
      ratio > 1 / RESOLUTION_CHANGE_RATIO &&
      !focusChanged &&
      floored === lastAppliedFloored
    ) {
      return
    }
    resolutionApplied = true
    lastAppliedFocus = focus
    lastAppliedFloored = floored
    appliedResolution = { ...target, worldPerPixel: perPixel, sizeHint: diagonal, focus }
    onApplied()
    resolutionListener?.(appliedResolution)
  }

  return {
    update,
    applied: () => appliedResolution,
    setFaceCulling(enabled) {
      if (faceCullingEnabled === enabled) return
      faceCullingEnabled = enabled
      // 次のフレームで必ず採り直す(印を捨てないと、姿勢も世代も同じで早期returnする)。
      lastFocusComputeEpoch = -1
      lastFocusComputeGeneration = -1
    },
    onChange(listener) {
      resolutionListener = listener
    },
  }
}
