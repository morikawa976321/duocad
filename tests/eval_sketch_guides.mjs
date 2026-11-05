/**
 * ボディ由来のスケッチの吸着先(`web/src/sketchGuideTargets.ts` の段ごとの候補と勝ち負け、`web/src/sketchGuides.ts` の画面の
 * 距離での拾い方と強調)を実際に評価する口。落ちたら、点を待つ間にボディの頂点・中心・中点・線・スケッチの図形との交点の
 * どれかへ吸着しないか、吸着しても置く座標がずれる。
 */
import assert from 'node:assert/strict'

import './ts_resolve_hook.mjs'
const THREE = await import('../web/node_modules/three/build/three.module.js')
const { guideMidpointTargets, guidePointTargets, guideSketchCrossings, nearerSide } = await import(
  '../web/src/sketchGuideTargets.ts'
)
const { createSketchGuideLayer } = await import('../web/src/sketchGuides.ts')
const { snapSketchPoint } = await import('../web/src/entityKinds.ts')
const { createProjector } = await import('../web/src/polylinePick.ts')
const { DEFAULT_RESOLUTION } = await import('../web/src/adaptiveResolution.ts')

const near = (actual, expected, message) => {
  assert.equal(actual.length, expected.length, `${message}: 長さ`)
  actual.forEach((value, index) => assert.ok(Math.abs(value - expected[index]) < 1e-9, `${message}: ${actual} != ${expected}`))
}

// 箱の上面の外周(線)・穴の縁(円)・斜めに切った穴(楕円)・球の交線の近似(ベジエ)・頂点
const guides = {
  sketchId: 's',
  entities: [
    { kind: 'line', params: { x0: 0, y0: 0, x1: 20, y1: 0 } },
    { kind: 'circle', params: { cx: 10, cy: 5, diameter: 4 } },
    { kind: 'ellipse', params: { cx: -20, cy: 10, width: 8, height: 4, angle: 0 } },
    { kind: 'bezier', params: { x0: 30, y0: 0, cx1: 31, cy1: 9, cx2: 39, cy2: 9, x1: 40, y1: 0 } },
    { kind: 'arc', params: { cx: 0, cy: -20, diameter: 10, start_deg: 0, end_deg: 90 } },
  ],
  points: [[0, 0], [20, 0], [50, 50]],
}

// ============================================================== 段ごとの候補
const points = guidePointTargets(guides)
const pointKey = (item) => `${item.u},${item.v},${item.entity}`
const keys = new Set(points.map(pointKey))
for (const expected of ['0,0,null', '20,0,null', '50,50,null', '10,5,1', '-20,10,2', '0,-20,4']) {
  assert.ok(keys.has(expected), `点の段に ${expected} が無い(頂点・円/楕円/円弧の中心)`)
}
for (const [u, v] of [[-16, 10], [-24, 10], [-20, 12], [-20, 8]]) {
  assert.ok(points.some((item) => Math.abs(item.u - u) < 1e-9 && Math.abs(item.v - v) < 1e-9 && item.entity === 2), `楕円の軸の端 (${u}, ${v}) が無い`)
}
assert.ok(!points.some((item) => item.entity === 3), 'ベジエの制御点は曲線の上に無いので点の段に入れない')

const midpoints = guideMidpointTargets(guides)
assert.equal(midpoints.length, 1, '中点の段は直線だけ')
near([midpoints[0].u, midpoints[0].v, ...midpoints[0].direction], [10, 0, 1, 0], '直線の中点と向き')
assert.equal(midpoints[0].entity, 0)

const sketchEntities = [
  { kind: 'line', params: { x0: 5, y0: -5, x1: 5, y1: 5 } }, // ボディの上面の縁 (y=0) と (5, 0) で交わる
  { kind: 'line', params: { x0: 4, y0: -5, x1: 4, y1: 5 } }, // スケッチどうしの交点は作らない(平行)
  { kind: 'line', params: { x0: 0, y0: 3, x1: 6, y1: -3 } }, // 上の2本とも交わる: スケッチどうしの交点は出さない
]
const crossings = guideSketchCrossings(guides, sketchEntities)
const crossingKeys = crossings.map((item) => `${Math.round(item.u * 1e9) / 1e9},${Math.round(item.v * 1e9) / 1e9},${item.entity}`).sort()
assert.deepEqual(crossingKeys, ['3,0,0', '4,0,0', '5,0,0'], 'ボディの線とスケッチの図形の交点だけ(スケッチどうしは出さない)')

