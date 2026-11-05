/**
 * 常設の原点ギズモ(issue #8)。原点の点・X/Y/Z軸・XY/XZ/YZ平面の7つを1つの
 * `THREE.Group` にまとめ、`setVisible` で一括に出し入れする。
 *
 * 7つはボディの頂点・辺・面と同じく指せる(コアの `DatumRef`。`duocad/datum.py`)。当たり判定は three の Raycaster を
 * 使わず、ここが持つ寸法から画面の距離(点・軸)とレイと平面の交差(平面)で解く(`viewport.ts` の `resolveHit` が
 * ソリッドの候補と並べる)。隠している間は当たらない。
 *
 * `planeGrid.ts` の無限フェードグリッドとは別物(あちらはスケッチを貼る先を指す間だけの装飾、こちらは常時表示の
 * 固定サイズの目印)。両方が同時に見えるので、深度バイアスは別枠 `DEPTH_BIAS_LAYER.originGizmo` を使い、
 * 床のグリッドと共平面でも手前へ出す。
 *
 * 軸線・平面とも正の方向だけを描く(当たりも描いた範囲だけ)。
 */
import * as THREE from 'three'

import { DATUM_AXIS_DIRECTIONS } from './commands/command'
import type { DatumName } from './session'
import { closestOnPolyline, screenHitOfPoint, type ScreenHit, type ScreenProjector } from './polylinePick'
import {
  AXIS_X_COLOR,
  AXIS_Y_COLOR,
  AXIS_Z_COLOR,
  BASE_PLANES,
  BASE_PLANE_HOVER_OPACITY,
  DEPTH_BIAS_LAYER,
  HIGHLIGHT_COLOR,
  HOVER_LIGHTEN,
  LAYER,
  ORIGIN_GIZMO_AXIS_GAP_MM,
  ORIGIN_GIZMO_AXIS_LENGTH_MM,
  ORIGIN_GIZMO_NEUTRAL_COLOR,
  ORIGIN_GIZMO_PLANE_GAP_MM,
  ORIGIN_GIZMO_PLANE_OPACITY,
  ORIGIN_GIZMO_PLANE_REACH_MM,
  ORIGIN_GIZMO_POINT_RADIUS_MM,
  lighten,
  withDepthBias,
} from './viewportConstants'

/** 画面で当たった点・軸(`hit.point` は狙われている3Dの点。遮蔽の判定に使う)。 */
export interface DatumScreenHit {
  name: DatumName
  hit: ScreenHit
}

/** レイが当たった平面と、その距離・交点。 */
export interface DatumPlaneHit {
  name: DatumName
  distance: number
  point: THREE.Vector3
}

export interface OriginGizmo {
  /** シーンへ足すオブジェクト(`root` にも `gridRoot` にも入れない単独のGroup)。 */
  readonly object: THREE.Group
  /** 7項目まとめての表示/非表示。 */
  setVisible(visible: boolean): void
  visible(): boolean
  /** カーソルの下のもの(明度を上げる)。 */
  setHover(name: DatumName | null): void
  /** 選択に入っているもの(選択の橙)。 */
  setSelected(names: readonly DatumName[]): void
  /** 原点の点の当たり(半径 `limitPx` の外・隠れているときは null)。 */
  pointAt(x: number, y: number, limitPx: number, project: ScreenProjector): DatumScreenHit | null
  /** 軸の当たり(半径 `limitPx` に入るもの全部)。 */
  axesAt(x: number, y: number, limitPx: number, project: ScreenProjector): DatumScreenHit[]
  /** レイが描いた範囲で当たる平面のうち最も近い1枚。 */
  planeAlong(ray: THREE.Ray): DatumPlaneHit | null
  dispose(): void
}

const AXES: ReadonlyArray<{ name: 'X' | 'Y' | 'Z'; color: number }> = [
  { name: 'X', color: AXIS_X_COLOR },
  { name: 'Y', color: AXIS_Y_COLOR },
  { name: 'Z', color: AXIS_Z_COLOR },
]

interface Part {
  name: DatumName
  material: THREE.MeshBasicMaterial | THREE.LineBasicMaterial
  color: number
  /** 平面だけ: 素の不透明度 */
  opacity: number | null
}

