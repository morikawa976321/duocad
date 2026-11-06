/**
 * 全体表示・SVG 取り込み後の寄せの入力を作る純関数(`web/src/viewFraming.ts`)を検査する。
 * 落ちたら: 全体表示で視点の向きが変わる、取り込んだ図形以外(既存の図形)まで寄せの対象に入っている、
 * またはプレビューが隠している元のボディまで全体表示の対象に入っている。
 */
import assert from 'node:assert/strict'
import './ts_resolve_hook.mjs'
const THREE = await import('../web/node_modules/three/build/three.module.js')
const { boxOfSketchEntities, boxOfVisible, keptDirectionOffset } = await import('../web/src/viewFraming.ts')

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

// 隠してある物(プレビュー中の元のボディ)は箱に入らない。長さ 2000 の元のボディを隠し、幅 1 の結果だけが見えている場合
const slab = (width, visible) => {
  const mesh = new THREE.Mesh(new THREE.BoxGeometry(width, 10, 10))
  mesh.visible = visible
  return mesh
}
const original = slab(2000, false)
const result = new THREE.Group().add(slab(1, true))
const seen = boxOfVisible([original, result])
assert.ok(Math.abs(seen.max.x - seen.min.x - 1) < 1e-9, `隠した元のボディまで箱に入っている: 幅 ${seen.max.x - seen.min.x}`)
assert.ok(boxOfVisible([original]).isEmpty(), '見えている物が無いのに箱が空でない')
original.visible = true
assert.ok(Math.abs(boxOfVisible([original, result]).max.x - 1000) < 1e-9, '見えているボディが箱に入っていない')

console.log('viewFraming.ts: 全ての想定を満たした')
