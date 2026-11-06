import assert from 'node:assert/strict'
import './ts_resolve_hook.mjs'

const { parseShifts } = await import('../web/src/commands/features.ts')
assert.deepEqual(parseShifts('０，１、－２'), [0, 1, -2])
assert.deepEqual(parseShifts('２＊３，８／２、（１＋２）'), [6, 4, 3])
assert.deepEqual(parseShifts('0, 1 -2'), [0, 1, -2])
assert.deepEqual(parseShifts(''), [])
for (const text of ['１．５，２', 'abc', '１／０', 'Infinity']) assert.equal(parseShifts(text), null, text)
