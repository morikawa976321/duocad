/**
 * `web/src/interferenceGroups.ts`(干渉チェックの結果を画面の形へ直す純関数)と `web/src/commands/interference.ts` の `parseClearance` を**実際に評価する**口。
 * `tests/test_ts_evaluators.py` が `node` でこれを走らせる。
 *
 * 落ちたら、次のどれか。
 * - 見出しが4つ(干渉・隙間不足・未確定・接触)でない・接触が開いている・微小な重なりが接触の外に出る。
 * - 隙間不足に触れている組が混ざる(触れている組は各見出しの中の印)・距離が測れなかった組が一覧から消える。
 * - 古い応答・版の違う結果・切れた接続の後の結果を描いてしまう。
 */
import assert from 'node:assert/strict'

import { parseClearance } from '../web/src/commands/interference.ts'
import { acceptResult, groupPairs, pairKey, summaryLines, visualFor } from '../web/src/interferenceGroups.ts'

const pair = (a, b, extra) => ({ a, b, a_name: a.toUpperCase(), b_name: b.toUpperCase(), warnings: [], ...extra })
const mesh = { positions: [0, 0, 0, 1, 0, 0, 0, 1, 0], indices: [0, 1, 2] }

// ---- 隙間の欄
assert.deepEqual(parseClearance(''), { ok: true, value: 0 })
assert.deepEqual(parseClearance(' 0.2 '), { ok: true, value: 0.2 })
for (const bad of ['-1', 'abc', 'NaN', 'Infinity']) assert.equal(parseClearance(bad).ok, false, bad)

// ---- 見出し
const pairs = [
  pair('a', 'b', { relation: 'overlap', volume: 25, clearance: 'short', render: { solid: mesh } }),
  pair('c', 'd', { relation: 'overlap', volume: 1270.35, render: { solid: mesh } }),
  pair('e', 'f', { relation: 'tiny_overlap', volume: 0.01, clearance: 'short' }),
  pair('g', 'h', { relation: 'contact', contact: 'face', area: 99.8, clearance: 'short' }),
  pair('i', 'j', { relation: 'separated', distance: 0.0999, points: [[0, 0, 0], [0.0999, 0, 0]], clearance: 'short' }),
  pair('k', 'l', { relation: 'separated', distance: 0.2, clearance: 'at' }),
  pair('m', 'n', { relation: 'separated', clearance: null, clearance_reason: '最短距離が打ち切られた' }),
  pair('o', 'p', { relation: 'unknown', reason: '30 秒で打ち切った' }),
]
const groups = groupPairs(pairs)
assert.deepEqual(groups.map((g) => g.heading), ['干渉', '隙間不足', '未確定', '接触'])
assert.deepEqual(groups.map((g) => g.open), [true, true, true, false], '干渉・隙間不足・未確定は開き、接触は畳む')
const rows = (key) => groups.find((g) => g.key === key).rows
assert.deepEqual(rows('overlap').map((r) => r.key), ['c|d', 'a|b'], '体積の大きい順')
assert.deepEqual(rows('short').map((r) => r.key), ['i|j'], '隙間不足には離れている組だけ')
assert.deepEqual(rows('unknown').map((r) => r.key).sort(), ['m|n', 'o|p'], '距離が測れなかった組も未確定に出す')
assert.deepEqual(rows('contact').map((r) => r.key), ['g|h', 'e|f'], '接触が先、微小な重なりは接触の中の印付きの行')
assert.ok(rows('contact')[1].marks.includes('微小な重なり'))
assert.ok(rows('contact')[0].marks.includes('隙間不足'), '触れている組の隙間不足は行の印')
assert.ok(rows('overlap')[1].marks.includes('隙間不足'))
assert.ok(!groups.flatMap((g) => g.rows).some((r) => r.key === 'k|l'), '要求どおり(at)の組は一覧に出さない')
assert.equal(rows('overlap')[0].detail, '体積 1270 mm³')
assert.equal(rows('contact')[0].detail, '面接触 99.8 mm²')
assert.equal(rows('short')[0].detail, '距離 0.0999 mm')
assert.equal(groupPairs([]).length, 4, '0 件でも見出しは4つ(調べたが無かったと読める)')

// ---- 要約
const summary = summaryLines({
  client_op: 1, clearance: 0.2, complete: false, not_started: 3,
  target: { mode: 'one_vs_rest', selected: ['bolt'], bodies: [], excluded_hidden: 2, pairs: 7 },
  counts: { short: 4, at: 1, ok: 2 },
})
assert.ok(summary[0].includes('bolt') && summary[0].includes('7 組') && summary[0].includes('隠した 2 個'))
assert.ok(summary.some((line) => line.includes('不足 4') && line.includes('要求どおり 1')))
assert.ok(summary.some((line) => line.includes('未着手 3')))

// ---- 結果の寿命
const live = { currentOp: 5, revision: '1.2.3', connected: true }
assert.equal(acceptResult({ client_op: 5, document_revision: '1.2.3' }, live), 'draw')
assert.equal(acceptResult({ client_op: 4, document_revision: '1.2.3' }, live), 'stale_request', '続けて起動したときの古い応答')
assert.equal(acceptResult({ client_op: 5, document_revision: '1.2.3' }, { ...live, currentOp: null }), 'stale_request', '取り消した後の応答')
assert.equal(acceptResult({ client_op: 5, document_revision: '1.2.2' }, live), 'stale_revision', '検査の間に版が変わった')
assert.equal(acceptResult({ client_op: 5, document_revision: '1.2.3' }, { ...live, revision: null }), 'stale_revision', '文書が閉じられた')
assert.equal(acceptResult({ client_op: 5, document_revision: '1.2.3' }, { ...live, connected: false }), 'disconnected')

// ---- ビューポートの層へ渡すもの
const payload = { client_op: 1, pairs }
assert.equal(visualFor(null, null), null)
assert.equal(visualFor(payload, null).solids.length, 2, '選んでいなければ重なりの立体を全部')
assert.equal(visualFor(payload, null).distance, null)
const picked = visualFor(payload, 'i|j')
assert.equal(picked.solids.length, 0)
assert.deepEqual(picked.distance, { from: [0, 0, 0], to: [0.0999, 0, 0], label: '0.0999 mm' })
assert.equal(visualFor(payload, 'a|b').solids.length, 1, '行を選ぶとその組の立体だけ')
assert.equal(pairKey(pairs[0]), 'a|b')

console.log('ok')
