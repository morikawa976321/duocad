/**
 * ボディ由来のスケッチの吸着先(`get_sketch_guides`)の取り寄せの持ち主。頼み直すのは、編集中のスケッチ・その平面・
 * 見えているボディの形のどれかが変わったときと、繋ぎ直したときだけ(状態はズームの粗さが変わっても届くので、届くたびには
 * 頼まない)。変わった時点で手元の吸着先を捨て、最後に頼んだ返事だけを画面へ渡す。失敗は札にしない(裏で頼んでいる)。
 */
import type { SketchGuidesPayload } from './session'
import type { SketchGuides } from './viewportTypes'

export interface SketchGuideRequests {
  onResult(payload: SketchGuidesPayload): void
}

export function createSketchGuideRequests(deps: {
  sketchId: () => string | null
  /** そのスケッチの枠(平面が変わったら変わる)。まだ届いていなければ undefined。 */
  frame: (sketchId: string) => unknown
  /** 見えている B-rep のボディの `body_id` と形の版(`BodyMeta.shape_revision`)。 */
  shapes: () => string[]
  connected: () => boolean
  request: (sketchId: string, requestId: number) => void
  show: (guides: SketchGuides | null) => void
}): SketchGuideRequests {
  /** 最後に出した要求の番号。これと違う返事は追い越された古いもの。 */
  let latest = 0

  // 文字列で比べる: 状態が届くたびにボディの配列と枠は作り直されるが、中身が同じなら効果を走らせない。
  const requestKey = $derived.by(() => {
    const id = deps.sketchId()
    if (id === null) return ''
    const frame = deps.frame(id)
    return frame === undefined ? '' : JSON.stringify([id, frame, deps.shapes()])
  })

  $effect(() => {
    const key = requestKey
    const connected = deps.connected()
    latest += 1
    deps.show(null)
    const id = deps.sketchId()
    if (key === '' || !connected || id === null) return
    deps.request(id, latest)
  })

  return {
    onResult(payload) {
      if (payload.request_id !== latest || payload.error !== null) return
      deps.show({ sketchId: payload.sketch_id, entities: payload.entities, points: payload.points })
    },
  }
}
