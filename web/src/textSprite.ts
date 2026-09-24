/**
 * 文字を1枚の札(canvas テクスチャの `THREE.Sprite`)にする。荷重の "250N"(`optimizeAnnotations.ts`)と
 * 測定の "12.345 mm"(`measureAnnotation.ts`)が同じ作りを共有する。
 *
 * 大きさは世界座標(`height`[mm])なので、寄れば大きく引けば小さく見える —— 呼び出し側が
 * モデルの大きさ(`contentDiagonal`)から決める。深度検査はしない(奥にあっても読める札)。
 */
import * as THREE from 'three'

export function makeTextSprite(
  text: string,
  height: number,
  textColor: string,
): { sprite: THREE.Sprite; disposables: Array<{ dispose(): void }> } {
  const canvas = document.createElement('canvas')
  canvas.width = 256
  canvas.height = 96
  const ctx = canvas.getContext('2d')
  if (ctx) {
    ctx.fillStyle = 'rgba(16, 22, 30, 0.85)'
    ctx.fillRect(0, 0, canvas.width, canvas.height)
    ctx.fillStyle = textColor
    ctx.font = 'bold 48px sans-serif'
    ctx.textBaseline = 'middle'
    ctx.fillText(text, 12, canvas.height / 2)
  }
  const texture = new THREE.CanvasTexture(canvas)
  const material = new THREE.SpriteMaterial({ map: texture, depthTest: false, depthWrite: false, transparent: true })
  const sprite = new THREE.Sprite(material)
  sprite.scale.set(height * (canvas.width / canvas.height), height, 1)
  return { sprite, disposables: [texture, material] }
}
