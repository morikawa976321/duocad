/** `PATTERN_RECTANGULAR.build` の事前検査を評価する口(`tests/test_pattern_build_check.py` が node で走らせる)。失敗したら非ゼロで終了する。 */
import assert from 'node:assert/strict'

import './ts_resolve_hook.mjs'

const { PATTERN_RECTANGULAR } = await import(new URL('../web/src/commands/features.ts', import.meta.url).href)

const build = (dx, dy, nx, ny) => PATTERN_RECTANGULAR.build({ entities: ['e1'], dx, dy, nx, ny })

assert.equal(build(10, 0, 3, 1).ok, true)
assert.equal(build(0, 10, 1, 3).ok, true)
assert.equal(build(10, 10, 2, 2).ok, true)
assert.equal(build(0, 0, 1, 1).ok, false)
assert.equal(build(0, 0, 2, 2).ok, false)
assert.match(build(0, 10, 3, 2).reason, /X間隔が0/)
assert.match(build(10, 0, 2, 3).reason, /Y間隔が0/)
