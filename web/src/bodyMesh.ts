/**
 * ボディ1つぶんの描画物: 全面を1つの `BufferGeometry` にまとめた Mesh と、面の境界線を1つにまとめた LineSegments。
 *
 * 面ごとに Mesh と LineSegments を持つと、27 部品 2,835 面で 5,670 オブジェクトになり、three.js が毎フレーム
 * オブジェクト単位に行列・視錐台・描画呼び出しを処理して、静止中でも主スレッドが 14〜16 ms/フレーム埋まっていた
 * (issue #171)。面は頂点の範囲(`FaceSpan`)で区別し、色は頂点色で塗る(材質は1つ)。three.js の材質配列 + groups は
 * group ごとに描画項目になるので使わない。
 *
 * 面ごとの頂点は面の間で共有しない(サーバの `kernel.tessellate` が面ごとに頂点を持つ)ので、面の範囲は連続し、
 * 法線をまとめて求めても面ごとに求めたのと同じになる。
 */
import * as THREE from 'three'

import type { FaceGeometry, GeometryPayload } from './viewportTypes'

/** 1面ぶんの範囲。`index` はコアの面の添字(1始まり)。 */
export interface FaceSpan {
  index: number
  vertexStart: number
  vertexCount: number
  triangleStart: number
  triangleCount: number
  /** 面の外接球(ピックの絞り込み。ローカル座標)。 */
  sphere: THREE.Sphere
}

export interface BodyMesh {
  bodyId: string
  mesh: THREE.Mesh
  /** 面の境界線(メッシュボディは持たない)。 */
  lines: THREE.LineSegments | null
  /** メッシュボディ(三角形がそのまま形。平らな陰影で、境界線を出さない)。 */
  flat: boolean
  faces: FaceSpan[]
  faceOf(faceIndex: number): FaceSpan | undefined
  /** 三角形の通し番号(レイキャストの `faceIndex`)から面の添字へ。 */
  faceIndexOfTriangle(triangle: number): number | undefined
  /** その面のいまの色(`setFaceColor` で塗ったもの)。 */
  faceColor(faceIndex: number): number | undefined
  /** その面を塗る。同じ色なら何もしない。塗ったら true(呼び手は描き直しを頼む)。 */
  setFaceColor(faceIndex: number, hex: number): boolean
  /** その面の頂点位置(まとめた配列の一部。書き換えない)。 */
  facePositions(faceIndex: number): Float32Array | null
  /** その面の頂点法線(同上)。 */
  faceNormals(faceIndex: number): Float32Array | null
  dispose(): void
}

/** 面の境界線を `EdgesGeometry` と同じ規則(閾値角 20 度)で、面ごとに求める(面をまたいだ辺はどれも境界)。 */
const EDGE_THRESHOLD_DEGREES = 20

/**
 * 面ごとの外接球と境界線を、面の頂点の配列(`positions`)ごとに覚える。カメラで測り直した面だけが新しい配列で届き、
 * 残りの面は前回と同じ配列のまま使い回される(`wire.ts`)ので、ボディを作り直しても変わらない面の計算を繰り返さない。
 */
const perFace = new WeakMap<object, { sphere: THREE.Sphere; edges: Float32Array | null }>()

function float32(values: Float32Array | number[]): Float32Array {
  return values instanceof Float32Array ? values : new Float32Array(values)
}

/** 面の外接球(ローカル座標。ピックの絞り込み)。 */
function faceSphere(face: FaceGeometry): THREE.Sphere {
  const known = perFace.get(face.positions)
  if (known !== undefined) return known.sphere
  const positions = float32(face.positions)
  const center = new THREE.Box3().setFromArray(positions).getCenter(new THREE.Vector3())
  let farthest = 0
  for (let at = 0; at < positions.length; at += 3) {
    const dx = positions[at] - center.x
    const dy = positions[at + 1] - center.y
    const dz = positions[at + 2] - center.z
    farthest = Math.max(farthest, dx * dx + dy * dy + dz * dz)
  }
  const sphere = new THREE.Sphere(center, Math.sqrt(farthest))
  perFace.set(face.positions, { sphere, edges: null })
  return sphere
}

