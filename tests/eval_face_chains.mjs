/** 面の連なりの覚え(`web/src/faceChains.ts`)。落ちたら、継ぎ面を共有する丸めの連なりが応答の順で入れ替わるか、丸めを1本外すと残す丸めの継ぎ面まで外れるか、代案で選択が広がり続ける。 */
import assert from 'node:assert/strict'
import './ts_resolve_hook.mjs'
import { createFaceChains, suggestedAdditions } from '../web/src/faceChains.ts'

const sent = []
let revision = 1
const chains = createFaceChains({ shapeRevisionOf: () => revision, send: (body, faces) => sent.push([body, faces]) })

// 62 と 64 は継ぎ面 63 を共有する。どちらを先に覚えても、起点の面の連なりが返る
chains.remember({ body_id: 'b', revision: 1, shape_revision: 1, origins: [62], chains: [[62, 63, 71]] })
chains.remember({ body_id: 'b', revision: 1, shape_revision: 1, origins: [64, 63], chains: [[63, 64, 65], [63]] })
assert.deepEqual(chains.chainOf('b', 62), [62, 63, 71])
assert.deepEqual(chains.chainOf('b', 64), [63, 64, 65])
assert.deepEqual(chains.chainOf('b', 63), [63])
assert.equal(chains.chainOf('b', 71), null) // 頼んでいない面は分からない

// 62 と 64 をクリックして選び、64 を外すとき、選択に残る 62 の連なりが使う継ぎ面 63 は残す
chains.picked('b', 62, true)
chains.picked('b', 64, true)
assert.deepEqual([...chains.sharedWith('b', 64, [62, 63, 71, 64, 65])], [63])
// 残す面は本人がクリックした起点だけで決まり、ほかの面の連なりを照会したかどうかで変わらない(63 は照会済みだが起点ではない)
chains.picked('b', 62, false)
assert.deepEqual([...chains.sharedWith('b', 64, [62, 63, 71, 64, 65])], [])
// 起点が選択から外れていれば残さない
chains.picked('b', 62, true)
assert.deepEqual([...chains.sharedWith('b', 64, [63, 64, 65])], [])
chains.forgetPicks()
assert.deepEqual([...chains.sharedWith('b', 64, [62, 63, 71, 64, 65])], [])

// 上面4辺の丸めのように、どの面からでも同じ連なりが返る。1 をクリックして選び、同じ連なりの 2 をクリックして外すとき、何も残さない
chains.remember({ body_id: 'b', revision: 2, shape_revision: 2, origins: [1, 2], chains: [[1, 2, 3, 4], [1, 2, 3, 4]] })
chains.picked('b', 1, true)
assert.deepEqual([...chains.sharedWith('b', 2, [1, 2, 3, 4])], [])
chains.forgetPicks()

// 形が変わったら古い覚えは使わない
revision = 2
assert.equal(chains.chainOf('b', 62), null)
assert.equal(chains.remember({ body_id: 'b', revision: 1, shape_revision: 1, origins: [62], chains: [[62]] }), false)

// 頼んだ面は同じ形では2度頼まない
chains.request('b', [62, 64])
chains.request('b', [62])
assert.deepEqual(sent, [['b', [62, 64]]])

// 代案: 本人の選択を含み、それより広いときだけ足す。足した選択のままなら、次の代案では広げない
const face = (index, body = 'b') => ({ body, kind: 'face', index })
const offer = { body_id: 'b', faces: [46, 58, 71] }
const wider = suggestedAdditions(offer, [face(58), face(46)], null)
assert.deepEqual(wider, { add: [71], key: 'b:46,58,71' })
assert.equal(suggestedAdditions({ body_id: 'b', faces: [45, 46, 58, 71] }, [face(71), face(58), face(46)], wider.key), null)
assert.equal(suggestedAdditions(offer, [face(58), face(12)], null), null) // 代案に無い面を選んでいる
assert.equal(suggestedAdditions(offer, [face(58, 'other')], null), null) // 別のボディ
assert.equal(suggestedAdditions(offer, [face(46), face(58), face(71)], null), null) // 足すものが無い

console.log('OK: face chains')
