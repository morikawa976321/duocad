/**
 * 丸めの面の連なり(`get_face_chains`)の覚え。「面の削除」で丸めの面1枚のクリックを、つながった丸め全体へ広げるのに使う
 * (広げるのは画面の選択で、verb は選ばれた面だけを消す。docs/FACE_EDIT.md カード b)。
 *
 * 規律は辺の連なり(`App.svelte` の `edgeChains`)と同じ: 連なりは形にだけ依存するので形の版(`shape_revision`)が鍵、
 * 同じ形の同じ面は2度頼まない、今の形の版でない応答は使わない。
 */
import type { FaceChainsPayload } from './session'

export interface FaceChains {
  /** その面が属する連なりの面の添字(昇順)。今の形の分がまだ届いていなければ null。 */
  chainOf(bodyId: string, faceIndex: number): number[] | null
  /** まだ頼んでいない面の連なりを頼む。形の版の分からないボディは頼まない。 */
  request(bodyId: string, faces: number[]): void
  /** 応答を覚える。今の形の版のものでなければ捨てて false。 */
  remember(payload: FaceChainsPayload): boolean
  /** 切断・要求の失敗で、「頼んだ」印を捨てる(次のクリックで頼み直せる)。 */
  forgetAsked(): void
}

export function createFaceChains(deps: {
  shapeRevisionOf(bodyId: string): number | undefined
  send(bodyId: string, faces: number[]): void
}): FaceChains {
  const known: Record<string, { shape_revision: number; chains: number[][] }> = {}
  const asked = new Set<string>()

  return {
    chainOf(bodyId, faceIndex) {
      const entry = known[bodyId]
      if (entry === undefined || entry.shape_revision !== deps.shapeRevisionOf(bodyId)) return null
      return entry.chains.find((chain) => chain.includes(faceIndex)) ?? null
    },
    request(bodyId, faces) {
      const shape = deps.shapeRevisionOf(bodyId)
      if (shape === undefined) return
      const missing = faces.filter((face) => !asked.has(`${bodyId}:${shape}:${face}`))
      if (missing.length === 0) return
      for (const face of missing) asked.add(`${bodyId}:${shape}:${face}`)
      deps.send(bodyId, missing)
    },
    remember(payload) {
      if (payload.shape_revision !== deps.shapeRevisionOf(payload.body_id)) return false
      const entry = known[payload.body_id]
      const chains = entry?.shape_revision === payload.shape_revision ? [...entry.chains, ...payload.chains] : payload.chains
      known[payload.body_id] = { shape_revision: payload.shape_revision, chains }
      return true
    },
    forgetAsked() {
      asked.clear()
    },
  }
}
