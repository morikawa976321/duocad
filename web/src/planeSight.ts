import type * as THREE from 'three'

/**
 * 視線と平面のなす角[度]の下限。これより浅い視線では、平面との交点を点として受けない。
 * 置いた値で、実測ではない。2 度では 1 画素の動きが、カメラの平面からの高さのおよそ 0.8 倍
 * (画角 45 度・縦 800 画素で見積もり)の移動になり、1 mm に丸めても置いた点は狙いからずれる。
 */
export const MIN_PLANE_SIGHT_DEG = 2

/** 視線 `direction`(正規化済み)が、法線 `normal`(正規化済み)の平面にほぼ平行か。 */
export function sightTooShallow(direction: THREE.Vector3, normal: THREE.Vector3): boolean {
  return Math.abs(direction.dot(normal)) < Math.sin((MIN_PLANE_SIGHT_DEG * Math.PI) / 180)
}
