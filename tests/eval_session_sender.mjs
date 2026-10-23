/** 送信口の型付けで JSON の形・識別子・ack の待機・未接続時の通知を変えない。 */
import assert from 'node:assert/strict'
import './ts_resolve_hook.mjs'

const sockets = []
class FakeWebSocket {
  static OPEN = 1
  readyState = 0
  sent = []
  constructor() { sockets.push(this) }
  send(raw) { this.sent.push(JSON.parse(raw)) }
  close() {}
}
globalThis.WebSocket = FakeWebSocket
globalThis.location = { href: 'http://127.0.0.1/', hostname: '127.0.0.1' }
globalThis.fetch = async () => ({ ok: true, json: async () => ({ ws_url: 'ws://127.0.0.1/' }) })

const { createSession } = await import('../web/src/session.ts')
const { buildArgs, commandRequest } = await import('../web/src/commands/command.ts')
const { IMPORT_STEP } = await import('../web/src/commands/files.ts')
const errors = [], waits = []
const hooks = new Proxy({}, { get: (_target, name) => {
  if (name === 'onError') return (...args) => errors.push(args)
  if (name === 'onWaiting') return (value) => waits.push(value)
  return () => {}
} })
const session = createSession(hooks)
try {
  session.start()
  await new Promise((resolve) => setImmediate(resolve))
  const socket = sockets[0]
  assert.ok(socket)
  session.sendCommand('fillet', { radius: 2 }, 17)
  session.sendCommand('fillet', { radius: 2 }, 17)
  assert.equal(errors.length, 2, '未接続時の同じ操作も毎回通知する')
  assert.deepEqual(socket.sent, [])
  socket.readyState = FakeWebSocket.OPEN
  socket.onopen()
  const last = () => socket.sent.at(-1)

  session.sendCommand('fillet', { radius: 2 }, 17)
  assert.deepEqual(last(), { cmd: 'fillet', radius: 2, client_op: 17, ack_id: 1 })
  assert.equal(typeof waits.at(-1), 'number')
  session.sendCommand('list_documents', {}, 18)
  assert.deepEqual(last(), { cmd: 'list_documents', client_op: 18 })
  session.undo()
  assert.deepEqual(last(), { cmd: 'undo', ack_id: 2 })
  socket.onmessage({ data: JSON.stringify({ type: 'done', ack_id: 2 }) })
  assert.equal(waits.at(-1), null, 'done はそれ以前の待機も解除する')

  const built = buildArgs(IMPORT_STEP, { path: ' fixture.step ' })
  assert.ok(built.ok)
  session.sendCommand(...[...commandRequest(built.request), 19])
  assert.deepEqual(last(), { cmd: 'import_step', path: 'fixture.step', client_op: 19, ack_id: 3 })
  session.sendCommand(...[...commandRequest(['add_circle', { center: [0, 0], diameter: 2 }], 'sketch-1'), 20])
  assert.deepEqual(last(), { cmd: 'add_circle', center: [0, 0], diameter: 2, sketch_id: 'sketch-1', client_op: 20, ack_id: 4 })
  assert.deepEqual(buildArgs(IMPORT_STEP, { path: '' }), { ok: false, reason: 'パスが空' })

  session.preview('fillet', { radius: 2 }, 99)
  assert.deepEqual(last(), { cmd: 'preview', verb: 'fillet', args: { radius: 2 }, preview_id: 99, ack_id: 5 })
  session.setViewResolution(1, 8, { box: [1, 2] })
  assert.deepEqual(last(), { cmd: 'set_view_resolution', world_per_pixel: 1, size_hint: 8, focus: { box: [1, 2] } })
  session.runOptimization({ elementSizeMm: null, minWidthMm: 5, debug: ['space'] })
  assert.deepEqual(last(), { cmd: 'run_optimization', element_size_mm: null, min_width_mm: 5, debug: ['space'] })
  session.finishOptimization(null, [])
  assert.deepEqual(last(), { cmd: 'finish_optimization' })
  session.finishOptimization(0, ['space'])
  assert.deepEqual(last(), { cmd: 'finish_optimization', step: 0, debug: ['space'] })
  session.optimizeStepVoxels(0)
  assert.deepEqual(last(), { cmd: 'optimize_step_voxels', index: 0, ack_id: 6 })
  session.setVisible('body-1', false, 21)
  assert.deepEqual(last(), { cmd: 'set_visible', body_id: 'body-1', visible: false, client_op: 21, ack_id: 7 })
  socket.onmessage({ data: JSON.stringify({ type: 'done', ack_id: 7 }) })
  assert.equal(waits.at(-1), null)
  assert.equal(errors.length, 2)
} finally {
  session.dispose()
}
console.log('session sender: OK')
