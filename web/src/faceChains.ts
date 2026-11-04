/**
 * 丸めの面の連なり(`get_face_chains`)の覚え。「面の削除」で丸めの面1枚のクリックを、つながった丸め全体へ広げるのに使う
 * (広げるのは画面の選択で、verb は選ばれた面だけを消す。docs/FACE_EDIT.md カード b)。
 *
 * 規律は辺の連なり(`App.svelte` の `edgeChains`)と同じ: 連なりは形にだけ依存するので形の版(`shape_revision`)が鍵、
 * 同じ形の同じ面は2度頼まない、今の形の版でない応答は使わない。辺と違って面の連なりは互いに重なる(丸めの角の継ぎ面は、
 * そこで会う丸めのどれの連なりにも入る)ので、起点の面ごとに覚え、本人がクリックした起点も覚える(1本外すときに残す面を決める)。
 */
import type { FaceChainsPayload } from './session'

export interface FaceChains {
  /** その面をクリックしたときに選ぶ面の添字(昇順)。今の形の分がまだ届いていなければ null。 */
  chainOf(bodyId: string, faceIndex: number): number[] | null
  /** 本人がクリックで選んだ(`selected`)・外した起点の面を覚える。 */
  picked(bodyId: string, origin: number, selected: boolean): void
  /**
   * 起点 `origin` の連なりを選択から外すときに残す面: 選択に残るほかの起点(`picked`)の連なりにも入っている面。
   * 継ぎ面を共有する丸めを1本外しても、残す丸めの継ぎ面は外さない。
   */
  sharedWith(bodyId: string, origin: number, selected: number[]): Set<number>
  /** クリックした起点の覚えを捨てる(コマンドを抜けた)。 */
  forgetPicks(): void
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
  const known: Record<string, { shape_revision: number; chains: Map<number, number[]> }> = {}
  const asked = new Set<string>()
  const picks: Record<string, Set<number>> = {}

  function current(bodyId: string): Map<number, number[]> | null {
    const entry = known[bodyId]
    return entry === undefined || entry.shape_revision !== deps.shapeRevisionOf(bodyId) ? null : entry.chains
  }

  return {
    chainOf(bodyId, faceIndex) {
      return current(bodyId)?.get(faceIndex) ?? null
    },
    picked(bodyId, origin, selected) {
      const origins = (picks[bodyId] ??= new Set())
      if (selected) origins.add(origin)
      else origins.delete(origin)
    },
    sharedWith(bodyId, origin, selected) {
      const chains = current(bodyId)
      const kept = new Set<number>()
      const chain = chains?.get(origin)
      if (chains === null || chain === undefined) return kept
      const inChain = new Set(chain)
      const still = new Set(selected)
      for (const other of picks[bodyId] ?? []) {
        if (other === origin || !still.has(other)) continue
        for (const face of chains.get(other) ?? []) if (inChain.has(face)) kept.add(face)
      }
      return kept
    },
    forgetPicks() {
      for (const key of Object.keys(picks)) delete picks[key]
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
      const chains = entry?.shape_revision === payload.shape_revision ? entry.chains : new Map<number, number[]>()
      payload.origins.forEach((origin, i) => chains.set(origin, payload.chains[i]))
      known[payload.body_id] = { shape_revision: payload.shape_revision, chains }
      return true
    },
    forgetAsked() {
      asked.clear()
    },
  }
}

/**
 * 「面の削除」のプレビューの代案(`PreviewPayload.suggestion`)のうち、選択へ足す面と、足した後の選択の鍵。
 * いまの選択が代案のボディの面だけで代案に含まれ、代案の方が広いときだけ足す。直前に代案で広げた選択(`widened`)のままなら
 * 足さない(代案から代案へ広がり続けない)。足さないときは null。
 */
export function suggestedAdditions(
  suggestion: { body_id: string; faces: number[] },
  selected: Array<{ body: string; index: number }>,
  widened: string | null,
): { add: number[]; key: string } | null {
  const key = (faces: number[]) => `${suggestion.body_id}:${[...faces].sort((a, b) => a - b).join(',')}`
  if (selected.length === 0 || selected.some((ref) => ref.body !== suggestion.body_id)) return null
  const chosen = selected.map((ref) => ref.index)
  if (key(chosen) === widened) return null
  const offered = new Set(suggestion.faces)
  if (chosen.some((face) => !offered.has(face))) return null
  const have = new Set(chosen)
  const add = suggestion.faces.filter((face) => !have.has(face))
  return add.length === 0 ? null : { add, key: key(suggestion.faces) }
}
