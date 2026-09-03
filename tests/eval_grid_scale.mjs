/**
 * `web/src/gridScale.ts` の純関数を**実際に評価する**ための小さな口。
 * `tests/test_grid_scale.py` が `node` でこれを走らせる(`tests/eval_adaptive_resolution.mjs`
 * と同じやり方 —— ソースの文字列比較ではなく実行結果で見る)。
 *
 * このロジックには**Python側の対応物が無い**(基準グリッドはTSにしか描かれない)ので、
 * 2言語を突き合わせる代わりに、ここで直接 assert して失敗したら非ゼロで終了する。
 *
 * Node は `.ts` をそのまま読める(型注釈の除去。Node 23.6以降は既定で有効)。
 */
import assert from 'node:assert/strict'
import {
  BASE_EXTENT,
  FADE_FULL,
  FADE_MIN,
  TIERS,
  BOUNDS_MARGIN_RATIO,
  boundingSquare,
  extentFor,
  fadeFromSpacingPx,
  mmPerPixel,
  snap125,
  snapForSpan,
  tierAlpha,
  tierAlphas,
} from '../web/src/gridScale.ts'

// ============================================================== 刻みは3段固定(内容で変わらない)
assert.deepEqual([...TIERS], [1, 10, 100], '刻みは 1/10/100mm の3段固定')
assert.ok(FADE_MIN < FADE_FULL, 'フェードの下端は上端より小さい')

// ============================================================== mmPerPixel
// 透視: 2 * tan(fov/2) * distance / height。45度・距離1000mm・900pxの手計算と突き合わせる。
{
  const expected = (2 * Math.tan(Math.PI / 8) * 1000) / 900
  const actual = mmPerPixel({ kind: 'persp', fovDeg: 45, distance: 1000 }, 900)
  assert.ok(Math.abs(actual - expected) < 1e-12, `透視の mmPerPixel が式と合わない: ${actual}`)
}
// 正投影: 視錐台の高さ ÷ 画素。距離という概念が無いので、距離で判定すると破綻する側。
assert.equal(mmPerPixel({ kind: 'ortho', frustumHeight: 900 }, 900), 1)
assert.equal(mmPerPixel({ kind: 'ortho', frustumHeight: 450 }, 900), 0.5)

// **符号も桁も飛ばない**: FOVを90から0へ寄せる間、常に正で単調に減り、0で0へ着地する。
// (正投影の値へ近づく必要は無い —— 別種の投影なので一致しなくてよい。)
{
  let previous = Infinity
  for (let fovDeg = 90; fovDeg >= 0; fovDeg -= 0.5) {
    const value = mmPerPixel({ kind: 'persp', fovDeg, distance: 1000 }, 900)
    assert.ok(Number.isFinite(value) && value >= 0, `fov=${fovDeg} で非有限か負: ${value}`)
    assert.ok(value <= previous + 1e-12, `fov=${fovDeg} で単調性が壊れた: ${value} > ${previous}`)
    // 隣り合う0.5度ぶんで桁が飛ばない(不連続な跳ねが無い)。
    if (Number.isFinite(previous)) {
      assert.ok(previous - value < 0.05, `fov=${fovDeg} で mmPerPixel が跳ねた: ${previous} -> ${value}`)
    }
    previous = value
  }
  assert.equal(mmPerPixel({ kind: 'persp', fovDeg: 0, distance: 1000 }, 900), 0)
}

// 距離にも視野の高さにも比例する(桁の意味が保たれている)。
assert.ok(
  Math.abs(
    mmPerPixel({ kind: 'persp', fovDeg: 45, distance: 2000 }, 900) -
      2 * mmPerPixel({ kind: 'persp', fovDeg: 45, distance: 1000 }, 900),
  ) < 1e-12,
  '距離を2倍にしたら mmPerPixel も2倍のはず',
)

// ============================================================== フェードの規則(1本しかない)
assert.equal(fadeFromSpacingPx(FADE_MIN), 0, `線間隔 ${FADE_MIN}px で不透明度0`)
assert.equal(fadeFromSpacingPx(FADE_FULL), 1, `線間隔 ${FADE_FULL}px で不透明度1`)
assert.equal(fadeFromSpacingPx(0), 0)
assert.equal(fadeFromSpacingPx(-5), 0, '負の線間隔でも0未満にはならない')
assert.equal(fadeFromSpacingPx(1e9), 1, 'どれだけ広くても1を超えない')
assert.equal(fadeFromSpacingPx(Number.POSITIVE_INFINITY), 1)
assert.equal(fadeFromSpacingPx(Number.NaN), 0, 'NaNは「描かない」側へ倒す')

