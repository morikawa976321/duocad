/**
 * 文字を canvas テクスチャの `THREE.Sprite` にする。深度検査はしない(奥でも読める)。大きさは世界座標(`makeTextSprite`)か、
 * 画面の画素で一定(`makeScreenTextSprite`。寄っても対象を覆わない)。
 */
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
  // 既定の NoColorSpace のままだと、レンダラの sRGB 出力で札の色が二重に変換されて明るく出る。
  texture.colorSpace = THREE.SRGBColorSpace
  const material = new THREE.SpriteMaterial({ map: texture, depthTest: false, depthWrite: false, transparent: true })
  const sprite = new THREE.Sprite(material)
  sprite.scale.set(height * (canvas.width / canvas.height), height, 1)
  return { sprite, disposables: [texture, material] }
}

/**
 * 投影の縦の係数 `p11`(`projectionMatrix.elements[5]`)と、視点空間の奥行き `depth`(正投影は null)で、
 * 縦 `viewportHeightPx` の画面での1画素ぶんの世界長さ。
 */
export function worldPerPixel(p11: number, depth: number | null, viewportHeightPx: number): number {
  const span = depth === null ? 2 / p11 : (2 * depth) / p11
  return span / Math.max(viewportHeightPx, 1)
}

/** `makeTextSprite` と同じ札を、描くたびに縦 `heightPx`[CSS 画素]になる大きさへ直す。 */
export function makeScreenTextSprite(
  text: string,
  heightPx: number,
  textColor: string,
): { sprite: THREE.Sprite; disposables: Array<{ dispose(): void }> } {
  const made = makeTextSprite(text, 1, textColor)
  const sprite = made.sprite
  const aspect = sprite.scale.x
  const size = new THREE.Vector2()
  const at = new THREE.Vector3()
  // 視錐台の判定は `onBeforeRender` より前に、直す前の大きさで走る(画面の端の札が消えたまま直らない)ので切る
  sprite.frustumCulled = false
  // 行列は `onBeforeRender` の後に組まれる(three の `renderObject`)ので、ここで直した大きさがその回の描画に効く
  sprite.onBeforeRender = (renderer, _scene, camera) => {
    renderer.getSize(size)
    at.setFromMatrixPosition(sprite.matrixWorld).applyMatrix4(camera.matrixWorldInverse)
    const depth = (camera as THREE.PerspectiveCamera).isPerspectiveCamera === true ? Math.max(-at.z, 1e-9) : null
    const height = heightPx * worldPerPixel(camera.projectionMatrix.elements[5], depth, size.y)
    sprite.scale.set(height * aspect, height, 1)
    sprite.updateMatrixWorld()
  }
  return made
}
