/**
 * `web/src/session.ts` の `createSession` を、WebSocket と fetch の偽物で**実際に動かす**口。
 * `tests/test_ts_evaluators.py` が `node` でこれを走らせる。
 *
 * 落ちたら、接続先の設定取得(`/duocad-config.json`)の待ち中に `dispose()` が呼ばれたとき、dispose の後に WebSocket が作られ、
 * 誰にも閉じられないまま `onStatus('open')` などのフックを呼び続ける(開発時の HMR 再マウントで踏む)。
 */
import assert from 'node:assert/strict'

import './ts_resolve_hook.mjs'

const sockets = []
class FakeWebSocket {
  static OPEN = 1
  constructor(url) {
    this.url = url
    this.readyState = 0
    this.closed = false
    sockets.push(this)
  }
  close() {
    this.closed = true
  }
  send() {}
}
globalThis.WebSocket = FakeWebSocket
globalThis.location = { href: 'http://127.0.0.1:8765/', hostname: '127.0.0.1' }

let resolveFetch = null
globalThis.fetch = () => new Promise((resolve) => (resolveFetch = resolve))
const config = { ok: true, json: async () => ({ ws_url: 'ws://127.0.0.1:8765' }) }

const { createSession } = await import('../web/src/session.ts')

/** 全フックが何もしない。`onStatus` だけ記録する。 */
function hooksWithStatus(statuses) {
  return new Proxy({}, { get: (_target, name) => (name === 'onStatus' ? (status) => statuses.push(status) : () => {}) })
}
const flush = () => new Promise((resolve) => setTimeout(resolve, 0))

// 設定取得の待ち中に dispose: WebSocket を作らない。
{
  const statuses = []
  const session = createSession(hooksWithStatus(statuses))
  session.start()
  assert.deepEqual(statuses, ['connecting'])
  session.dispose()
  resolveFetch(config)
  await flush()
  assert.equal(sockets.length, 0, `dispose の後に WebSocket が作られた: ${sockets.map((s) => s.url)}`)
  assert.deepEqual(statuses, ['connecting'], 'dispose の後にフックが呼ばれた')
}

// 対照: 破棄しなければ、取得できたあとに WebSocket を作る。
{
  const session = createSession(hooksWithStatus([]))
  session.start()
  await flush()
  assert.equal(sockets.length, 1, '破棄していないのに WebSocket が作られない')
  assert.equal(sockets[0].url, 'ws://127.0.0.1:8765')
  session.dispose()
  assert.equal(sockets[0].closed, true, 'dispose が開いているソケットを閉じない')
}

console.log('session.ts(dispose): 全ての想定を満たした')
