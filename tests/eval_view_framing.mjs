/**
 * 全体表示・SVG 取り込み後の寄せの入力を作る純関数(`web/src/viewFraming.ts`)を検査する。
 * 落ちたら: 全体表示で視点の向きが変わる、または取り込んだ図形以外(既存の図形)まで寄せの対象に入っている。
 */
import assert from 'node:assert/strict'
import './ts_resolve_hook.mjs'
const THREE = await import('../web/node_modules/three/build/three.module.js')
const { boxOfSketchEntities, keptDirectionOffset } = await import('../web/src/viewFraming.ts')

const home = new THREE.Vector3(1, -1, 0.6)

// 向きは今のまま、長さだけ home に揃う
const kept = keptDirectionOffset(new THREE.Vector3(0, -50, 0), new THREE.Vector3(0, 0, 0), home)
assert.ok(Math.abs(kept.length() - home.length()) < 1e-9)
assert.ok(kept.clone().normalize().distanceTo(new THREE.Vector3(0, -1, 0)) < 1e-9, '向きが変わっている')
// 注視点にカメラが重なっていれば home の向き
const degenerate = keptDirectionOffset(new THREE.Vector3(3, 3, 3), new THREE.Vector3(3, 3, 3), home)
assert.ok(degenerate.distanceTo(home) < 1e-9)

// 指定した図形の折れ線だけが箱に入る
const line = (entityId, ...points) => ({ entityId, polylineWorld: points.map((p) => new THREE.Vector3(...p)) })
const lines = [line('old', [0, 0, 0], [1, 1, 0]), line('new', [100, 0, 0], [120, 30, 0]), line('new', [110, 50, 0])]
const box = boxOfSketchEntities(lines, new Set(['new']))
assert.deepEqual(box.min.toArray(), [100, 0, 0])
assert.deepEqual(box.max.toArray(), [120, 50, 0])
assert.ok(boxOfSketchEntities(lines, new Set(['none'])).isEmpty(), '該当が無いのに箱が空でない')
assert.ok(boxOfSketchEntities([{ entityId: 'new', polylineWorld: undefined }], new Set(['new'])).isEmpty())

console.log('viewFraming.ts: 全ての想定を満たした')
