/** 面の境界線(`web/src/edgeSegments.ts`)が three.js の `EdgesGeometry` と同じ線分を同じ順に返す。 */
import assert from 'node:assert/strict'
import './ts_resolve_hook.mjs'

const THREE = await import(new URL('../web/node_modules/three/build/three.module.js', import.meta.url).href)
const { edgeSegments } = await import('../web/src/edgeSegments.ts')

function expected(positions, indices, threshold) {
  const geometry = new THREE.BufferGeometry()
  geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3))
  geometry.setIndex(new THREE.BufferAttribute(indices, 1))
  return new THREE.EdgesGeometry(geometry, threshold).getAttribute('position').array
}

function same(name, positions, indices, threshold) {
  const want = expected(positions, indices, threshold)
  const got = edgeSegments(positions, indices, threshold)
  assert.equal(got.length, want.length, `${name}(閾値 ${threshold})の線分の数`)
  assert.ok(Buffer.from(got.buffer, got.byteOffset, got.byteLength).equals(Buffer.from(want.buffer, want.byteOffset, want.byteLength)), `${name}(閾値 ${threshold})の線分`)
}

function fromThree(geometry) {
  const positions = Float32Array.from(geometry.getAttribute('position').array)
  const index = geometry.getIndex()
  const indices = index ? Uint32Array.from(index.array) : Uint32Array.from({ length: positions.length / 3 }, (_, i) => i)
  return { positions, indices }
}

let seed = 12345
function random() {
  seed = (seed + 0x6d2b79f5) | 0
  let t = seed
  t = Math.imul(t ^ (t >>> 15), t | 1)
  t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
  return ((t ^ (t >>> 14)) >>> 0) / 4294967296
}

const THRESHOLDS = [0, 1, 20, 45, 90, 179]

// three の形(角で頂点が分かれた箱、継ぎ目と極で頂点が重なる球・円柱・円錐)
const shapes = {
  box: new THREE.BoxGeometry(10, 7, 3, 2, 3, 1),
  sphere: new THREE.SphereGeometry(5, 12, 8),
  cylinder: new THREE.CylinderGeometry(2, 2, 8, 16, 3),
  cone: new THREE.ConeGeometry(3, 5, 9),
  torusKnot: new THREE.TorusKnotGeometry(4, 1, 40, 6),
}
for (const [name, geometry] of Object.entries(shapes)) {
  const { positions, indices } = fromThree(geometry)
  for (const threshold of THRESHOLDS) same(name, positions, indices, threshold)
}

// 乱数の格子: 同じ座標の別の頂点・丸めの境目(x × 10⁴ が k + 0.5)・-0・裏返った三角形・3枚が共有する辺・潰れた三角形・同じ三角形の重複
for (let trial = 0; trial < 300; trial += 1) {
  const columns = 2 + Math.floor(random() * 8)
  const rows = 2 + Math.floor(random() * 8)
  const coordinates = []
  for (let r = 0; r < rows; r += 1) {
    for (let c = 0; c < columns; c += 1) {
      let x = c + (random() - 0.5) * 0.2
      let y = r + (random() - 0.5) * 0.2
      let z = (random() - 0.5) * (trial % 3 === 0 ? 4 : 0.3)
      if (random() < 0.1) x = Math.round(x * 1e4) / 1e4 + 0.5e-4
      if (random() < 0.05) z = -0
      coordinates.push([x, y, z])
    }
  }
  const vertices = []
  const vertexOf = coordinates.map((point) => {
    vertices.push(point)
    return vertices.length - 1
  })
  // 同じ座標(と、丸めの桁の内外にずれた座標)に別の番号の頂点(OCCT の継ぎ目の頂点)
  const twin = (index) => {
    const roll = random()
    if (roll >= 0.3) return index
    const [x, y, z] = vertices[index]
    const shift = roll < 0.2 ? 0 : roll < 0.25 ? 2e-5 : 3e-4
    vertices.push([x + shift, y, z])
    return vertices.length - 1
  }
  const triangles = []
  for (let r = 0; r + 1 < rows; r += 1) {
    for (let c = 0; c + 1 < columns; c += 1) {
      const p = vertexOf[r * columns + c]
      const q = vertexOf[r * columns + c + 1]
      const s = vertexOf[(r + 1) * columns + c]
      const t = vertexOf[(r + 1) * columns + c + 1]
      triangles.push([twin(p), twin(q), twin(t)], [twin(p), twin(t), twin(s)])
    }
  }
  const extra = []
  for (const triangle of triangles) {
    const roll = random()
    if (roll < 0.05) extra.push([triangle[0], triangle[2], triangle[1]]) // 裏返った同じ三角形
    else if (roll < 0.08) extra.push([...triangle]) // 同じ三角形の重複
    else if (roll < 0.1) extra.push([triangle[0], triangle[1], vertexOf[Math.floor(random() * vertexOf.length)]]) // 3枚目が辺を共有
    else if (roll < 0.12) extra.push([triangle[0], triangle[0], triangle[1]]) // 潰れた三角形
  }
  const all = [...triangles, ...extra]
  for (let i = all.length - 1; i > 0; i -= 1) {
    if (random() < 0.3) {
      const j = Math.floor(random() * (i + 1))
      ;[all[i], all[j]] = [all[j], all[i]]
    }
  }
  const positions = new Float32Array(vertices.flat())
  const indices = new Uint32Array(all.flat())
  same(`格子 ${trial}`, positions, indices, THRESHOLDS[trial % THRESHOLDS.length])
}

