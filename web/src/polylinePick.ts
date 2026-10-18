/**
 * 折れ線・点の screen pick(画面上のCSSピクセルで測る)。カーソルまでの距離²に加えて、狙われている3Dの点
 * (画面で最も近い点を透視補正で3Dへ戻したもの)を返す: 遮蔽の判定(`occlusion.ts`)がその点で視線を引く。
 *
 * 距離の式は `screenPick.ts`(three を持たない純関数)。ここは world → screen の射影と折れ線の歩き方だけ。
 */
import * as THREE from 'three'

import { distSq, perspectiveParam, segmentParam } from './screenPick'

/** 折れ線1本ぶんの world 座標の読み口(配列は作らない)。 */
export interface PickPolyline {
  count: number
  at(index: number, out: THREE.Vector3): void
}

/** 画面での当たり: カーソルまでの距離²[px²]と、狙われている3Dの点(呼び手のものではない新しいベクトル)。 */
export interface ScreenHit {
  distSq: number
  point: THREE.Vector3
}

/** 1回の当たり判定のあいだ使い回す射影(カメラの行列と寸法を固定する)。 */
export interface ScreenProjector {
  /** 世界座標 → 画素。カメラの裏・視錐台の奥行きの外は false。`out.w` はクリップ空間の w。 */
  project(point: THREE.Vector3, out: ProjectedPoint): boolean
}

export interface ProjectedPoint {
  x: number
  y: number
  w: number
}

export function createProjector(camera: THREE.Camera, width: number, height: number): ScreenProjector {
  const scratch = new THREE.Vector4()
  const viewProjection = new THREE.Matrix4().multiplyMatrices(camera.projectionMatrix, camera.matrixWorldInverse)
  return {
    project(point, out) {
      scratch.set(point.x, point.y, point.z, 1).applyMatrix4(viewProjection)
      if (scratch.w === 0) return false
      const z = scratch.z / scratch.w
      // `viewport.ts` の `projectRaw` と同じ境目(NDC の z が [-1, 1] の外は描かれない)
      if (z < -1 || z > 1) return false
      out.x = ((scratch.x / scratch.w + 1) / 2) * width
      out.y = ((1 - scratch.y / scratch.w) / 2) * height
      out.w = scratch.w
      return true
    },
  }
}

const pointA = new THREE.Vector3()
const pointB = new THREE.Vector3()
const clippedA = new THREE.Vector3()
const clippedB = new THREE.Vector3()
const screenA: ProjectedPoint = { x: 0, y: 0, w: 1 }
const screenB: ProjectedPoint = { x: 0, y: 0, w: 1 }

/**
 * 折れ線の上でカーソルに画面上いちばん近い点。射影できた隣り合う2点だけを線分と見る。見えなければ null。
 * `clip`(断面で残る側が正の符号付き距離)があれば、切られた線分は落とし、平面をまたぐ線分は交点で切り詰めてから測る
 * (端点を落とすだけだと切り口手前の見えている部分が指せない)。
 */
export function closestOnPolyline(
  x: number,
  y: number,
  polyline: PickPolyline,
  projector: ScreenProjector,
  clip: ((point: THREE.Vector3) => number) | null = null,
): ScreenHit | null {
  let bestDistSq = Infinity
  let best: THREE.Vector3 | null = null
  let previousDistance = 0
  for (let at = 0; at < polyline.count; at += 1) {
    polyline.at(at, pointB)
    const distance = clip === null ? 0 : clip(pointB)
    if (at > 0 && (previousDistance >= 0 || distance >= 0)) {
      clippedA.copy(pointA)
      clippedB.copy(pointB)
      if (clip !== null) {
        const crossing = previousDistance / (previousDistance - distance)
        if (previousDistance < 0) clippedA.lerpVectors(pointA, pointB, crossing)
        else if (distance < 0) clippedB.lerpVectors(pointA, pointB, crossing)
      }
      if (projector.project(clippedA, screenA) && projector.project(clippedB, screenB)) {
        const s = segmentParam(x, y, screenA.x, screenA.y, screenB.x, screenB.y)
        const d = distSq(x, y, screenA.x + s * (screenB.x - screenA.x), screenA.y + s * (screenB.y - screenA.y))
        if (d < bestDistSq) {
          bestDistSq = d
          best = (best ?? new THREE.Vector3()).lerpVectors(clippedA, clippedB, perspectiveParam(s, screenA.w, screenB.w))
        }
      }
    }
    pointA.copy(pointB)
    previousDistance = distance
  }
  return best === null ? null : { distSq: bestDistSq, point: best }
}

const projected: ProjectedPoint = { x: 0, y: 0, w: 1 }

/** 点1つの当たり(射影できなければ null)。 */
export function screenHitOfPoint(x: number, y: number, point: THREE.Vector3, projector: ScreenProjector): ScreenHit | null {
  if (!projector.project(point, projected)) return null
  return { distSq: distSq(x, y, projected.x, projected.y), point: point.clone() }
}
