/**
 * 断面表示(issue #6)。基準3平面の1枚でボディを切り、切り口を塗り、法線方向の矢印(ハンドル)の
 * ドラッグで位置を動かす。状態の正は App で、ここは写すだけ。判断の理由は docs/VIEWPORT.md「断面」。
 *
 * 切り口はステンシルの偶奇で塗るので、閉じた立体でしか正しくない(開いたシェルでは板がはみ出す・欠ける)。
 *
 * three.js のレイキャストはクリップ平面を見ない。切られた側が指せないことは viewport が
 * `cut` / `keeps` / `signedDistance` で判定する。`cut` の偶奇の規則は描画のステンシルと揃えてある。
 */
import * as THREE from 'three'

import { distSqPointSegment } from './screenPick'
import type { BasePlaneName } from './session'
import {
  BASE_PLANES,
  DEPTH_BIAS_LAYER,
  HOME_VIEW_OFFSET,
  HOVER_LIGHTEN,
  LAYER,
  SECTION_CAP_COLOR,
  SECTION_FRAME_COLOR,
  SECTION_FRAME_MARGIN_MIN_MM,
  SECTION_FRAME_MARGIN_RATIO,
  SECTION_HANDLE,
  SECTION_HANDLE_COLOR,
  lighten,
  withDepthBias,
} from './viewportConstants'
import type { SectionInfo, SectionState } from './viewportTypes'

export interface SectionViewDeps {
  /** その画素を通るレイ。使い回しの実体なので受け取ったらすぐ読む。 */
  rayAt(offsetX: number, offsetY: number): THREE.Ray
  /** いま使っているカメラ(透視と正投影で差し替わる)。 */
  camera(): THREE.Camera
  /** 世界座標 → コンテナ内の画素。カメラの裏だけ null(画面外でも返す)。 */
  project(point: THREE.Vector3): { x: number; y: number } | null
  /** ハンドルのドラッグで位置が動いた。 */
  onDrag(position: number): void
  /** 「何が指せるか」が変わった(viewport のホバーの世代番号を進める)。 */
  pickChanged(): void
}

/**
 * レイと断面の関係。`kept` / `cut` はレイ全体が残る側 / 切られた側にある。
 * 平面をよぎるなら、よぎるレイ距離 `t` と、レイの始点が残る側か。
 */
export type SectionCut = 'kept' | 'cut' | { originKept: boolean; t: number }

export interface SectionView {
  /** シーンへ足すオブジェクト(`root` にも `gridRoot` にも入れない)。 */
  readonly object: THREE.Group
  /** ボディ側の材質に渡す共有配列。ON のあいだだけ平面が1枚入る。 */
  readonly clipPlanes: THREE.Plane[]
  setState(state: SectionState): void
  info(): SectionInfo
  /** 枠・切り口・ハンドルの置き場を決める内容の外接箱。 */
  setContent(box: THREE.Box3): void
  /** ボディの面ジオメトリ(切り口の判定用。ON のときだけ結合メッシュを作る)。 */
  setBody(bodyId: string, geometries: readonly THREE.BufferGeometry[]): void
  removeBody(bodyId: string): void
  setBodyVisible(bodyId: string, visible: boolean): void
  cut(ray: THREE.Ray): SectionCut
  /** その点が残る側か(OFF なら常に true)。 */
  keeps(point: THREE.Vector3): boolean
  /** OFF なら null(呼び出し側が判定そのものを省ける)。 */
  keepsFilter(): ((point: THREE.Vector3) => boolean) | null
  /** 残る側が正の符号付き距離[mm]。OFF なら null。 */
  signedDistance(): ((point: THREE.Vector3) => number) | null
  handleAt(offsetX: number, offsetY: number): boolean
  /** ハンドルの上なら掴んで true。 */
  pointerDown(offsetX: number, offsetY: number): boolean
  pointerMove(offsetX: number, offsetY: number): void
  pointerUp(): void
  dragging(): boolean
  setHover(hovered: boolean): void
  /** 毎フレーム。ハンドルの見かけの大きさを画素で保つ。 */
  update(): void
  dispose(): void
}

const Y_AXIS = new THREE.Vector3(0, 1, 0)
/** 視線と法線のなす角がこれより浅いとドラッグ量が発散するので、その間は動かさない(sin²)。 */
const DRAG_PARALLEL_LIMIT = 0.01
const STENCIL_BITS = 8

