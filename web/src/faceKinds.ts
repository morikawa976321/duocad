/**
 * ボディごとの面の種類(`FaceGeometry.kind`)の控え。`state.bodies` は形を持たず、形は版が変わったボディだけ届くので、
 * 届いたときに控える。食える面の種類を持つ引数(`SelectionArgSpec.surfaceKinds`)が引く。
 */
import type { BodyGeometry } from './session'

export interface FaceKinds {
  /** 状態が届くたびに呼ぶ(`changed` は今回形が届いたボディ、`alive` は文書にいる全ボディ)。 */
  update(changed: readonly BodyGeometry[], alive: readonly string[]): void
  /** 面の種類(届いていなければ null)。 */
  kindOf(bodyId: string, index: number): string | null
}

export function createFaceKinds(): FaceKinds {
  const kinds = new Map<string, Map<number, string>>()
  return {
    update(changed, alive) {
      for (const body of changed) {
        if (body.geometry === null) kinds.delete(body.body_id)
        else kinds.set(body.body_id, new Map(body.geometry.faces.map((face) => [face.index, face.kind])))
      }
      const living = new Set(alive)
      for (const bodyId of [...kinds.keys()]) if (!living.has(bodyId)) kinds.delete(bodyId)
    },
    kindOf: (bodyId, index) => kinds.get(bodyId)?.get(index) ?? null,
  }
}
