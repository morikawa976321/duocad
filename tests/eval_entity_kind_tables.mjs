/** Python側の表との比較用に、実際のTSの図形種別・点のキーを返す。 */
import assert from 'node:assert/strict'
import './ts_resolve_hook.mjs'

const { entityPointKeys } = await import('../web/src/entityKinds.ts')
const keys = entityPointKeys()
const expected = structuredClone(keys)
keys.line[0][0] = '__modified__'
keys.line.push(['extraX', 'extraY'])
delete keys.circle
assert.deepEqual(entityPointKeys(), expected, '返り値の書き換えが内部の点の表を変えた')
process.stdout.write(JSON.stringify(expected))