/** 面の境界線の線分(`faceSphere` の後に呼ぶ。メッシュボディは求めない)。 */
function faceEdges(face: FaceGeometry): Float32Array {
  const known = perFace.get(face.positions) as { sphere: THREE.Sphere; edges: Float32Array | null }
  if (known.edges !== null) return known.edges
  const part = new THREE.BufferGeometry()
  part.setAttribute('position', new THREE.BufferAttribute(float32(face.positions), 3))
  part.setIndex(new THREE.BufferAttribute(face.indices instanceof Uint32Array ? face.indices : Uint32Array.from(face.indices), 1))
  const edgesGeometry = new THREE.EdgesGeometry(part, EDGE_THRESHOLD_DEGREES)
  known.edges = edgesGeometry.getAttribute('position').array as Float32Array
  edgesGeometry.dispose()
  part.dispose()
  return known.edges
}

export function buildBodyMesh(
  bodyId: string,
  payload: GeometryPayload,
  materials: { smooth: THREE.Material; flat: THREE.Material },
  edgeMaterial: THREE.Material,
  baseColor: number,
): BodyMesh {
  const faces: FaceGeometry[] = payload.faces.filter((face) => face.indices.length > 0) // 三角形が取れなかった面(サーバが警告を出す)
  // メッシュボディは面1つ・`kind: "mesh"` で届く(`duocad/meshbody.py`)。
  const flat = faces.some((face) => face.kind === 'mesh')
  let vertexTotal = 0
  let indexTotal = 0
  for (const face of faces) {
    vertexTotal += face.positions.length / 3
    indexTotal += face.indices.length
  }
  const positions = new Float32Array(vertexTotal * 3)
  const indices = new Uint32Array(indexTotal)
  const colors = new Float32Array(vertexTotal * 3)
  const triangleFaces = new Int32Array(indexTotal / 3)
  const spans: FaceSpan[] = []
  const byIndex = new Map<number, FaceSpan>()
  const painted = new Map<number, number>()
  let vertexAt = 0
  let indexAt = 0
  for (const face of faces) {
    const vertexCount = face.positions.length / 3
    positions.set(face.positions, vertexAt * 3)
    for (let k = 0; k < face.indices.length; k += 1) indices[indexAt + k] = face.indices[k] + vertexAt
    const triangleStart = indexAt / 3
    const triangleCount = face.indices.length / 3
    triangleFaces.fill(face.index, triangleStart, triangleStart + triangleCount)
    const sphere = faceSphere(face)
    const span: FaceSpan = { index: face.index, vertexStart: vertexAt, vertexCount, triangleStart, triangleCount, sphere }
    spans.push(span)
    byIndex.set(face.index, span)
    vertexAt += vertexCount
    indexAt += face.indices.length
  }

  const geometry = new THREE.BufferGeometry()
  geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3))
  geometry.setIndex(new THREE.BufferAttribute(indices, 1))
  const colorAttribute = new THREE.BufferAttribute(colors, 3)
  colorAttribute.setUsage(THREE.DynamicDrawUsage)
  geometry.setAttribute('color', colorAttribute)
  geometry.computeVertexNormals()
  geometry.computeBoundingSphere()
  geometry.computeBoundingBox()

  const mesh = new THREE.Mesh(geometry, flat ? materials.flat : materials.smooth)
  mesh.userData.bodyId = bodyId
  mesh.userData.triangleFaces = triangleFaces
  mesh.userData.flatShaded = flat // 一覧の絵(`thumbnail.ts`)が材質を選ぶ
  mesh.raycast = raycastByFace(spans)

  const color = new THREE.Color()
  function setFaceColor(faceIndex: number, hex: number): boolean {
    const span = byIndex.get(faceIndex)
    if (span === undefined || painted.get(faceIndex) === hex) return false
    painted.set(faceIndex, hex)
    // 頂点色は線形空間の値として読まれる。`Color.setHex` は sRGB の16進から線形へ直す(材質の `color` と同じ見え方)。
    color.setHex(hex)
    for (let at = span.vertexStart; at < span.vertexStart + span.vertexCount; at += 1) {
      colors[at * 3] = color.r
      colors[at * 3 + 1] = color.g
      colors[at * 3 + 2] = color.b
    }
    colorAttribute.addUpdateRange(span.vertexStart * 3, span.vertexCount * 3)
    colorAttribute.needsUpdate = true
    return true
  }
  for (const span of spans) setFaceColor(span.index, baseColor)

  let lines: THREE.LineSegments | null = null
  if (!flat) {
    // 境界線(隣り合う同色の面を狙い分けるため)。面ごとに `EdgesGeometry` を取って繋ぐ: ボディ全体に掛けると、
    // 接線で続く面の境目(角度 0)が「同じ面の内側」と同じ扱いで消える。
    const segments: Float32Array[] = []
    let segmentTotal = 0
    for (const face of faces) {
      const array = faceEdges(face)
      segments.push(array)
      segmentTotal += array.length
    }
    const linePositions = new Float32Array(segmentTotal)
    let at = 0
    for (const array of segments) {
      linePositions.set(array, at)
      at += array.length
    }
    const lineGeometry = new THREE.BufferGeometry()
    lineGeometry.setAttribute('position', new THREE.BufferAttribute(linePositions, 3))
    lineGeometry.computeBoundingSphere()
    lines = new THREE.LineSegments(lineGeometry, edgeMaterial)
    lines.raycast = () => {} // 指すのは面と、サーバの折れ線(辺)。境界線は飾り
  }

  return {
    bodyId,
    mesh,
    lines,
    flat,
    faces: spans,
    faceOf: (faceIndex) => byIndex.get(faceIndex),
    faceIndexOfTriangle: (triangle) => (triangle >= 0 && triangle < triangleFaces.length ? triangleFaces[triangle] : undefined),
    faceColor: (faceIndex) => painted.get(faceIndex),
    setFaceColor,
    facePositions(faceIndex) {
      const span = byIndex.get(faceIndex)
      return span === undefined ? null : positions.subarray(span.vertexStart * 3, (span.vertexStart + span.vertexCount) * 3)
    },
    faceNormals(faceIndex) {
      const span = byIndex.get(faceIndex)
      const normal = geometry.getAttribute('normal').array as Float32Array
      return span === undefined ? null : normal.subarray(span.vertexStart * 3, (span.vertexStart + span.vertexCount) * 3)
    },
    dispose() {
      geometry.dispose()
      lines?.geometry.dispose()
    },
  }
}

