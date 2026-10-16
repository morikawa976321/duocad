/**
 * `web/src/commands/sketchGeometry.ts` を**実際に評価する**口。
 * `tests/test_ts_evaluators.py` が `node` でこれを走らせる。
 *
 * ここで見るのは、スケッチの描く4verbが「クリックで集めた点」と「打った寸法」から出す
 * 形そのもの。とくに円弧は**掃引の向き**(コアは `(end-start) % 360` で必ず反時計回り、
 * 幽霊は差の符号どおり)を `build` 側で閉じているので、符号が破れると**幽霊と確定結果が
 * 別の弧になる**。ちょうど半円の場合が最も破れやすい。
 *
 * Node は `.ts` をそのまま読める(型注釈の除去)。`sketchGeometry.ts` の import は
 * `import type` だけなので、実行時の依存は無い。
 */
import assert from 'node:assert/strict'
import {
  arcFromPoints,
  circleFromPoints,
  ellipseFromPoints,
  lineFromPoints,
  rectFromPoints,
} from '../web/src/commands/sketchGeometry.ts'

function close(actual, expected, what) {
  assert.ok(
    Math.abs(actual - expected) < 1e-2,
    `${what}: ${actual} (期待 ${expected})`,
  )
}

// ============================================================== 線分
{
  // 空欄なら2点そのまま
  const plain = lineFromPoints([0, 0], [3, 4], undefined, undefined)
  close(plain.p1[0], 3, '線分の終点u')
  close(plain.p1[1], 4, '線分の終点v')

  // 長さ50・角度30を打つ(クリックは始点だけ) —— マウスの位置は無視される
  const typedBoth = lineFromPoints([1, 2], [99, 99], 50, 30)
  close(typedBoth.p1[0], 1 + 50 * Math.cos(Math.PI / 6), '長さ・角度を打った終点u')
  close(typedBoth.p1[1], 2 + 50 * Math.sin(Math.PI / 6), '長さ・角度を打った終点v')

  // 角度だけ打てば「その向きに、マウスまでの長さ」(Fusionの角度ロック)
  const angleOnly = lineFromPoints([0, 0], [0, 10], undefined, 0)
  close(angleOnly.p1[0], 10, '角度だけ打った終点u')
  close(angleOnly.p1[1], 0, '角度だけ打った終点v')

  // 長さだけ打てば向きはマウスが握る
  const lengthOnly = lineFromPoints([0, 0], [0, 3], 10, undefined)
  close(lengthOnly.p1[0], 0, '長さだけ打った終点u')
  close(lengthOnly.p1[1], 10, '長さだけ打った終点v')

  assert.equal(lineFromPoints([5, 5], [5, 5], undefined, undefined), null, '長さ0の線分は作れない')
  assert.equal(lineFromPoints([0, 0], [1, 0], 0, undefined), null, '長さ0を打っても作れない')
  assert.equal(lineFromPoints([0, 0], [1, 0], -3, undefined), null, '負の長さは作れない')
}