// **ON/OFFの瞬間切替ではない**: 閾値の間に本当に中間値がある(点滅の原理的な排除)。
{
  const middle = fadeFromSpacingPx((FADE_MIN + FADE_FULL) / 2)
  assert.ok(middle > 0.01 && middle < 0.99, `閾値の中央が中間値でない(=瞬間切替になっている): ${middle}`)
}

// ============================================================== tierAlpha: 範囲・単調・連続
{
  // 極端な寄り引きを含む広い範囲を対数で掃き、**3段とも** [0,1] を出ず、mmPerPx について
  // 単調に減り、隣り合う標本の間で跳ねないことを見る(境界 FADE_MIN / FADE_FULL を跨ぐ)。
  const samples = []
  for (let e = -4; e <= 4; e += 0.005) samples.push(10 ** e)

  for (const step of TIERS) {
    let previous = null
    for (const mmPerPx of samples) {
      const alpha = tierAlpha(step, mmPerPx)
      assert.ok(
        alpha >= 0 && alpha <= 1,
        `tierAlpha(${step}, ${mmPerPx}) が [0,1] の外: ${alpha}`,
      )
      if (previous !== null) {
        assert.ok(
          alpha <= previous.alpha + 1e-12,
          `引くほど濃くなっている(単調性が壊れた): step=${step} ${previous.mmPerPx} -> ${mmPerPx}`,
        )
        assert.ok(
          previous.alpha - alpha < 0.05,
          `境界で不透明度が飛んだ(不連続): step=${step} ${previous.mmPerPx} -> ${mmPerPx} ` +
            `(${previous.alpha} -> ${alpha})`,
        )
      }
      previous = { mmPerPx, alpha }
    }
  }
}

// **段の順序**: 同じ視点では、刻みが粗い段ほど濃い(細い段から順に消える)。
for (const mmPerPx of [0.001, 0.01, 0.1, 0.3, 0.5, 1, 3, 10, 30, 100, 1000]) {
  const [fine, mid, coarse] = tierAlphas(mmPerPx)
  assert.ok(
    fine <= mid + 1e-12 && mid <= coarse + 1e-12,
    `段の順序が壊れた(mmPerPx=${mmPerPx}): ${JSON.stringify([fine, mid, coarse])}`,
  )
}

// **100mm段にも特例は無い**: 極端に引けば同じ規則で消える。
assert.equal(tierAlpha(100, 1000), 0, '100mm段も引き切れば消える(段ごとの特例が無いこと)')
assert.equal(tierAlpha(100, 0.01), 1, 'ふつうの視点では100mm段は完全に濃い')

// 退化した入力(描画側が渡しうる値)。
assert.equal(tierAlpha(1, 0), 1, 'mmPerPx=0 は「無限に寄っている」＝線間隔は無限大')
assert.equal(tierAlpha(1, Number.NaN), 0)
assert.equal(tierAlpha(1, Number.POSITIVE_INFINITY), 0)

// ============================================================== 1mm段が消える視野の大きさ
// 高さ900pxの画面で、1mm段が「見えている/薄い/消えた」と言える視野の高さ[mm]。
// 完了条件の目安(線間隔1.5pxで0・6pxで1)から**数百mmを超えたあたりで消える**ことを見る。
{
  const heightPx = 900
  const alphaAtViewHeight = (mm) => tierAlpha(1, mmPerPixel({ kind: 'ortho', frustumHeight: mm }, heightPx))
  assert.equal(alphaAtViewHeight(100), 1, '視野100mmでは1mm段ははっきり見えている')
  const partial = alphaAtViewHeight(300)
  assert.ok(partial > 0 && partial < 1, `視野300mmでは1mm段は薄くなっている途中のはず: ${partial}`)
  assert.ok(alphaAtViewHeight(500) > 0, '視野500mmではまだ完全には消えていない')
  assert.equal(alphaAtViewHeight(1400), 0, '視野1400mmでは1mm段は消えている')
  // 10mm段はその視野ではまだ濃い(3段が同時に消えたりしない)。
  assert.equal(tierAlpha(10, mmPerPixel({ kind: 'ortho', frustumHeight: 1400 }, heightPx)), 1)
}

// ============================================================== snap125(ピック範囲の丸め)
assert.equal(snap125(1), 1)
assert.equal(snap125(1.5), 2)
assert.equal(snap125(2), 2)
assert.equal(snap125(3), 5)
assert.equal(snap125(5), 5)
assert.equal(snap125(7), 10)
assert.equal(snap125(10), 10)
assert.equal(snap125(0.03), 0.05) // 1未満でも同じ規則で丸まる
assert.equal(snap125(999), 1000)
assert.equal(snap125(302.4), 500)

