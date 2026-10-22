/**
 * 干渉チェックの装飾(重なりの立体・接触面・接触の辺と点・最短距離の線と札)。ピックの対象にしない。`viewport.showInterference` が唯一の書き手。
 * `planeGrid.ts` と同じ形: 必要なものを引数で受け取り、ハンドルを返す(`viewport.ts` は呼ぶだけ)。
 *
 * 深度検査をしない: 重なりの立体は2つのボディの内側にあるので、深度を見ると不透明な面に隠れて見えない。測定の層(`measureAnnotation.ts`)と
 * 同じく描く順序(`LAYER.measure`)で手前に出す。
 */
import * as THREE from 'three'

import type { InterferenceVisual, RenderMesh } from './interferenceTypes'
import { makeTextSprite } from './textSprite'
import { INTERFERENCE_CONTACT_COLOR, INTERFERENCE_OVERLAP_COLOR, LAYER, MEASURE_COLOR, MEASURE_TEXT_COLOR } from './viewportConstants'

export interface InterferenceLayerDeps {
  /** 内容の外接直方体の対角[mm]。札の大きさに使う。 */
  contentDiagonal(): number
}

export interface InterferenceLayer {
  /** `scene` へ足すのは呼び出し側。 */
  object: THREE.Group
  show(visual: InterferenceVisual | null): void
  /** 実機検査が数える: 層に載っている赤い立体の数。 */
  overlapSolidCount(): number
  dispose(): void
}

export function createInterferenceLayer(deps: InterferenceLayerDeps): InterferenceLayer {
  const root = new THREE.Group()
  root.name = 'interference'
  const solidMaterial = new THREE.MeshBasicMaterial({
    color: INTERFERENCE_OVERLAP_COLOR,
    transparent: true,
    opacity: 0.5,
    side: THREE.DoubleSide,
    depthTest: false,
    depthWrite: false,
  })
  const faceMaterial = new THREE.MeshBasicMaterial({
    color: INTERFERENCE_CONTACT_COLOR,
    transparent: true,
    opacity: 0.45,
    side: THREE.DoubleSide,
    depthTest: false,
    depthWrite: false,
  })
  const contactLineMaterial = new THREE.LineBasicMaterial({
    color: INTERFERENCE_CONTACT_COLOR,
    depthTest: false,
    depthWrite: false,
    transparent: true,
  })
  const pointMaterial = new THREE.PointsMaterial({
    color: INTERFERENCE_CONTACT_COLOR,
    size: 9,
    sizeAttenuation: false,
    depthTest: false,
    depthWrite: false,
  })
  const distanceLineMaterial = new THREE.LineBasicMaterial({
    color: MEASURE_COLOR,
    depthTest: false,
    depthWrite: false,
    transparent: true,
  })
  const distanceEndMaterial = new THREE.PointsMaterial({
    color: MEASURE_COLOR,
    size: 8,
    sizeAttenuation: false,
    depthTest: false,
    depthWrite: false,
  })
  let disposables: Array<{ dispose(): void }> = []
  let solidCount = 0

  function clear(): void {
    for (const child of [...root.children]) root.remove(child)
    for (const item of disposables) item.dispose()
    disposables = []
    solidCount = 0
  }

  function addMesh(mesh: RenderMesh, material: THREE.Material): THREE.Mesh | null {
    if (mesh.indices.length === 0) return null
    const geometry = new THREE.BufferGeometry()
    geometry.setAttribute('position', new THREE.Float32BufferAttribute(mesh.positions, 3))
    geometry.setIndex(mesh.indices)
    const object = new THREE.Mesh(geometry, material)
    object.renderOrder = LAYER.measure
    root.add(object)
    disposables.push(geometry)
    return object
  }

  function addPoints(points: number[][], material: THREE.PointsMaterial, line: THREE.LineBasicMaterial | null): void {
    if (points.length === 0) return
    const vectors = points.map((point) => new THREE.Vector3(point[0], point[1], point[2]))
    const geometry = new THREE.BufferGeometry().setFromPoints(vectors)
    const cloud = new THREE.Points(geometry, material)
    cloud.renderOrder = LAYER.measure
    root.add(cloud)
    disposables.push(geometry)
    if (line !== null) {
      const lineGeometry = new THREE.BufferGeometry().setFromPoints(vectors)
      const polyline = new THREE.Line(lineGeometry, line)
      polyline.renderOrder = LAYER.measure
      root.add(polyline)
      disposables.push(lineGeometry)
    }
  }

  function show(visual: InterferenceVisual | null): void {
    clear()
    if (visual === null) return
    for (const solid of visual.solids) {
      if (addMesh(solid, solidMaterial) !== null) solidCount += 1
    }
    for (const face of visual.faces) addMesh(face, faceMaterial)
    for (const edge of visual.edges) {
      const vectors = edge.map((point) => new THREE.Vector3(point[0], point[1], point[2]))
      if (vectors.length < 2) continue
      const geometry = new THREE.BufferGeometry().setFromPoints(vectors)
      const line = new THREE.Line(geometry, contactLineMaterial)
      line.renderOrder = LAYER.measure
      root.add(line)
      disposables.push(geometry)
    }
    addPoints(visual.points, pointMaterial, null)
    if (visual.distance !== null) {
      const { from, to, label } = visual.distance
      addPoints([from, to], distanceEndMaterial, distanceLineMaterial)
      const height = Math.max(deps.contentDiagonal(), 1e-6) * 0.04
      const { sprite, disposables: spriteDisposables } = makeTextSprite(label, height, MEASURE_TEXT_COLOR)
      sprite.position.set((from[0] + to[0]) / 2, (from[1] + to[1]) / 2, (from[2] + to[2]) / 2)
      sprite.renderOrder = LAYER.measure
      root.add(sprite)
      disposables.push(...spriteDisposables)
    }
  }

  return {
    object: root,
    show,
    overlapSolidCount: () => solidCount,
    dispose() {
      clear()
      for (const material of [solidMaterial, faceMaterial, contactLineMaterial, pointMaterial, distanceLineMaterial, distanceEndMaterial]) {
        material.dispose()
      }
    },
  }
}
