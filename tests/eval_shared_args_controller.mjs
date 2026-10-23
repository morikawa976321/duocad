/** 共有引数の送信順・外部更新・入力の保護・消去の範囲が変わったら落ちる。 */
import assert from 'node:assert/strict'
import './ts_resolve_hook.mjs'
const {
  createSharedArgsController, initialSharedArgsState, transitionSharedArgs,
  publishedArgs, sharedArgsChangeNote,
} = await import('../web/src/sharedArgsController.ts')
import { createPreviewController } from '../web/src/previewController.ts'

const number = (name = 'radius') => ({ kind: 'number', name, label: name, inputId: name })
const text = { kind: 'text', name: 'shifts', label: 'ずれ', inputId: 'shifts', toShared: (v) => v === '?' ? undefined : v.split(',').map(Number) }
const axis = {
  kind: 'choice', name: 'axis', label: '軸', options: [
    { value: 'x' }, { value: 'line', sends: null, impliedBy: 'axis_line' },
  ],
}
const ref = { sketch: 's', entity: 'e', point: null }
const specs = [number(), text, axis, { kind: 'point', name: 'corner' }, { kind: 'pick', name: 'axis_line' }, { kind: 'plane', name: 'plane' }]
const emptyDraft = () => ({ numberText: {}, textValues: {}, choices: {}, points: {}, picks: {}, planes: {} })
let cases = 0
function check(name, run) { run(); cases += 1; console.log(`OK: ${name}`) }
function freeze(value) {
  if (value && typeof value === 'object' && !Object.isFrozen(value)) {
    Object.freeze(value)
    for (const child of Object.values(value)) freeze(child)
  }
  return value
}
function machine(fields = specs) {
  let state = initialSharedArgsState()
  let draft = emptyDraft()
  const effects = []
  const context = { active: true, committed: false, specs: fields }
  return {
    context, effects,
    get state() { return state },
    get draft() { return draft },
    set draft(value) { draft = value },
    event(event) {
      const next = transitionSharedArgs(freeze(state), freeze(event))
      state = next.state
      effects.push(...next.effects)
      for (const effect of next.effects) if (effect.type === 'render') draft = effect.draft
      return next.effects
    },
    receive(remote, focusedInputId = null) {
      return this.event({ type: 'receive', remote, specs: fields, draft, focusedInputId })
    },
    input(args) { return this.event({ type: 'input', args }) },
    visible(names) { return this.event({ type: 'visible', names }) },
    fire() { return this.event({ type: 'timer', token: state.pending.token, context: { ...context } }) },
    flush(args) { return this.event({ type: 'flush', args, context: { ...context } }) },
    sent() { return effects.filter((e) => e.type === 'send').map((e) => e.args) },
  }
}

check('送信は250msの末尾だけで、同じ値と自分の配信の返りは送信も描画もしない', () => {
  const m = machine()
  assert.deepEqual(m.input({ radius: 2 }).map((e) => [e.type, e.delay]), [['arm', 250]])
  const old = m.state.pending.token
  m.input({ radius: 3 })
  assert.deepEqual(m.event({ type: 'timer', token: old, context: m.context }), [])
  assert.deepEqual(m.sent(), [])
  m.fire()
  assert.deepEqual(m.sent(), [{ radius: 3 }])
  assert.deepEqual(m.state.sentKeys, ['radius'])
  assert.deepEqual(m.receive({ radius: 3 }), [])
  assert.deepEqual(m.state.remote, { radius: 3 })
  m.input({ radius: 3 })
  m.fire()
  assert.deepEqual(m.sent(), [{ radius: 3 }])
})

check('AI・別画面の値は数値・文字・選択肢・点・ピック・平面へ戻る', () => {
  const m = machine()
  m.receive({ radius: 2, shifts: [1, 2], axis_line: ref, corner: ['3', 4], base_plane: 'XY' })
  assert.deepEqual(m.draft, {
    numberText: { radius: '2' }, textValues: { shifts: '1,2' }, choices: { axis: 'line' },
    points: { corner: [3, 4] }, picks: { axis_line: ref }, planes: { plane: 'XY' },
  })
  assert.deepEqual(m.state.sentKeys, [])
  assert.deepEqual(m.sent(), [])
})

check('フォーカス中の数値は外部値で奪わず、blurで自分の未送信値を1回出す', () => {
  const m = machine()
  m.receive({ radius: 2 })
  m.draft = { ...m.draft, numberText: { radius: '1+2' } }
  m.input({ radius: 3 })
  const effects = m.receive({ radius: 4 }, 'radius')
  assert.equal(m.draft.numberText.radius, '1+2')
  assert.equal(m.state.focusedInputId, 'radius')
  assert.deepEqual(m.state.remote, { radius: 4 })
  assert.deepEqual(m.state.lastPublished, { radius: 4 })
  assert.deepEqual(m.state.pending.args, { radius: 3 })
  assert.equal(effects.at(-1).message, '外から radius=4 に変わったが、打っている途中なので欄は変えていない')
  m.flush({ radius: 3 })
  assert.equal(m.state.pending, null)
  assert.equal(m.state.focusedInputId, null)
  assert.deepEqual(m.sent(), [{ radius: 3 }])
})

