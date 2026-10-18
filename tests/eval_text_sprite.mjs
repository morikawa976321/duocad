/**
 * `web/src/textSprite.ts`(3Dの札: 測定の距離・荷重の大きさ)を、canvas の最小の偽物で**実際に動かす**口。
 * `tests/test_ts_evaluators.py` が `node` でこれを走らせる。
 *
 * 落ちたら、札の canvas が文字より狭く、右端の単位が切れる(固定幅 256px だと `12.345 mm` 以上で欠ける)。
 */
import assert from 'node:assert/strict'

import './ts_resolve_hook.mjs'

const GLYPH_PX = 26 // 偽の測り: 1文字 26px(48px の太字で数字がこの程度の幅)

/** 描いた結果を残す canvas。幅を変えると 2D コンテキストの font が初期化される(ブラウザと同じ)。 */
function fakeCanvas() {
  const drawn = []
  const ctx = {
    font: '10px sans-serif',
    fillStyle: '',
    textBaseline: 'alphabetic',
    measureText: (text) => ({ width: text.length * GLYPH_PX * (ctx.font.includes('48px') ? 1 : 0.2) }),
    fillRect() {},
    fillText: (text, x, y, maxWidth) => drawn.push({ text, x, y, maxWidth, font: ctx.font }),
  }
  let width = 300
  const canvas = {
    height: 150,
    get width() {
      return width
    },
    set width(value) {
      width = value
      ctx.font = '10px sans-serif'
    },
    getContext: () => ctx,
  }
  return { canvas, drawn }
}

let created = null
globalThis.document = {
  createElement: () => {
    created = fakeCanvas()
    return created.canvas
  },
}

const { makeTextSprite, spriteCanvasWidth } = await import('../web/src/textSprite.ts')

// 文字の幅に余白(左右12px)を足した幅になる。
assert.equal(spriteCanvasWidth(100), 124)
assert.equal(spriteCanvasWidth(100.2), 125, '端数は切り上げる')
assert.ok(spriteCanvasWidth(0) > 0, '空の文字でも幅は正')
assert.equal(spriteCanvasWidth(1e6), 2048, 'GPU のテクスチャ上限を超えない')

// 測定の札と荷重の札: 10mm 未満から4桁まで、どれも文字が収まる(以前は 12.345 mm 以上で欠けた)。
for (const text of ['5.000 mm', '12.345 mm', '100.000 mm', '1234.567 mm', '12345.67N', '90.000°']) {
  const { sprite } = makeTextSprite(text, 10, '#ffffff')
  const { canvas, drawn } = created
  const textWidth = text.length * GLYPH_PX
  assert.ok(canvas.width >= textWidth + 24, `${text}: canvas ${canvas.width}px に文字 ${textWidth}px が収まらない`)
  assert.equal(drawn.length, 1)
  assert.equal(drawn[0].font, 'bold 48px sans-serif', 'canvas の大きさを変えたあとに font を設定し直していない(測ったとおりの字で描けない)')
  assert.equal(drawn[0].x, 12)
  assert.equal(drawn[0].maxWidth, canvas.width - 24, '収まらないときの保険(横に縮める)')
  // sprite の縦横比は canvas の実寸から出る(幅を変えても文字がつぶれない)。
  assert.ok(Math.abs(sprite.scale.x / sprite.scale.y - canvas.width / canvas.height) < 1e-12)
  assert.equal(sprite.scale.y, 10)
}

console.log('textSprite.ts: 全ての想定を満たした')
