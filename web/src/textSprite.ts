/** 文字を canvas テクスチャの `THREE.Sprite` にする。大きさは世界座標(`height`[mm])で、深度検査はしない(奥でも読める)。 */
import * as THREE from 'three'

const SPRITE_FONT = 'bold 48px sans-serif'
const SPRITE_HEIGHT_PX = 96
/** 文字の左右の余白[画素]。 */
const SPRITE_PADDING_PX = 12
/** canvas の幅の上限[画素]。これを超える長さの文字は横に縮めて収める(GPU のテクスチャ上限を超えない)。 */
const SPRITE_MAX_WIDTH_PX = 2048
/** 文字が測れない(2D コンテキストが無い)ときの幅[画素]。 */
const SPRITE_FALLBACK_WIDTH_PX = 256

/** 文字(幅 `textWidthPx`)が余白つきで収まる canvas の幅[画素]。 */
export function spriteCanvasWidth(textWidthPx: number): number {
  const wanted = Math.ceil(textWidthPx) + 2 * SPRITE_PADDING_PX
  return Math.min(Math.max(wanted, 2 * SPRITE_PADDING_PX + 1), SPRITE_MAX_WIDTH_PX)
}

export function makeTextSprite(
  text: string,
  height: number,
  textColor: string,
): { sprite: THREE.Sprite; disposables: Array<{ dispose(): void }> } {
  const canvas = document.createElement('canvas')
  canvas.height = SPRITE_HEIGHT_PX
  const ctx = canvas.getContext('2d')
  // 幅は文字に合わせる。固定幅だと 10mm 以上の距離(`12.345 mm`)で右端の単位が切れる。
  let width = SPRITE_FALLBACK_WIDTH_PX
  if (ctx) {
    ctx.font = SPRITE_FONT
    width = spriteCanvasWidth(ctx.measureText(text).width)
  }
  // canvas の大きさを変えると 2D コンテキストの状態(font など)が初期化されるので、描く前に設定し直す。
  canvas.width = width
  if (ctx) {
    ctx.fillStyle = 'rgba(16, 22, 30, 0.85)'
    ctx.fillRect(0, 0, canvas.width, canvas.height)
    ctx.fillStyle = textColor
    ctx.font = SPRITE_FONT
    ctx.textBaseline = 'middle'
    ctx.fillText(text, SPRITE_PADDING_PX, canvas.height / 2, canvas.width - 2 * SPRITE_PADDING_PX)
  }
  const texture = new THREE.CanvasTexture(canvas)
  const material = new THREE.SpriteMaterial({ map: texture, depthTest: false, depthWrite: false, transparent: true })
  const sprite = new THREE.Sprite(material)
  sprite.scale.set(height * (canvas.width / canvas.height), height, 1)
  return { sprite, disposables: [texture, material] }
}
