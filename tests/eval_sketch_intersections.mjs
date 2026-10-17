/**
 * `web/src/entityKinds.ts` の `sketchIntersections`(編集中スケッチの図形どうしの交点。スナップが本物の点と
 * 同じ優先度で拾う)を**実際に評価する**口。`tests/test_ts_evaluators.py` が `node` でこれを走らせる。
 *
 * 外接矩形が離れた組を解かない近道(issue #172)が、全組を解いた答えと同じ交点を返すかを見る。落ちたら、
 * 端で接する図形どうし(長方形の辺の上に置いた線の端・円に接する線)の交点が消え、そこへ吸い付かなくなる。
 * 答え合わせの全組版は製品の `entityIntersectables` から区間を借りる(近道だけを外した版)。
 */
import assert from 'node:assert/strict'

import './ts_resolve_hook.mjs'
const { entityIntersectables, sketchIntersections } = await import(new URL('../web/src/entityKinds.ts', import.meta.url).href)

/** 線分どうし・線分と円の交点を全組で素朴に解く(近道なし)。製品の式と同じ規則(端を含む・許容なし)。 */
function bruteForce(handles) {
  const out = []
  for (let i = 0; i < handles.length; i += 1) {
    for (let j = i + 1; j < handles.length; j += 1) {
      for (const a of entityIntersectables(handles[i].kind, handles[i].params)) {
        for (const b of entityIntersectables(handles[j].kind, handles[j].params)) {
          for (const hit of solve(a, b)) out.push({ u: hit[0], v: hit[1], entityIds: [handles[i].entityId, handles[j].entityId] })
        }
      }
    }
  }
  return out
}

function solve(a, b) {
  if (a.kind === 'segment' && b.kind === 'segment') {
    const dax = a.b[0] - a.a[0]
    const day = a.b[1] - a.a[1]
    const dbx = b.b[0] - b.a[0]
    const dby = b.b[1] - b.a[1]
    const denom = dax * dby - day * dbx
    if (denom === 0) return []
    const dx = b.a[0] - a.a[0]
    const dy = b.a[1] - a.a[1]
    const t = (dx * dby - dy * dbx) / denom
    const s = (dx * day - dy * dax) / denom
    if (t < 0 || t > 1 || s < 0 || s > 1) return []
    return [[a.a[0] + t * dax, a.a[1] + t * day]]
  }
  if (a.kind === 'circle' && b.kind === 'segment') return solve(b, a)
  if (a.kind === 'segment' && b.kind === 'circle') {
    const dx = a.b[0] - a.a[0]
    const dy = a.b[1] - a.a[1]
    const fx = a.a[0] - b.center[0]
    const fy = a.a[1] - b.center[1]
    const qa = dx * dx + dy * dy
    if (qa === 0) return []
    const qb = 2 * (fx * dx + fy * dy)
    const qc = fx * fx + fy * fy - b.radius * b.radius
    const disc = qb * qb - 4 * qa * qc
    if (disc < 0) return []
    const out = []
    for (const t of [(-qb - Math.sqrt(disc)) / (2 * qa), (-qb + Math.sqrt(disc)) / (2 * qa)]) {
      if (t >= 0 && t <= 1) out.push([a.a[0] + t * dx, a.a[1] + t * dy])
    }
    return out
  }
  // 円どうし
  const dx = b.center[0] - a.center[0]
  const dy = b.center[1] - a.center[1]
  const d = Math.hypot(dx, dy)
  if (d === 0 || d > a.radius + b.radius || d < Math.abs(a.radius - b.radius)) return []
  const along = (d * d + a.radius * a.radius - b.radius * b.radius) / (2 * d)
  const heightSq = a.radius * a.radius - along * along
  const height = heightSq > 0 ? Math.sqrt(heightSq) : 0
  const mx = a.center[0] + (along * dx) / d
  const my = a.center[1] + (along * dy) / d
  if (height === 0) return [[mx, my]]
  const ox = -(dy / d) * height
  const oy = (dx / d) * height
  return [[mx + ox, my + oy], [mx - ox, my - oy]]
}

const key = (p) => `${p.entityIds.join('|')}@${p.u.toFixed(9)},${p.v.toFixed(9)}`
const sorted = (points) => points.map(key).sort()

