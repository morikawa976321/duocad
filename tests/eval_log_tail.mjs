/**
 * `web/src/logTail.ts`(状態配信の操作ログの前後比較)を実際に評価する口。`tests/test_ts_evaluators.py` が走らせる。
 *
 * 落ちたら、外(AI か別の画面)の操作で人のコマンドが畳まれたとき、確定されたのか別の操作で畳まれただけなのかを取り違える。
 */
import assert from 'node:assert/strict'

import './ts_resolve_hook.mjs'

const { appendedEntries } = await import('../web/src/logTail.ts')

const entry = (seq, verb, args = {}) => ({ seq, verb, args })
const sketch = entry(1, 'sketch_create', { plane: 'XY' })
const fillet = entry(2, 'fillet', { radius: 1 })

// 積んだ分だけを返す。
assert.deepEqual(appendedEntries([sketch], [sketch, fillet]), [fillet])
assert.deepEqual(appendedEntries([sketch, fillet], [sketch, fillet]), [])

// undo で外れただけなら何も積んでいない。
assert.deepEqual(appendedEntries([sketch, fillet], [sketch]), [])

// undo の後に積むと同じ seq が別の操作に振られる。共通の先頭は中身で比べる。
const chamfer = entry(2, 'chamfer', { distance: 1 })
assert.deepEqual(appendedEntries([sketch, fillet], [sketch, chamfer]), [chamfer])
const otherFillet = entry(2, 'fillet', { radius: 2 })
assert.deepEqual(appendedEntries([sketch, fillet], [sketch, otherFillet]), [otherFillet])

console.log('logTail.ts: 全ての想定を満たした')
