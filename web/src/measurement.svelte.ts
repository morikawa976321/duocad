/**
 * 直近の測定の結果の持ち主(verb ではない。文書もログも変えない)。測った選択が変わるまで残り、
 * 次のコマンドの起動・Esc(App が `clear` を呼ぶ)・モードの切り替えでも消える。
 */
import { refKey } from './commands/command'
import { measureLabel, measureReadout } from './measureText'
import type { MeasurePayload, SelectionRef } from './session'
import type { Viewport } from './viewport'

export interface Measurement {
  readonly current: MeasurePayload | null
  readonly text: string
  onResult(payload: MeasurePayload): void
  clear(): void
}

export function createMeasurement(deps: {
  selection: () => SelectionRef[]
  modeKind: () => string
  viewport: () => Viewport | null
  fail: (message: string) => void
}): Measurement {
  let current = $state<MeasurePayload | null>(null)
  let measuredKey = ''

  function selectionKey(): string {
    return deps.selection().map(refKey).join('|')
  }

  // 測った選択が変われば結果は嘘になるので消す。
  $effect(() => {
    if (current !== null && selectionKey() !== measuredKey) current = null
  })
  // モードが変わったら消す。
  $effect(() => {
    void deps.modeKind()
    current = null
  })

  $effect(() => {
    // 先に読む(`?.` の短絡で依存を失わない)。
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
