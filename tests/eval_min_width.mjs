/** `web/src/minWidth.ts` の純関数を評価する口(`tests/test_min_width_ui.py` が node で走らせる)。失敗したら非ゼロで終了する。 */
import assert from 'node:assert/strict'
import { judgeMinWidth, shownCeilingMm, shownFloorMm, shownGuaranteedMm } from '../web/src/minWidth.ts'

const stepOf = (size, cells) => ({
  index: 0, element_size_mm: size, element_count: 1, min_width_floor_mm: 2 * size, min_width_ceiling_mm: cells * size,
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
