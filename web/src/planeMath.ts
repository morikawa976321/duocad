/** 平面の枠(原点・u軸・v軸を数値3つの配列で持つもの)から three.js の点を作る小物。 */
import * as THREE from 'three'

/** `frame.origin` / `u` / `v` / `normal` のような数値3つの配列から `Vector3` を作る(正規化はしない。要る側が `.normalize()` を付ける)。 */
export function frameVector(values: readonly number[]): THREE.Vector3 {
  return new THREE.Vector3(values[0], values[1], values[2])
}

/** 平面上の点 `origin + u·U + v·V`(`origin` が null なら原点。コアの `kernel_sketch._to_pnt` と同じ規則)。 */
export function planePoint(
  origin: readonly number[] | null,
  axisU: readonly number[],
  axisV: readonly number[],
  u: number,
  v: number,
): THREE.Vector3 {
  return new THREE.Vector3(
    (origin?.[0] ?? 0) + u * axisU[0] + v * axisV[0],
    (origin?.[1] ?? 0) + u * axisU[1] + v * axisV[1],
    (origin?.[2] ?? 0) + u * axisU[2] + v * axisV[2],
  )
}