/** 線分・長方形・円だけの題材(円弧は `solve` に無いので使わない)。 */
function sketch(seed, count) {
  let state = seed
  const rand = () => ((state = (state * 1103515245 + 12345) % 2147483648) / 2147483648)
  const handles = []
  for (let i = 0; i < count; i += 1) {
    const x = Math.round(rand() * 40)
    const y = Math.round(rand() * 40)
    const pick = rand()
    if (pick < 0.45) {
      handles.push({ entityId: `e${i}`, kind: 'line', params: { x0: x, y0: y, x1: x + Math.round(rand() * 20 - 10), y1: y + Math.round(rand() * 20 - 10) } })
    } else if (pick < 0.8) {
      handles.push({ entityId: `e${i}`, kind: 'rectangle', params: { x, y, width: 1 + Math.round(rand() * 10), height: 1 + Math.round(rand() * 10) } })
    } else {
      handles.push({ entityId: `e${i}`, kind: 'circle', params: { cx: x, cy: y, diameter: 2 * (1 + Math.round(rand() * 6)) } })
    }
  }
  return handles
}

// 端で接するだけの組(外接矩形がちょうど接する): 長方形の右辺の上に始点を置いた線、円に接する水平線。
const touching = [
  { entityId: 'r', kind: 'rectangle', params: { x: 0, y: 0, width: 10, height: 10 } },
  { entityId: 'l', kind: 'line', params: { x0: 10, y0: 5, x1: 20, y1: 5 } },
  { entityId: 'c', kind: 'circle', params: { cx: 30, cy: 0, diameter: 10 } },
  { entityId: 't', kind: 'line', params: { x0: 20, y0: 5, x1: 40, y1: 5 } },
]
for (const handles of [touching, sketch(1, 40), sketch(7, 80), sketch(42, 120)]) {
  for (const handle of handles) {
    assert.ok(entityIntersectables(handle.kind, handle.params).every((curve) => curve.kind !== 'arc'), `題材に円弧が混ざった: ${handle.kind}`)
  }
  assert.deepEqual(sorted(sketchIntersections(handles)), sorted(bruteForce(handles)))
}
assert.ok(sketchIntersections(touching).some((p) => p.entityIds.includes('r') && p.entityIds.includes('l')), '端で接する長方形と線の交点が無い')
assert.ok(sketchIntersections(touching).some((p) => p.entityIds.includes('c') && p.entityIds.includes('t')), '円に接する線の交点が無い')
// 楕円と線分: 交点が楕円と線分の両方に乗る。軸に平行な線は手計算値と一致する。楕円と曲線の組は求めない。
{
  const ellipse = { entityId: 'e', kind: 'ellipse', params: { cx: 10, cy: 5, width: 20, height: 8, angle: 30 } }
  const rad = (30 * Math.PI) / 180
  const onEllipse = ([u, v]) => {
    const x = (u - 10) * Math.cos(rad) + (v - 5) * Math.sin(rad)
    const y = -(u - 10) * Math.sin(rad) + (v - 5) * Math.cos(rad)
    return Math.abs((x / 10) ** 2 + (y / 4) ** 2 - 1) < 1e-9
  }
  const lines = [
    { entityId: 'a', kind: 'line', params: { x0: -5, y0: 5, x1: 25, y1: 5 } },
    { entityId: 'b', kind: 'line', params: { x0: 10, y0: 5, x1: 30, y1: 20 } },
    { entityId: 'f', kind: 'line', params: { x0: 40, y0: 40, x1: 50, y1: 41 } },
  ]
  const hits = sketchIntersections([ellipse, ...lines]).filter((p) => p.entityIds.includes('e'))
  assert.equal(hits.filter((p) => p.entityIds.includes('a')).length, 2, '楕円を横切る線の交点は2つ')
  assert.equal(hits.filter((p) => p.entityIds.includes('b')).length, 1, '中心から出る線の交点は1つ')
  assert.equal(hits.filter((p) => p.entityIds.includes('f')).length, 0, '離れた線に交点は無い')
  for (const hit of hits) assert.ok(onEllipse([hit.u, hit.v]), `交点が楕円に乗らない: (${hit.u}, ${hit.v})`)

  const level = { entityId: 'g', kind: 'ellipse', params: { cx: 0, cy: 0, width: 20, height: 8, angle: 0 } }
  const vertical = { entityId: 'v', kind: 'line', params: { x0: 6, y0: -10, x1: 6, y1: 10 } }
  const expected = 4 * Math.sqrt(1 - 0.36)
  const vs = sketchIntersections([level, vertical]).map((p) => p.v).sort((a, b) => a - b)
  assert.ok(Math.abs(vs[0] + expected) < 1e-12 && Math.abs(vs[1] - expected) < 1e-12, `x=6 の交点が ±${expected} でない: ${vs}`)

  const circle = { entityId: 'c', kind: 'circle', params: { cx: 0, cy: 0, diameter: 12 } }
  const other = { entityId: 'h', kind: 'ellipse', params: { cx: 3, cy: 0, width: 20, height: 4, angle: 90 } }
  assert.equal(sketchIntersections([level, circle, other]).length, 0, '楕円と曲線の交点は求めない(docs/ELLIPSE.md)')
}
console.log('ok sketchIntersections')
