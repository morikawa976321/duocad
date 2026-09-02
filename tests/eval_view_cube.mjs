/**
 * `web/src/viewCube.ts` の純関数(`regionCells` / `cellBox` / `cellDirection` /
 * `upForDirection`)を**実際に評価する**ための小さな口。`tests/test_view_cube.py` が
 * `node` でこれを走らせる(`tests/eval_grid_scale.mjs` と同じやり方 —— ソースの文字列
 * 比較ではなく実行結果で見る)。**Python側の対応物は無い**(ビューキューブはTSにしか
 * 描かれない)ので、ここで直接 assert して失敗したら非ゼロで終わる。**skipしない**。
 *
 * Node は `.ts` をそのまま読める(型注釈の除去。Node 23.6以降は既定で有効)。
 */
import assert from 'node:assert/strict'
import { EDGE_T, cellBox, cellDirection, regionCells, upForDirection } from '../web/src/viewCube.ts'

// -------------------------------------------------------------- regionCells: 26個・重複無し
const cells = regionCells()
assert.equal(cells.length, 26, '27セルから中心を除くと26個のはず')
const keys = cells.map(([i, j, k]) => `${i},${j},${k}`)
assert.equal(new Set(keys).size, 26, 'セルの重複がある')
assert.ok(!keys.includes('0,0,0'), '中心セルが混ざっている')
for (const [i, j, k] of cells) {
  for (const value of [i, j, k]) assert.ok([-1, 0, 1].includes(value), `セルの成分が範囲外: ${value}`)
}

// -------------------------------------------------------------- 面6・辺12・角8
function magnitude([i, j, k]) {
  return Math.abs(i) + Math.abs(j) + Math.abs(k)
}
const byMagnitude = { 1: 0, 2: 0, 3: 0 }
for (const cell of cells) byMagnitude[magnitude(cell)] += 1
assert.deepEqual(byMagnitude, { 1: 6, 2: 12, 3: 8 }, '面6・辺12・角8の内訳と食い違う')

// -------------------------------------------------------------- cellBox: 重ならず、外側が表面に一致
// 全26領域(+中心)ぶんの区間を軸ごとに集め、境界がちょうど一致する(重なりも隙間もない)ことを見る。
const allCells = [...cells, [0, 0, 0]]
for (const axis of [0, 1, 2]) {
  const intervals = new Map() // key: 'idx' -> {lo, hi}
  for (const cell of allCells) {
    const idx = cell[axis]
    const { center, size } = cellBox(...cell)
    const half = size[axis] / 2
    const lo = center[axis] - half
    const hi = center[axis] + half
    const existing = intervals.get(idx)
    if (existing) {
      assert.ok(Math.abs(existing.lo - lo) < 1e-9 && Math.abs(existing.hi - hi) < 1e-9,
        `axis=${axis} idx=${idx} の区間が別のセルと食い違う`)
    } else {
      intervals.set(idx, { lo, hi })
    }
  }
  // -1, 0, 1 の3区間が過不足なく [-1, 1] を埋める(重なりも隙間も無い)。
  const neg = intervals.get(-1)
  const mid = intervals.get(0)
  const pos = intervals.get(1)
  assert.ok(neg && mid && pos, `axis=${axis} の3区間が揃っていない`)
  assert.equal(neg.lo, -1, `axis=${axis} の外側の面が立方体の表面(-1)に一致しない`)
  assert.equal(pos.hi, 1, `axis=${axis} の外側の面が立方体の表面(1)に一致しない`)
  assert.ok(Math.abs(neg.hi - mid.lo) < 1e-9, `axis=${axis} で -1側と中央のセルが重なる/隙間がある`)
  assert.ok(Math.abs(mid.hi - pos.lo) < 1e-9, `axis=${axis} で中央と1側のセルが重なる/隙間がある`)
}
// EDGE_T が想定どおり効いていること(既定値0.3の場合の具体例)。
{
  const { center, size } = cellBox(1, 0, 0)
  assert.ok(Math.abs(center[0] - (1 - EDGE_T)) < 1e-9)
  assert.ok(Math.abs(size[0] - 2 * EDGE_T) < 1e-9)
}

// -------------------------------------------------------------- cellDirection: 正規化されている
for (const [i, j, k] of cells) {
  const [dx, dy, dz] = cellDirection(i, j, k)
  const length = Math.hypot(dx, dy, dz)
  assert.ok(Math.abs(length - 1) < 1e-9, `cellDirection(${i},${j},${k}) が単位ベクトルでない`)
  // 向きは (i,j,k) と同じ符号・比率(正規化しただけ)。
  const expectedLength = Math.hypot(i, j, k)
  assert.ok(Math.abs(dx - i / expectedLength) < 1e-9)
  assert.ok(Math.abs(dy - j / expectedLength) < 1e-9)
  assert.ok(Math.abs(dz - k / expectedLength) < 1e-9)
}

// -------------------------------------------------------------- upForDirection
function dot(a, b) {
  return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
}
for (const [i, j, k] of cells) {
  const d = cellDirection(i, j, k)
  const up = upForDirection(d)
  const length = Math.hypot(...up)
  assert.ok(Math.abs(length - 1) < 1e-9, `upForDirection(${d}) が単位ベクトルでない`)
  // **非退化**: up が d と(反)平行にならない(lookAt の基底が不定にならないことの必要条件)。
  // これが壊れるのは |d.z| が1に極めて近いのに +Z を返してしまう実装だけである。
  assert.ok(Math.abs(dot(up, d)) < 1 - 1e-6, `upForDirection(${d}) が方向と(反)平行=退化している: up=${up}`)
}
// 真上・真下は厳密に ±Y で、d ((0,0,±1)) と厳密に直交する。
{
  const up = upForDirection([0, 0, 1])
  assert.deepEqual(up, [0, 1, 0], '真上(+Z)の up は +Y のはず')
  assert.equal(dot(up, [0, 0, 1]), 0)
}
{
  const up = upForDirection([0, 0, -1])
  assert.deepEqual(up, [0, -1, 0], '真下(-Z)の up は -Y のはず')
  assert.equal(dot(up, [0, 0, -1]), 0)
}
// 真上・真下でない既定ケース(水平方向。d.z===0)は +Z で、d と厳密に直交する。
for (const [i, j, k] of cells) {
  const d = cellDirection(i, j, k)
  if (d[2] !== 0) continue
  const up = upForDirection(d)
  assert.deepEqual(up, [0, 0, 1], `水平方向(${d})の up は既定の +Z のはず`)
  assert.equal(dot(up, d), 0)
}

console.log('view cube: all assertions passed')
