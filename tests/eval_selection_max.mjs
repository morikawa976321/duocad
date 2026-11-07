/**
 * `web/src/commands/command.ts` の `evictionsForClick`(個数の上限がある選択引数への、上限を超えるクリック)を評価する。
 * `tests/test_ts_evaluators.py` が `node` でこれを走らせる。
 *
 * 落ちたら、ねじ・測定で選び間違えたとき、最古の選択を外す置き換えにならず、外す手間が2クリックのままになる(または不要な選択まで外れる)。
 */
import assert from 'node:assert/strict'

import './ts_resolve_hook.mjs'

const { evictionsForClick } = await import('../web/src/commands/command.ts')
const features = await import('../web/src/commands/features.ts')
const measure = await import('../web/src/commands/measure.ts')

const face = (index) => ({ body: 'b1', kind: 'face', index })
const edge = (index) => ({ body: 'b1', kind: 'edge', index })
const cylinder = () => 'cylinder_full'

// ねじ(円筒面1つまで): 選んである面があれば、新しい面を足すときその面を外す
assert.deepEqual(evictionsForClick(features.THREAD, {}, face(2), [face(1)], cylinder), [face(1)])
assert.deepEqual(evictionsForClick(features.THREAD, {}, face(2), [], cylinder), [], '空なら何も外さない')
// 全周の円筒でない面は、この引数が食わない(断りの文言のまま。置き換えない)
assert.deepEqual(evictionsForClick(features.THREAD, {}, face(2), [face(1)], () => 'plane'), [])
// 辺は種別が違うので食わない
assert.deepEqual(evictionsForClick(features.THREAD, {}, edge(1), [face(1)], cylinder), [])

// 測定(ちょうど2つまで): 3つ目のクリックは最古の1つを外す。2つ未満なら外さない
assert.deepEqual(evictionsForClick(measure.MEASURE, {}, edge(3), [edge(1)]), [])
assert.deepEqual(evictionsForClick(measure.MEASURE, {}, edge(3), [edge(1), face(2)]), [edge(1)])
// すでに上限を超えて選ばれていれば、超えた分も含めて古いほうから外す
assert.deepEqual(evictionsForClick(measure.MEASURE, {}, edge(4), [edge(1), face(2), edge(3)]), [edge(1), face(2)])

// 上限の無い引数(フィレットの辺)は外さない
assert.deepEqual(evictionsForClick(features.FILLET, {}, edge(5), [edge(1), edge(2), edge(3)]), [])
console.log('eval_selection_max: ok')
