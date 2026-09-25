/**
 * 「シーンに足したオブジェクト」と「捨てるもの(ジオメトリ・共有していない材質)」の組。
 *
 * `add()` が親へ足すのと配列へ控えるのを**同時に**行うので、足したのに控え忘れて `clear()` で
 * 画面に残る・捨て忘れる事故が構造的に起きない(docs/WEB_PITFALLS.md「シーンから除去すべき
 * オブジェクトの配列と、ピック判定に要る配列を兼用しない」)。**ピック判定に要る配列**
 * (`faceMeshes` / `edgeLines` / `handleMeshes` / `regionMeshes` 等)は呼び出し側が別に持つ ——
 * この束は除去と破棄のためだけのもの。
 */
import * as THREE from 'three'

export interface SceneBatch {
  /** `object` を親へ足し、`disposables` と一緒に控える。足したオブジェクトをそのまま返す(ピック用の配列へ push できる)。 */
  add<T extends THREE.Object3D>(object: T, ...disposables: Array<{ dispose(): void }>): T
  /** オブジェクトを持たない捨てもの(この束だけが使う材質など)。 */
  own(...disposables: Array<{ dispose(): void }>): void
  /** 足したものを親から外し、控えたものを全部捨てる。空にした束はまた使える。 */
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
