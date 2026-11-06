import assert from 'node:assert/strict'
import './ts_resolve_hook.mjs'

const { parseShifts } = await import('../web/src/commands/features.ts')
assert.deepEqual(parseShifts('０，１、－２'), [0, 1, -2])
assert.deepEqual(parseShifts('２＊３，８／２、（１＋２）'), [6, 4, 3])
assert.deepEqual(parseShifts('0, 1 -2'), [0, 1, -2])
assert.deepEqual(parseShifts('(1 + 2),0'), [3, 0])
assert.deepEqual(parseShifts('atan2(0,1),0'), [0, 0])
assert.deepEqual(parseShifts(' （１ ＋ （２＊３）），atan2(0, 1)、-2 '), [7, 0, -2])
assert.deepEqual(parseShifts('0\t1\n-2'), [0, 1, -2])
assert.deepEqual(parseShifts(' ,，、 '), [])
assert.deepEqual(parseShifts(''), [])
for (const text of ['１．５，２', 'abc', '１／０', 'Infinity', '(1 + 2', '1),0', 'atan2(1,2),0', '(1 / 0),0']) assert.equal(parseShifts(text), null, text)
