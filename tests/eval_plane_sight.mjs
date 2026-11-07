/**
 * 視線が平面に浅すぎるかの判定(`web/src/planeSight.ts`)を検査する。
 * 落ちたら: 地平線付近のクリックが極端に遠い座標の点として確定する、または斜めから描けなくなる。
 */
import assert from 'node:assert/strict'
import './ts_resolve_hook.mjs'
const THREE = await import('../web/node_modules/three/build/three.module.js')
const { MIN_PLANE_SIGHT_DEG, sightTooShallow } = await import('../web/src/planeSight.ts')

const normal = new THREE.Vector3(0, 0, 1)
const along = (deg) => new THREE.Vector3(Math.cos((deg * Math.PI) / 180), 0, -Math.sin((deg * Math.PI) / 180))

assert.equal(sightTooShallow(along(90), normal), false, '真上から見ているのに断っている')
assert.equal(sightTooShallow(along(30), normal), false, '斜めから見ているのに断っている')
assert.equal(sightTooShallow(along(MIN_PLANE_SIGHT_DEG + 0.5), normal), false)
assert.equal(sightTooShallow(along(MIN_PLANE_SIGHT_DEG - 0.5), normal), true, '浅すぎる視線を受けている')
assert.equal(sightTooShallow(along(0), normal), true)
assert.equal(sightTooShallow(along(-1), normal), true, '裏側から浅く見ても同じ')

console.log('planeSight.ts: 全ての想定を満たした')
