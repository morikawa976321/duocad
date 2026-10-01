/**
 * 直近の測定の結果の持ち主(**verb ではない** —— 文書もログも変えない)。
 *
 * 結果は**測った選択が変わるまで残る**。そのほかに消えるのは、次のコマンドの起動・Esc
 * (どちらも App が `clear` を呼ぶ)・モードの切り替え。読み札(`#measure-result`)と3Dの線
 * (`viewport.showMeasurement`)は、どちらもここの結果から導く。
 */
import { refKey } from './commands/command'
import { measureLabel, measureReadout } from './measureText'
import type { MeasurePayload, SelectionRef } from './session'
import type { Viewport } from './viewport'

export interface Measurement {
  /** 直近の測定の結果(無ければ null)。 */
  readonly current: MeasurePayload | null
  /** 読み札の文言(結果が無ければ空)。 */
  readonly text: string
  /** `measure` の応答が届いた。 */
  onResult(payload: MeasurePayload): void
  clear(): void
}

export function createMeasurement(deps: {
  /** 今の選択(コアの写し)。 */
  selection: () => SelectionRef[]
  modeKind: () => string
  viewport: () => Viewport | null
  fail: (message: string) => void
}): Measurement {
  let current = $state<MeasurePayload | null>(null)
  /** `current` を測ったときの選択の印(`refKey` の並び。origin は見ない)。 */
  let measuredKey = ''

  function selectionKey(): string {
    return deps.selection().map(refKey).join('|')
  }

  // 測った選択が変われば結果は嘘になるので消す(選び直し・undo・AIの提案のどれでも)。
  $effect(() => {
    if (current !== null && selectionKey() !== measuredKey) current = null
  })
  // モードが変わったら消す(コマンド層を `{#key mode.kind}` で作り直すのと同じ理由)。
  $effect(() => {
    void deps.modeKind()
    current = null
  })

  $effect(() => {
    // 先に読んでから渡す(`?.` の短絡で依存を失わない)。
    const between = current?.between ?? null
    if (between === null || between.from === null || between.to === null) {
      deps.viewport()?.showMeasurement(null)
      return
    }
    deps.viewport()?.showMeasurement({ from: between.from, to: between.to, label: measureLabel(between) })
  })

  const text = $derived(current?.between ? measureReadout(current.between) : '')

  return {
    get current() {
      return current
    },
    get text() {
      return text
    },
    onResult(payload) {
      if (payload.between_error) {
        current = null
        deps.fail(payload.between_error)
        return
      }
      if (payload.between === undefined || payload.between === null) {
        current = null
        return
      }
      current = payload
      measuredKey = selectionKey()
    },
    clear() {
      current = null
    },
  }
}