// ============================================================== 同じ段の勝ち負け
assert.equal(nearerSide(null, null), null)
assert.equal(nearerSide(4, null), 'entity')
assert.equal(nearerSide(null, 4), 'guide')
assert.equal(nearerSide(4, 4), 'entity', '同着はスケッチの図形')
assert.equal(nearerSide(4, 3.9), 'guide', 'ボディ由来の方が近ければそちら')
assert.equal(nearerSide(Infinity, 3), 'guide', '図形の置く点が射影できないときはボディ由来')

// ============================================================== 置く座標はそのまま
near(snapSketchPoint(10.4, 5.6, { snapAt: [10, 5] }, () => undefined, undefined, true), [10, 5], 'ボディ由来の当たりは snapAt に置く')

// ============================================================== 画面の距離で拾う(1 px = 1 mm、(u,v) = (0,0) が画面の中心)
const camera = new THREE.OrthographicCamera(-50, 50, 50, -50, 0.1, 1000)
// 既定の上向きは `viewportConstants.ts` が Z へ変えている(読み込んだ時点で効く)。真上から見るので Y を上にする。
camera.up.set(0, 1, 0)
camera.position.set(0, 0, 100)
camera.lookAt(0, 0, 0)
camera.updateMatrixWorld()
const project = createProjector(camera, 100, 100)
const frame = { origin: [0, 0, 0], u: [1, 0, 0], v: [0, 1, 0], normal: [0, 0, 1] }
const screen = (u, v) => [u + 50, 50 - v]
const layer = createSketchGuideLayer()
assert.equal(layer.has('s'), false)
layer.set(guides)
assert.equal(layer.has('s'), true)
assert.equal(layer.has('other'), false, '別のスケッチの吸着先は当たらない')

const vertex = layer.pointAt(...screen(20.5, 0.5), frame, [], project)
assert.ok(vertex !== null, '頂点の近くで点の段が当たる')
near(vertex.hit.snapAt, [20, 0], '頂点へ吸着')
assert.equal(vertex.hit.kind, 'point')
assert.ok(Math.abs(vertex.distSq - 0.5) < 1e-6, '距離²は画面の距離')

const center = layer.pointAt(...screen(11, 5), frame, [], project)
near(center.hit.snapAt, [10, 5], '穴の中心へ吸着')
assert.equal(center.hit.entity, 1, '中心は円の線を強調する')

assert.equal(layer.pointAt(...screen(30, 30), frame, [], project), null, '半径(7 px)の外は当たらない')

const crossing = layer.pointAt(...screen(5.5, 0.5), frame, sketchEntities, project)
near(crossing.hit.snapAt, [5, 0], 'スケッチの図形との交点へ吸着')

const midpoint = layer.midpointAt(...screen(10, 1), frame, project)
near(midpoint.hit.snapAt, [10, 0], '直線の中点へ吸着')
assert.equal(midpoint.hit.kind, 'midpoint')
near(midpoint.hit.snapDirection, [1, 0], '中点のティックの向き')

const line = layer.lineAt(...screen(14, 2), frame, { u: 14, v: 2 }, DEFAULT_RESOLUTION, project)
near(line.hit.snapAt, [14, 0], '線の上へ落とす')
assert.equal(line.hit.kind, 'line')
assert.equal(line.hit.entity, 0)
assert.equal(layer.lineAt(...screen(14, -9), frame, { u: 14, v: -9 }, DEFAULT_RESOLUTION, project), null, '線の半径(6 px)の外')

const onCircle = layer.lineAt(...screen(12.5, 5.5), frame, { u: 12.5, v: 5.5 }, DEFAULT_RESOLUTION, project)
assert.equal(onCircle.hit.entity, 1, '円の線にも当たる')
assert.ok(Math.abs(Math.hypot(onCircle.hit.snapAt[0] - 10, onCircle.hit.snapAt[1] - 5) - 2) < 1e-9, '円の上へ落とす')

// ============================================================== 強調は当たっている間だけ
assert.equal(layer.object.children.length, 0, '何も当たっていなければ描かない')
assert.equal(layer.setHover(line.hit, frame, DEFAULT_RESOLUTION), true)
assert.equal(layer.object.children.length, 1, '当たった線を描く')
assert.equal(layer.setHover(line.hit, frame, DEFAULT_RESOLUTION), false, '同じ線なら描き直さない')
assert.equal(layer.setHover(vertex.hit, frame, DEFAULT_RESOLUTION), true, '線を持たない頂点では強調を消す')
assert.equal(layer.object.children.length, 0)
layer.setHover(line.hit, frame, DEFAULT_RESOLUTION)
layer.set(null)
assert.equal(layer.object.children.length, 0, '捨てたら強調も消える')
assert.equal(layer.pointAt(...screen(20, 0), frame, [], project), null, '捨てた後は当たらない')
layer.dispose()

console.log('eval_sketch_guides: OK')
