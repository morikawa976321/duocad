/**
 * カメラについて回るヘッドライト。three.js の平行光源は向きを「光の位置 − `target` の位置」で決め、
 * `target` の既定はワールド原点。そのままだと光はカメラ→原点の向きに差し、注視点が原点から離れるほど視線とずれて、
 * 正面を向いた面が逆光(環境光だけ)になる。`target` を注視点へ置く。
 *
 * 使い方: `light` をカメラの子に、`aim` をシーンに足し、描く前に `follow(注視点)` を呼ぶ。
 */
import * as THREE from 'three'

/** 光のカメラ空間での位置(右上手前から当てる)。注視点までの距離に比べて十分小さいので、光はほぼ視線と同じ向きになる。 */
const HEADLIGHT_OFFSET = [0.4, 0.7, 1] as const

export interface Headlight {
  readonly light: THREE.DirectionalLight
  /** 光の `target`。シーンの子にしないと `matrixWorld` が更新されない。 */
  readonly aim: THREE.Object3D
  /** 注視点(`OrbitControls.target`)へ向ける。 */
  follow(target: THREE.Vector3): void
}

export function createHeadlight(): Headlight {
  const light = new THREE.DirectionalLight(0xffffff, 1.6)
  light.position.set(...HEADLIGHT_OFFSET)
  const aim = new THREE.Object3D()
  light.target = aim
  return {
    light,
    aim,
    follow(target) {
      aim.position.copy(target)
    },
  }
}
