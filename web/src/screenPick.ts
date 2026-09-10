/**
 * 当たり判定の距離計算(**画面上のCSSピクセル空間**)。**three.js を持ち込まない** ——
 * Node からこのファイルだけを読んで検査できる(`tests/eval_screen_pick.mjs`)。
 *
 * ここが持つのは「射影済みの2D座標から hit/miss を決める」規則だけで、world → screen の
 * 射影そのものは `viewport.ts` の `projectForPick` が持つ。
 *
 * **半径は inclusive**(距離ちょうど `radiusPx` は hit)。**同着は先に来た候補が勝つ** ——
 * 呼び出し側は「点 → 線」のように優先順位を付けた順で候補を渡すので、同じ距離なら
 * 先に渡した側を返すのが優先順位の意味と一致する。
 */

/**
 * 点 (px,py) から線分 AB までの距離²[px²]。t を [0,1] に clamp するので、垂足が
 * 線分の外へ出るときは近い方の端点までの距離になる。
 *
 * clamp の式は `entityKinds.ts` の `projectToSegment`(UV空間)と同じもの。**用途が違う**
 * (あちらは当たった後にスナップ座標を作る)ので共通化していない。
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
  /** カーソルからの距離²[px²]。 */
  distSq: number
}

/**
 * 半径 `radiusPx` 以内で距離最小の候補。1つも入っていなければ null。
 *
 * Raycaster の「3D距離が最も近いもの」ではなく **screen 距離が最も近いもの**を返す
 * (奥行きの違う辺が重なって見えるとき、見た目に近い方が狙いである)。
 */
export function pickNearestWithinRadius<T>(
  radiusPx: number,
  candidates: Iterable<ScreenPickCandidate<T>>,
): T | null {
  const limit = radiusPx * radiusPx
  let best: T | null = null
  let bestDistSq = Infinity
  for (const candidate of candidates) {
    // NaN は両方の比較で false になるので、ここを通れない(黙って勝たない)。
    if (candidate.distSq <= limit && candidate.distSq < bestDistSq) {
      best = candidate.item
      bestDistSq = candidate.distSq
    }
  }
  return best
}
