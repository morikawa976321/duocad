/** プレビューの競合で、古い幽霊の採用・余分な送信・終了後の発火が起きたら落ちる。 */
import assert from 'node:assert/strict'
import './ts_resolve_hook.mjs'
import { createPreviewController, initialPreviewState, transitionPreview } from '../web/src/previewController.ts'

const input = (radius = 2, selection = 'edge:a:1', mode = 'server', verb = 'fillet') => ({
  verb, args: { radius }, selection, mode,
})
const response = (id, bodies = [{}], ghost = null) => ({
  preview_id: id, verb: 'fillet', bodies, sketch_ghost: ghost,
})
let cases = 0
function check(name, run) {
  run()
  cases += 1
  console.log(`OK: ${name}`)
}
function machine() {
  let state = initialPreviewState()
  const effects = []
  return {
    get state() { return state },
    effects,
    event(event) {
      Object.freeze(state)
      const next = transitionPreview(state, event)
      state = next.state
      effects.push(...next.effects)
      return next.effects
    },
    update(value, now = 0) { return this.event({ type: 'input', input: value, now }) },
    fire(kind, now = this.state[kind].at) {
      return this.event({ type: 'timer', kind, token: this.state[kind].token, now })
    },
    receive(id) { return this.event({ type: 'response', payload: response(id) }) },
    sent() { return effects.filter((effect) => effect.type === 'send').map(({ input, id }) => [input.verb, input.args, id]) },
  }
}

check('ローカルは即時描画し、同じ入力は送信も描き直しもしない', () => {
  const m = machine()
  assert.equal(m.update(input(2, '', 'local', 'move_body'))[0].type, 'local')
  assert.deepEqual(m.update(input(2, '', 'local', 'move_body'), 500), [])
  assert.equal(m.state.debounce, null)
  assert.deepEqual(m.sent(), [])
  assert.deepEqual(m.receive(null), [])
})

check('打ち終わり250ms後の1回だけを送り、750msの上限で途中の値を送る', () => {
  const m = machine()
  m.update(input(1), 0)
  m.update(input(2), 200)
  m.update(input(3), 400)
  m.update(input(4), 600)
  assert.equal(m.state.debounce.at, 750)
  assert.deepEqual(m.sent(), [])
  m.fire('debounce', 750)
  assert.deepEqual(m.sent(), [['fillet', { radius: 4 }, 1]])
  m.update(input(5), 800)
  assert.equal(m.state.debounce.at, 1050)
  m.fire('debounce')
  assert.deepEqual(m.sent(), [['fillet', { radius: 4 }, 1], ['fillet', { radius: 5 }, 2]])
  assert.deepEqual(m.update(input(5), 2000), [])
})

check('取り消し済みタイマーでは送らない', () => {
  const m = machine()
  m.update(input(), 0)
  const old = m.state.debounce.token
  assert.deepEqual(m.sent(), [])
  m.update(input(3), 249)
  assert.deepEqual(m.event({ type: 'timer', kind: 'debounce', token: old, now: 500 }), [])
  m.fire('debounce')
  assert.deepEqual(m.sent(), [['fillet', { radius: 3 }, 1]])
})

check('打ち直しのデバウンス中と送信後に前の応答を採用しない', () => {
  const m = machine()
  m.update(input())
  m.fire('debounce')
  assert.equal(m.receive(1)[0].type, 'render')
  m.update(input(3), 300)
  assert.deepEqual(m.receive(1), [])
  m.fire('debounce')
  assert.deepEqual(m.receive(1), [])
  assert.equal(m.receive(2)[0].type, 'render')
})

check('0ms半径2→250ms要求1→300ms半径3→350ms要求1応答は描かず、550msに要求2を送る', () => {
  const m = machine()
  m.update(input(2), 0)
  m.fire('debounce', 250)
  m.update(input(3), 300)
  assert.deepEqual(m.event({ type: 'response', payload: response(1), now: 350 }), [])
  assert.equal(m.state.debounce.at, 550)
  assert.deepEqual(m.sent(), [['fillet', { radius: 2 }, 1]])
  m.fire('debounce', 550)
  assert.deepEqual(m.sent(), [['fillet', { radius: 2 }, 1], ['fillet', { radius: 3 }, 2]])
  assert.equal(m.receive(2)[0].type, 'render')
})

