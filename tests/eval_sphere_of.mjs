/** 辺・頂点の外接球(`web/src/solidFeatures.ts` の `sphereOf`)が、three.js の `Box3` で囲んで求めた球と同じ値になる。 */
import assert from 'node:assert/strict'
import './ts_resolve_hook.mjs'

const THREE = await import(new URL('../web/node_modules/three/build/three.module.js', import.meta.url).href)
const { sphereOf } = await import('../web/src/solidFeatures.ts')

function expected(points) {
  const box = new THREE.Box3()
  const scratch = new THREE.Vector3()
  for (let at = 0; at + 2 < points.length; at += 3) box.expandByPoint(scratch.set(points[at], points[at + 1], points[at + 2]))
  return box.isEmpty() ? new THREE.Sphere(new THREE.Vector3(), -1) : box.getBoundingSphere(new THREE.Sphere())
}

function same(name, points) {
  const want = expected(points)
  const got = sphereOf(points)
  for (const [a, b, what] of [
    [got.center.x, want.center.x, 'x'],
    [got.center.y, want.center.y, 'y'],
    [got.center.z, want.center.z, 'z'],
    [got.radius, want.radius, 'radius'],
  ]) {
    assert.ok(Object.is(a, b) || (Number.isNaN(a) && Number.isNaN(b)), `${name} の ${what}: ${a} と ${b}`)
  }
}

let seed = 7
function random() {
  seed = (seed * 1103515245 + 12345) % 2147483648
  return seed / 2147483648
}

same('点なし', new Float32Array(0))
same('端数の要素だけ', [1, 2])
same('1点', new Float32Array([1, -2, 3]))
same('-0 を含む', [-0, 0, -0, 0, -0, 0])
same('NaN を含む', [0, 0, 0, NaN, 1, 2, 3, 4, 5])
same('無限大を含む', [0, 0, 0, Infinity, 1, 2])
for (let trial = 0; trial < 500; trial += 1) {
  const count = 1 + Math.floor(random() * 60)
  const scale = 10 ** Math.floor(random() * 8 - 3)
  const values = Array.from({ length: count * 3 + (trial % 3) }, () => (random() - 0.5) * scale)
  same(`乱数 ${trial}(数の配列)`, values)
  same(`乱数 ${trial}(Float32Array)`, new Float32Array(values))
}

console.log('ok')
