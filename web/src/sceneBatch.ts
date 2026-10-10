/**
 * 「シーンに足したオブジェクト」と「捨てるもの(ジオメトリ・共有していない材質)」の組。
 * ピック判定に要る配列は呼び出し側が別に持つ(この束は除去と破棄のためだけ。
 * docs/WEB_PITFALLS.md「シーンから除去すべきオブジェクトの配列と、ピック判定に要る配列を兼用しない」)。
 */
import * as THREE from 'three'

export interface SceneBatch {
  /** `object` を親へ足し、`disposables` と一緒に控える。足したオブジェクトをそのまま返す。 */
  add<T extends THREE.Object3D>(object: T, ...disposables: Array<{ dispose(): void }>): T
  /** オブジェクトを持たない捨てもの。 */
  own(...disposables: Array<{ dispose(): void }>): void
  clear(): void
  readonly objects: readonly THREE.Object3D[]
}

export function createBatch(parent: THREE.Object3D): SceneBatch {
  const objects: THREE.Object3D[] = []
  const disposables: Array<{ dispose(): void }> = []
  return {
    add(object, ...items) {
      parent.add(object)
      objects.push(object)
      disposables.push(...items)
      return object
    },
    own(...items) {
      disposables.push(...items)
    },
    clear() {
      for (const object of objects) parent.remove(object)
      for (const item of disposables) item.dispose()
      objects.length = 0
      disposables.length = 0
    },
    get objects() {
      return objects
    },
  }
}