/**
 * 面ごとの外接球で絞ってから三角形を当てるレイキャスト(`Mesh.raycast` の差し替え)。面ごとに Mesh を持っていたときは
 * three.js が面ごとの外接球で絞っていたので、まとめた後も同じ絞り方を残す(大きなボディで全三角形を当てない)。
 * 両面(`DoubleSide`)として当てる。戻す `faceIndex` は three.js と同じく三角形の通し番号。
 */
function raycastByFace(spans: FaceSpan[]): THREE.Mesh['raycast'] {
  const inverse = new THREE.Matrix4()
  const local = new THREE.Ray()
  const worldSphere = new THREE.Sphere()
  const a = new THREE.Vector3()
  const b = new THREE.Vector3()
  const c = new THREE.Vector3()
  const hit = new THREE.Vector3()
  return function (this: THREE.Mesh, raycaster: THREE.Raycaster, intersects: THREE.Intersection[]): void {
    const geometry = this.geometry
    if (geometry.boundingSphere === null) geometry.computeBoundingSphere()
    worldSphere.copy(geometry.boundingSphere as THREE.Sphere).applyMatrix4(this.matrixWorld)
    if (!raycaster.ray.intersectsSphere(worldSphere)) return
    inverse.copy(this.matrixWorld).invert()
    local.copy(raycaster.ray).applyMatrix4(inverse)
    const position = geometry.getAttribute('position')
    const index = geometry.getIndex() as THREE.BufferAttribute
    for (const span of spans) {
      if (!local.intersectsSphere(span.sphere)) continue
      for (let triangle = span.triangleStart; triangle < span.triangleStart + span.triangleCount; triangle += 1) {
        a.fromBufferAttribute(position, index.getX(triangle * 3))
        b.fromBufferAttribute(position, index.getX(triangle * 3 + 1))
        c.fromBufferAttribute(position, index.getX(triangle * 3 + 2))
        if (local.intersectTriangle(a, b, c, false, hit) === null) continue
        const point = hit.clone().applyMatrix4(this.matrixWorld)
        const distance = raycaster.ray.origin.distanceTo(point)
        if (distance < raycaster.near || distance > raycaster.far) continue
        intersects.push({ distance, point, object: this, faceIndex: triangle, face: null })
      }
    }
  }
}
