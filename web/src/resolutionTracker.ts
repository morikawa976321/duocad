/**
 * 可変解像度(ズーム連動)。**寄ったら細かく、引いたら粗く。** 曲線が多角形に見えるのは弦と弧の
 * 乖離が数画素になったときなので、目標精度は世界座標のmmではなく**画面の画素**で決める
 * (式と定数は `adaptiveResolution.ts`。同じものが `duocad/resolution.py` にもある)。
 *
 * ここが持つのは「カメラが止まったか」の判定と、直近に採用した解像度だけ。見えているものの
 * 大きさ(`contentDiagonal`)と面の見本(`faceSamples`)は本編が書くので getter で借りる。
 */
import * as THREE from 'three'

import { DEFAULT_RESOLUTION, viewResolution } from './adaptiveResolution'
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
 * **いま細かく測っている面が粗い側へ落ちるまでの余裕[画素]**(`faceCulling.ts` の
 * `selectFineFaces`)。0にすると、視錐台の縁にある面が「入った/出た」を往復し、
 * そのたびに27部品ぶんの測り直しがサーバへ飛ぶ。**画素で決める**のは、
 * 縁の余裕が寄り引きで変わってはいけないため(当たり判定の太さと同じ流儀)。
 */
const FOCUS_HYSTERESIS_PIXELS = 16

type Focus = Record<string, number[] | null>

/** 本編(`viewport.ts`)から借りるもの。 */
export interface ResolutionTrackerDeps {
  /** 今のカメラ(透視/正投影の切り替えで差し替わるので getter)。 */
  camera(): THREE.PerspectiveCamera | THREE.OrthographicCamera
  /** 画面1画素ぶんの世界座標の長さ(本編の `worldPerPixel`。グリッドと同じ物差し)。 */
  worldPerPixel(): number
  /** いま見えているものの外接対角[mm](注記と near/far も読むので本編が持つ)。 */
  contentDiagonal(): number
  /** Body ごとの面の見本(`buildBody` / `clearBody` が書く)。 */
  faceSamples: ReadonlyMap<string, BodySamples>
  /** 面の見本が変わるたびに増える番号(同じなら `computeFocus` を再走査しない)。 */
  focusGeneration(): number
  /** カメラが意味のある幅で動いた(near/far を引き直す。`updateClipPlanes`)。 */
  onMoved(): void
  /** 採用した解像度が変わった(ブラウザ内で完結するスケッチの折れ線を描き直す)。 */
  onApplied(): void
}

export interface ResolutionTracker {
  /** カメラが落ち着いていれば解像度を採り直す(毎フレーム `tick` から呼ぶ)。 */
  update(now: number): void
  /** 直近に採用した解像度(スケッチの折れ線と `viewResolution()` が読む)。 */
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
  /** 直近に「止まっている」と記録したカメラ(位置・姿勢・倍率)。**上記の閾値で比べる。** */
  const lastCameraPosition = new THREE.Vector3(NaN, NaN, NaN)
  const lastCameraQuaternion = new THREE.Quaternion(0, 0, 0, 1)
  let lastCameraZoom = -1
  /** カメラが**意味のある幅で**動くたびに増える番号(`computeFocus` の再計算の鍵)。 */
  let cameraEpoch = 0
  let steadySince = 0
  /** 直近に `computeFocus` したときの姿勢と世代(同じなら再走査しない)。 */
  let lastFocusComputeEpoch = -1
  let lastFocusComputeGeneration = -1
  let faceCullingEnabled = true
  let lastAppliedFocus: Focus | null = null

  /**
   * カメラが**意味のある幅で**動いたか(位置・姿勢・正投影の倍率)。
   *
   * **一致ではなく閾値で見る。** `OrbitControls` の減衰は指数で近づくので、カメラは
   * いつまでも「完全には」止まらない —— 値の一致を「止まった」の条件にすると
   * `steadySince` が毎フレーム振り出しに戻り、**解像度が二度と採り直されない**
   * (実測: ホイールを回しても目標偏差が動かなくなった)。
   * 閾値は `worldPerPixel` と同じ流儀で**画面の画素**で決める —— 世界座標のmmで決めると、
   * 20mmの立方体と600mmの筐体で「止まった」の意味が変わる。
   */
  function cameraMoved(): boolean {
    const current = camera()
    if (!Number.isFinite(lastCameraPosition.x)) return true
    if (current.position.distanceTo(lastCameraPosition) > worldPerPixel() * CAMERA_STILL_PIXELS) {
      return true
    }
    // 四元数の内積は向きの近さ(1で一致)。符号は向きに影響しないので絶対値で見る。
    if (Math.abs(current.quaternion.dot(lastCameraQuaternion)) < 1 - CAMERA_STILL_ANGLE) return true
    return Math.abs(current.zoom - lastCameraZoom) > Math.abs(lastCameraZoom) * RESOLUTION_STILL_EPSILON
  }

  /** 「ここで止まっている」を記録する(次のフレームの `cameraMoved` の基準)。 */
  function rememberCamera(): void {
    const current = camera()
    lastCameraPosition.copy(current.position)
    lastCameraQuaternion.copy(current.quaternion)
    lastCameraZoom = current.zoom
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

  /** 視錐台とバックフェイスから、サーバへ送る `focus` を組み立てる(落ち着いた後だけ呼ぶ)。 */
  function computeFocus(): Focus | null {
    // 切ってあるときは**何も指定しない** = 全Body全面が細かい側(導入前と同じ)。
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
   * **門**はズームに加え**姿勢**も見る —— 寄りだけでは `worldPerPixel` が動かず、
   * 視錐台が変わっても気づけない。落ち着いたあと `computeFocus` は2835面を走査するので、
   * 姿勢と `focusGeneration` が同じなら再走査しない。
   *
   * **`moved` の分岐に `onMoved()`(near/far の引き直し)を相乗りさせてある**。near/farは
   * カメラ距離が変わるたびに引き直すべきだが、毎フレーム呼ぶ必要は無い —— この分岐は
   * 既存の画素基準の閾値判定(`cameraMoved()`)で門が閉じているので、オービット/パン/ドリーの
   * 減衰が画素以下に収まった時点で自然に呼ばれなくなる(動いている間は毎フレーム呼ばれるが、
   * `updateProjectionMatrix()` は毎フレームでも安い)。
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
    // **同じカメラ・同じ形なら判定もやり直さない**(`computeFocus` は2835面を走査する)。
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
    appliedResolution = { ...target, worldPerPixel: perPixel, sizeHint: diagonal, focus }
    // **スケッチはここで描き直す**(ブラウザ内で完結する経路。サーバ往復が要らない)。
    onApplied()
    resolutionListener?.(appliedResolution)
  }

  return {
    update,
    applied: () => appliedResolution,
    setFaceCulling(enabled) {
      if (faceCullingEnabled === enabled) return
      faceCullingEnabled = enabled
      // **次のフレームで必ず採り直す**。カメラが止まったままでも判定が変わるので、
      // 直前に計算した印を捨てないと「姿勢も世代も同じ」で早期returnしてしまう。
      lastFocusComputeEpoch = -1
      lastFocusComputeGeneration = -1
    },
    onChange(listener) {
      resolutionListener = listener
    },
  }
}
