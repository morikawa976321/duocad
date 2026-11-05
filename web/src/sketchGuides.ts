/**
 * ボディ由来の吸着先(スケッチの平面と交わる・接するボディの頂点・辺・交線。コアの `get_sketch_guides`)を画面の距離で拾い、
 * 当たったものの線を明るく描く。描くのは当たっている間だけで、立体の内側を通る断面の線も立体の上に描く
 * (編集中のスケッチの線と同じ描き方)。当たるのは点を待っている間だけ(呼び手の `viewport.ts` の `sketchSnapAt` が決める)。
 *
 * 段ごとの候補の並べ方は `sketchGuideTargets.ts`、スケッチの図形との勝ち負けは呼び手。
 */
import * as THREE from 'three'

import { type ViewResolution } from './adaptiveResolution'
import { entityPoints, snapToEntity } from './entityKinds'
import { planePoint } from './planeMath'
import { closestOnPolyline, type ProjectedPoint, type ScreenProjector } from './polylinePick'
import { distSq } from './screenPick'
import {
  guideMidpointTargets, guidePointTargets, guideSketchCrossings, type GuideMidpoint, type GuideTarget,
} from './sketchGuideTargets'
import { buildSketchLine, createSketchLineMaterial, type SketchFatLine } from './sketchLine'
import { EDGE_PICK_PIXELS, HOVER_LIGHTEN, LAYER, PICK_EDGE_COLOR, SKETCH_HANDLE_PIXELS, lighten } from './viewportConstants'
import type { SketchDraw, SketchGuideHit, SketchGuides } from './viewportTypes'

type Frame = SketchDraw['frame']
type SketchEntities = SketchDraw['handles']

/** 当たり1つと、カーソルまでの画面の距離²[px²](同じ段のスケッチの図形と比べる)。 */
export interface GuideCandidate {
  hit: SketchGuideHit
  distSq: number
}

export interface SketchGuideLayer {
  readonly object: THREE.Group
  /** 吸着先を差し替える(null で捨てる)。 */
  set(guides: SketchGuides | null): void
  /** いま持っている吸着先がこのスケッチのものか。 */
  has(sketchId: string): boolean
  /** 点の段(頂点・中心・軸の端・スケッチの図形との交点)。半径はスケッチの点と同じ。 */
  pointAt(x: number, y: number, frame: Frame, sketchEntities: SketchEntities, project: ScreenProjector): GuideCandidate | null
  /** 中点の段(直線の中点)。 */
  midpointAt(x: number, y: number, frame: Frame, project: ScreenProjector): GuideCandidate | null
  /** 線の段。`raw` はカーソルの (u,v) で、当たった線へ落とした点を `snapAt` にする。 */
  lineAt(
    x: number,
    y: number,
    frame: Frame,
    raw: { u: number; v: number },
    resolution: ViewResolution,
    project: ScreenProjector,
  ): GuideCandidate | null
  /** 当たっているものの線を明るく描く(null で消す)。描き直したら true。 */
  setHover(hit: SketchGuideHit | null, frame: Frame | null, resolution: ViewResolution): boolean
  setLineResolution(width: number, height: number): void
  dispose(): void
}

