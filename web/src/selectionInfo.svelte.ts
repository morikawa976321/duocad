/**
 * 選択情報の表示(issue #140)の持ち主。選択が**ちょうど1項目**のあいだだけ、その寸法をサーバの
 * `inspect_selection` で取り寄せて持つ。選択は AI と共有の集合なので、AI が選択を変えても同じ道で追従する。
 *
 * 取り直すのは「選択」か「文書」が変わったときだけ(状態はズームの粗さが変わっても届くので、届くたびには取らない)。
 */
import { refKey } from './commands/command'
import type { MeasureItem, SelectionInfoPayload, SelectionRef } from './session'

export interface SelectionInfo {
  readonly item: MeasureItem | null
  readonly error: string | null
  onResult(payload: SelectionInfoPayload): void
}

export function createSelectionInfo(deps: {
  selection: () => SelectionRef[]
  /** 文書が変わったら変わる文字列(同じ選択でも寸法が変わりうる)。 */
  documentVersion: () => string
  connected: () => boolean
  request: (requestId: number) => void
}): SelectionInfo {
  let item = $state<MeasureItem | null>(null)
  let error = $state<string | null>(null)
  /** 最後に出した要求の番号。これと違う返事は追い越された古いもの。 */
  let latest = 0
  let shownSelection = ''

  // 文字列で比べる: 状態が届くたびに選択の配列は作り直されるが、中身が同じなら効果を走らせない。
  const selectionKey = $derived.by(() => {
    const items = deps.selection()
    return items.length === 1 ? refKey(items[0]) : ''
  })
  const requestKey = $derived(selectionKey === '' ? '' : `${selectionKey}@${deps.documentVersion()}`)

  $effect(() => {
    const key = requestKey
    const selected = selectionKey
    latest += 1
    // 別の対象へ移ったら前の値を消す(返事を待つ間に前の対象の値が新しい選択の名で見えないように)。
    // 同じ対象で文書だけ変わったときは、返事が来るまで前の値を出したまま(ちらつかせない)。
    if (selected !== shownSelection) {
      item = null
      error = null
      shownSelection = selected
    }
    if (key === '' || !deps.connected()) return
    deps.request(latest)
  })

  return {
    get item() {
      return item
    },
    get error() {
      return error
    },
    onResult(payload) {
      if (payload.request_id !== latest) return
      item = payload.item
      error = payload.error
    },
  }
}
