/**
 * `web/src/session.ts` の再接続を、WebSocket・fetch・タイマーの偽物で**実際に動かす**口。
 * `tests/test_ts_evaluators.py` が `node` でこれを走らせる。
 *
 * 落ちたら、(1) 切断のあと窓が前面に戻った・回線が戻ったときに次の再試行を待たず繋ぎ直す、(2) 繋がっている・繋いでいる途中は
 * 何もしない、(3) 繋ぎ直ったとき知らせを出した切断だけを `onConnectionRestored` で言う、のどれかが崩れている。
 */
import assert from 'node:assert/strict'

import './ts_resolve_hook.mjs'

const sockets = []
class FakeWebSocket {
  static OPEN = 1
  constructor(url) {
    this.url = url
    this.readyState = 0
    sockets.push(this)
  }
  close() {}
  send() {}
}
globalThis.WebSocket = FakeWebSocket
globalThis.location = { href: 'http://127.0.0.1:8765/', hostname: '127.0.0.1' }
globalThis.fetch = async () => ({ ok: true, json: async () => ({ ws_url: 'ws://127.0.0.1:8765' }) })
globalThis.document = Object.assign(new EventTarget(), { visibilityState: 'visible' })
globalThis.window = new EventTarget()

/** 再試行の待ち(`setTimeout`)の偽物。時間を進めず、残っているかと取り消されたかだけを見る。 */
const timers = []
globalThis.setTimeout = (fn, ms) => {
  const timer = { fn, ms, cancelled: false }
  timers.push(timer)
  return timer
}
globalThis.clearTimeout = (timer) => {
  if (timer) timer.cancelled = true
}
const pendingTimers = () => timers.filter((timer) => !timer.cancelled).length

const { createSession } = await import('../web/src/session.ts')

const calls = []
const hooks = new Proxy(
  {},
  {
    get: (_target, name) => (...args) => {
      if (name === 'onError' || name === 'onReconnected' || name === 'onConnectionRestored') calls.push([name, ...args])
    },
  },
)
const flush = () => new Promise((resolve) => setImmediate(resolve))
const names = () => calls.map((call) => call[0])

const session = createSession(hooks)
session.start()
await flush()
assert.equal(sockets.length, 1)

// 繋がっている間は、前面に戻っても何もしない。最初の接続は「再接続」ではない。
sockets[0].onopen()
assert.deepEqual(names(), [], '最初の接続で再接続のフックが呼ばれた')
document.dispatchEvent(new Event('visibilitychange'))
await flush()
assert.equal(sockets.length, 1, '繋がっているのに繋ぎ直した')

// 切断 → 再試行の待ちの最中に裏のままなら動かず、前面に戻ったら待たず繋ぐ。待ちの予約は取り消す。
sockets[0].onclose()
assert.equal(pendingTimers(), 1)
document.visibilityState = 'hidden'
document.dispatchEvent(new Event('visibilitychange'))
await flush()
assert.equal(sockets.length, 1, '裏にあるまま繋ぎ直した')
document.visibilityState = 'visible'
document.dispatchEvent(new Event('visibilitychange'))
await flush()
assert.equal(sockets.length, 2, '前面に戻っても待たずに繋ぎ直さない')
assert.equal(pendingTimers(), 0, '繋ぎ直したのに再試行の予約が残っている')

// 繋いでいる途中の合図では、二重に繋がない。
document.dispatchEvent(new Event('visibilitychange'))
window.dispatchEvent(new Event('online'))
await flush()
assert.equal(sockets.length, 2, '繋いでいる途中に二重に繋いだ')

// 知らせを出さずに済んだ切断: 繋ぎ直ったら再接続のフックだけ(知らせの取り下げは呼ばない)。
sockets[1].onopen()
assert.deepEqual(names(), ['onReconnected'])

// 繋ぎ直しの失敗は1回目で知らせる。回線が戻った合図で繋ぎ直し、開いたら知らせの文言つきで取り下げを頼む。
calls.length = 0
sockets[1].onclose()
window.dispatchEvent(new Event('online'))
await flush()
assert.equal(sockets.length, 3, '回線が戻っても待たずに繋ぎ直さない')
sockets[2].onclose()
const notice = calls.find((call) => call[0] === 'onError')
assert.ok(notice, '繋ぎ直しの失敗が知らされない')
window.dispatchEvent(new Event('online'))
await flush()
assert.equal(sockets.length, 4)
sockets[3].onopen()
assert.deepEqual(calls.slice(1), [['onConnectionRestored', notice[1]], ['onReconnected']])

// 取り下げは1回だけ(次の切断が静かに済めば、また呼ばない)。
calls.length = 0
sockets[3].onclose()
document.dispatchEvent(new Event('visibilitychange'))
await flush()
sockets[4].onopen()
assert.deepEqual(names(), ['onReconnected'])

// dispose の後は合図に反応しない。
sockets[4].onclose()
session.dispose()
document.dispatchEvent(new Event('visibilitychange'))
window.dispatchEvent(new Event('online'))
await flush()
assert.equal(sockets.length, 5, 'dispose の後に繋ぎ直した')

console.log('session.ts(再接続): 全ての想定を満たした')
