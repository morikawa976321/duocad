/**
 * `web/src/frameLoop.ts`(描画ループ)を**実際に評価する**口。`tests/test_ts_evaluators.py` が `node` でこれを走らせる。
 *
 * 落ちたら、1フレームの処理が1回投げただけで次のフレームが予約されず、3Dビューが以後描かれなくなる
 * (アプリ側のリスナーや可変解像度の折り直しが投げると、画面が固まって見えて原因が遠い)。
 */
import assert from 'node:assert/strict'

import { startFrameLoop } from '../web/src/frameLoop.ts'

/** 手で回す requestAnimationFrame。 */
function fakeFrames() {
  let next = null
  let handle = 0
  const reports = []
  return {
    reports,
    deps: {
      request(callback) {
        next = { callback, handle: ++handle }
        return handle
      },
      cancel(h) {
        if (next !== null && next.handle === h) next = null
      },
      report(error) {
        reports.push(error)
      },
    },
    pending: () => next !== null,
    /** 予約されているフレームを1つ走らせる。 */
    run() {
      assert.ok(next !== null, 'フレームが予約されていない(ループが止まっている)')
      const { callback } = next
      next = null
      callback()
    },
  }
}

// 1回投げても次のフレームは予約され、投げた後の正常なフレームでまた処理が走る。
{
  const frames = fakeFrames()
  let calls = 0
  const loop = startFrameLoop(() => {
    calls += 1
    if (calls === 2) throw new Error('listener failed')
  }, frames.deps)
  frames.run()
  assert.equal(calls, 1)
  frames.run() // 2回目が投げる
  assert.equal(calls, 2)
  assert.ok(frames.pending(), '例外の後に次のフレームが予約されていない')
  assert.equal(frames.reports.length, 1)
  assert.equal(frames.reports[0].message, 'listener failed')
  frames.run()
  assert.equal(calls, 3, '例外の後のフレームで処理が再開しない')
  loop.stop()
}

// 投げ続けても報告は最初の1回(毎フレームの洪水にしない)。直ってからまた投げたら、もう一度報告する。
{
  const frames = fakeFrames()
  let calls = 0
  startFrameLoop(() => {
    calls += 1
    if (calls <= 3 || calls === 5) throw new Error(`failure ${calls}`)
  }, frames.deps)
  for (let i = 0; i < 5; i += 1) frames.run()
  assert.deepEqual(frames.reports.map((error) => error.message), ['failure 1', 'failure 5'])
}

// 止めたら予約を取り消し、以後は呼ばれない。
{
  const frames = fakeFrames()
  let calls = 0
  const loop = startFrameLoop(() => (calls += 1), frames.deps)
  frames.run()
  loop.stop()
  assert.ok(!frames.pending(), '止めたのに予約が残っている')
  assert.equal(calls, 1)
}

// step の中で止めても、取り消されるのは step の前に予約した次のフレーム。
{
  const frames = fakeFrames()
  let loop = null
  let calls = 0
  loop = startFrameLoop(() => {
    calls += 1
    loop.stop()
  }, frames.deps)
  frames.run()
  assert.ok(!frames.pending(), '処理の中で止めたのに次のフレームが残っている')
  assert.equal(calls, 1)
}

console.log('frameLoop.ts: 全ての想定を満たした')