check('フォーカス中の文字の外部消去も奪わず、空の通知を出す', () => {
  const m = machine()
  m.receive({ shifts: [1, 2] })
  const effects = m.receive({}, 'shifts')
  assert.equal(m.draft.textValues.shifts, '1,2')
  assert.equal(effects.at(-1).message, '外から ずれ=(空) に変わったが、打っている途中なので欄は変えていない')
  m.flush({ shifts: [1, 2] })
  assert.deepEqual(m.sent(), [{ shifts: [1, 2] }])
})

check('未送信のタイマーは外部更新で置き換えず、発火時の差分を送る', () => {
  const m = machine()
  m.receive({ radius: 2 })
  m.input({ radius: 3 })
  m.receive({ radius: 4 }, 'radius')
  m.fire()
  assert.deepEqual(m.sent(), [{ radius: 3 }])
  assert.deepEqual(m.state.remote, { radius: 4 })
  assert.deepEqual(m.state.lastPublished, { radius: 3 })
})

check('表示済みのAI値の消去と非表示への切替はnull、未表示と欄の無いキーは残す', () => {
  const m = machine([number('angle'), number('hidden')])
  m.visible(['angle'])
  m.receive({ angle: 90, hidden: 7, scale: 2, sketch_id: 's' })
  m.visible([])
  m.flush({})
  assert.deepEqual(m.sent(), [{ angle: null }])
  assert.deepEqual(m.state.lastPublished, { hidden: 7, scale: 2, sketch_id: 's' })
})

check('buildだけが作る名前も、自分が一度送ったキーなら消去する', () => {
  const m = machine([])
  m.flush({ base_plane: 'XY', mode: 'new' })
  m.flush({})
  m.flush({})
  assert.deepEqual(m.sent(), [{ base_plane: 'XY', mode: 'new' }, { base_plane: null, mode: null }])
})

check('対応できない外部値は消さず、自分で送った後は消せる', () => {
  const m = machine()
  m.visible(['axis', 'corner', 'axis_line'])
  m.receive({ axis: 'z', corner: '不明', axis_line: 12 })
  assert.deepEqual(m.state.unmapped, ['axis', 'corner', 'axis_line'])
  m.flush({})
  assert.deepEqual(m.sent(), [])
  m.flush({ axis: 'x' })
  assert.deepEqual(m.state.unmapped, ['corner', 'axis_line'])
  m.flush({})
  assert.deepEqual(m.sent(), [{ axis: 'x' }, { axis: null }])
})

check('配信からキーが消えると点・ピック・通常選択は消え、基準平面は保つ', () => {
  const m = machine()
  m.receive({ radius: 2, shifts: [1], axis: 'x', axis_line: ref, corner: [0, 0], base_plane: 'YZ' })
  m.receive({})
  assert.deepEqual(m.draft, {
    numberText: { radius: '' }, textValues: { shifts: '' }, choices: {}, points: {}, picks: {}, planes: { plane: 'YZ' },
  })
})

check('sends:nullの選択と未送信の切替は既存の逆引き規則を通す', () => {
  const m = machine()
  m.receive({ axis_line: ref })
  assert.equal(m.draft.choices.axis, 'line')
  m.draft = { ...m.draft, choices: { axis: 'x' } }
  m.receive({ axis_line: ref, radius: 3 })
  assert.deepEqual(m.draft.choices, {})
  m.draft = { ...m.draft, choices: { axis: 'line' } }
  m.receive({ radius: 4 })
  assert.equal(m.draft.choices.axis, 'line')
})

check('未送信の数式は自分の送信の返りで数値文字列に直さない', () => {
  const m = machine()
  m.draft = { ...m.draft, numberText: { radius: '1+2' } }
  m.flush({ radius: 3 })
  assert.deepEqual(m.receive({ radius: 3 }), [])
  assert.equal(m.draft.numberText.radius, '1+2')
})

