/**
 * `web/src/viewCubeOverlay.ts` の押下・ドラッグ・Pointer Lock の状態機械を、DOM の最小の偽物で**実際に動かす**口。
 * `tests/test_ts_evaluators.py` が `node` でこれを走らせる(WebGL は要らない。描画は呼ばない)。
 *
 * 落ちたら、次のどちらか。
 * - `pointercancel`(OS が掴みを横取りした)でキューブの押下が解除されず、`capturing()` が真のまま残る。
 *   `viewport.ts` は `capturing()` が真の間 `pointermove` をキューブへ渡すので、ボタンを押していないのにカメラが回り続ける。
 * - ドラッグ開始の直後に離した/打ち切ったとき、遅れて成立した Pointer Lock が解除されず、Esc までカーソルが固定される。
 */
import assert from 'node:assert/strict'

import './ts_resolve_hook.mjs'

const THREE = await import(new URL('../web/node_modules/three/build/three.module.js', import.meta.url).href)

/** 偽の DOM。`pointerlockchange` を手で起こす。 */
function setup() {
  const log = { locks: 0, exits: 0, captured: [], released: [] }
  const listeners = new Map()
  const domElement = {
    clientHeight: 600,
    setPointerCapture: (id) => log.captured.push(id),
    releasePointerCapture: (id) => log.released.push(id),
    requestPointerLock: () => {
      log.locks += 1
      return Promise.resolve()
    },
  }
  const fakeDocument = {
    pointerLockElement: null,
    createElement: () => ({ getContext: () => null }),
    addEventListener: (type, listener) => listeners.set(type, listener),
    removeEventListener: (type) => listeners.delete(type),
    exitPointerLock() {
      log.exits += 1
      fakeDocument.pointerLockElement = null
      listeners.get('pointerlockchange')?.()
    },
  }
  globalThis.document = fakeDocument
  return { log, domElement, fakeDocument, listeners }
}

const { createViewCubeOverlay } = await import('../web/src/viewCubeOverlay.ts')

function makeCube(env) {
  const perspective = new THREE.PerspectiveCamera(45, 1, 0.1, 100)
  const orthographic = new THREE.OrthographicCamera(-1, 1, 1, -1, 0.1, 100)
  return createViewCubeOverlay({
    renderer: { domElement: env.domElement },
    camera: () => perspective,
    perspective,
    orthographic,
    controls: {
      rotateSpeed: 1,
      rotateLeft() {},
      rotateUp() {},
    },
    size: () => ({ width: 1000, height: 600 }),
    overlayOffset: () => 0,
  })
}

const down = { offsetX: 900, offsetY: 80, pointerId: 7, button: 0 }
const dragMove = { offsetX: 912, offsetY: 80, movementX: 12, movementY: 0 }

// --- pointercancel: 押下だけ(まだドラッグ未確定)でも、ドラッグ中でも解除される。
for (const dragging of [false, true]) {
  const env = setup()
  const cube = makeCube(env)
  assert.equal(cube.pointerDown(down), true)
  assert.equal(cube.capturing(), true)
  if (dragging) {
    cube.pointerMove(dragMove)
    assert.equal(env.log.locks, 1, 'しきい値を超えたらロックを要求する')
  }
  cube.cancel()
  assert.equal(cube.capturing(), false, `pointercancel の後も掴んだまま(ドラッグ=${dragging})`)
  assert.deepEqual(env.log.released, [7], 'ポインタの捕捉を外していない')
}

// --- 何もしていなければ cancel は何もしない。
{
  const env = setup()
  const cube = makeCube(env)
  cube.cancel()
  assert.equal(cube.capturing(), false)
  assert.deepEqual(env.log.released, [])
}

// --- 要求したロックが離した後に成立しても、取り残さない。
for (const ending of ['pointerUp', 'cancel']) {
  const env = setup()
  const cube = makeCube(env)
  cube.pointerDown(down)
  cube.pointerMove(dragMove) // ロックを要求(成立はまだ)
  if (ending === 'pointerUp') cube.pointerUp({ button: 0, offsetX: 912, offsetY: 80 })
  else cube.cancel()
  assert.equal(cube.capturing(), false)
  assert.equal(env.log.exits, 0, 'まだロックされていないので exit は要らない')
  // 遅れてロックが成立する。
  env.fakeDocument.pointerLockElement = env.domElement
  env.listeners.get('pointerlockchange')()
  assert.equal(env.log.exits, 1, `${ending} の後に成立したロックが解除されない(Esc まで固定される)`)
}

// --- ドラッグ中に成立したロックは保つ(通常のドラッグ)。
{
  const env = setup()
  const cube = makeCube(env)
  cube.pointerDown(down)
  cube.pointerMove(dragMove)
  env.fakeDocument.pointerLockElement = env.domElement
  env.listeners.get('pointerlockchange')()
  assert.equal(env.log.exits, 0, 'ドラッグ中のロックを外している')
  assert.equal(cube.capturing(), true)
  cube.pointerUp({ button: 0, offsetX: 912, offsetY: 80 })
  assert.equal(env.log.exits, 1, '離したらロックを外す')
}

console.log('viewCubeOverlay.ts: 全ての想定を満たした')
