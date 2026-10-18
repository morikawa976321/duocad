/**
 * `web/src/pixelRatio.ts`(描画バッファの画素比)を**実際に評価する**口。`tests/test_ts_evaluators.py` が `node` でこれを走らせる。
 *
 * 落ちたら、ブラウザのズーム・別モニタへの移動で `devicePixelRatio` が変わっても描画バッファの解像度が追従しない
 * (100%で開いて拡大するとぼやけ、高DPIから等倍へ移るとバッファを無駄に持つ)。
 */
import assert from 'node:assert/strict'

import { cappedPixelRatio, nextPixelRatio } from '../web/src/pixelRatio.ts'

assert.equal(cappedPixelRatio(1), 1)
assert.equal(cappedPixelRatio(1.5), 1.5)
assert.equal(cappedPixelRatio(3), 2, '3倍以上はバッファが大きくなるだけ')
assert.equal(cappedPixelRatio(undefined), 1)
assert.equal(cappedPixelRatio(0), 1)

// 変わっていなければ null(毎フレーム呼ぶので、何もしない側が既定)。
assert.equal(nextPixelRatio(1, 1), null)
assert.equal(nextPixelRatio(2, 2), null)
assert.equal(nextPixelRatio(2, 3), null, '上限で頭打ちの値が同じなら変わっていない')
assert.equal(nextPixelRatio(1, undefined), null)

// 変わったら新しい値(上限をかけた後)。ズームで上がる・高DPIから等倍へ下がる・上限を超える。
assert.equal(nextPixelRatio(1, 2), 2, '100%で開いて拡大した')
assert.equal(nextPixelRatio(2, 1), 1, '高DPIモニタから等倍へ移った')
assert.equal(nextPixelRatio(2, 1.25), 1.25)
assert.equal(nextPixelRatio(1, 3), 2, '上限をかけた値')

console.log('pixelRatio.ts: 全ての想定を満たした')
