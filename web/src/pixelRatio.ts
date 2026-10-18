/** 描画バッファの画素比(`devicePixelRatio`)の扱い。ブラウザのズームや別モニタへの移動で実行中に変わる。 */

/** 画素比の上限。3倍以上はバッファが大きくなるだけで見た目の差が出ない。 */
const MAX_PIXEL_RATIO = 2

/** `renderer.setPixelRatio` に渡す値。 */
export function cappedPixelRatio(devicePixelRatio: number | undefined): number {
  return Math.min(devicePixelRatio || 1, MAX_PIXEL_RATIO)
}

interface PixelRatioWindow {
  readonly devicePixelRatio: number
  matchMedia(query: string): Pick<MediaQueryList, 'addEventListener' | 'removeEventListener'>
}

/**
 * `devicePixelRatio` が変わるたびに `onChange` を呼ぶ。止める関数を返す。
 * 値が変わったことを知る標準の口は無く、`(resolution: Ndppx)` の一致が外れるのを見張る。変わったら新しい値で張り直す。
 * (ブラウザのズームは CSS 寸法も変えるので `ResizeObserver` でも気づけるが、同じ寸法のまま別モニタへ移ると気づけない。)
 */
export function watchPixelRatio(win: PixelRatioWindow, onChange: () => void): () => void {
  let query: ReturnType<PixelRatioWindow['matchMedia']> | null = null

  function changed(): void {
    onChange()
    arm()
  }

  function arm(): void {
    query?.removeEventListener('change', changed)
    query = win.matchMedia(`(resolution: ${win.devicePixelRatio}dppx)`)
    query.addEventListener('change', changed)
  }

  arm()
  return () => {
    query?.removeEventListener('change', changed)
    query = null
  }
}
