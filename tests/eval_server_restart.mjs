/**
 * `web/src/serverRestart.svelte.ts`(安定版の立て直しの画面側)を、Svelte の `compileModule` で組み、fetch・時計・storage の偽物で
 * **実際に動かす**口。`tests/test_ts_evaluators.py` が `node` でこれを走らせる。
 *
 * 落ちたら、(1) 立て直しが3分を超えたとき全面表示に案内と閉じる口が出ない・閉じても立て直しが止まる・時間切れで状態が戻らない、
 * (2) 繋ぎ直したときのサーバの版の入れ替わり(安定版のときだけ。同じ版へ続けて読み直さない)の判定が崩れている。
 */
import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { stripTypeScriptTypes } from 'node:module'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'

import { compileModule } from '../web/node_modules/svelte/src/compiler/index.js'

const source = readFileSync(new URL('../web/src/serverRestart.svelte.ts', import.meta.url), 'utf8')
const runtime = new URL('../web/node_modules/svelte/src/internal/client/index.js', import.meta.url).href
const compiled = compileModule(stripTypeScriptTypes(source), { generate: 'client', filename: 'serverRestart.svelte.js' }).js.code
const dir = mkdtempSync(join(tmpdir(), 'duocad-eval-restart-'))
const file = join(dir, 'serverRestart.mjs')
writeFileSync(file, compiled.replaceAll("'svelte/internal/client'", JSON.stringify(runtime)))

// ---- 偽物: 仮想の時計(タイマーを時刻順に進める)・fetch・storage・location
let now = 0
let timers = []
const realSetTimeout = globalThis.setTimeout
Date.now = () => now
globalThis.setTimeout = (fn, ms) => {
  timers.push({ at: now + ms, fn })
  return timers.length
}
const settle = () => new Promise((resolve) => setImmediate(resolve))
/** 仮想の時計を `ms` 進める。タイマーは期限の順に1つずつ起こし、そのたびに非同期の続きを流す。 */
async function advance(ms) {
  const until = now + ms
  for (;;) {
    await settle()
    timers.sort((a, b) => a.at - b.at)
    const next = timers[0]
    if (!next || next.at > until) break
    timers.shift()
    now = Math.max(now, next.at)
    next.fn()
  }
  now = until
  await settle()
}

let server = { boot: 'a', channel: 'stable', version: { commit: 'aaa1111' } }
let serverDown = false
const posts = []
globalThis.fetch = async (url, options) => {
  if (options?.method === 'POST') {
    posts.push(String(url))
    return { status: 202, json: async () => ({}) }
  }
  if (serverDown) throw new Error('down')
  return { ok: true, json: async () => structuredClone(server) }
}
const storage = new Map()
globalThis.sessionStorage = {
  getItem: (key) => (storage.has(key) ? storage.get(key) : null),
  setItem: (key, value) => storage.set(key, value),
  removeItem: (key) => storage.delete(key),
}
let reloads = 0
globalThis.location = { href: 'http://127.0.0.1:8775/', reload: () => (reloads += 1) }

const { restart, restartServer, reloadIfServerReplaced, loadConfig, PENDING_KEY } = await import(pathToFileURL(file).href)
const fails = []
const shown = { title: '更新中…', detail: '', failPrefix: '更新を始められなかった' }

// ---- 立て直しの全面表示: 3分で案内と閉じる口、閉じても続く、15分で状態が戻る
const baseline = await loadConfig()
assert.equal(baseline.version.commit, 'aaa1111')
const running = restartServer('api/update', shown, (message) => fails.push(message))
await advance(1000)
assert.notEqual(restart.current, null)
assert.equal(restart.slow, false, '開始直後に案内が出ている')
await advance(2 * 60 * 1000)
assert.equal(restart.slow, false, '3分に満たないのに案内が出ている')
await advance(61 * 1000)
assert.equal(restart.slow, true, '3分を超えても案内が出ない')
assert.equal(restart.closed, false)
restart.close()
assert.equal(restart.closed, true)
assert.notEqual(restart.current, null, '閉じたら立て直しの状態まで消えた(更新ボタンが押せてしまう)')
await advance(5 * 60 * 1000)
assert.equal(reloads, 0, '立ち直っていないのに読み直した')
assert.notEqual(restart.current, null, '閉じたら立て直しの待ちが止まった')
assert.equal(storage.has(PENDING_KEY), true)
await advance(10 * 60 * 1000)
await running
assert.equal(restart.current, null)
assert.equal(restart.slow, false, '時間切れのあと案内の状態が残っている')
assert.equal(restart.closed, false, '時間切れのあと閉じた印が残っている')
assert.equal(storage.has(PENDING_KEY), false)
assert.equal(fails.length, 1)

// ---- 繋ぎ直したときの版の入れ替わり
reloads = 0
storage.clear()
await reloadIfServerReplaced()
assert.equal(reloads, 0, '同じ版なのに読み直した(boot だけが違う)')
server = { ...server, boot: 'c', version: { commit: 'bbb2222' } }
await reloadIfServerReplaced()
assert.equal(reloads, 1, '版が変わったのに読み直さない')
await reloadIfServerReplaced()
assert.equal(reloads, 1, '同じ版へ続けて2回読み直した')
server = { ...server, version: { commit: 'ccc3333' } }
await reloadIfServerReplaced()
assert.equal(reloads, 2, 'さらに別の版になっても読み直さない')
serverDown = true
await reloadIfServerReplaced()
assert.equal(reloads, 2, 'サーバの状態が取れないのに読み直した')
serverDown = false

// 開発版では、版が変わっても読み直さない(版は作業ツリーの HEAD で、コミットのたびに変わる)。
// 版の基準は最初に取れた設定を覚えているので、別のモジュールとして読み直して開発版を基準にする。
{
  server = { boot: 'd', channel: 'dev', version: { commit: 'ddd4444' } }
  const fresh = await import(pathToFileURL(file).href + '?dev')
  await fresh.loadConfig()
  server = { ...server, version: { commit: 'eee5555' } }
  await fresh.reloadIfServerReplaced()
  assert.equal(reloads, 2, '開発版なのに版の違いで読み直した')
}

// 立ち直ったら読み直す(閉じていても)。読み直しの後はこのモジュールの状態を使わない。
storage.clear()
reloads = 0
const second = restartServer('api/update', shown, (message) => fails.push(message))
await advance(4 * 60 * 1000)
assert.equal(restart.slow, true)
restart.close()
server = { ...server, boot: 'b' }
await advance(3000)
await second
assert.equal(reloads, 1, '立ち直ったのに読み直さない')

globalThis.setTimeout = realSetTimeout
console.log('serverRestart.svelte.ts: 全ての想定を満たした')