// ============================================================== 長方形
{
  // 2点だけ(右上へ引く)
  const drag = rectFromPoints([10, 20], [40, 60], undefined, undefined)
  assert.deepEqual(drag.corner, [10, 20], '右上へ引いた長方形の左下')
  close(drag.width, 30, '右上へ引いた幅')
  close(drag.height, 40, '右上へ引いた高さ')

  // 左下へ引くと左下の角が動く(種別は軸平行のまま)
  const back = rectFromPoints([40, 60], [10, 20], undefined, undefined)
  assert.deepEqual(back.corner, [10, 20], '左下へ引いた長方形の左下')
  close(back.width, 30, '左下へ引いた幅')

  // 角を置いて寸法だけ打つ(既存の打鍵の道。`opposite === corner` で正側に倒れる)
  const keyed = rectFromPoints([30, -30], [30, -30], 10, 6)
  assert.deepEqual(keyed.corner, [30, -30], '寸法だけ打った長方形の左下')
  close(keyed.width, 10, '寸法だけ打った幅')
  close(keyed.height, 6, '寸法だけ打った高さ')

  // **打った寸法も引いた向きへ伸ばす**(始点の反対側へ回ると向きが反転する)
  const flipped = rectFromPoints([0, 0], [-1, -1], 10, 6)
  close(flipped.corner[0], -10, '左下へ回したときの左下u')
  close(flipped.corner[1], -6, '左下へ回したときの左下v')

  // 幅だけ打てば高さはマウスが握る
  const half = rectFromPoints([0, 0], [1, 25], 10, undefined)
  close(half.width, 10, '幅だけ打ったときの幅')
  close(half.height, 25, '幅だけ打ったときの高さ')

  assert.equal(rectFromPoints([0, 0], [0, 5], undefined, undefined), null, '幅0の長方形は作れない')
  assert.equal(rectFromPoints([0, 0], [5, 5], 0, 5), null, '幅0を打っても作れない')
}

// ============================================================== 円
{
  const dragged = circleFromPoints([1, 1], [4, 5], undefined)
  close(dragged.diameter, 10, '円周の点までの距離の2倍が直径')

  const keyed = circleFromPoints([0, 0], [0, 0], 20)
  close(keyed.diameter, 20, '打った直径')

  assert.equal(circleFromPoints([0, 0], [0, 0], undefined), null, '直径0の円は作れない')
  assert.equal(circleFromPoints([0, 0], [1, 0], -1), null, '負の直径は作れない')
}

// ============================================================== 楕円
{
  // 1本目の軸の端 (3,4) → 向き atan2(4,3)、横の直径 10。3点目は1本目の軸の直線から 2 離れている → 縦の直径 4。
  const dragged = ellipseFromPoints([0, 0], [3, 4], [-1.6 + 0.6, 1.2 + 0.8], {})
  close(dragged.width, 10, '中心から軸の端までの2倍が横の直径')
  close(dragged.angle, (Math.atan2(4, 3) * 180) / Math.PI, '軸の端の向きが角度')
  close(dragged.height, 4, '1本目の軸の直線からの距離の2倍が縦の直径')

  const keyed = ellipseFromPoints([5, 5], undefined, undefined, { width: 30, height: 12, angle: 20 })
  assert.deepEqual(keyed, { center: [5, 5], width: 30, height: 12, angle: 20 }, '3つとも打てば点は要らない')

  const tall = ellipseFromPoints([0, 0], [2, 0], [0, 9], {})
  close(tall.height, 18, '縦の方が長くてよい')

  assert.equal(ellipseFromPoints([0, 0], [3, 0], [5, 0], {}), null, '3点目が1本目の軸の上なら縦の直径0')
  assert.equal(ellipseFromPoints([0, 0], undefined, [0, 3], { angle: 0 }), null, '横の直径を決めるものが無い')
  assert.equal(ellipseFromPoints([0, 0], [3, 0], [0, 3], { width: -1 }), null, '負の直径は作れない')
}

