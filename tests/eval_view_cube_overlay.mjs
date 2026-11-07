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

function makeCube(env, cameraPosition = [0, -10, 0]) {
  const perspective = new THREE.PerspectiveCamera(45, 1, 0.1, 100)
  perspective.position.set(...cameraPosition)
  const orthographic = new THREE.OrthographicCamera(-1, 1, 1, -1, 0.1, 100)
  const cube = createViewCubeOverlay({
    renderer: {
      domElement: env.domElement,
      clearDepth() {},
      setViewport() {},
      setScissor() {},
      setScissorTest() {},
      render: (scene) => scene.updateMatrixWorld(),
    },
    camera: () => perspective,
    perspective,
    orthographic,
    controls: {
      target: new THREE.Vector3(),
      rotateSpeed: 1,
      rotateLeft() {},
      rotateUp() {},
    },
    size: () => ({ width: 1000, height: 600 }),
    overlayOffset: () => 0,
  })
  cube.render() // 当たりの判定は描いた第2パスの行列を使う
  cube.camera = perspective
  return cube
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

// --- 当たりは描いた立方体の外形だけ。矩形の余白は下のモデルへ通す。
{
  const env = setup()
  const cube = makeCube(env)
  assert.equal(cube.inside(916, 84), true, '立方体の中心は当たる')
  assert.equal(cube.inside(850, 18), false, '矩形の角の余白は当たらない')
  assert.equal(cube.pointerDown({ offsetX: 850, offsetY: 18, pointerId: 3, button: 0 }), false, '余白の押下をキューブが引き取っている')
  assert.equal(cube.capturing(), false)
  assert.equal(cube.inside(500, 300), false, '矩形の外は当たらない')
}

// --- 正対している面を押したら反対側へ回る。辺・斜めからの面は今どおりその向きへ。
function clickAt(cube, x, y) {
  assert.equal(cube.pointerDown({ offsetX: x, offsetY: y, pointerId: 5, button: 0 }), true)
  cube.pointerUp({ offsetX: x, offsetY: y, button: 0 })
  cube.stepSnap(performance.now() + 10_000)
}
{
  const env = setup()
  const cube = makeCube(env)
  clickAt(cube, 916, 84)
  assert.ok(cube.camera.position.y > 9.9, '前を向いて前の面を押したのに後ろへ回っていない')
}
{
  const env = setup()
  const cube = makeCube(env)
  clickAt(cube, 916, 49) // 前の面の上の辺
  assert.ok(cube.camera.position.y < -1 && cube.camera.position.z > 1, '辺の押下が辺の向きへ行っていない')
}
{
  const env = setup()
  const cube = makeCube(env, [10, -10, 0]) // 前と右の中間(辺の向き)
  clickAt(cube, 916, 84)
  assert.ok(cube.camera.position.y < -1, '斜めから見ているときは、押した面の向きへ通常どおり回る')
}

console.log('viewCubeOverlay.ts: 全ての想定を満たした')
