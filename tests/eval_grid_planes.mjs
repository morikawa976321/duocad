/**
 * `web/src/gridPlanes.ts` の `createGridPlanes` を**実際に組み立て**、スケッチ中のグリッドがどのframeに出るかを読む。
 * `tests/test_ts_evaluators.py` が `node` でこれを走らせる(WebGL は要らない。見るのはグリッドのシェーダ引数だけ)。
 *
 * 落ちたら、オフセット平面(基準平面・面からのオフセット)へのスケッチ中に、グリッドがスケッチ平面ではなく
 * 原点を通る XY 床に出ている(XZ・YZ のオフセットでは真横から見えてグリッドがまったく出ない)。
 */
import assert from 'node:assert/strict'

import './ts_resolve_hook.mjs'

// `gridPlanes.ts` が読むのと同じ three(`web/node_modules`)。tests/ からは素の名前で引けない。
const THREE = await import(new URL('../web/node_modules/three/build/three.module.js', import.meta.url).href)
const { createGridPlanes } = await import('../web/src/gridPlanes.ts')

const contentBox = new THREE.Box3(new THREE.Vector3(0, 0, 0), new THREE.Vector3(40, 20, 10))
const camera = new THREE.PerspectiveCamera()

function sketchGrid(frame) {
  const grid = createGridPlanes({
    contentBox: () => contentBox.clone(),
    facePositions: () => null,
    worldPerPixel: () => 0.1,
    camera: () => camera,
    onExtentChange() {},
  })
  grid.rescale(contentBox, true)
  grid.setSketchFrame(frame, null)
  return grid
}

/** 見えているグリッド1枚の frame(シェーダ引数)。 */
function shownFrame(grid) {
  const shown = grid.object.children.filter((child) => child.visible && child.material?.uniforms?.uOrigin)
  assert.equal(shown.length, 1, `見えているグリッドは1枚のはず: ${shown.length}`)
  const { uOrigin, uAxisU, uAxisV } = shown[0].material.uniforms
  return { origin: uOrigin.value.toArray(), u: uAxisU.value.toArray(), v: uAxisV.value.toArray() }
}

const close = (actual, expected, label) => {
  for (let i = 0; i < 3; i += 1) assert.ok(Math.abs(actual[i] - expected[i]) < 1e-9, `${label}: ${actual} != ${expected}`)
}

// 基準平面へのスケッチ(原点を通る): これまでどおりその平面。
{
  const grid = sketchGrid({ origin: [0, 0, 0], u: [1, 0, 0], v: [0, 0, 1] })
  const frame = shownFrame(grid)
  close(frame.origin, [0, 0, 0], 'XZ 基準の原点')
  close(frame.v, [0, 0, 1], 'XZ 基準の v')
  assert.deepEqual(grid.info().planes, ['XZ'])
  assert.equal(grid.info().axis, 'Y', '面外の軸は Y')
}

// XY のオフセット(z=30): グリッドはスケッチ平面(z=30)に出る。原点を通る XY 床ではない。
{
  const grid = sketchGrid({ origin: [0, 0, 30], u: [1, 0, 0], v: [0, 1, 0] })
  const frame = shownFrame(grid)
  close(frame.origin, [0, 0, 30], 'XY オフセットの原点')
  close(frame.u, [1, 0, 0], 'XY オフセットの u')
  close(frame.v, [0, 1, 0], 'XY オフセットの v')
  assert.deepEqual(grid.info().planes, ['XY'], '向きが基準平面と一致するので軸の色・面外の軸はその平面のもの')
  assert.equal(grid.info().axis, 'Z')
}

// XZ のオフセット(y=25)・YZ のオフセット(x=-15): 以前は XY 床に落ちて、正対するとグリッドが見えなかった。
{
  const grid = sketchGrid({ origin: [0, 25, 0], u: [1, 0, 0], v: [0, 0, 1] })
  const frame = shownFrame(grid)
  close(frame.origin, [0, 25, 0], 'XZ オフセットの原点')
  close(frame.v, [0, 0, 1], 'XZ オフセットの v')
  assert.equal(grid.info().axis, 'Y')
}
{
  const grid = sketchGrid({ origin: [-15, 0, 0], u: [0, 1, 0], v: [0, 0, 1] })
  const frame = shownFrame(grid)
  close(frame.origin, [-15, 0, 0], 'YZ オフセットの原点')
  close(frame.u, [0, 1, 0], 'YZ オフセットの u')
  assert.equal(grid.info().axis, 'X')
}

// 面からのオフセット(向きが基準平面と合わない): XY 床ではなくそのframe。直交軸は出さない。
{
  const s = Math.SQRT1_2
  const grid = sketchGrid({ origin: [5, 6, 7], u: [s, s, 0], v: [0, 0, 1] })
  const frame = shownFrame(grid)
  close(frame.origin, [5, 6, 7], '斜めの面からのオフセットの原点')
  close(frame.u, [s, s, 0], '斜めの面からのオフセットの u')
  assert.equal(grid.info().axis, null, '基準平面ではないので面外の軸は出さない')
  assert.deepEqual(grid.info().planes, ['sketch-plane'])
}

// スケッチ外の通常表示は原点を通る XY 床 + Z 軸。
{
  const grid = sketchGrid(null)
  close(shownFrame(grid).origin, [0, 0, 0], '通常表示の原点')
  assert.deepEqual(grid.info().planes, ['XY'])
  assert.equal(grid.info().axis, 'Z')
}

// 枠はスケッチ平面から測る: 内容が (0..40, 0..20) の z=30 オフセットでも、枠は有限で内容を覆う。
{
  const grid = sketchGrid({ origin: [0, 0, 30], u: [1, 0, 0], v: [0, 1, 0] })
  const [bounds] = grid.info().planeBounds
  assert.ok(bounds !== null && Number.isFinite(bounds.half) && bounds.half >= 20, `枠が内容を覆わない: ${JSON.stringify(bounds)}`)
}

console.log('gridPlanes.ts: 全ての想定を満たした')