check('引数が同じでも選択が変われば1回送り、旧選択の遅延応答は捨てる', () => {
  const m = machine()
  m.update(input())
  m.fire('debounce')
  m.update(input(2, 'edge:a:1|edge:a:2'), 300)
  assert.deepEqual(m.receive(1), [])
  m.fire('debounce')
  assert.deepEqual(m.sent(), [['fillet', { radius: 2 }, 1], ['fillet', { radius: 2 }, 2]])
  assert.equal(m.receive(2)[0].type, 'render')
})

check('形状配信の連続は250ms待って1回再送し、その前の応答は捨てる', () => {
  const m = machine()
  m.update(input())
  m.fire('debounce')
  assert.equal(m.event({ type: 'shapes', now: 300 })[0].type, 'hidden')
  assert.deepEqual(m.receive(1), [])
  const old = m.state.refire.token
  m.event({ type: 'shapes', now: 500 })
  assert.equal(m.state.refire.at, 750)
  assert.deepEqual(m.event({ type: 'timer', kind: 'refire', token: old, now: 550 }), [])
  m.fire('refire')
  assert.deepEqual(m.sent(), [['fillet', { radius: 2 }, 1], ['fillet', { radius: 2 }, 2]])
  assert.equal(m.receive(2)[0].type, 'render')
})

check('形状再送より先に打ち直しを送れば、余分な再送は出ない', () => {
  const m = machine()
  m.update(input())
  m.fire('debounce')
  m.update(input(3), 300)
  m.event({ type: 'shapes', now: 400 })
  const old = m.state.refire.token
  m.fire('debounce')
  assert.equal(m.state.refire, null)
  assert.deepEqual(m.event({ type: 'timer', kind: 'refire', token: old, now: 650 }), [])
  assert.deepEqual(m.sent(), [['fillet', { radius: 2 }, 1], ['fillet', { radius: 3 }, 2]])
})

check('打ち直し待ちの間の形状再送は従来の送信順を保ち、旧引数の応答を採用しない', () => {
  const m = machine()
  m.update(input())
  m.fire('debounce')
  m.event({ type: 'shapes', now: 300 })
  m.update(input(3), 400)
  m.fire('refire')
  assert.deepEqual(m.receive(2), [])
  m.fire('debounce')
  assert.deepEqual(m.sent(), [
    ['fillet', { radius: 2 }, 1], ['fillet', { radius: 2 }, 2], ['fillet', { radius: 3 }, 3],
  ])
  assert.equal(m.receive(3)[0].type, 'render')
})

for (const terminal of [
  { type: 'clear', resetKey: true },
  { type: 'detach' },
  { type: 'dispose' },
]) {
  check(`${JSON.stringify(terminal)}で打鍵待ち・再送待ち・応答待ちを終える`, () => {
    const m = machine()
    m.update(input())
    m.fire('debounce')
    m.event({ type: 'shapes', now: 300 })
    m.update(input(3), 400)
    const debounce = m.state.debounce.token
    const refire = m.state.refire.token
    assert.ok(m.event(terminal).some((effect) => effect.type === 'clear'))
    assert.equal(m.state.debounce, null)
    assert.equal(m.state.refire, null)
    assert.equal(m.state.waitingSince, terminal.type === 'clear' ? 400 : null)
    assert.equal(m.state.server, null)
    assert.deepEqual(m.receive(1), [])
    for (const [kind, token] of [['debounce', debounce], ['refire', refire]]) {
      assert.deepEqual(m.event({ type: 'timer', kind, token, now: 1000 }), [])
    }
    m.event({ type: 'shapes', now: 1100 })
    assert.equal(m.state.refire, null)
    assert.equal(m.sent().length, 1)
  })
}

check('同じホストで構え直すときも最大待ちの送信時刻を変えない', () => {
  const m = machine()
  m.update(input(), 0)
  m.event({ type: 'clear', resetKey: true })
  m.update(input(3), 700)
  assert.equal(m.state.debounce.at, 750)
  m.fire('debounce')
  assert.deepEqual(m.sent(), [['fillet', { radius: 3 }, 2]])
})

check('サーバ幽霊のクリアは、後から打った入力の送信を妨げない', () => {
  const m = machine()
  m.update(input())
  m.fire('debounce')
  m.event({ type: 'shapes', now: 300 })
  m.update(input(3), 400)
  const pending = m.state.debounce
  m.event({ type: 'clear', resetKey: false })
  assert.equal(m.state.refire, null)
  assert.equal(m.state.server, null)
  assert.equal(m.state.debounce, pending)
  assert.deepEqual(m.receive(1), [])
  m.fire('debounce')
  assert.deepEqual(m.sent(), [['fillet', { radius: 2 }, 1], ['fillet', { radius: 3 }, 3]])
  assert.equal(m.receive(3)[0].type, 'render')
})

