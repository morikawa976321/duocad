/**
 * `web/src/headlight.ts`(カメラについて回るヘッドライト)を**実際に評価する**口。
 * `tests/test_ts_evaluators.py` が `node` でこれを走らせる(WebGL は要らない。光の向きは行列から読む)。
 *
 * 落ちたら、ヘッドライトの向きが視線とずれる: 平行光源の `target` が既定のワールド原点のままだと、注視点が原点から
 * 離れた部品では光が「カメラ→原点」の向きに差し、正面の面が逆光(環境光だけ)になる。
 */
import assert from 'node:assert/strict'

import { createHeadlight } from '../web/src/headlight.ts'

const THREE = await import(new URL('../web/node_modules/three/build/three.module.js', import.meta.url).href)

/** 注視点 `target` をカメラが距離 `distance` で見ているとき、光が差す向きと視線のなす角[度]。 */
function angleToViewDirection(target, distance, followTarget) {
  const scene = new THREE.Scene()
  const camera = new THREE.PerspectiveCamera(45, 1, 0.1, 10000)
  camera.position.copy(target).add(new THREE.Vector3(0, -distance, 0)) // 注視点の手前(-Y側)から +Y を見る
  camera.lookAt(target)
  scene.add(camera)
  const headlight = createHeadlight()
  camera.add(headlight.light)
  scene.add(headlight.aim)
  if (followTarget) headlight.follow(target)
  scene.updateMatrixWorld(true)

  const lightWorld = headlight.light.getWorldPosition(new THREE.Vector3())
  const aimWorld = headlight.aim.getWorldPosition(new THREE.Vector3())
  // 光が物体へ向かう向き = target - 光の位置
  const lightDirection = aimWorld.sub(lightWorld).normalize()
  const viewDirection = camera.getWorldDirection(new THREE.Vector3())
  return (Math.acos(Math.min(1, lightDirection.dot(viewDirection))) * 180) / Math.PI
}

const FAR_FROM_ORIGIN = new THREE.Vector3(1000, 0, 0)

// 対照: target を追従させなければ、原点から離れた部品では光が視線から大きくずれる(これが欠陥の姿)。
assert.ok(angleToViewDirection(FAR_FROM_ORIGIN, 300, false) > 30, '対照が成り立たない(原点を向いた光が視線に近い)')

// 追従させれば、原点からどれだけ離れていても光は視線にほぼ沿う(カメラ空間の位置のずれぶんだけ)。
for (const target of [new THREE.Vector3(0, 0, 0), FAR_FROM_ORIGIN, new THREE.Vector3(-400, 250, 90)]) {
  const angle = angleToViewDirection(target, 300, true)
  assert.ok(angle < 5, `ヘッドライトが視線からずれている: ${angle.toFixed(2)}度 (target ${target.toArray()})`)
}

// 近寄るほど光の位置のオフセットが効いて斜めになるが、離れた位置関係には依らない(原点に注視点があるときと同じ角度)。
const atOrigin = angleToViewDirection(new THREE.Vector3(0, 0, 0), 100, true)
const away = angleToViewDirection(new THREE.Vector3(5000, -3000, 800), 100, true)
assert.ok(Math.abs(atOrigin - away) < 1e-6, `注視点の位置で角度が変わる: ${atOrigin} vs ${away}`)

console.log('headlight.ts: 全ての想定を満たした')
