/**
 * `web/src/viewCube.ts` の純関数(`regionCells` / `cellBox` / `cellDirection` /
 * `upForDirection`)を**実際に評価する**ための小さな口。`tests/test_ts_evaluators.py` が
 * `node` でこれを走らせる。**Python側の対応物は無い**(ビューキューブはTSにしか
 * 描かれない)ので、ここで直接 assert して失敗したら非ゼロで終わる。
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
// EDGE_T が想定どおり効いていること(値そのものには依存させない)。
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
// 26方向すべてで |up・d| がほぼ0であること(水平/真上/真下に限らず全方向で厳密な直交を要求する)。
for (const [i, j, k] of cells) {
  const d = cellDirection(i, j, k)
  const up = upForDirection(d)
  const length = Math.hypot(...up)
  assert.ok(Math.abs(length - 1) < 1e-9, `upForDirection(${d}) が単位ベクトルでない`)
  assert.ok(Math.abs(dot(up, d)) < 1e-9, `upForDirection(${d}) が方向と直交していない: up=${up}, dot=${dot(up, d)}`)
}
// 真上・真下は厳密に ±Y。
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
// 水平方向(d.z===0)では射影が何もしないので、既定の +Z のまま返る(仕様の「既定は+Z」の実例)。
for (const [i, j, k] of cells) {
  const d = cellDirection(i, j, k)
  if (d[2] !== 0) continue
  const up = upForDirection(d)
  assert.deepEqual(up, [0, 0, 1], `水平方向(${d})の up は既定の +Z のはず`)
}
// 斜め方向(辺・角の一部)では、射影により +Z から傾く(=非自明な直交ベクトルになる)ことも見る
// (射影が実際に効いていること自体の検査。既定+Zをそのまま返すだけの実装だと、ここが+Zのままで
// 上の直交性検査にひっかかるはず)。
{
  const d = cellDirection(1, -1, 1) // 角(コーナー)の1つ。z成分が0でも±1でもない
  const up = upForDirection(d)
  assert.ok(Math.abs(up[0] - 0) > 1e-6 || Math.abs(up[1] - 0) > 1e-6, `斜め方向で up が +Z のまま(射影が効いていない): up=${up}`)
}

console.log('view cube: all assertions passed')