check('確定の後の配信と応答で復活せず、失敗後の同じ値の打ち直しは送れる', () => {
  const m = machine()
  m.update(input())
  m.fire('debounce')
  m.event({ type: 'clear', resetKey: true })
  m.update(null)
  m.event({ type: 'shapes', now: 400 })
  assert.deepEqual(m.receive(1), [])
  m.update(input(), 500)
  m.fire('debounce')
  assert.deepEqual(m.sent(), [['fillet', { radius: 2 }, 1], ['fillet', { radius: 2 }, 3]])
})

check('コマンド切替・子の再生成で前の要求を持ち越さない', () => {
  const m = machine()
  m.update(input())
  m.fire('debounce')
  m.event({ type: 'detach' })
  m.update(input(2, '', 'local', 'move_body'), 400)
  assert.deepEqual(m.receive(1), [])
  assert.equal(m.state.server, null)
  assert.equal(m.state.debounce, null)
  assert.equal(m.sent().length, 1)
})

check('切断で応答だけを失効させ、再接続だけでは余分に送らない', () => {
  const m = machine()
  m.update(input())
  m.fire('debounce')
  m.event({ type: 'disconnect' })
  assert.deepEqual(m.receive(1), [])
  assert.deepEqual(m.update(input(), 400), [])
  assert.equal(m.sent().length, 1)
  m.event({ type: 'shapes', now: 500 })
  m.fire('refire')
  assert.deepEqual(m.receive(1), [])
  assert.equal(m.receive(2)[0].type, 'render')
})

check('空入力は待ちを消し、次の打ち始めから750msを数え直す', () => {
  const m = machine()
  m.update(input(), 0)
  m.update(null, 700)
  m.update(input(3), 800)
  assert.equal(m.state.debounce.at, 1050)
  assert.equal(m.state.waitingSince, 800)
})

check('送信と共有引数のタイマーの順序を保つため、待ち時間を時計の再読で縮めない', () => {
  let clock = 100.125
  const delays = []
  const controller = createPreviewController({
    now: () => { clock += 0.125; return clock },
    setTimer: (_callback, delay) => { delays.push(delay); return delays.length },
    clearTimer() {}, send() {}, local() {}, render() {}, clear() {}, hidden() {},
  })
  controller.update(input())
  assert.deepEqual(delays, [250])
  controller.dispose()
})

check('副作用の実行器も取消後のコールバックと破棄後の応答を描かない', () => {
  let now = 0
  let nextTimer = 0
  const pending = new Map()
  const callbacks = []
  const calls = []
  const controller = createPreviewController({
    now: () => now,
    setTimer(callback, delay) {
      const id = ++nextTimer
      pending.set(id, { callback, at: now + delay })
      callbacks.push(callback)
      return id
    },
    clearTimer: (id) => pending.delete(id),
    send: (...args) => calls.push(['send', ...args]),
    local: (...args) => calls.push(['local', ...args]),
    render: (payload) => calls.push(['render', payload]),
    clear: () => calls.push(['clear']),
    hidden: () => calls.push(['hidden']),
  })
  controller.update(input())
  assert.deepEqual(calls, [])
  assert.equal(pending.get(1).at, 250)
  now = 250
  callbacks[0]()
  assert.deepEqual(calls, [['send', 'fillet', { radius: 2 }, 1]])
  controller.receive(response(1, [], { sketch_id: 's', entities: [{}], pieces: 2 }))
  assert.equal(calls.at(-1)[0], 'render')
  controller.shapesChanged()
  controller.update(input(3))
  assert.equal(pending.size, 2)
  controller.cancel()
  assert.equal(pending.size, 0)
  const afterCancel = calls.length
  now = 1000
  for (const callback of callbacks) callback()
  controller.receive(response(1))
  assert.equal(calls.length, afterCancel)
  controller.update(input(3, '', 'local', 'move_body'))
  assert.equal(calls.at(-1)[0], 'local')
  controller.dispose()
  const afterDispose = calls.length
  controller.update(input())
  controller.receive(response(1))
  controller.shapesChanged()
  controller.cancel()
  assert.equal(calls.length, afterDispose)
  assert.equal(pending.size, 0)
})

console.log(`previewController: ${cases}件通過`)
