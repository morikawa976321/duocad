/** `web/src/minWidth.ts` の純関数を評価する口(`tests/test_ts_evaluators.py` が node で走らせる)。失敗したら非ゼロで終了する。 */
import assert from 'node:assert/strict'
import {
  clampStepIndex, isOutOfRange, judgeMinWidth, roundUpNotice, shownCeilingMm, shownFloorMm, shownGuaranteedMm,
  shownMinWidthMm, shownStepMm, stepAt,
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

// 段の番号の丸め(目盛りが空・目盛りより大きい番号)と、範囲外の判定(App と OptimizePanel が同じものを読む)。
const ladder = [stepOf(1, 5), stepOf(2, 5), stepOf(3, 5)]
assert.equal(stepAt([], 0), null)
assert.equal(stepAt([], 3), null)
assert.equal(clampStepIndex(3, 0), 0)
assert.equal(clampStepIndex(7, 3), 2)
assert.equal(clampStepIndex(1, 3), 1)
assert.equal(stepAt(ladder, 7), ladder[2])
assert.equal(stepAt(ladder, 1), ladder[1])
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
