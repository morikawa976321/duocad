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

/**
 * 1回の当たり判定のあいだ使い回す射影(カメラの行列と寸法を固定する)。寸法は作るときに1回だけ読んだもの
 * (`viewport.ts` の `size()` は DOM の寸法を読むので、候補ごとに呼ぶと強制レイアウトがホバーの時間の4割を占めた。実測)。
 */
export interface ScreenProjector {
  readonly width: number
  readonly height: number
  /**
   * 球の画面の半径を見積もる定数。透視では「奥行き1mmあたりの画素」(`height / (2·tan(fov/2))`。奥行き d の1mmは
   * `focalPx / d` 画素)、正投影では1mmあたりの画素(奥行きに依らない)。
   */
  readonly focalPx: number
  readonly orthographic: boolean
  /** 透視の near(これより手前に掛かる球は画面での広がりを見積もれない)。 */
  readonly near: number
  /** 世界座標 → 画素。カメラの裏・視錐台の奥行きの外は false。`out.w` はクリップ空間の w。 */
  project(point: THREE.Vector3, out: ProjectedPoint): boolean
  /** 線分 a-b を視錐台の奥行きの範囲(近平面・遠平面)で切り詰めて outA・outB へ入れる。範囲に掛からなければ false。 */
  clipDepth(a: THREE.Vector3, b: THREE.Vector3, outA: THREE.Vector3, outB: THREE.Vector3): boolean
}

export interface ProjectedPoint {
  x: number
  y: number
  w: number
}

/** 奥行きの切り口を NDC の z=±1 からこの割合だけ内側に取る(切り口の点が `project` の境目で丸め誤差に落ちない)。 */
const DEPTH_CLIP_MARGIN = 1e-6

export function createProjector(camera: THREE.Camera, width: number, height: number): ScreenProjector {
  const scratch = new THREE.Vector4()
  const clipA = new THREE.Vector4()
  const clipB = new THREE.Vector4()
  const kept = new THREE.Vector3()
  const viewProjection = new THREE.Matrix4().multiplyMatrices(camera.projectionMatrix, camera.matrixWorldInverse)
  const orthographic = camera instanceof THREE.OrthographicCamera
  let focalPx = 0
  let near = 0
  if (camera instanceof THREE.OrthographicCamera) {
    focalPx = (camera.zoom * height) / (camera.top - camera.bottom)
  } else if (camera instanceof THREE.PerspectiveCamera) {
    focalPx = (camera.zoom * height) / (2 * Math.tan(THREE.MathUtils.degToRad(camera.fov) / 2))
    near = camera.near
  }
  return {
    width,
    height,
    focalPx,
    orthographic,
    near,
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
    clipDepth(a, b, outA, outB) {
      clipA.set(a.x, a.y, a.z, 1).applyMatrix4(viewProjection)
      clipB.set(b.x, b.y, b.z, 1).applyMatrix4(viewProjection)
      let t0 = 0
      let t1 = 1
      // 視錐台の奥行きの内側は -w <= z <= w。クリップ空間は世界座標の線形変換なので、線分の割合 t はそのまま使える。
      for (const sign of [1, -1]) {
        const da = clipA.w * (1 - DEPTH_CLIP_MARGIN) + sign * clipA.z
        const db = clipB.w * (1 - DEPTH_CLIP_MARGIN) + sign * clipB.z
        if (da < 0 && db < 0) return false
        if (da < 0) t0 = Math.max(t0, da / (da - db))
        else if (db < 0) t1 = Math.min(t1, da / (da - db))
      }
      if (t0 > t1) return false
      // outA・outB が a・b と同じベクトルでもよいように、a を退避してから書く。
      kept.copy(a)
      outA.lerpVectors(kept, b, t0)
      outB.lerpVectors(kept, b, t1)
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
 * 折れ線の上でカーソルに画面上いちばん近い点。隣り合う2点を、視錐台の奥行きの範囲(近平面・遠平面)で切り詰めた線分と見る。見えなければ null。
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
      if (projector.clipDepth(clippedA, clippedB, clippedA, clippedB) && projector.project(clippedA, screenA) && projector.project(clippedB, screenB)) {
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
