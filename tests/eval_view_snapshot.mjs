/** 視点の控えの比べ方(`web/src/viewSnapshot.ts`)が、止まったカメラの揺れを同じと見て、本当の動きを違うと見る。 */
import assert from 'node:assert/strict'
import './ts_resolve_hook.mjs'

const { snapshotView, sameView } = await import('../web/src/viewSnapshot.ts')

const view = [0.707107, 0.707107, 0, 0, -0.276172, 0.276172, 0.920575, 0, 0.650945, -0.650945, 0.390567, 0, 140.932268, -95.932268, 77.559361, 1]
const projection = [2.719969, 0, 0, 0, 0, 3.732051, 0, 0, 0, 0, -1.000093, -1, 0, 0, -0.371577, 0]
const base = snapshotView(view, projection, 944, 688)

// スケッチモードの揺れ: 0 の成分が -1e-8 と +1e-8 を行き来する(小数6桁の文字列では -0.000000 と 0.000000 に分かれた)
const jitter = (values, sign) => values.map((value, i) => value + (i % 3 === 0 ? sign : -sign) * 1e-8)
assert.ok(sameView(snapshotView(jitter(view, 1), jitter(projection, 1), 944, 688), snapshotView(jitter(view, -1), jitter(projection, -1), 944, 688)))
assert.ok(sameView(base, snapshotView(view.map((v, i) => (i === 2 ? -1e-8 : v)), projection, 944, 688)))

// 丸めの境目をまたぐ揺れも同じ
assert.ok(sameView(snapshotView([0.0000005 - 1e-9, ...view.slice(1)], projection, 944, 688), snapshotView([0.0000005 + 1e-9, ...view.slice(1)], projection, 944, 688)))

// 本当に動いた・寸法が変わった・投影が変わった
assert.ok(!sameView(base, snapshotView(view.map((v, i) => (i === 12 ? v + 1e-3 : v)), projection, 944, 688)))
assert.ok(!sameView(base, snapshotView(view, projection, 944, 600)))
assert.ok(!sameView(base, snapshotView(view, projection.map((v, i) => (i === 5 ? v * 1.01 : v)), 944, 688)))
assert.ok(!sameView(base, snapshotView(view, projection.map((v, i) => (i === 0 ? NaN : v)), 944, 688)))

// 控えは写し(元の配列を後で書き換えても控えは変わらない)
const live = Float64Array.from(view)
const kept = snapshotView(live, projection, 944, 688)
live[12] += 5
assert.ok(sameView(kept, base))

console.log('ok')