for (const terminal of ['cancel', 'reset', 'dispose']) {
  check(`${terminal}は保留送信を終え、古いコールバックでは送らない`, () => {
    const m = machine()
    m.visible(['radius'])
    m.flush({ radius: 2 })
    m.input({ radius: 3 })
    const token = m.state.pending.token
    m.event({ type: terminal })
    assert.equal(m.state.pending, null)
    assert.deepEqual(m.event({ type: 'timer', token, context: m.context }), [])
    if (terminal === 'reset') {
      assert.deepEqual(m.state.lastPublished, {})
      assert.deepEqual(m.state.sentKeys, [])
      assert.deepEqual(m.state.everLive, [])
      m.input({ radius: 4 })
      assert.notEqual(m.state.pending.token, token)
    }
    if (terminal === 'dispose') assert.deepEqual(m.input({ radius: 4 }), [])
    assert.deepEqual(m.sent(), [{ radius: 2 }])
  })
}

check('確定後と無起動のタイマーは送らず、確定失敗後は同じ値を送れる', () => {
  const m = machine()
  m.input({ radius: 2 })
  m.context.committed = true
  m.fire()
  m.context.committed = false
  m.input({ radius: 2 })
  m.context.active = false
  m.fire()
  assert.deepEqual(m.sent(), [])
  m.context.active = true
  m.flush({ radius: 2 })
  assert.deepEqual(m.sent(), [{ radius: 2 }])
})

check('公開引数は宣言と表示欄で絞り、選択肢・文字を変換し、buildの値を優先する', () => {
  const command = { id: 'loft' }
  const fields = [text, { kind: 'selection', name: 'selection' }, { kind: 'text', name: 'name' }]
  assert.deepEqual(publishedArgs(command, fields, { shifts: '1,2', name: '', selection: [ref], hidden: 9 }, null), { shifts: [1, 2] })
  assert.deepEqual(publishedArgs(command, fields, { shifts: '?' }, null), {})
  assert.deepEqual(publishedArgs(command, fields, { shifts: '1,2' }, { ok: true, args: { shifts: [3, 4], hidden: 9 } }), { shifts: [3, 4] })
  assert.deepEqual(publishedArgs({ id: 'revolve' }, [axis], { axis: 'line' }, null), {})
  assert.deepEqual(publishedArgs({ id: 'revolve' }, [axis], { axis: 'x' }, null), { axis: 'x' })
})

check('Appの共有引数の通知は差分の順序・空・表示名を保つ', () => {
  assert.equal(sharedArgsChangeNote({ radius: 2, name: '旧' }, { name: '新', angle: 90 }, 'フィレット', (name) => name === 'radius' ? '半径' : name),
    '外から「フィレット」の引数が変わった: 半径=(空) / name="新" / angle=90')
  assert.equal(sharedArgsChangeNote({ radius: 2 }, { radius: 2 }, 'フィレット', (n) => n), null)
})

check('実行器はblur・Enter・再生成の送信順とプレビューの先行タイマーを保つ', () => {
  let nextTimer = 0
  const timers = new Map()
  const callbacks = []
  const calls = []
  const context = { active: true, committed: false, specs }
  const setTimer = (callback, delay) => {
    const id = ++nextTimer
    timers.set(id, { callback, delay })
    callbacks.push(callback)
    return id
  }
  const clearTimer = (id) => timers.delete(id)
  let draft = emptyDraft()
  const preview = createPreviewController({
    now: () => 0, setTimer, clearTimer,
    send: (...args) => calls.push(['preview', ...args]),
    local() {}, render() {}, clear() {}, hidden() {},
  })
  const shared = createSharedArgsController({
    context: () => context, setTimer, clearTimer,
    send: (args) => calls.push(['set_command_args', args]),
    render: (value) => { draft = value }, note: (message) => calls.push(['note', message]),
  })
  shared.receive({ radius: 2 }, specs, draft, null)
  assert.equal(draft.numberText.radius, '2')
  preview.update({ verb: 'fillet', args: { radius: 3 }, selection: '', mode: 'server' })
  shared.update({ radius: 3 })
  assert.deepEqual([...timers.values()].map((v) => v.delay), [250, 250])
  callbacks[0]()
  callbacks[1]()
  assert.deepEqual(calls, [['preview', 'fillet', { radius: 3 }, 1], ['set_command_args', { radius: 3 }]])
  shared.update({ radius: 4 })
  shared.flush({ radius: 4 })
  assert.deepEqual(calls.at(-1), ['set_command_args', { radius: 4 }])
  shared.update({ radius: 5 })
  context.committed = true
  shared.cancel()
  preview.cancel()
  calls.push(['fillet', { radius: 5 }])
  for (const callback of callbacks) callback()
  assert.deepEqual(calls.slice(2), [['set_command_args', { radius: 4 }], ['fillet', { radius: 5 }]])
  shared.reset()
  context.committed = false
  shared.update({ radius: 6 })
  shared.dispose()
  const count = calls.length
  for (const callback of callbacks) callback()
  shared.receive({ radius: 7 }, specs, draft, null)
  assert.equal(calls.length, count)
  preview.dispose()
})

console.log(`sharedArgsController: ${cases}件通過`)