// ============================================================== 円弧(手計算値)
// p0=(-1,0) / p1=(1,0) の弦(長さ2)に対して、`via` の位置で劣弧・優弧・半円を作る。
{
  const minor = arcFromPoints([-1, 0], [1, 0], [0, 0.5], undefined)
  close(minor.center[0], 0, '劣弧の中心u')
  close(minor.center[1], -0.75, '劣弧の中心v')
  close(minor.radius, 1.25, '劣弧の半径')
  close(minor.start_deg, 36.87, '劣弧の開始角')
  close(minor.end_deg, 143.13, '劣弧の終了角')

  const major = arcFromPoints([-1, 0], [1, 0], [0, 2], undefined)
  close(major.center[1], 0.75, '優弧の中心v')
  close(major.radius, 1.25, '優弧の半径')
  close(major.start_deg, 323.13, '優弧の開始角')
  close(major.end_deg, 576.87, '優弧の終了角')

  // **ちょうど半円** —— 優弧/劣弧の大小比較で向きを決めると破れる場合。
  const half = arcFromPoints([-1, 0], [1, 0], [0, 1], undefined)
  close(half.center[0], 0, '半円の中心u')
  close(half.center[1], 0, '半円の中心v')
  close(half.radius, 1, '半円の半径')
  close(half.start_deg, 0, '半円の開始角')
  close(half.end_deg, 180, '半円の終了角')

  // 半径を打つと中心を置き直す(via はもう円の上に無いので、向きだけ引き継ぐ)
  const wide = arcFromPoints([-1, 0], [1, 0], [0, 2], 5)
  close(wide.center[0], 0, '半径を打った優弧の中心u')
  close(wide.center[1], Math.sqrt(24), '半径を打った優弧の中心v')
  close(wide.start_deg, 281.537, '半径を打った優弧の開始角')
  close(wide.end_deg, 618.463, '半径を打った優弧の終了角')

  // 半径を打っても劣弧は劣弧のまま(中心は via の反対側へ行く)
  const narrow = arcFromPoints([-1, 0], [1, 0], [0, 0.5], 5)
  close(narrow.center[1], -Math.sqrt(24), '半径を打った劣弧の中心v')
}

// ============================================================== 円弧の不変条件
{
  // **常に start <= end**(コアの `(end-start) % 360` が反時計回りにしか掃かないため)。
  // 弦の左右・優弧劣弧・半円をひととおり回して、掃引が (0,360) に収まることも見る。
  for (const via of [[0, 0.5], [0, -0.5], [0, 2], [0, -2], [0, 1], [0, -1], [0.5, 0.9]]) {
    for (const radius of [undefined, 1.5, 5]) {
      const arc = arcFromPoints([-1, 0], [1, 0], via, radius)
      if (arc === null) continue
      const what = `via=${JSON.stringify(via)} radius=${radius}`
      assert.ok(arc.start_deg <= arc.end_deg, `開始角が終了角を超えた: ${what}`)
      const sweep = arc.end_deg - arc.start_deg
      assert.ok(sweep > 0 && sweep < 360, `掃引が (0,360) の外: ${sweep} (${what})`)
      // 弧の中点が via と同じ側にある(= 見えている幽霊と同じ弧を送っている)
      const mid = ((arc.start_deg + sweep / 2) * Math.PI) / 180
      const my = arc.center[1] + arc.radius * Math.sin(mid)
      assert.ok(my * via[1] > 0, `弧の中点が via と反対側: ${what}`)
      // 両端が本当に p0 / p1 を通る
      const s = (arc.start_deg * Math.PI) / 180
      const e = (arc.end_deg * Math.PI) / 180
      const ends = [
        [arc.center[0] + arc.radius * Math.cos(s), arc.center[1] + arc.radius * Math.sin(s)],
        [arc.center[0] + arc.radius * Math.cos(e), arc.center[1] + arc.radius * Math.sin(e)],
      ]
      for (const target of [[-1, 0], [1, 0]]) {
        const hit = ends.some(
          (point) => Math.hypot(point[0] - target[0], point[1] - target[1]) < 1e-6,
        )
        assert.ok(hit, `端点 ${JSON.stringify(target)} を通らない: ${what}`)
      }
    }
  }

  assert.equal(arcFromPoints([-1, 0], [1, 0], [0, 0], undefined), null, '3点が一直線なら作れない')
  assert.equal(arcFromPoints([0, 0], [0, 0], [1, 1], undefined), null, '始点と終点が同じなら作れない')
  assert.equal(arcFromPoints([-1, 0], [1, 0], [0, 1], 0.9), null, '弦の半分より小さい半径は作れない')
  assert.ok(arcFromPoints([-1, 0], [1, 0], [0, 1], 1) !== null, '弦のちょうど半分の半径は作れる')
}

console.log('sketchGeometry: OK')