function planeSpec(plane: BasePlaneName): (typeof BASE_PLANES)[number] {
  const spec = BASE_PLANES.find((candidate) => candidate.name === plane)
  if (!spec) throw new Error(`未知の基準平面: ${plane}`)
  return spec
}

/** 面ごとのジオメトリを位置と添字だけの1本へ(描く回数をボディあたり1回にする)。 */
function mergePositions(geometries: readonly THREE.BufferGeometry[]): THREE.BufferGeometry {
  let vertexCount = 0
  let indexCount = 0
  for (const geometry of geometries) {
    const count = geometry.getAttribute('position').count
    vertexCount += count
    indexCount += geometry.getIndex()?.count ?? count
  }
  const positions = new Float32Array(vertexCount * 3)
  const indices = new Uint32Array(indexCount)
  let vertexOffset = 0
  let indexOffset = 0
  for (const geometry of geometries) {
    const position = geometry.getAttribute('position')
    for (let at = 0; at < position.count; at += 1) {
      positions[(vertexOffset + at) * 3] = position.getX(at)
      positions[(vertexOffset + at) * 3 + 1] = position.getY(at)
      positions[(vertexOffset + at) * 3 + 2] = position.getZ(at)
    }
    const index = geometry.getIndex()
    const count = index?.count ?? position.count
    for (let at = 0; at < count; at += 1) {
      indices[indexOffset + at] = vertexOffset + (index ? index.getX(at) : at)
    }
    vertexOffset += position.count
    indexOffset += count
  }
  const merged = new THREE.BufferGeometry()
  merged.setAttribute('position', new THREE.BufferAttribute(positions, 3))
  merged.setIndex(new THREE.BufferAttribute(indices, 1))
  merged.computeBoundingSphere()
  return merged
}

