/** 描画バッファの画素比(`devicePixelRatio`)の扱い。ブラウザのズームや別モニタへの移動で実行中に変わる。 */

/** 画素比の上限。3倍以上はバッファが大きくなるだけで見た目の差が出ない。 */
const MAX_PIXEL_RATIO = 2

/** `renderer.setPixelRatio` に渡す値。 */
export function cappedPixelRatio(devicePixelRatio: number | undefined): number {
  return Math.min(devicePixelRatio || 1, MAX_PIXEL_RATIO)
}

/**
 * 画素比が変わっていれば新しい値、同じなら `null`。描画ループが毎フレーム `renderer.getPixelRatio()` と
 * `window.devicePixelRatio` を比べて呼ぶ。イベントで待たない: ズームは `ResizeObserver` で気づけるが、同じ CSS 寸法のまま
 * 別モニタへ移ると気づけず、`matchMedia` の resolution の change は実機でしか確かめられない(headless のエミュレーションでは発火しない)。
 */
export function nextPixelRatio(current: number, devicePixelRatio: number | undefined): number | null {
  const wanted = cappedPixelRatio(devicePixelRatio)
  return wanted === current ? null : wanted
}
