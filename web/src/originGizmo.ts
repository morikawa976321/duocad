/**
 * **常設の原点ギズモ**(issue #8)。原点の点・X/Y/Z軸・XY/XZ/YZ平面の7つを1つの
 * `THREE.Group` にまとめ、`setVisible` の1つのトグルで一括に出し入れする(本人指定
 * 2026-09-13「7つまとめて」)。
 *
 * **`planeGrid.ts` の無限フェードグリッドとは別物。** あちらはスケッチの対象待ち・
 * 編集中だけの一時的な当たり判定つき装飾で、こちらは常時表示・当たり判定なしの
 * 固定サイズの目印(Fusion 360の原点フォルダに相当)。両方が同時に見えることがある
 * (通常表示は元々XY床を出している)ので、深度バイアスは別枠
 * (`DEPTH_BIAS_LAYER.originGizmo`)を使い、床のグリッドと共平面でも確実に手前へ出す。
 *
 * 軸線・平面とも**正の方向だけ**を描く(本人指定)。
 */
import * as THREE from 'three'

import {
  AXIS_X_COLOR,
  AXIS_Y_COLOR,
  AXIS_Z_COLOR,
  BASE_PLANES,
  DEPTH_BIAS_LAYER,
  LAYER,
  ORIGIN_GIZMO_AXIS_GAP_MM,
  ORIGIN_GIZMO_AXIS_LENGTH_MM,
  ORIGIN_GIZMO_NEUTRAL_COLOR,
  ORIGIN_GIZMO_PLANE_GAP_MM,
  ORIGIN_GIZMO_PLANE_OPACITY,
  ORIGIN_GIZMO_PLANE_REACH_MM,
  ORIGIN_GIZMO_POINT_RADIUS_MM,
  withDepthBias,
} from './viewportConstants'

export interface OriginGizmo {
  /** シーンへ足すオブジェクト(常設。`root`にも`gridRoot`にも入れない単独のGroup)。 */
  readonly object: THREE.Group
  /** 7項目まとめての表示/非表示。 */
  setVisible(visible: boolean): void
  dispose(): void
}

const AXES: ReadonlyArray<{ dir: readonly [number, number, number]; color: number }> = [
  { dir: [1, 0, 0], color: AXIS_X_COLOR },
  { dir: [0, 1, 0], color: AXIS_Y_COLOR },
  { dir: [0, 0, 1], color: AXIS_Z_COLOR },
]

export function createOriginGizmo(): OriginGizmo {
  const group = new THREE.Group()
  group.renderOrder = LAYER.originGizmo
  group.matrixAutoUpdate = false

  const disposables: Array<{ dispose(): void }> = []

  const pointMaterial = new THREE.MeshBasicMaterial({ color: ORIGIN_GIZMO_NEUTRAL_COLOR })
  disposables.push(pointMaterial)
  const pointGeometry = new THREE.SphereGeometry(ORIGIN_GIZMO_POINT_RADIUS_MM, 12, 8)
  disposables.push(pointGeometry)
  const pointMesh = new THREE.Mesh(pointGeometry, pointMaterial)
  pointMesh.raycast = () => {}
  group.add(pointMesh)

  for (const axis of AXES) {
    const material = withDepthBias(
      new THREE.LineBasicMaterial({ color: axis.color, transparent: true, depthWrite: false }),
      DEPTH_BIAS_LAYER.originGizmo,
    )
    disposables.push(material)
    const geometry = new THREE.BufferGeometry().setFromPoints([
      new THREE.Vector3(...axis.dir).multiplyScalar(ORIGIN_GIZMO_AXIS_GAP_MM),
      new THREE.Vector3(...axis.dir).multiplyScalar(ORIGIN_GIZMO_AXIS_LENGTH_MM),
    ])
    disposables.push(geometry)
    const line = new THREE.Line(geometry, material)
    line.raycast = () => {}
    group.add(line)
  }

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
    // 原点コーナーを軸線から離すぶんだけ内側へ寄せる(u=gap..reach, v=gap..reach)。
    // 遠い角は動かさない(平面の大きさそのものは変えず、原点側だけ切り欠く)。
    const near = ORIGIN_GIZMO_PLANE_GAP_MM
    const far = ORIGIN_GIZMO_PLANE_REACH_MM
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
  }

  return {
    object: group,
    setVisible(visible) {
      group.visible = visible
    },
    dispose() {
      for (const item of disposables) item.dispose()
    },
  }
}