export function createOriginGizmo(): OriginGizmo {
  const group = new THREE.Group()
  group.renderOrder = LAYER.originGizmo
  group.matrixAutoUpdate = false

  const disposables: Array<{ dispose(): void }> = []
  const parts: Part[] = []

  const pointMaterial = new THREE.MeshBasicMaterial({ color: ORIGIN_GIZMO_NEUTRAL_COLOR })
  disposables.push(pointMaterial)
  const pointGeometry = new THREE.SphereGeometry(ORIGIN_GIZMO_POINT_RADIUS_MM, 12, 8)
  disposables.push(pointGeometry)
  const pointMesh = new THREE.Mesh(pointGeometry, pointMaterial)
  pointMesh.raycast = () => {}
  group.add(pointMesh)
  parts.push({ name: 'origin', material: pointMaterial, color: ORIGIN_GIZMO_NEUTRAL_COLOR, opacity: null })

  const axisEnds = new Map<DatumName, [THREE.Vector3, THREE.Vector3]>()
  for (const axis of AXES) {
    const material = withDepthBias(
      new THREE.LineBasicMaterial({ color: axis.color, transparent: true, depthWrite: false }),
      DEPTH_BIAS_LAYER.originGizmo,
    )
    disposables.push(material)
    const dir = DATUM_AXIS_DIRECTIONS[axis.name]
    const ends: [THREE.Vector3, THREE.Vector3] = [
      new THREE.Vector3(...dir).multiplyScalar(ORIGIN_GIZMO_AXIS_GAP_MM),
      new THREE.Vector3(...dir).multiplyScalar(ORIGIN_GIZMO_AXIS_LENGTH_MM),
    ]
    axisEnds.set(axis.name, ends)
    const geometry = new THREE.BufferGeometry().setFromPoints(ends)
    disposables.push(geometry)
    const line = new THREE.Line(geometry, material)
    line.raycast = () => {}
    group.add(line)
    parts.push({ name: axis.name, material, color: axis.color, opacity: null })
  }

  const near = ORIGIN_GIZMO_PLANE_GAP_MM
  const far = ORIGIN_GIZMO_PLANE_REACH_MM
  for (const spec of BASE_PLANES) {
    const material = withDepthBias(
      new THREE.MeshBasicMaterial({
        color: ORIGIN_GIZMO_NEUTRAL_COLOR,
        side: THREE.DoubleSide,
        transparent: true,
        opacity: ORIGIN_GIZMO_PLANE_OPACITY,
        depthWrite: false,
      }),
      DEPTH_BIAS_LAYER.originGizmo,
    )
    disposables.push(material)

    const u = new THREE.Vector3(...spec.u)
    const v = new THREE.Vector3(...spec.v)
    // 原点側の角だけ軸線から離す(遠い角は動かさない)。
    const corners = [
      [near, near],
      [far, near],
      [far, far],
      [near, far],
    ].map(([su, sv]) => new THREE.Vector3().addScaledVector(u, su).addScaledVector(v, sv))

    const geometry = new THREE.BufferGeometry()
    geometry.setFromPoints([corners[0], corners[1], corners[2], corners[0], corners[2], corners[3]])
    disposables.push(geometry)
    const mesh = new THREE.Mesh(geometry, material)
    mesh.raycast = () => {}
    group.add(mesh)
    parts.push({ name: spec.name, material, color: ORIGIN_GIZMO_NEUTRAL_COLOR, opacity: ORIGIN_GIZMO_PLANE_OPACITY })
  }

  let hovered: DatumName | null = null
  let selected = new Set<DatumName>()

  function paint(): void {
    for (const part of parts) {
      const base = selected.has(part.name) ? HIGHLIGHT_COLOR : part.color
      const hot = hovered === part.name
      part.material.color.setHex(hot ? lighten(base, HOVER_LIGHTEN) : base)
      if (part.opacity !== null) {
        const lit = hot || selected.has(part.name)
        part.material.opacity = lit ? part.opacity + BASE_PLANE_HOVER_OPACITY : part.opacity
      }
    }
  }

  const origin = new THREE.Vector3()
  const hitPoint = new THREE.Vector3()

  return {
    object: group,
    setVisible(visible) {
      group.visible = visible
    },
    visible() {
      return group.visible
    },
    setHover(name) {
      if (name === hovered) return
      hovered = name
      paint()
    },
    setSelected(names) {
      const next = new Set(names)
      if (next.size === selected.size && [...next].every((name) => selected.has(name))) return
      selected = next
      paint()
    },
    pointAt(x, y, limitPx, project) {
      if (!group.visible) return null
      const hit = screenHitOfPoint(x, y, origin, project)
      return hit === null || hit.distSq > limitPx * limitPx ? null : { name: 'origin', hit }
    },
    axesAt(x, y, limitPx, project) {
      if (!group.visible) return []
      const out: DatumScreenHit[] = []
      for (const [name, ends] of axisEnds) {
        const hit = closestOnPolyline(x, y, { count: 2, at: (index, target) => target.copy(ends[index]) }, project)
        if (hit !== null && hit.distSq <= limitPx * limitPx) out.push({ name, hit })
      }
      return out
    },
    planeAlong(ray) {
      if (!group.visible) return null
      let best: DatumPlaneHit | null = null
      for (const spec of BASE_PLANES) {
        const plane = new THREE.Plane(new THREE.Vector3(...spec.normal), 0)
        if (ray.intersectPlane(plane, hitPoint) === null) continue
        const u = hitPoint.dot(new THREE.Vector3(...spec.u))
        const v = hitPoint.dot(new THREE.Vector3(...spec.v))
        if (u < near || u > far || v < near || v > far) continue
        const distance = ray.origin.distanceTo(hitPoint)
        if (best === null || distance < best.distance) best = { name: spec.name, distance, point: hitPoint.clone() }
      }
      return best
    },
    dispose() {
      for (const item of disposables) item.dispose()
    },
  }
}
