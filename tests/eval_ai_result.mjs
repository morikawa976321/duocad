/**
 * `web/src/aiResult.ts`(AIパネルが `ai_result` を文字にする純関数)を**実際に評価する**口。
 * `tests/test_ts_evaluators.py` が `node` でこれを走らせる。
 *
 * 落ちたら、AIが失敗した理由(`ai_result.detail`)が画面に出ない(provider_error は固定文の「接続に失敗した」だけで、
 * API キー無効・URL 違い・通信断の区別が付かない)か、理由が無い固定文で言い切っている。
 */
import assert from 'node:assert/strict'

import './ts_resolve_hook.mjs'

const { inputBlockedReason, resultDetail, resultLine } = await import('../web/src/aiResult.ts')

// 接続の失敗: 固定文に理由(接続できなかったURLとOSのエラー)を添える。
const providerError = { status: 'provider_error', text: '', detail: 'HTTP 401: invalid x-api-key' }
assert.equal(resultLine(providerError), '接続に失敗した')
assert.equal(resultDetail(providerError), 'HTTP 401: invalid x-api-key')

// 止まった: 「理由不明」と言い切らない。生の停止理由を添える。
const stopped = { status: 'stopped', text: '', detail: "stop_reason='pause_turn'" }
assert.ok(!resultLine(stopped).includes('理由不明'), '理由があるのに理由不明と言っている')
assert.equal(resultDetail(stopped), "stop_reason='pause_turn'")

// 文脈長の超過も数値が detail にある。
assert.equal(resultDetail({ status: 'context_overflow', text: '', detail: 'n_ctx 4096 を超えた' }), 'n_ctx 4096 を超えた')

// 本文があればそれを出す(固定文で上書きしない)。
assert.equal(resultLine({ status: 'completed', text: 'できた', detail: '' }), 'できた')
assert.equal(resultLine({ status: 'refusal', text: 'お断りします', detail: "stop_reason='refusal'" }), 'お断りします')

// 固定文と同じ内容の detail は足さない(中断・時間切れ)。正常終了にも出さない。
assert.equal(resultDetail({ status: 'cancelled', text: '', detail: '中断フラグが立った' }), '')
assert.equal(resultDetail({ status: 'completed', text: 'ok', detail: '' }), '')
assert.equal(resultDetail({ status: 'provider_error', text: '', detail: '' }), '', '理由が空なら行ごと出さない')

// 長い理由は折りたたむ(会話欄を押し流さない)。改行・連続空白は1つの空白に。
const long = resultDetail({ status: 'provider_error', text: '', detail: `a\n\n  b ${'x'.repeat(1000)}` })
assert.ok(long.startsWith('a b x'), long.slice(0, 20))
assert.ok(long.length <= 241 && long.endsWith('…'), `折りたたまれていない: ${long.length}`)

// 知らない状態は状態名をそのまま出す(空行にしない)。
assert.equal(resultLine({ status: 'brand_new', text: '', detail: '' }), 'brand_new')

// 入力を止める理由: 切断中は(ai_status が残っていても)送らせない。送れなかった発話が会話欄に残り、入力欄だけ空になる。
assert.equal(inputBlockedReason('open', true), null)
assert.equal(inputBlockedReason('open', false), '接続を待っている')
for (const connection of ['connecting', 'closed']) {
  assert.ok(inputBlockedReason(connection, true) !== null, `${connection} の間は送れないはず`)
  assert.ok(inputBlockedReason(connection, false) !== null)
}

console.log('aiResult.ts: 全ての想定を満たした')
