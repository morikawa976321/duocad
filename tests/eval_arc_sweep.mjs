/**
 * `web/src/entityKinds.ts` の円弧の掃引量(`arcSweepDeg`)を、描画・吸着・交点の3本で**実際に評価する**口。
 * `tests/test_ts_evaluators.py` が `node` でこれを走らせる。
 *
 * コアは掃引量を常に `(end_deg - start_deg) % 360` にする(`duocad/kernel_sketch.py` の `_edges_arc`)。MCP の `add_arc` は
 * 角度の範囲を制限しないので、差が 360 以上や負の弧がコアに届く。落ちたら、そのとき画面(幽霊・当たり・吸着・交点)がコアと別の弧を指す。
 */
import assert from 'node:assert/strict'

import './ts_resolve_hook.mjs'
const { arcSweepDeg, entityPoints, sketchIntersections, snapToEntity } = await import(
  new URL('../web/src/entityKinds.ts', import.meta.url).href
)

const near = (a, b, label) => assert.ok(Math.abs(a - b) < 1e-9, `${label}: ${a} != ${b}`)

// ============================================================== arcSweepDeg: Python の (end - start) % 360 と同じ。0 は一周。
near(arcSweepDeg(0, 90), 90, '素直な弧')
near(arcSweepDeg(350, 10), 20, '0度をまたぐ弧(end < start)')
near(arcSweepDeg(0, 400), 40, '差が 360 を超える弧はコアと同じく 40 度')
near(arcSweepDeg(10, -720), 350, '差が負で -360 を超える弧(Python: -730 % 360 = 350)')
near(arcSweepDeg(0, -90), 270, '時計回りに見える指定もコアは反時計回りに掃く')
near(arcSweepDeg(0, 360), 360, '差が 360 は一周として描く')
near(arcSweepDeg(30, 30), 360, '差が 0 も一周として描く(これまでの描き方)')
near(arcSweepDeg(10, 370.5), 0.5, '端数は保たれる')

// ============================================================== 折れ線・吸着・交点が「同じ始点で正規化した弧」と一致する
// [実際のパラメータ, 同じ弧を差 (0,360] で書いたもの]
const CASES = [
  [{ start_deg: 0, end_deg: 400 }, { start_deg: 0, end_deg: 40 }],
  [{ start_deg: 10, end_deg: -720 }, { start_deg: 10, end_deg: 360 }],
  [{ start_deg: 350, end_deg: 10 }, { start_deg: 350, end_deg: 370 }],
  [{ start_deg: -200, end_deg: 500 }, { start_deg: -200, end_deg: -200 + 340 }],
]
for (const [actual, normalized] of CASES) {
  const label = `${actual.start_deg}→${actual.end_deg}`
  const a = { cx: 3, cy: -2, radius: 10, ...actual }
  const n = { cx: 3, cy: -2, radius: 10, ...normalized }

  const pa = entityPoints('arc', a)
  const pn = entityPoints('arc', n)
  assert.equal(pa.length, pn.length, `${label}: 折れ線の点数`)
  pa.forEach((point, i) => {
    near(point[0], pn[i][0], `${label}: 点${i}のu`)
    near(point[1], pn[i][1], `${label}: 点${i}のv`)
  })

  for (const probe of [[3 + 20, -2], [3, -2 + 20], [3 - 20, -2], [3, -2 - 20], [3 + 14, -2 + 14]]) {
    const sa = snapToEntity('arc', a, probe[0], probe[1])
    const sn = snapToEntity('arc', n, probe[0], probe[1])
    near(sa[0], sn[0], `${label}: 吸着のu @${probe}`)
    near(sa[1], sn[1], `${label}: 吸着のv @${probe}`)
  }
}

// 交点: 円(中心 0, 半径 10)を水平線 v=5 が 30° と 150° で切る。弧 0→400 はコアでは 0〜40° なので 30° だけが交点。
{
  const line = { entityId: 'line', kind: 'line', params: { x0: -20, y0: 5, x1: 20, y1: 5 } }
  const arc = { entityId: 'arc', kind: 'arc', params: { cx: 0, cy: 0, radius: 10, start_deg: 0, end_deg: 400 } }
  const hits = sketchIntersections([line, arc])
  assert.equal(hits.length, 1, `コアでは 0〜40° の弧。交点は 1 つのはず: ${JSON.stringify(hits)}`)
  near(hits[0].u, Math.sqrt(75), '交点のu(30°)')
  near(hits[0].v, 5, '交点のv')
}

console.log('entityKinds.ts(円弧の掃引): 全ての想定を満たした')
