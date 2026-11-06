/**
 * 視点を「今の向きのまま」箱へ寄せるための純関数(`viewport.ts` の `frameBox` に渡す入力を作る)。
 * 全体表示(F キー・ビューキューブ下のボタン)と SVG 取り込み後の寄せが使う。
 */
import * as THREE from 'three'

/** 注視点からカメラへの向きを保ち、長さを `home` に揃えたオフセット(`frameBox` の `offset`)。重なっていれば `home` の向き。 */
export function keptDirectionOffset(
  position: THREE.Vector3,
  target: THREE.Vector3,
  home: THREE.Vector3,
): THREE.Vector3 {
  const offset = position.clone().sub(target)
  if (offset.lengthSq() < 1e-18) offset.copy(home)
  return offset.setLength(home.length())
}

/**
 * `objects` のうち見えているもの(`visible`)の外接箱。どれも見えていなければ空の箱。
 * `Box3.setFromObject` は `visible=false` の物も数えるので、プレビューが隠している元のボディ(`viewport.ts` の
 * `setBodyVisible`)を入れると、見えている結果より大きい箱になる。
 */
export function boxOfVisible(objects: Iterable<THREE.Object3D>): THREE.Box3 {
  const box = new THREE.Box3()
  for (const object of objects) if (object.visible) box.expandByObject(object)
  return box
}

/** スケッチの折れ線(世界座標)のうち `entityIds` の図形だけの外接箱。該当が無ければ空の箱。 */
export function boxOfSketchEntities(
  lines: Iterable<{ entityId: unknown; polylineWorld: readonly THREE.Vector3[] | undefined }>,
  entityIds: ReadonlySet<string>,
): THREE.Box3 {
  const box = new THREE.Box3()
  for (const line of lines) {
    if (typeof line.entityId !== 'string' || !entityIds.has(line.entityId)) continue
    for (const point of line.polylineWorld ?? []) box.expandByPoint(point)
  }
  return box
}
