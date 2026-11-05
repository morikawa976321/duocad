/**
 * ボディ由来の吸着先(`SketchGuides`)から、当たり判定の段ごとの候補を (u,v) で並べる純関数。three.js を持たないので
 * Node から直接検査できる(`tests/eval_sketch_guides.mjs`)。画面の距離で拾うのは `sketchGuides.ts`。
 *
 * 顔ぶれはスケッチの図形の吸着と揃える: 点の段に頂点・中心・軸の端・スケッチの図形との交点、中点の段に直線の中点、
 * 線の段に線そのもの(`sketchGuides.ts`)。
 */
import { entityHandles, entityMidpoints, entityVisualCorners, intersectionsBetween } from './entityKinds'
import type { SketchGuides } from './viewportTypes'

/** 候補1つ。`entity` は強調して描く線(`SketchGuides.entities` の添字)。線を持たない頂点は null。 */
export interface GuideTarget {
  u: number
  v: number
  entity: number | null
}

export interface GuideMidpoint extends GuideTarget {
  direction: readonly [number, number]
}

/**
 * 点の段の候補: 交わりの頂点、円・円弧・楕円の中心、楕円の軸の端。ベジエ(コアが近似した曲線)の取っ手は
 * 内側の制御点が曲線の上に無いので入れない(端は交わりの頂点として `points` に来る)。
 */
export function guidePointTargets(guides: SketchGuides): GuideTarget[] {
  const out: GuideTarget[] = guides.points.map(([u, v]) => ({ u, v, entity: null }))
  guides.entities.forEach((item, entity) => {
    if (item.kind === 'bezier') return
    for (const handle of entityHandles(item.kind, item.params)) out.push({ u: handle.u, v: handle.v, entity })
    for (const [u, v] of entityVisualCorners(item.kind, item.params)) out.push({ u, v, entity })
  })
  return out
}

/** 中点の段の候補: 直線の中点。 */
export function guideMidpointTargets(guides: SketchGuides): GuideMidpoint[] {
  const out: GuideMidpoint[] = []
  guides.entities.forEach((item, entity) => {
    for (const midpoint of entityMidpoints(item.kind, item.params)) out.push({ ...midpoint, entity })
  })
  return out
}

/** 点の段の候補のうち、ボディの線とスケッチの図形の交点(ボディの線どうし・スケッチの図形どうしは見ない)。 */
export function guideSketchCrossings(
  guides: SketchGuides,
  sketchEntities: ReadonlyArray<{ kind: string; params: Record<string, number> }>,
): GuideTarget[] {
  return intersectionsBetween(guides.entities, sketchEntities).map(({ u, v, index }) => ({ u, v, entity: index }))
}

/**
 * 同じ段のスケッチの図形とボディ由来の吸着先のどちらを取るか(画面の距離²)。同着はスケッチの図形: ボディの頂点に
 * 吸着して描いた点はぴったり重なり、置く座標はどちらでも同じなので、強調を描いた図形の側に残す。
 */
export function nearerSide(entityDistSq: number | null, guideDistSq: number | null): 'entity' | 'guide' | null {
  if (entityDistSq === null) return guideDistSq === null ? null : 'guide'
  if (guideDistSq === null) return 'entity'
  return guideDistSq < entityDistSq ? 'guide' : 'entity'
}
