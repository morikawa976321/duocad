/**
 * `web/src/commands/sketch.ts` のスケッチ作成2つ(`SKETCH_CREATE` / `SKETCH_CREATE_OFFSET`)が、貼る先の値から送る引数を評価する。
 * `tests/test_ts_evaluators.py` が `node` でこれを走らせる。
 *
 * 落ちたら、スケッチ・面を指したのに `base_plane` を送って選択が無視されるか、オフセット平面の作成が貼る先待ちの外で走る扱いになる。
 */
import assert from 'node:assert/strict'

import './ts_resolve_hook.mjs'

const { SKETCH_CREATE, SKETCH_CREATE_OFFSET, runsOutsideSketch } = await import('../web/src/commands/sketch.ts')

assert.deepEqual(SKETCH_CREATE_OFFSET.build({ target: 'sketch', distance: 2 }), { ok: true, args: { distance: 2 } })
assert.deepEqual(SKETCH_CREATE_OFFSET.build({ target: 'face', distance: 2 }), { ok: true, args: { distance: 2 } })
assert.deepEqual(SKETCH_CREATE_OFFSET.build({ target: 'XZ', distance: -3 }), {
  ok: true,
  args: { base_plane: 'XZ', distance: -3 },
})
assert.deepEqual(SKETCH_CREATE.build({ target: 'sketch' }), { ok: true, args: {} })
assert.deepEqual(SKETCH_CREATE.build({ target: 'YZ' }), { ok: true, args: { base_plane: 'YZ' } })
assert.equal(SKETCH_CREATE_OFFSET.build({ target: 'region', distance: 2 }).ok, false, '知らない値は通さない')

// 貼る先待ち(`sketch-pending`)はこの2つのどちらを構えていても続く(`App.svelte` の `mode`)。
assert.equal(runsOutsideSketch(SKETCH_CREATE.id), true)
assert.equal(runsOutsideSketch(SKETCH_CREATE_OFFSET.id), true)
assert.equal(runsOutsideSketch('add_line'), false)
