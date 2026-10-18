/**
 * `web/src/gridPlanes.ts` の `createGridPlanes` を**実際に組み立て**、スケッチ中のグリッドがどのframeに出るかを読む。
 * `tests/test_ts_evaluators.py` が `node` でこれを走らせる(WebGL は要らない。見るのはグリッドのシェーダ引数と面外の軸線だけ)。
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

/** 見えているグリッド1枚の frame・面内の軸線の有無・枠(シェーダ引数)。 */
function shown(grid) {
  const visible = grid.object.children.filter((child) => child.visible && child.material?.uniforms?.uOrigin)
  assert.equal(visible.length, 1, `見えているグリッドは1枚のはず: ${visible.length}`)
  const { uOrigin, uAxisU, uAxisV, uAxisOn, uBoundsHalf } = visible[0].material.uniforms
  return {
    origin: uOrigin.value.toArray(),
    u: uAxisU.value.toArray(),
    v: uAxisV.value.toArray(),
    axesInPlane: uAxisOn.value.toArray().some((on) => on > 0.5),
    half: uBoundsHalf.value,
  }
}

/** 面外の軸線(ワールド原点を通る1本)。向き(絶対値の単位ベクトル)と片側の長さ。無ければ null。 */
function outOfPlaneAxis(grid) {
  const line = grid.object.children.find((child) => child.isLine)
  if (line === undefined) return null
  const p = line.geometry.getAttribute('position')
  const end = new THREE.Vector3(p.getX(1), p.getY(1), p.getZ(1))
  const reach = end.length()
  return { dir: end.normalize().toArray().map(Math.abs), reach }
}

const close = (actual, expected, label) => {
  for (let i = 0; i < 3; i += 1) assert.ok(Math.abs(actual[i] - expected[i]) < 1e-9, `${label}: ${actual} != ${expected}`)
}

// 基準平面へのスケッチ(原点を通る): これまでどおりその平面。軸の色と面外の軸(Y)も出る。
{
  const grid = sketchGrid({ origin: [0, 0, 0], u: [1, 0, 0], v: [0, 0, 1] })
  const frame = shown(grid)
  close(frame.origin, [0, 0, 0], 'XZ 基準の原点')
  close(frame.v, [0, 0, 1], 'XZ 基準の v')
  assert.equal(frame.axesInPlane, true)
  close(outOfPlaneAxis(grid).dir, [0, 1, 0], 'XZ 基準の面外の軸')
}

// XY のオフセット(z=30): グリッドはスケッチ平面(z=30)に出る。原点を通る XY 床ではない。
{
  const grid = sketchGrid({ origin: [0, 0, 30], u: [1, 0, 0], v: [0, 1, 0] })
  const frame = shown(grid)
  close(frame.origin, [0, 0, 30], 'XY オフセットの原点')
  close(frame.u, [1, 0, 0], 'XY オフセットの u')
  close(frame.v, [0, 1, 0], 'XY オフセットの v')
  assert.equal(frame.axesInPlane, true, '向きが基準平面と一致するので軸の色はその平面のもの')
  close(outOfPlaneAxis(grid).dir, [0, 0, 1], 'XY オフセットの面外の軸')
  // 枠は内容(0..40, 0..20)を覆う有限の値。
  assert.ok(Number.isFinite(frame.half) && frame.half >= 20, `枠が内容を覆わない: ${frame.half}`)
}

// 遠いオフセット(z=500): 面外の軸線(ワールド原点を通る)はオフセット平面まで届く。
{
  const grid = sketchGrid({ origin: [0, 0, 500], u: [1, 0, 0], v: [0, 1, 0] })
  assert.ok(outOfPlaneAxis(grid).reach >= 500, `面外の軸線がオフセット平面に届かない: ${outOfPlaneAxis(grid).reach}`)
}

// XZ のオフセット(y=25)・YZ のオフセット(x=-15): 以前は XY 床に落ちて、正対するとグリッドが見えなかった。
{
  const grid = sketchGrid({ origin: [0, 25, 0], u: [1, 0, 0], v: [0, 0, 1] })
  const frame = shown(grid)
  close(frame.origin, [0, 25, 0], 'XZ オフセットの原点')
  close(frame.v, [0, 0, 1], 'XZ オフセットの v')
  close(outOfPlaneAxis(grid).dir, [0, 1, 0], 'XZ オフセットの面外の軸')
}
{
  const grid = sketchGrid({ origin: [-15, 0, 0], u: [0, 1, 0], v: [0, 0, 1] })
  const frame = shown(grid)
  close(frame.origin, [-15, 0, 0], 'YZ オフセットの原点')
  close(frame.u, [0, 1, 0], 'YZ オフセットの u')
  close(outOfPlaneAxis(grid).dir, [1, 0, 0], 'YZ オフセットの面外の軸')
}

// 面からのオフセット(向きが基準平面と合わない): XY 床ではなくそのframe。軸の色も面外の軸も出さない。
{
  const s = Math.SQRT1_2
  const grid = sketchGrid({ origin: [5, 6, 7], u: [s, s, 0], v: [0, 0, 1] })
  const frame = shown(grid)
  close(frame.origin, [5, 6, 7], '斜めの面からのオフセットの原点')
  close(frame.u, [s, s, 0], '斜めの面からのオフセットの u')
  assert.equal(frame.axesInPlane, false, '基準平面ではないので軸の色は出さない')
  assert.equal(outOfPlaneAxis(grid), null, '基準平面ではないので面外の軸は出さない')
}

// スケッチ外の通常表示は原点を通る XY 床 + Z 軸。
{
  const grid = sketchGrid(null)
  close(shown(grid).origin, [0, 0, 0], '通常表示の原点')
  close(outOfPlaneAxis(grid).dir, [0, 0, 1], '通常表示の面外の軸')
}

console.log('gridPlanes.ts: 全ての想定を満たした')
