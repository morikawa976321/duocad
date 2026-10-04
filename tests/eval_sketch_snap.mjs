/**
 * `web/src/entityKinds.ts` の `snapSketchPoint`(スケッチの点入力のスナップ3段)を**実際に評価する**口。
 * `tests/test_ts_evaluators.py` が `node` でこれを走らせる。
 *
 * クリック・幽霊・カーソル強調はどれもこの1本を通る(通っていること自体は `smoke-hover.mjs` が
 * 実ブラウザで見る)。ここで見るのは落ちる先の規則 —— 落ちたら、吸い付いて見える位置か確定する位置が
 * その種別でずれる。
 *
 * 期待値はこのファイルが自前の三角関数で出す(製品の `rectangleCorners` を借りると、同じ間違いを
 * 両側で共有して素通りする)。
 */
import assert from 'node:assert/strict'
import { register } from 'node:module'

// Node の型除去は拡張子の無い相対 import を解決しない(`eval_command_defs.mjs` と同じフック)。
register(
  'data:text/javascript,' +
    encodeURIComponent(`
export async function resolve(specifier, context, next) {
  try {
    return await next(specifier, context)
  } catch (error) {
    if (!specifier.startsWith('.') || specifier.endsWith('.ts')) throw error
    return next(specifier + '.ts', context)
  }
}`),
)
const { entityHandles, snapSketchPoint } = await import(new URL('../web/src/entityKinds.ts', import.meta.url).href)

const ENTITIES = {
  circle: { kind: 'circle', params: { cx: 60, cy: 20, radius: 10 } },
  rectangle: { kind: 'rectangle', params: { x: 10, y: 10, width: 30, height: 20 } },
  tilted: { kind: 'rectangle', params: { x: 85, y: 5, width: 24, height: 12, angle: 35 } },
  line: { kind: 'line', params: { x0: 0, y0: 0, x1: 10, y1: 0 } },
}
const entityOf = (id) => ENTITIES[id]
const snap = (u, v, hit, roundGrid = true) => snapSketchPoint(u, v, hit, entityOf, undefined, roundGrid)

// ============================================================== 何にも当たっていない
assert.deepEqual(snap(73.4, 5.35, null), [73, 5], '点を待っている間は 1mm へ丸める')
assert.deepEqual(snap(73.4, 5.35, null, false), [73.4, 5.35], '選択のクリックでは丸めない')
assert.deepEqual(snap(1.2, 3.4, { entityId: 'gone', point: null }), [1, 3], '図形が引けなければ丸めへ落ちる')

// ============================================================== snapAt(角・交点)はそのまま
assert.deepEqual(
  snap(1.2, 3.4, { entityId: 'circle', point: null, snapAt: [7.25, 8.5] }),
  [7.25, 8.5],
  'snapAt があれば図形へ射影し直さない',
)

// ============================================================== 点マーカーの上はその点に完全一致
{
  const corner = entityHandles('rectangle', ENTITIES.rectangle.params)[0]
  assert.deepEqual(
    snap(corner.u + 0.31, corner.v - 0.27, { entityId: 'rectangle', point: corner.key }),
    [corner.u, corner.v],
    '点マーカーに当たっていれば、ずれて押してもその点の座標',
  )
  const end = entityHandles('line', ENTITIES.line.params).find((handle) => handle.key === 'x1')
  assert.deepEqual(snap(9.6, 0.4, { entityId: 'line', point: 'x1' }), [end.u, end.v])
}

// ============================================================== 円の線の上は半径ちょうど
{
  const { cx, cy, radius } = ENTITIES.circle.params
  for (const [u, v] of [[cx + radius + 0.4, cy + 0.4], [cx - 6.8, cy + 7.9], [cx + 0.2, cy - radius + 0.5]]) {
    const [su, sv] = snap(u, v, { entityId: 'circle', point: null })
    assert.ok(
      Math.abs(Math.hypot(su - cx, sv - cy) - radius) < 1e-9,
      `円の上に落ちない: (${u}, ${v}) → (${su}, ${sv})`,
    )
  }
}

// ============================================================== 傾いた長方形は、傾いた辺の上
{
  const { x, y, width, angle } = ENTITIES.tilted.params
  const rad = (angle * Math.PI) / 180
  const start = [x, y]
  const end = [x + width * Math.cos(rad), y + width * Math.sin(rad)]
  const middle = [(start[0] + end[0]) / 2, (start[1] + end[1]) / 2]
  // 辺の中ほどから、辺に垂直な向きへ 0.4mm 外す(軸平行前提の snap なら外接箱の辺へ落ちてミリ単位で外れる)
  const [su, sv] = snap(middle[0] + 0.4 * Math.sin(rad), middle[1] - 0.4 * Math.cos(rad), {
    entityId: 'tilted',
    point: null,
  })
  const du = end[0] - start[0]
  const dv = end[1] - start[1]
  const off = Math.abs((su - start[0]) * dv - (sv - start[1]) * du) / width
  const along = ((su - start[0]) * du + (sv - start[1]) * dv) / width
  assert.ok(off < 1e-9, `傾いた辺に乗らない: 辺からの距離 ${off}mm`)
  assert.ok(along > 0 && along < width, `辺の範囲の外へ落ちた: 辺に沿う位置 ${along}mm`)
}

console.log('ok')