// 数の配列で届いた添字(`FaceGeometry.indices` は number[] のこともある)
{
  const { positions, indices } = fromThree(shapes.sphere)
  const want = expected(positions, indices, 20)
  assert.deepEqual(Array.from(edgeSegments(positions, Array.from(indices), 20)), Array.from(want))
}

// 座標が NaN の頂点どうしは同じ頂点として突き合わさる(`EdgesGeometry` の文字列は "NaN")
{
  const positions = new Float32Array([0, 0, 0, 1, 0, 0, NaN, NaN, NaN, 1, 1, 0, NaN, NaN, NaN, 0, 1, 0])
  const indices = new Uint32Array([0, 1, 2, 4, 1, 3, 0, 4, 5])
  for (const threshold of THRESHOLDS) same('NaN の頂点', positions, indices, threshold)
}

// 大きな座標(× 10⁴ が 32 ビットを超える)・無限大・x と y の -0・3枚が共有する辺
{
  const big = fromThree(new THREE.SphereGeometry(5, 10, 6))
  for (let i = 0; i < big.positions.length; i += 1) big.positions[i] = big.positions[i] * 1e6 + 3e8
  for (const threshold of THRESHOLDS) same('大きな座標', big.positions, big.indices, threshold)
  const positions = new Float32Array([-0, -0, 0, 1, 0, 0, 0, 1, 0, Infinity, 0, 0, 0, -Infinity, 1, -0.00001, 0.00004, 0])
  const indices = new Uint32Array([0, 1, 2, 1, 0, 3, 0, 1, 4, 1, 0, 5, 2, 1, 3, 3, 4, 5])
  for (const threshold of THRESHOLDS) same('無限大と3枚の辺', positions, indices, threshold)
}

// 刻みが2の冪の倍数の格子(丸めた座標の下位 32 ビットが偏る・全部 0 になる)でも、同じ結果を EdgesGeometry より速く返す。
// 頂点の表の位置を下位ビットだけで決めていたときは全頂点が同じ位置に集まり、3 秒(EdgesGeometry は 0.4 秒)かかった
for (const step of [8192, 268435456]) {
  const side = 256
  const positions = new Float32Array(side * side * 3)
  for (let y = 0; y < side; y += 1) {
    for (let x = 0; x < side; x += 1) positions.set([step * x, step * y, 0], (y * side + x) * 3)
  }
  const indices = []
  for (let y = 0; y + 1 < side; y += 1) {
    for (let x = 0; x + 1 < side; x += 1) {
      const p = y * side + x
      indices.push(p, p + 1, p + side + 1, p, p + side + 1, p + side)
    }
  }
  const index = Uint32Array.from(indices)
  const t0 = performance.now()
  const want = expected(positions, index, 20)
  const t1 = performance.now()
  const got = edgeSegments(positions, index, 20)
  const t2 = performance.now()
  assert.deepEqual(Array.from(got), Array.from(want))
  assert.ok(t2 - t1 < t1 - t0, `刻み ${step} の格子で EdgesGeometry より遅い: ${(t2 - t1).toFixed(0)} ms(EdgesGeometry ${(t1 - t0).toFixed(0)} ms)`)
}

// 三角形が無い面は線分も無い
assert.equal(edgeSegments(new Float32Array(0), new Uint32Array(0), 20).length, 0)

console.log('ok')