// ============================================================== extentFor(ピック範囲・near/far下限)
const emptyBox = { isEmpty: () => true, min: { x: 0, y: 0, z: 0 }, max: { x: 0, y: 0, z: 0 } }
assert.equal(extentFor(emptyBox), BASE_EXTENT, '空の文書は ±100mm のはず')

const box252 = {
  isEmpty: () => false,
  min: { x: -252, y: -10, z: -5 },
  max: { x: 10, y: 5, z: 5 },
}
assert.equal(extentFor(box252), 500)

const tinyBox = { isEmpty: () => false, min: { x: -1, y: -1, z: -1 }, max: { x: 1, y: 1, z: 1 } }
assert.equal(extentFor(tinyBox), BASE_EXTENT, '小さい内容でも空の文書より狭くならない')

// 対角ではなく最遠成分から丸まる。
const farAlongOneAxis = {
  isEmpty: () => false,
  min: { x: -1, y: -1, z: -1 },
  max: { x: 1000, y: 1, z: 1 },
}
assert.equal(extentFor(farAlongOneAxis), 2000) // 1000*1.2=1200 → snap125 → 2000

// 原点をまたがない箱でも、原点からの最遠成分だけで半幅を決める(原点中心のまま広げるだけ)。
const farFromOrigin = {
  isEmpty: () => false,
  min: { x: 400, y: 400, z: 0 },
  max: { x: 500, y: 500, z: 10 },
}
assert.equal(extentFor(farFromOrigin), 1000) // reach=500 → *1.2=600 → snap125 → 1000

// ============================================================== snapForSpan(枠の縁を載せる刻み)
assert.equal(snapForSpan(200), 100, '長辺200mm → 100mm段に載せる')
assert.equal(snapForSpan(40), 10, '40mmの面を100mm角の枠で囲まない(面の何倍もの破綻に戻る)')
assert.equal(snapForSpan(3), 1)
assert.equal(snapForSpan(0), 1, '退化した入力でも最小段へ倒す')
assert.equal(snapForSpan(-5), 1)
assert.equal(snapForSpan(Number.NaN), 1)

// ============================================================== boundingSquare(描画範囲)
const bs = (box, includeOrigin = false) => boundingSquare(box, { includeOrigin })

assert.deepEqual(
  boundingSquare(null, { includeOrigin: true }),
  { centerU: 0, centerV: 0, half: BASE_EXTENT },
  '空の文書は従来どおり ±100mm',
)

// **正方形であること**と、四辺が段の倍数に載ること。
{
  const b = bs({ uMin: 0, uMax: 100, vMin: 0, vMax: 100 }, true)
  const step = snapForSpan(100)
  for (const edge of [b.centerU - b.half, b.centerU + b.half, b.centerV - b.half, b.centerV + b.half]) {
    assert.equal(Math.round(edge / step) * step, edge, `縁 ${edge} が ${step}mm の倍数に載っていない`)
  }
  // 内容が縁ぴったりでも余白が付く(0..100 が枠の内側に入る)。
  assert.ok(b.centerU - b.half < 0 && b.centerU + b.half > 100, '一回り分の余白が付いていない')
}

// **面の中心に載る**(原点対称の半幅をやめた件)。u:0-40 / v:-20-0 の面は中心(20,-10)。
{
  const b = bs({ uMin: 0, uMax: 40, vMin: -20, vMax: 0 })
  assert.equal(b.centerU, 20)
  assert.equal(b.centerV, -10)
  assert.ok(b.half >= 20, '面を覆えていない')
  assert.ok(b.half <= 40, '面の何倍もの大きさになっている')
}

// **原点を必ず含める**(基準平面。モデルが原点から遠くても原点が枠の外へ出ない)。
{
  const far = { uMin: 400, uMax: 500, vMin: 400, vMax: 500 }
  const withOrigin = bs(far, true)
  assert.ok(
    withOrigin.centerU - withOrigin.half <= 0 && withOrigin.centerV - withOrigin.half <= 0,
    '原点が枠の外へ出ている',
  )
  const withoutOrigin = bs(far)
  assert.ok(withoutOrigin.half < withOrigin.half, '原点を含めない方が枠は小さいはず')
  assert.ok(withoutOrigin.centerU > 0, '原点を含めなければ内容側に寄る')
}

// 非有限な入力は空の文書と同じ扱いへ倒す(壊れた値で画面を埋めない)。
assert.deepEqual(bs({ uMin: 0, uMax: Number.NaN, vMin: 0, vMax: 1 }), {
  centerU: 0,
  centerV: 0,
  half: BASE_EXTENT,
})

assert.ok(BOUNDS_MARGIN_RATIO > 0, '余白の比が正でない')

console.log('grid scale: all assertions passed')
