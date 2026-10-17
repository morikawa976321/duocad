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

import './ts_resolve_hook.mjs'
const { entityHandles, snapSketchPoint } = await import(new URL('../web/src/entityKinds.ts', import.meta.url).href)
const { viewResolution } = await import(new URL('../web/src/adaptiveResolution.ts', import.meta.url).href)

const ENTITIES = {
  circle: { kind: 'circle', params: { cx: 60, cy: 20, diameter: 20 } },
  ellipse: { kind: 'ellipse', params: { cx: -40, cy: 15, width: 30, height: 12, angle: 25 } },
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
  const { cx, cy, diameter } = ENTITIES.circle.params
  const radius = diameter / 2
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

// ============================================================== 楕円の線の上は楕円そのもの(折れ線の上ではない)
{
  const { cx, cy, width, height, angle } = ENTITIES.ellipse.params
  const rad = (angle * Math.PI) / 180
  const local = (u, v) => [(u - cx) * Math.cos(rad) + (v - cy) * Math.sin(rad), -(u - cx) * Math.sin(rad) + (v - cy) * Math.cos(rad)]
  for (const [u, v] of [[cx + 16, cy + 7], [cx - 3, cy + 6.5], [cx + 1, cy - 9], [cx - 14, cy - 4]]) {
    for (const resolution of [undefined, { angularDeflection: 0.6, chordTolerance: 2 }]) {
      const [su, sv] = snapSketchPoint(u, v, { entityId: 'ellipse', point: null }, entityOf, resolution, true)
      const [x, y] = local(su, sv)
      const residual = Math.abs((x / (width / 2)) ** 2 + (y / (height / 2)) ** 2 - 1)
      assert.ok(residual < 1e-9, `楕円の上に落ちない: (${u}, ${v}) → (${su}, ${sv}) 残差 ${residual}`)
      // 最近点なら、落ちた点からカーソルへの向きは楕円の接線に直交する。
      const tx = -(width / 2) * (y / (height / 2))
      const ty = (height / 2) * (x / (width / 2))
      const [qx, qy] = local(u, v)
      const dot = ((qx - x) * tx + (qy - y) * ty) / (Math.hypot(qx - x, qy - y) * Math.hypot(tx, ty))
      assert.ok(Math.abs(dot) < 1e-9, `楕円の最近点でない: (${u}, ${v}) → (${su}, ${sv}) cos ${dot}`)
    }
  }
}

// 極端に細い楕円を強く拡大しても最近点に落ちる(ニュートン法は反対側の根へ飛んだ。sol レビュー 2026-10-17 の再現値)。
// 答え合わせは楕円を細かく刻んだ点のうち最も近いもの(刻み 2π/2e6 → 最近点の距離の誤差は 1e-9mm 級)。
{
  const flat = { kind: 'ellipse', params: { cx: 0, cy: 0, width: 20, height: 0.002, angle: 0 } }
  const nearest = (u, v) => {
    let best = Infinity
    const steps = 2_000_000
    for (let i = 0; i < steps; i += 1) {
      const t = (2 * Math.PI * i) / steps
      best = Math.min(best, Math.hypot(10 * Math.cos(t) - u, 0.001 * Math.sin(t) - v))
    }
    return best
  }
  const onCurve = [9.999, -0.000014141782065918276]
  // 長軸のごく近くの内側の点(s + 1 の桁落ち・0 除算。sol レビュー 2026-10-17)も入れる。
  for (const [u, v] of [[9.999, -0.00008], onCurve, [3.2, 0.0004], [-9.9995, 0.0002], [3.2, 1e-18], [3.2, 1e-20], [-3.2, -1e-20]]) {
    for (const worldPerPixel of [0.0001, 0.00001]) {
      const [su, sv] = snapSketchPoint(u, v, { entityId: 'flat', point: null }, () => flat, viewResolution(worldPerPixel, 20), true)
      const got = Math.hypot(su - u, sv - v)
      const best = nearest(u, v)
      assert.ok(got <= best + 1e-9, `細い楕円で最近点に落ちない: (${u}, ${v}) → (${su}, ${sv}) 距離 ${got} / 最近 ${best}`)
    }
  }
}

// 回した細い楕円の長軸の上の点(局所座標へ戻す丸めで短軸の座標が 0 近くの微小値になる)。
{
  const turned = { kind: 'ellipse', params: { cx: 1, cy: 2, width: 20, height: 0.002, angle: 30 } }
  const rad = (30 * Math.PI) / 180
  for (const along of [3.2, -7.5, 0.4]) {
    const u = 1 + along * Math.cos(rad)
    const v = 2 + along * Math.sin(rad)
    const [su, sv] = snapSketchPoint(u, v, { entityId: 'turned', point: null }, () => turned, viewResolution(0.00001, 20), true)
    let best = Infinity
    for (let i = 0; i < 2_000_000; i += 1) {
      const t = (2 * Math.PI * i) / 2_000_000
      const x = 10 * Math.cos(t)
      const y = 0.001 * Math.sin(t)
      best = Math.min(best, Math.hypot(1 + x * Math.cos(rad) - y * Math.sin(rad) - u, 2 + x * Math.sin(rad) + y * Math.cos(rad) - v))
    }
    const got = Math.hypot(su - u, sv - v)
    assert.ok(got <= best + 1e-9, `回した細い楕円で最近点に落ちない: 長軸上 ${along} → 距離 ${got} / 最近 ${best}`)
  }
}

console.log('ok')
