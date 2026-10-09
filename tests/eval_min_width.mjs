/** `web/src/minWidth.ts` の純関数を評価する口(`tests/test_ts_evaluators.py` が node で走らせる)。失敗したら非ゼロで終了する。 */
import assert from 'node:assert/strict'
import {
  chosenStep, isOutOfRange, judgeMinWidth, roundUpNotice, shownCeilingMm, shownElementSizeRange, shownFloorMm,
  shownGuaranteedMm, shownMinWidthMm, shownStepMm,
} from '../web/src/minWidth.ts'

const stepOf = (size, cells) => ({
  index: 0, element_size_mm: size, design_elements_estimate: 1, min_width_floor_mm: 2 * size, min_width_ceiling_mm: cells * size,
  // サーバーの `min_width_steps_mm` と同じく、入力するとちょうどその m になる値(m × size / size が丸めで m を超えるときは下げる)。
  min_width_steps_mm: Array.from({ length: cells - 1 }, (_, i) => {
    let value = (i + 2) * size
    while (Math.ceil(value / size) > i + 2) value -= value * Number.EPSILON
    return value
  }),
})

// 要素サイズ 2.4501mm の下限は 4.9002mm。四捨五入の「4.90」を表示すると、表示どおりに入力して下限割れになる。
const awkward = stepOf(2.4501, 40)
assert.equal(shownFloorMm(awkward), '4.91')
assert.equal(judgeMinWidth(4.9, awkward).kind, 'below')
assert.equal(judgeMinWidth(Number(shownFloorMm(awkward)), awkward).kind, 'ok')
assert.equal(shownCeilingMm(awkward), '98.00')
assert.equal(judgeMinWidth(Number(shownCeilingMm(awkward)), awkward).kind, 'ok')
assert.equal(judgeMinWidth(5000, awkward).kind, 'above')
assert.equal(judgeMinWidth(null, awkward).kind, 'none')
assert.equal(shownGuaranteedMm(judgeMinWidth(5, awkward)), '7.35')

// 段(m × 要素サイズ)のあいだの入力は次の段へ切り上がる。段ちょうど・その少し上・少し下で m が期待どおりになり、
// 切り上がるときだけ切り上げ先の説明が出る(実 spacing 2.4063mm で 4.9 を入れると 7.2189mm)。
const grid = stepOf(2.4063, 60)
const m = (v) => judgeMinWidth(v, grid).cells
assert.equal(m(2 * 2.4063), 2)
assert.equal(m(3 * 2.4063), 3)
assert.equal(m(3 * 2.4063 - 1e-6), 3)
assert.equal(m(3 * 2.4063 + 1e-6), 4)
assert.equal(m(4.9), 3)
assert.equal(m(60 * 2.4063), 60)
assert.equal(judgeMinWidth(60 * 2.4063, grid).guaranteedMm, 60 * 2.4063)
assert.equal(judgeMinWidth(4.9, grid).roundedUpBy > 0.45, true)
assert.equal(judgeMinWidth(4.9, grid).lowerStepMm, 2 * 2.4063)
assert.equal(judgeMinWidth(2 * 2.4063, grid).lowerStepMm, null)
assert.match(roundUpNotice(judgeMinWidth(4.9, grid), 4.9), /m=3 へ切り上がって 7\.21mm .*47\.3%/)
for (const exact of [2 * 2.4063, 3 * 2.4063, 17 * 2.4063, ...grid.min_width_steps_mm]) {
  assert.equal(judgeMinWidth(exact, grid).roundedUpBy, null, `段ちょうど ${exact}`)
  assert.equal(roundUpNotice(judgeMinWidth(exact, grid), exact), null)
}

assert.doesNotThrow(() => shownStepMm(2e-98)) // toFixed は100桁まで

// 梯子(要素サイズの候補)と、範囲外の判定(App と OptimizePanel が同じものを読む)。
const ladder = [stepOf(1, 5), stepOf(2, 5), stepOf(3, 5)]
// 要素サイズの自由入力: 空欄は梯子の先頭(既定)、入力はその値への応答の `selected` だけを使う(古い値の応答では走らせない)。
const picked = stepOf(1.5, 5)
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
assert.equal(isOutOfRange({ kind: 'none' }), false)
assert.equal(isOutOfRange(judgeMinWidth(5, awkward)), false)
assert.equal(isOutOfRange(judgeMinWidth(4.9, awkward)), true)
assert.equal(isOutOfRange(judgeMinWidth(5000, awkward)), true)

// 実行後の通知の保証値も切り捨てる(四捨五入だと 4.9998mm が「5」になり、実行前のパネルの 4.99mm と食い違う)。
assert.equal(shownMinWidthMm(4.9998), '4.99')
assert.equal(shownMinWidthMm(7.2189), '7.21')

// 表示した下限・上限は、どんな要素サイズ・セル数でもそのまま入力して通る(判定と表示が食い違わない)。
let seed = 20260921
const random = () => (seed = (seed * 1664525 + 1013904223) % 4294967296) / 4294967296
for (let i = 0; i < 5000; i++) {
  const size = 10 ** (-3.5 + random() * 5) // 0.0003mm〜30mm(小さい部品では 0.01mm の桁に収まらない)
  const step = stepOf(size, 3 + Math.floor(random() * 60))
  assert.equal(judgeMinWidth(Number(shownFloorMm(step)), step).kind, 'ok', `floor ${JSON.stringify(step)}`)
  assert.equal(judgeMinWidth(Number(shownCeilingMm(step)), step).kind, 'ok', `ceiling ${JSON.stringify(step)}`)
}
console.log('ok')
