/**
 * `web/src/polylinePick.ts`(折れ線の screen pick)を、実際のカメラで動かす口。
 * `tests/test_ts_evaluators.py` が `node` でこれを走らせる。
 *
 * 落ちたら、片端がカメラの後ろ(近平面の手前)にある線分が、描かれている部分があるのに候補から落ちている。
 */
import assert from 'node:assert/strict'

import './ts_resolve_hook.mjs'

const THREE = await import(new URL('../web/node_modules/three/build/three.module.js', import.meta.url).href)
const { closestOnPolyline, createProjector } = await import('../web/src/polylinePick.ts')

function polyline(points) {
  return { count: points.length, at: (index, out) => out.copy(points[index]) }
}

// 原点から -Z 向きに見る透視カメラ(near 1, far 100)。画面は 200×200、中心 (100, 100)。
const camera = new THREE.PerspectiveCamera(90, 1, 1, 100)
camera.position.set(0, 0, 0)
camera.lookAt(0, 0, -1)
camera.updateMatrixWorld()
const projector = createProjector(camera, 200, 200)

// 全体が見えている線分: 従来どおり当たる。
const inside = closestOnPolyline(100, 100, polyline([new THREE.Vector3(-10, 0, -10), new THREE.Vector3(10, 0, -10)]), projector)
assert.ok(inside !== null && inside.distSq < 1e-6, '見えている線分が当たらない')

// 片端がカメラの後ろ(z=+5)の線分: 近平面より先の部分が指せる。z=-10 の側の端の近くを狙う。
const behind = [new THREE.Vector3(5, 0, -10), new THREE.Vector3(5, 0, 5)]
const nearEnd = closestOnPolyline(100 + (5 / 10) * 100, 100, polyline(behind), projector)
assert.ok(nearEnd !== null, '片端がカメラの後ろの線分が候補から落ちている')
assert.ok(nearEnd.distSq < 1e-6, `描かれている部分に当たらない: ${nearEnd.distSq}`)
assert.ok(nearEnd.point.z <= -1 + 1e-6, `当たりの点が近平面の手前にある: ${nearEnd.point.z}`)

// 近平面で切った端: x=5 の線は z=-1 で画面の 100 + 5/1*100 = 600 の外へ向かうが、線分が画面の外でも距離は測れる。
const clipped = closestOnPolyline(600, 100, polyline(behind), projector)
assert.ok(clipped !== null && clipped.distSq < 1e-3, '切り口の点が当たらない')

// 全体がカメラの後ろ・遠平面の先: 候補にならない。
assert.equal(closestOnPolyline(100, 100, polyline([new THREE.Vector3(0, 0, 3), new THREE.Vector3(1, 0, 5)]), projector), null)
assert.equal(closestOnPolyline(100, 100, polyline([new THREE.Vector3(0, 0, -150), new THREE.Vector3(1, 0, -200)]), projector), null)

// 遠平面をまたぐ線分も、手前の部分が指せる。
const farCrossing = closestOnPolyline(100, 100, polyline([new THREE.Vector3(0, 0, -50), new THREE.Vector3(0, 0, -500)]), projector)
assert.ok(farCrossing !== null && farCrossing.point.z >= -100 - 1e-3, '遠平面をまたぐ線分の手前が指せない')

// near と far の比が大きい視点(大きい文書で手前へ深く寄ったとき)の、見えている奥の線分: 遠平面の手前に詰まった z で落とさない。
const deep = new THREE.PerspectiveCamera(30, 1, 0.01, 600000)
deep.position.set(0, 0, 0)
deep.lookAt(0, 0, -1)
deep.updateMatrixWorld()
const deepProjector = createProjector(deep, 800, 800)
const deepHit = closestOnPolyline(400, 400, polyline([new THREE.Vector3(-1000, 0, -30000), new THREE.Vector3(1000, 0, -30000)]), deepProjector)
assert.ok(deepHit !== null && deepHit.distSq < 1e-6, 'near と far の比が大きい視点で、見えている奥の線分が当たらない')
const deepCrossing = closestOnPolyline(400, 400, polyline([new THREE.Vector3(0, 0, -300000), new THREE.Vector3(0, 0, -900000)]), deepProjector)
assert.ok(deepCrossing !== null && deepCrossing.point.z >= -600000 - 1, 'near と far の比が大きい視点で、遠平面をまたぐ線分の手前が指せない')

// 正投影でも同じ(near 手前の端)。
const ortho = new THREE.OrthographicCamera(-100, 100, 100, -100, 1, 100)
ortho.position.set(0, 0, 0)
ortho.lookAt(0, 0, -1)
ortho.updateMatrixWorld()
const orthoProjector = createProjector(ortho, 200, 200)
const orthoHit = closestOnPolyline(110, 100, polyline([new THREE.Vector3(10, 0, -10), new THREE.Vector3(10, 0, 5)]), orthoProjector)
assert.ok(orthoHit !== null && orthoHit.distSq < 1e-6, '正投影で近平面にかかる線分が当たらない')

console.log('polylinePick.ts: 全ての想定を満たした')
