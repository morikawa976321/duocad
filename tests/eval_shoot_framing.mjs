/**
 * `web/scripts/framing.mjs`(比較スクショの距離決め)を実際に評価する。`tests/test_web_scripts.py` が呼ぶ。
 * 真上・真下の向き(Z軸と平行)で NaN にならないこと、零ベクトルの向きを断ること。
 */
import assert from 'node:assert/strict'

import { distanceFor } from '../web/scripts/framing.mjs'

// X 方向に長く、Y 方向に短い箱(右の向きを取り違えると、距離が変わる)
const box = new Float32Array([0, 0, 0, 40, 0, 0, 0, 10, 0, 40, 10, 0, 0, 0, 5, 40, 10, 5])
const centre = [20, 5, 2.5]
const FOV = 50
const ASPECT = 900 / 700

const slanted = distanceFor([0.5, 0.5, 1], centre, [box], FOV, ASPECT)
assert.ok(Number.isFinite(slanted) && slanted > 0, `斜めの向きで距離が出ない: ${slanted}`)

for (const sign of [1, -1]) {
  const vertical = distanceFor([0, 0, sign], centre, [box], FOV, ASPECT)
  assert.ok(Number.isFinite(vertical) && vertical > 0, `真${sign > 0 ? '上' : '下'}で距離が出ない: ${vertical}`)
  // ほんの少し傾けた向きと連続している(退化の分岐が three.js の lookAt と同じ右を選んでいる)
  const nearly = distanceFor([1e-4, 0, sign], centre, [box], FOV, ASPECT)
  assert.ok(Math.abs(vertical - nearly) / nearly < 1e-3, `真${sign > 0 ? '上' : '下'}が傾けた向きと食い違う: ${vertical} / ${nearly}`)
}

for (const bad of [[0, 0, 0], [Number.NaN, 0, 1], [Infinity, 0, 0]]) {
  assert.throws(() => distanceFor(bad, centre, [box], FOV, ASPECT), /視線の向きが使えない/, `向き ${bad} を断らない`)
}
console.log('ok')
