/**
 * `web/src/selectionInfoText.ts`(選択情報の見出しと行)と `web/src/measureText.ts` の書式を**実際に評価する**口。
 * `tests/test_ts_evaluators.py` が `node` でこれを走らせる。
 *
 * 落ちたら、次のどれか。
 * - 面・辺の種類が `other` のまま画面に出る。
 * - 取得失敗の文に例外クラス名が残る。
 * - 座標・Δ に単位 mm が付かない。
 * - 法線・向き(単位ベクトル)に長さの単位 mm が付く。
 */
import assert from 'node:assert/strict'

import './ts_resolve_hook.mjs'

const { selectionInfoErrorText, selectionInfoRows } = await import('../web/src/selectionInfoText.ts')
const { measureReadout } = await import('../web/src/measureText.ts')

const face = { kind: 'face', surface_kind: 'other', area: 1, normal: [0, 0, 1], center: [1, 2, 3] }
const kindOf = (item) => selectionInfoRows(item).find((row) => row.label === '種類').value
assert.equal(kindOf(face), '曲面(その他)')
assert.equal(kindOf({ kind: 'edge', edge_kind: 'other', length: 1, start: [0, 0, 0], end: [1, 0, 0] }), '曲線')
assert.equal(kindOf({ ...face, surface_kind: 'sphere' }), 'sphere', '辞書に無い種類は値のまま出す')

assert.equal(selectionInfoErrorText('ValueError: 面が見つからない'), '面が見つからない')
assert.equal(selectionInfoErrorText('面が見つからない'), '面が見つからない')
assert.equal(selectionInfoErrorText('KeyError: x: y'), 'x: y', '先頭の例外名だけを落とす')

assert.equal(selectionInfoRows({ kind: 'vertex', point: [1, 2, 3] })[0].value, '(1.000, 2.000, 3.000) mm')
const valueOf = (item, label) => selectionInfoRows(item).find((row) => row.label === label).value
const axis = { kind: 'datum', datum: 'X', point: [0, 0, 0], direction: [1, 0, 0] }
assert.equal(valueOf(axis, '向き'), '(1.000, 0.000, 0.000)')
assert.equal(valueOf(axis, '座標'), '(0.000, 0.000, 0.000) mm')
assert.equal(valueOf({ kind: 'datum', datum: 'XY', point: [0, 0, 0], normal: [0, 0, 1] }, '法線'), '(0.000, 0.000, 1.000)')
assert.equal(valueOf({ ...face, surface_kind: 'plane' }, '法線'), '(0.000, 0.000, 1.000)')
assert.equal(valueOf({ ...face, surface_kind: 'plane' }, '面積重心'), '(1.000, 2.000, 3.000) mm')
assert.match(
  measureReadout({ kind: 'point-point', distance: 5, angle_deg: null, delta: [3, 4, 0] }),
  /Δx 3\.000 mm {2}Δy 4\.000 mm {2}Δz 0\.000 mm/,
)

assert.doesNotMatch(measureReadout({ kind: 'point-point', distance: 5, angle_deg: null, delta: [3, 4, 0] }), /無限/)
for (const kind of ['point-line', 'point-plane', 'line-line', 'line-plane', 'plane-plane']) {
  assert.match(measureReadout({ kind, distance: 1, angle_deg: null, delta: null }), /\(無限に延長\): 距離/, kind)
}
assert.match(measureReadout({ kind: 'line-line', distance: null, angle_deg: 30, delta: null }), /^直線と直線\(無限に延長\): 角度 30\.000°/)
