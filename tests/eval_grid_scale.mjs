/**
 * `web/src/gridScale.ts` の純関数(`snap125` / `extentFor` / `stepFor`)を**実際に評価する**
 * ための小さな口。`tests/test_grid_scale.py` が `node` でこれを走らせる
 * (`tests/eval_adaptive_resolution.mjs` と同じやり方 —— ソースの文字列比較ではなく実行結果で
 * 見る)。
 *
 * このロジックには**Python側の対応物が無い**(基準グリッドはTSにしか描かれない)ので、
 * 2言語を突き合わせる代わりに、ここで直接 assert して失敗したら非ゼロで終了する。
 *
 * Node は `.ts` をそのまま読める(型注釈の除去。Node 23.6以降は既定で有効)。
 */
import assert from 'node:assert/strict'
import { BASE_EXTENT, GRID_DIVISIONS, extentFor, snap125, stepFor } from '../web/src/gridScale.ts'

// -------------------------------------------------------------- snap125: 1-2-5 の桁上げ
assert.equal(snap125(1), 1)
assert.equal(snap125(1.5), 2)
assert.equal(snap125(2), 2)
assert.equal(snap125(3), 5)
assert.equal(snap125(5), 5)
assert.equal(snap125(7), 10)
assert.equal(snap125(10), 10)
assert.equal(snap125(0.03), 0.05) // 1未満でも同じ規則で丸まる
assert.equal(snap125(999), 1000)
assert.equal(snap125(302.4), 500) // タスクの完了条件に書かれた例そのもの

// -------------------------------------------------------------- extentFor: 空の文書
const emptyBox = { isEmpty: () => true, min: { x: 0, y: 0, z: 0 }, max: { x: 0, y: 0, z: 0 } }
assert.equal(extentFor(emptyBox), BASE_EXTENT, '空の文書は現状と同じ ±100mm のはず')

// -------------------------------------------------------------- extentFor: 完了条件の例
// 「半幅252mm → extent 500」(box の |min.x| が252)。
const box252 = {
  isEmpty: () => false,
  min: { x: -252, y: -10, z: -5 },
  max: { x: 10, y: 5, z: 5 },
}
assert.equal(extentFor(box252), 500)

// -------------------------------------------------------------- extentFor: 下限(BASE_EXTENT)
const tinyBox = { isEmpty: () => false, min: { x: -1, y: -1, z: -1 }, max: { x: 1, y: 1, z: 1 } }
assert.equal(extentFor(tinyBox), BASE_EXTENT, '小さい内容でも空の文書より狭くならない')

// -------------------------------------------------------------- extentFor: 対角ではなく最遠成分
// 1軸だけ突出した非対称な箱。対角(≈1000.003)ではなく「最遠成分(1000)」から丸まることを見る。
const farAlongOneAxis = {
  isEmpty: () => false,
  min: { x: -1, y: -1, z: -1 },
  max: { x: 1000, y: 1, z: 1 },
}
assert.equal(extentFor(farAlongOneAxis), 2000) // 1000*1.2=1200 → snap125 → 2000

// -------------------------------------------------------------- extentFor: 原点中心のまま
// 原点をまたがない(原点から離れた)箱でも、原点からの最遠成分だけで半幅を決める
// (モデルをグリッド側へ寄せる追従はしない=原点中心のまま広げるだけ、が本タスクの範囲)。
const farFromOrigin = {
  isEmpty: () => false,
  min: { x: 400, y: 400, z: 0 },
  max: { x: 500, y: 500, z: 10 },
}
assert.equal(extentFor(farFromOrigin), 1000) // reach=500 → *1.2=600 → snap125 → 1000

// -------------------------------------------------------------- stepFor: 分割数がほぼ一定
for (const extent of [BASE_EXTENT, 200, 500, 1000, 2000, 5000, 10_000]) {
  const step = stepFor(extent)
  assert.equal(extent / step, GRID_DIVISIONS, `extent=${extent} で分割数が GRID_DIVISIONS からずれた`)
}
assert.equal(stepFor(BASE_EXTENT), 10, '空の文書では現状と同じ10mm刻みのはず')

console.log('grid scale: all assertions passed')
