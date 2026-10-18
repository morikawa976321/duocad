/**
 * `web/src/pixelRatio.ts`(描画バッファの画素比)を**実際に評価する**口。`tests/test_ts_evaluators.py` が `node` でこれを走らせる。
 *
 * 落ちたら、ブラウザのズーム・別モニタへの移動で `devicePixelRatio` が変わっても描画バッファの解像度が追従しない
 * (100%で開いて拡大するとぼやけ、高DPIから等倍へ移るとバッファを無駄に持つ)。
 */
import assert from 'node:assert/strict'

import { cappedPixelRatio, watchPixelRatio } from '../web/src/pixelRatio.ts'

assert.equal(cappedPixelRatio(1), 1)
assert.equal(cappedPixelRatio(1.5), 1.5)
assert.equal(cappedPixelRatio(3), 2, '3倍以上はバッファが大きくなるだけ')
assert.equal(cappedPixelRatio(undefined), 1)
assert.equal(cappedPixelRatio(0), 1)

/** `matchMedia` の最小の偽物。クエリごとの 'change' リスナーを持ち、`fire(query)` で一致が外れたことにする。 */
function fakeWindow(initial) {
  const listeners = new Map()
  const win = {
    devicePixelRatio: initial,
    queries: [],
    matchMedia(query) {
      win.queries.push(query)
      const entry = { query, set: new Set() }
      listeners.set(query, entry)
      return {
        addEventListener: (_type, listener) => entry.set.add(listener),
        removeEventListener: (_type, listener) => entry.set.delete(listener),
      }
    },
    /** いまの `devicePixelRatio` のクエリに張られているリスナーの数。 */
    watching: () => listeners.get(`(resolution: ${win.devicePixelRatio}dppx)`)?.set.size ?? 0,
    /** 値が変わった(ブラウザは古い値のクエリの一致が外れたことを知らせる)。 */
    change(next) {
      const old = listeners.get(`(resolution: ${win.devicePixelRatio}dppx)`)
      win.devicePixelRatio = next
      for (const listener of [...old.set]) listener()
    },
  }
  return win
}

{
  const win = fakeWindow(1)
  let calls = 0
  const stop = watchPixelRatio(win, () => (calls += 1))
  assert.deepEqual(win.queries, ['(resolution: 1dppx)'])
  assert.equal(win.watching(), 1)

  win.change(2) // ズームや別モニタ
  assert.equal(calls, 1, '画素比が変わったのに知らせない')
  assert.equal(win.queries.at(-1), '(resolution: 2dppx)', '新しい値で張り直していない(次の変化に気づけない)')
  assert.equal(win.watching(), 1)

  win.change(1.25) // もう一度変わっても気づく
  assert.equal(calls, 2)

  stop()
  assert.equal(win.watching(), 0, '止めたのにリスナーが残っている')
}

console.log('pixelRatio.ts: 全ての想定を満たした')
