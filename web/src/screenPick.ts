/**
 * 当たり判定の距離計算(画面上のCSSピクセル空間)。three.js を持ち込まない —— Node からこのファイルだけを
 * 読んで検査できる(`tests/eval_screen_pick.mjs`)。world → screen の射影は `viewport.ts` の `projectRaw`。
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
  const t = segmentParam(px, py, ax, ay, bx, by)
  const cx = ax + t * (bx - ax) - px
  const cy = ay + t * (by - ay) - py
  return cx * cx + cy * cy
}

/** 点 (px,py) に最も近い線分 AB 上の点の位置(A=0・B=1。[0,1] に clamp)。 */
export function segmentParam(px: number, py: number, ax: number, ay: number, bx: number, by: number): number {
  const dx = bx - ax
  const dy = by - ay
  const lengthSq = dx * dx + dy * dy
  return lengthSq === 0 ? 0 : Math.min(1, Math.max(0, ((px - ax) * dx + (py - ay) * dy) / lengthSq))
}

/**
 * 画面上の線分の位置 `s` を、元の3Dの線分の位置へ戻す。透視投影では画面上の補間は3Dの補間と一致しない
 * (奥の端ほど縮んで見える)ので、端点のクリップ空間の w(透視は視点からの奥行き、正投影は 1)で重み付ける。
 */
export function perspectiveParam(s: number, wa: number, wb: number): number {
  const denominator = (1 - s) / wa + s / wb
  return denominator === 0 || !Number.isFinite(denominator) ? s : s / wb / denominator
}

/** 重なった候補を順位付けるための1件(`rankByOverlap`)。 */
export interface RankedCandidate<T> extends ScreenPickCandidate<T> {
  /** ソリッドの的か(スケッチの的より優先する。docs/DESIGN_DECISIONS.md「見えるものはすべてさわれる」)。 */
  solid: boolean
  /** 同着を決める安定した鍵(届いた順で勝敗が揺れないように)。 */
  key: string
}

/**
 * 同じ細かさの候補(点どうし・線どうし)を狙われている順に並べる。画面距離が近い順だが、スケッチの的は
 * `overlapPx` だけ遠いものとして扱う: 重なって見える(距離の差がそれ以内)ならソリッドが先、離れていれば近い方が先。
 * 半径の外の候補は落とす。同着は `key` の順。
 */
export function rankByOverlap<T>(
  radiusPx: number,
  overlapPx: number,
  candidates: Iterable<RankedCandidate<T>>,
): Array<RankedCandidate<T>> {
  const limit = radiusPx * radiusPx
  const kept: Array<{ candidate: RankedCandidate<T>; score: number }> = []
  for (const candidate of candidates) {
    if (!(candidate.distSq <= limit)) continue
    kept.push({ candidate, score: Math.sqrt(candidate.distSq) + (candidate.solid ? 0 : overlapPx) })
  }
  kept.sort((a, b) => a.score - b.score || (a.candidate.key < b.candidate.key ? -1 : a.candidate.key > b.candidate.key ? 1 : 0))
  return kept.map((entry) => entry.candidate)
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
