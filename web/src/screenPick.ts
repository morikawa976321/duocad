/**
 * 当たり判定の距離計算(画面上のCSSピクセル空間)。three.js を持ち込まない —— Node からこのファイルだけを
 * 読んで検査できる(`tests/eval_screen_pick.mjs`)。world → screen の射影は `viewport.ts` の `projectForPick`。
 *
 * 半径は inclusive(距離ちょうど `radiusPx` は hit)。同着は先に来た候補が勝つ
 * (呼び出し側が優先順位の順で渡すため)。
 */

/** 画面上の2点の距離の2乗。 */
export function distSq(ax: number, ay: number, bx: number, by: number): number {
  const dx = ax - bx
  const dy = ay - by
  return dx * dx + dy * dy
}

/**
 * 点 (px,py) から線分 AB までの距離²。t を [0,1] に clamp する。
 * `entityKinds.ts` の `projectToSegment`(UV空間・スナップ座標用)と式は同じだが、用途が違うので共通化していない。
 */
export function distSqPointSegment(
  px: number,
  py: number,
  ax: number,
  ay: number,
  bx: number,
  by: number,
): number {
  const dx = bx - ax
  const dy = by - ay
  const lengthSq = dx * dx + dy * dy
  const t = lengthSq === 0 ? 0 : Math.min(1, Math.max(0, ((px - ax) * dx + (py - ay) * dy) / lengthSq))
  const cx = ax + t * dx - px
  const cy = ay + t * dy - py
  return cx * cx + cy * cy
}

export interface ScreenPickCandidate<T> {
  item: T
  distSq: number
}

/**
 * 半径 `radiusPx` 以内で距離最小の候補。無ければ null。
 * Raycaster の3D距離ではなく screen 距離で選ぶ(奥行きの違う辺が重なるとき、見た目に近い方が狙い)。
 */
export function pickNearestWithinRadius<T>(
  radiusPx: number,
  candidates: Iterable<ScreenPickCandidate<T>>,
): T | null {
  const limit = radiusPx * radiusPx
  let best: T | null = null
  let bestDistSq = Infinity
  for (const candidate of candidates) {
    // NaN は両方の比較で false になるので勝たない。
    if (candidate.distSq <= limit && candidate.distSq < bestDistSq) {
      best = candidate.item
      bestDistSq = candidate.distSq
    }
  }
  return best
}