export function createSectionView(deps: SectionViewDeps): SectionView {
  const group = new THREE.Group()
  group.visible = false

  let state: SectionState = { on: false, plane: 'XY', position: 0, inverted: false }
  let axis = 2
  let keepSign: 1 | -1 = -1
  const axisUnit = new THREE.Vector3(0, 0, 1)
  /** ハンドルの向き(切られる側)。 */
  const arrowDir = new THREE.Vector3(0, 0, 1)
  /** ハンドルの根元(内容の外接箱の中心を平面へ落とした点)。 */
  const base = new THREE.Vector3()
  const content = new THREE.Box3()

  const plane = new THREE.Plane()
  const clipPlanes: THREE.Plane[] = []

  // ------------------------------------------------------------------ 切り口の判定メッシュ
  const stencilRoot = new THREE.Group()
  group.add(stencilRoot)
  const bodyGeometries = new Map<string, readonly THREE.BufferGeometry[]>()
  const stencilMeshes = new Map<string, { mesh: THREE.Mesh; bit: number }>()
  const hiddenBodies = new Set<string>()
  const stencilMaterials: THREE.MeshBasicMaterial[] = []

  function stencilMaterial(bit: number): THREE.MeshBasicMaterial {
    const cached = stencilMaterials[bit]
    if (cached) return cached
    const material = new THREE.MeshBasicMaterial({
      side: THREE.DoubleSide,
      colorWrite: false,
      depthWrite: false,
      depthTest: false,
      stencilWrite: true,
      stencilFunc: THREE.AlwaysStencilFunc,
      stencilWriteMask: 1 << bit,
      stencilFail: THREE.InvertStencilOp,
      stencilZFail: THREE.InvertStencilOp,
      stencilZPass: THREE.InvertStencilOp,
      clippingPlanes: clipPlanes,
    })
    stencilMaterials[bit] = material
    return material
  }

  /** いちばん使われていないビット。同じビットの2ボディが重なると、断面が打ち消し合って塗られない。 */
  function freeBit(): number {
    const uses = new Array<number>(STENCIL_BITS).fill(0)
    for (const entry of stencilMeshes.values()) uses[entry.bit] += 1
    return uses.indexOf(Math.min(...uses))
  }

  function disposeStencil(bodyId: string): void {
    const entry = stencilMeshes.get(bodyId)
    if (!entry) return
    stencilRoot.remove(entry.mesh)
    entry.mesh.geometry.dispose()
    stencilMeshes.delete(bodyId)
  }

  function buildStencil(bodyId: string): void {
    disposeStencil(bodyId)
    const geometries = bodyGeometries.get(bodyId)
    if (!geometries || geometries.length === 0) return
    const bit = freeBit()
    const mesh = new THREE.Mesh(mergePositions(geometries), stencilMaterial(bit))
    mesh.renderOrder = LAYER.sectionStencil
    mesh.raycast = () => {}
    mesh.visible = !hiddenBodies.has(bodyId)
    stencilRoot.add(mesh)
    stencilMeshes.set(bodyId, { mesh, bit })
  }

  // ------------------------------------------------------------------ 切り口の板・枠
  const cornerPositions = new Float32Array(12)
  const capGeometry = new THREE.BufferGeometry()
  capGeometry.setAttribute('position', new THREE.BufferAttribute(cornerPositions, 3))
  capGeometry.setAttribute('normal', new THREE.BufferAttribute(new Float32Array(12), 3))
  capGeometry.setIndex([0, 1, 2, 0, 2, 3])
  const capMaterial = new THREE.MeshLambertMaterial({
    color: SECTION_CAP_COLOR,
    side: THREE.DoubleSide,
    stencilWrite: true,
    stencilRef: 0,
    stencilFunc: THREE.NotEqualStencilFunc,
  })
  const cap = new THREE.Mesh(capGeometry, capMaterial)
  cap.renderOrder = LAYER.sectionCap
  cap.raycast = () => {}
  group.add(cap)

  const frameGeometry = new THREE.BufferGeometry()
  frameGeometry.setAttribute('position', new THREE.BufferAttribute(cornerPositions, 3))
  const frameMaterial = withDepthBias(
    new THREE.LineBasicMaterial({ color: SECTION_FRAME_COLOR, transparent: true, depthWrite: false }),
    DEPTH_BIAS_LAYER.sectionFrame,
  )
  const frame = new THREE.LineLoop(frameGeometry, frameMaterial)
  frame.renderOrder = LAYER.sectionFrame
  frame.raycast = () => {}
  group.add(frame)

  // ------------------------------------------------------------------ ハンドル(矢印)
  const handle = new THREE.Group()
  handle.renderOrder = LAYER.sectionHandle
  const handleMaterial = new THREE.MeshBasicMaterial({
    color: SECTION_HANDLE_COLOR,
    // 指せるもの(A群)と同じく深度を見ない。透明の列へ入れるのは後から描く装飾に上書きさせないため。
    depthTest: false,
    depthWrite: false,
    transparent: true,
  })
  const shaftGeometry = new THREE.CylinderGeometry(
    SECTION_HANDLE.shaftRadius,
    SECTION_HANDLE.shaftRadius,
    SECTION_HANDLE.shaftLength,
    12,
  ).translate(0, SECTION_HANDLE.shaftLength / 2, 0)
  const coneGeometry = new THREE.ConeGeometry(SECTION_HANDLE.coneRadius, SECTION_HANDLE.coneLength, 16).translate(
    0,
    SECTION_HANDLE.shaftLength + SECTION_HANDLE.coneLength / 2,
    0,
  )
  for (const geometry of [shaftGeometry, coneGeometry]) {
    const mesh = new THREE.Mesh(geometry, handleMaterial)
    mesh.raycast = () => {}
    handle.add(mesh)
  }
  group.add(handle)

  let hovered = false
  let drag: { startS: number | null; startPosition: number; base: THREE.Vector3 } | null = null

  function applyHandleColor(): void {
    handleMaterial.color.setHex(
      hovered || drag !== null ? lighten(SECTION_HANDLE_COLOR, HOVER_LIGHTEN) : SECTION_HANDLE_COLOR,
    )
  }

  // ------------------------------------------------------------------ 状態の反映
  function signed(point: THREE.Vector3): number {
    return keepSign * (point.getComponent(axis) - state.position)
  }

  function keeps(point: THREE.Vector3): boolean {
    return !state.on || signed(point) >= 0
  }

  function hasContent(): boolean {
    return state.on && !content.isEmpty()
  }

  function rebuildShape(): void {
    const visible = hasContent()
    cap.visible = visible
    frame.visible = visible
    handle.visible = visible
    if (!visible) return
    const spec = planeSpec(state.plane)
    const uAxis = spec.u.indexOf(1)
    const vAxis = spec.v.indexOf(1)
    const uMin = content.min.getComponent(uAxis)
    const uMax = content.max.getComponent(uAxis)
    const vMin = content.min.getComponent(vAxis)
    const vMax = content.max.getComponent(vAxis)
    const margin = Math.max(SECTION_FRAME_MARGIN_RATIO * Math.max(uMax - uMin, vMax - vMin), SECTION_FRAME_MARGIN_MIN_MM)
    const corners = [
      [uMin - margin, vMin - margin],
      [uMax + margin, vMin - margin],
      [uMax + margin, vMax + margin],
      [uMin - margin, vMax + margin],
    ]
    const normals = capGeometry.getAttribute('normal') as THREE.BufferAttribute
    const point = new THREE.Vector3()
    corners.forEach(([u, v], at) => {
      point.setComponent(uAxis, u)
      point.setComponent(vAxis, v)
      point.setComponent(axis, state.position)
      cornerPositions.set([point.x, point.y, point.z], at * 3)
      normals.setXYZ(at, axisUnit.x, axisUnit.y, axisUnit.z)
    })
    capGeometry.getAttribute('position').needsUpdate = true
    normals.needsUpdate = true
    capGeometry.computeBoundingSphere()
    frameGeometry.getAttribute('position').needsUpdate = true
    frameGeometry.computeBoundingSphere()

    content.getCenter(base)
    base.setComponent(axis, state.position)
    handle.position.copy(base)
    handle.quaternion.setFromUnitVectors(Y_AXIS, arrowDir)
  }

  function refresh(): void {
    const spec = planeSpec(state.plane)
    axis = spec.normal.indexOf(1)
    axisUnit.set(spec.normal[0], spec.normal[1], spec.normal[2])
    // 反転なしで消すのはホームビューのカメラがいる側
    const homeSide: 1 | -1 = HOME_VIEW_OFFSET.getComponent(axis) >= 0 ? 1 : -1
    keepSign = state.inverted ? homeSide : (-homeSide as 1 | -1)
    plane.normal.copy(axisUnit).multiplyScalar(keepSign)
    plane.constant = -keepSign * state.position
    arrowDir.copy(axisUnit).multiplyScalar(-keepSign)
    group.visible = state.on
    rebuildShape()
    deps.pickChanged()
  }

  // ------------------------------------------------------------------ ハンドルの当たりとドラッグ
  const scratchA = new THREE.Vector3()
  const scratchB = new THREE.Vector3()

  /** `at` での1画素ぶんの世界長さ(カメラの右方向に1mm動かして測る。透視でも正投影でも同じ式)。 */
  function worldPerPixelAt(at: THREE.Vector3): number | null {
    const right = scratchA.setFromMatrixColumn(deps.camera().matrixWorld, 0).normalize()
    const a = deps.project(at)
    const b = deps.project(scratchB.copy(at).add(right))
    if (a === null || b === null) return null
    const pixels = Math.hypot(b.x - a.x, b.y - a.y)
    return pixels > 1e-9 ? 1 / pixels : null
  }

  function handleSegment(): { a: { x: number; y: number }; b: { x: number; y: number }; scale: number } | null {
    if (!hasContent()) return null
    const scale = worldPerPixelAt(base)
    if (scale === null) return null
    const a = deps.project(base)
    const b = deps.project(
      scratchB.copy(base).addScaledVector(arrowDir, (SECTION_HANDLE.shaftLength + SECTION_HANDLE.coneLength) * scale),
    )
    if (a === null || b === null) return null
    return { a, b, scale }
  }

  function handleAt(offsetX: number, offsetY: number): boolean {
    const segment = handleSegment()
    if (segment === null) return false
    const { a, b } = segment
    return distSqPointSegment(offsetX, offsetY, a.x, a.y, b.x, b.y) <= SECTION_HANDLE.pickRadius ** 2
  }

  /** レイに最も近い、`origin` を通る法線軸上の点の座標(`origin` からの符号付き距離)。 */
  function axisParameter(ray: THREE.Ray, origin: THREE.Vector3): number | null {
    const w = scratchA.copy(ray.origin).sub(origin)
    const b = ray.direction.dot(axisUnit)
    const denominator = 1 - b * b
    if (denominator < DRAG_PARALLEL_LIMIT) return null
    return (axisUnit.dot(w) - b * ray.direction.dot(w)) / denominator
  }

  // ------------------------------------------------------------------ 公開
  return {
    object: group,
    clipPlanes,
    setState(next) {
      if (
        next.on === state.on &&
        next.plane === state.plane &&
        next.position === state.position &&
        next.inverted === state.inverted
      ) {
        return
      }
      const wasOn = state.on
      state = { ...next }
      if (state.on !== wasOn) {
        if (state.on) {
          clipPlanes.push(plane)
          for (const bodyId of bodyGeometries.keys()) buildStencil(bodyId)
        } else {
          clipPlanes.length = 0
          for (const bodyId of [...stencilMeshes.keys()]) disposeStencil(bodyId)
          drag = null
          applyHandleColor()
        }
      }
      refresh()
    },
    info() {
      const segment = handleSegment()
      let handleInfo: SectionInfo['handle'] = null
      if (segment !== null) {
        const grab = deps.project(
          scratchB.copy(base).addScaledVector(arrowDir, SECTION_HANDLE.shaftLength * 0.6 * segment.scale),
        )
        const tip = deps.project(scratchA.copy(base).addScaledVector(axisUnit, SECTION_HANDLE.shaftLength * segment.scale))
        if (grab !== null && tip !== null) {
          const dx = tip.x - segment.a.x
          const dy = tip.y - segment.a.y
          const length = Math.hypot(dx, dy)
          if (length > 1) handleInfo = { x: grab.x, y: grab.y, dir: { x: dx / length, y: dy / length } }
        }
      }
      return { ...state, keepSign, handle: handleInfo, stencilBodies: stencilMeshes.size }
    },
    setContent(box) {
      if (box.equals(content)) return
      content.copy(box)
      rebuildShape()
    },
    setBody(bodyId, geometries) {
      bodyGeometries.set(bodyId, geometries)
      if (state.on) buildStencil(bodyId)
    },
    removeBody(bodyId) {
      bodyGeometries.delete(bodyId)
      hiddenBodies.delete(bodyId)
      disposeStencil(bodyId)
    },
    setBodyVisible(bodyId, visible) {
      if (visible) hiddenBodies.delete(bodyId)
      else hiddenBodies.add(bodyId)
      const entry = stencilMeshes.get(bodyId)
      if (entry) entry.mesh.visible = visible
    },
    cut(ray) {
      if (!state.on) return 'kept'
      const originDistance = signed(ray.origin)
      const approach = keepSign * ray.direction.getComponent(axis)
      const t = Math.abs(approach) < 1e-12 ? -1 : -originDistance / approach
      if (t <= 0) return originDistance >= 0 ? 'kept' : 'cut'
      return { originKept: originDistance >= 0, t }
    },
    keeps,
    keepsFilter() {
      return state.on ? keeps : null
    },
    signedDistance() {
      return state.on ? signed : null
    },
    handleAt,
    pointerDown(offsetX, offsetY) {
      if (!handleAt(offsetX, offsetY)) return false
      const origin = base.clone()
      drag = { startS: axisParameter(deps.rayAt(offsetX, offsetY), origin), startPosition: state.position, base: origin }
      applyHandleColor()
      return true
    },
    pointerMove(offsetX, offsetY) {
      if (drag === null) return
      const s = axisParameter(deps.rayAt(offsetX, offsetY), drag.base)
      if (s === null) return
      if (drag.startS === null) {
        // 真正面から掴んだ: 動かせる角度になった所を起点にする(跳ばない)
        drag.startS = s
        drag.startPosition = state.position
        return
      }
      const position = drag.startPosition + (s - drag.startS)
      if (!Number.isFinite(position) || position === state.position) return
      state = { ...state, position }
      refresh()
      deps.onDrag(position)
    },
    pointerUp() {
      drag = null
      applyHandleColor()
    },
    dragging() {
      return drag !== null
    },
    setHover(next) {
      if (hovered === next) return
      hovered = next
      applyHandleColor()
    },
    update() {
      if (!handle.visible) return
      const scale = worldPerPixelAt(base)
      if (scale !== null) handle.scale.setScalar(scale)
    },
    dispose() {
      for (const bodyId of [...stencilMeshes.keys()]) disposeStencil(bodyId)
      for (const material of stencilMaterials) material?.dispose()
      capGeometry.dispose()
      capMaterial.dispose()
      frameGeometry.dispose()
      frameMaterial.dispose()
      shaftGeometry.dispose()
      coneGeometry.dispose()
      handleMaterial.dispose()
    },
  }
}
