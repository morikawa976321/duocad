/**
 * `web/src/screenPick.ts` の純関数を**実際に評価する**ための小さな口。
 * `tests/test_screen_pick.py` が `node` でこれを走らせる(`tests/eval_grid_scale.mjs` と
 * 同じやり方 —— ソースの文字列比較ではなく実行結果で見る)。
 *
 * このロジックには**Python側の対応物が無い**(当たり判定はTSにしか無い)ので、
 * 2言語を突き合わせる代わりに、ここで直接 assert して失敗したら非ゼロで終了する。
 *
 * Node は `.ts` をそのまま読める(型注釈の除去。Node 23.6以降は既定で有効)。
 */
import assert from 'node:assert/strict'

import { distSqPointSegment, pickNearestWithinRadius } from '../web/src/screenPick.ts'

// ============================================================== distSqPointSegment
// 線分の真上(距離0)。
assert.equal(distSqPointSegment(5, 0, 0, 0, 10, 0), 0, '線分上の点の距離が0でない')

// 垂足が線分の内側にあるとき: 垂線の長さ。
assert.equal(distSqPointSegment(5, 3, 0, 0, 10, 0), 9, '垂線の距離²が合わない')

// **垂足が線分の外**: 端点までの距離になる(t を [0,1] に clamp している)。
// 無限直線への距離なら 0 になってしまう位置で確かめる。
assert.equal(distSqPointSegment(-4, 0, 0, 0, 10, 0), 16, '始点の外で端点距離になっていない')
assert.equal(distSqPointSegment(13, 4, 0, 0, 10, 0), 25, '終点の外で端点距離になっていない')

// 斜めの線分でも同じ(y=x の線分 (0,0)-(10,10) と、その延長上の外側の点)。
{
  // (-3,-3) は無限直線の上にあるが、線分の外なので始点 (0,0) までの距離になる。
  assert.equal(distSqPointSegment(-3, -3, 0, 0, 10, 10), 18, '斜めの線分で端点へ落ちていない')
  // (5,5) は線分の上。
  assert.equal(distSqPointSegment(5, 5, 0, 0, 10, 10), 0, '斜めの線分上で0にならない')
  // (0,10) から y=x への垂線の足は (5,5)。距離² = 50。
  assert.equal(distSqPointSegment(0, 10, 0, 0, 10, 10), 50, '斜めの線分への垂線が合わない')
}

// **長さ0の線分**(同じ点が2つ並んだ折れ線)。0除算で NaN にせず、その点までの距離を返す。
assert.equal(distSqPointSegment(3, 4, 7, 7, 7, 7), 25, '退化した線分で点距離にならない')

// 引数の並び(A と B)を入れ替えても同じ値(距離は向きを持たない)。
for (const [px, py] of [[5, 3], [-4, 0], [13, 4], [0, 0]]) {
  assert.equal(
    distSqPointSegment(px, py, 0, 0, 10, 0),
    distSqPointSegment(px, py, 10, 0, 0, 0),
    `端点の順序で距離が変わった: (${px},${py})`,
  )
}

// ============================================================== pickNearestWithinRadius
// 空の候補。
assert.equal(pickNearestWithinRadius(6, []), null, '候補が無いのに何かを返した')

// **境界は inclusive**: 距離ちょうど radiusPx は hit、そこから少しでも外れたら miss。
{
  const radius = 6
  assert.equal(
    pickNearestWithinRadius(radius, [{ item: 'edge', distSq: radius * radius }]),
    'edge',
    '距離ちょうど半径が hit になっていない',
  )
  const outside = (radius + 1e-9) ** 2
  assert.equal(
    pickNearestWithinRadius(radius, [{ item: 'edge', distSq: outside }]),
    null,
    '半径を超えた候補が hit になった',
  )
}

// 半径内に複数あれば**距離が最小のもの**(渡した順ではない)。
assert.equal(
  pickNearestWithinRadius(6, [
    { item: 'far', distSq: 8 * 8 },
    { item: 'near', distSq: 3 * 3 },
    { item: 'mid', distSq: 5 * 5 },
  ]),
  'near',
  '半径内で最も近い候補を返していない',
)

// 半径の外にしか候補が無ければ null(「一番近いもの」を無条件に返さない)。
assert.equal(
  pickNearestWithinRadius(6, [{ item: 'far', distSq: 8 * 8 }]),
  null,
  '半径外の候補を拾ってしまった',
)

// **同着は先に来た候補が勝つ**(呼び出し側が優先順位の順に渡す約束)。
assert.equal(
  pickNearestWithinRadius(6, [
    { item: 'first', distSq: 4 },
    { item: 'second', distSq: 4 },
  ]),
  'first',
  '同着で後の候補に入れ替わった',
)

// NaN は黙って勝たない(射影が壊れた候補が最短として通ると、当たっていない物が取れる)。
assert.equal(
  pickNearestWithinRadius(6, [{ item: 'broken', distSq: Number.NaN }]),
  null,
  'NaN の候補が hit になった',
)
assert.equal(
  pickNearestWithinRadius(6, [
    { item: 'broken', distSq: Number.NaN },
    { item: 'real', distSq: 1 },
  ]),
  'real',
  'NaN の候補が最短を横取りした',
)

// 半径0でも「ちょうど乗っている」候補だけは取れる(inclusive の帰結)。
assert.equal(pickNearestWithinRadius(0, [{ item: 'exact', distSq: 0 }]), 'exact', '半径0で真上が取れない')
assert.equal(pickNearestWithinRadius(0, [{ item: 'off', distSq: 1e-12 }]), null, '半径0で外れた点が取れた')

// ============================================================== 2つを組み合わせた実使用形
// 折れ線(点列)の最小距離を取ってから半径で判定する —— `viewport.ts` の `pickLinesAt` が
// やっていることと同じ形。カーソル (0,0)、L字の折れ線。
{
  const polyline = [
    [10, 10],
    [10, -10],
    [30, -10],
  ]
  let best = Infinity
  for (let at = 0; at < polyline.length - 1; at += 1) {
    const [ax, ay] = polyline[at]
    const [bx, by] = polyline[at + 1]
    best = Math.min(best, distSqPointSegment(0, 0, ax, ay, bx, by))
  }
  // 最も近いのは 1本目の線分 x=10 の (10,0) で、距離は 10。
  assert.equal(best, 100, '折れ線の最小距離が合わない')
  assert.equal(pickNearestWithinRadius(6, [{ item: 'line', distSq: best }]), null, '10px 先の線が 6px で取れた')
  assert.equal(pickNearestWithinRadius(10, [{ item: 'line', distSq: best }]), 'line', '10px 先の線が 10px で取れない')
}

console.log('screen pick: all assertions passed')
