/** `web/src/minWidth.ts` の純関数を評価する口(`tests/test_ts_evaluators.py` が node で走らせる)。失敗したら非ゼロで終了する。 */
import assert from 'node:assert/strict'
import {
  chosenStep, shownElementSizeRange, shownMinWidthMm, snapMinWidth, stepCells,
} from '../web/src/minWidth.ts'

const stepOf = (size, lower, upper) => ({
  index: 0, element_size_mm: size, design_elements_estimate: 1, min_width_floor_mm: lower * size, min_width_ceiling_mm: upper * size,
  // サーバーの `min_width_steps_mm` と同じく、渡すとちょうどその m になる値(m × size / size が丸めで m を超えるときは下げる)。
  min_width_steps_mm: Array.from({ length: Math.max(upper - lower + 1, 0) }, (_, i) => {
    let value = (lower + i) * size
    while (Math.ceil(value / size) > lower + i) value -= value * Number.EPSILON
    return value
  }),
})

// 要素サイズの自由入力: 空欄は梯子の先頭(既定)、入力はその値への応答の `selected` だけを使う(古い値の応答では走らせない)。
const ladder = [stepOf(1, 2, 5), stepOf(2, 2, 5), stepOf(3, 2, 5)]
const picked = stepOf(1.5, 2, 5)
assert.equal(chosenStep(null, null), null)
assert.equal(chosenStep({ error: 'まだ条件面が無い' }, null), null)
assert.equal(chosenStep({ steps: ladder }, null), ladder[0])
assert.equal(chosenStep({ steps: [] }, null), null)
assert.equal(chosenStep({ steps: ladder, requested_element_size_mm: 1.5, selected: picked }, 1.5), picked)
assert.equal(chosenStep({ steps: ladder, requested_element_size_mm: 1.5, selected: picked }, 2), null)
assert.equal(chosenStep({ steps: ladder, requested_element_size_mm: 9, selected_error: '上限より粗い' }, 9), null)

// 範囲の表示: 上限は実格子へ切り下げる前の値(4mm)まで受けるので 3.98 ではなく 4.00、下限は切り上げ。
assert.deepEqual(
  shownElementSizeRange({ min_element_size_mm: 3.2581818, max_element_size_mm: 3.9822, from_condition_faces_mm: 14.14, max_element_size_cap_mm: 4 }),
  { lower: '3.26', upper: '4.00' },
)
assert.deepEqual(
  shownElementSizeRange({ min_element_size_mm: 1, max_element_size_mm: 2.95, from_condition_faces_mm: 3.0, max_element_size_cap_mm: 4 }),
  { lower: '1.00', upper: '3.00' },
)
assert.equal(shownElementSizeRange({ error: 'x' }), null)

// 要素サイズを変えたときの最小幅の寄せ先: 前に選んだ幅以上でいちばん細い段。より細い段へは黙って下げない。
const coarse = stepOf(4, 2, 3).min_width_steps_mm // 8, 12
const fine = stepOf(2.4063, 2, 10).min_width_steps_mm
assert.equal(snapMinWidth(coarse, null), null)
assert.equal(snapMinWidth([], 8), null)
assert.equal(snapMinWidth(coarse, 8), 8)
assert.equal(snapMinWidth(coarse, 9), 12)
assert.equal(snapMinWidth(coarse, 30), 12) // 上限を超えていたら上限の段
assert.equal(snapMinWidth(fine, 8), fine[2]) // 7.22 ではなく 9.63
for (const value of fine) assert.equal(snapMinWidth(fine, value), value, `段ちょうど ${value}`)
assert.equal(snapMinWidth(fine, fine[3] * (1 + 1e-12)), fine[3]) // 1ulp 違いは同じ段
assert.equal(stepCells(stepOf(2.4063, 2, 10), fine[5]), 7)

// 段と実行後の通知の保証値は切り捨てる(四捨五入だと 4.9998mm が「5」になる)。
assert.equal(shownMinWidthMm(4.9998), '4.99')
assert.equal(shownMinWidthMm(7.2189), '7.21')
assert.doesNotThrow(() => shownMinWidthMm(2e-98))
console.log('ok')