export function createSketchGuideLayer(): SketchGuideLayer {
  const object = new THREE.Group()
  const lineResolution = new THREE.Vector2(1, 1)
  const hoverMaterial = createSketchLineMaterial(lighten(PICK_EDGE_COLOR, HOVER_LIGHTEN), lineResolution)
  let guides: SketchGuides | null = null
  let points: GuideTarget[] = []
  let midpoints: GuideMidpoint[] = []
  /** スケッチの図形との交点。図形が変わったとき(配列が差し替わったとき)だけ解き直す。 */
  let crossings: { of: SketchEntities | null; targets: GuideTarget[] } = { of: null, targets: [] }
  /** 線の世界座標の折れ線。枠と粗さが変わったときだけ折り直す。 */
  let polylines: { key: string; frame: Frame | null; lines: THREE.Vector3[][] } = { key: '', frame: null, lines: [] }
  /** 明るく描いている線。`source` は折った時の折れ線の組(枠・粗さが変わると差し替わるので描き直す)。 */
  let highlight: { entity: number; source: THREE.Vector3[][]; line: SketchFatLine; geometry: THREE.BufferGeometry } | null = null
  const projected: ProjectedPoint = { x: 0, y: 0, w: 1 }

  function clearHighlight(): void {
    if (highlight === null) return
    object.remove(highlight.line)
    highlight.geometry.dispose()
    highlight = null
  }

  function linesFor(frame: Frame, resolution: ViewResolution): THREE.Vector3[][] {
    const key = `${resolution.deviation}|${resolution.angularDeflection}`
    if (polylines.frame === frame && polylines.key === key) return polylines.lines
    const lines = (guides?.entities ?? []).map((item) =>
      entityPoints(item.kind, item.params, resolution).map(([u, v]) => planePoint(frame.origin, frame.u, frame.v, u, v)),
    )
    polylines = { key, frame, lines }
    return lines
  }

  function nearest(
    x: number,
    y: number,
    frame: Frame,
    targets: ReadonlyArray<GuideTarget>,
    project: ScreenProjector,
    best: { target: GuideTarget; distSq: number } | null,
  ): { target: GuideTarget; distSq: number } | null {
    const limit = SKETCH_HANDLE_PIXELS * SKETCH_HANDLE_PIXELS
    for (const target of targets) {
      if (!project.project(planePoint(frame.origin, frame.u, frame.v, target.u, target.v), projected)) continue
      const d = distSq(x, y, projected.x, projected.y)
      if (d <= limit && (best === null || d < best.distSq)) best = { target, distSq: d }
    }
    return best
  }

  return {
    object,
    set(next) {
      guides = next
      points = next === null ? [] : guidePointTargets(next)
      midpoints = next === null ? [] : guideMidpointTargets(next)
      crossings = { of: null, targets: [] }
      polylines = { key: '', frame: null, lines: [] }
      clearHighlight()
    },
    has(sketchId) {
      return guides !== null && guides.sketchId === sketchId
    },
    pointAt(x, y, frame, sketchEntities, project) {
      if (guides === null) return null
      if (crossings.of !== sketchEntities) crossings = { of: sketchEntities, targets: guideSketchCrossings(guides, sketchEntities) }
      const best = nearest(x, y, frame, crossings.targets, project, nearest(x, y, frame, points, project, null))
      if (best === null) return null
      const { u, v, entity } = best.target
      return { hit: { sketchId: guides.sketchId, snapAt: [u, v], kind: 'point', entity }, distSq: best.distSq }
    },
    midpointAt(x, y, frame, project) {
      if (guides === null) return null
      const best = nearest(x, y, frame, midpoints, project, null)
      if (best === null) return null
      const { u, v, entity, direction } = best.target as GuideMidpoint
      return {
        hit: { sketchId: guides.sketchId, snapAt: [u, v], kind: 'midpoint', snapDirection: direction, entity },
        distSq: best.distSq,
      }
    },
    lineAt(x, y, frame, raw, resolution, project) {
      if (guides === null) return null
      const lines = linesFor(frame, resolution)
      const limit = EDGE_PICK_PIXELS * EDGE_PICK_PIXELS
      let bestEntity = -1
      let bestDistSq = Infinity
      for (let entity = 0; entity < lines.length; entity += 1) {
        const line = lines[entity]
        const hit = closestOnPolyline(x, y, { count: line.length, at: (index, out) => out.copy(line[index]) }, project)
        if (hit !== null && hit.distSq <= limit && hit.distSq < bestDistSq) {
          bestEntity = entity
          bestDistSq = hit.distSq
        }
      }
      if (bestEntity < 0) return null
      const item = guides.entities[bestEntity]
      const snapAt = snapToEntity(item.kind, item.params, raw.u, raw.v, resolution)
      return { hit: { sketchId: guides.sketchId, snapAt, kind: 'line', entity: bestEntity }, distSq: bestDistSq }
    },
    setHover(hit, frame, resolution) {
      const entity = hit?.entity ?? null
      if (entity === null || frame === null || guides === null) {
        if (highlight === null) return false
        clearHighlight()
        return true
      }
      const lines = linesFor(frame, resolution)
      if (highlight !== null && highlight.entity === entity && highlight.source === lines) return false
      clearHighlight()
      const built = buildSketchLine(lines[entity] ?? [], hoverMaterial)
      if (built === null) return true
      built.object.renderOrder = LAYER.sketchLine
      object.add(built.object)
      highlight = { entity, source: lines, line: built.object, geometry: built.geometry }
      return true
    },
    setLineResolution(width, height) {
      lineResolution.set(width, height)
    },
    dispose() {
      clearHighlight()
      hoverMaterial.dispose()
    },
  }
}
