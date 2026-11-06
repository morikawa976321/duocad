/**
 * この画面が送って配信がまだ返っていない選択の変更を、配信済みの選択へ重ねる(`App.svelte` の `currentItems`)。
 * 効き方はコアの `duocad/selection.py`(`modify_selection` / `toggle_selection` / `clear_selection`)と揃える。
 */
import { refKey } from './commands/command'
import type { SelectionRef } from './session'

export type SelectionEdit = (items: SelectionRef[]) => SelectionRef[]

/** 無いものを順に足し(入っている参照の位置は動かさない)、その後で外す(両方にあれば外す側が勝つ)。 */
export function modifyEdit(add: readonly SelectionRef[], remove: readonly SelectionRef[]): SelectionEdit {
  return (items) => {
    const have = new Set(items.map(refKey))
    const added = add.filter((ref) => {
      const key = refKey(ref)
      if (have.has(key)) return false
      have.add(key)
      return true
    })
    const gone = new Set(remove.map(refKey))
    return [...items, ...added].filter((item) => !gone.has(refKey(item)))
  }
}

/** 入っていれば外し、無ければ末尾に足す。 */
export function toggleEdit(ref: SelectionRef): SelectionEdit {
  const key = refKey(ref)
  return (items) => {
    const kept = items.filter((item) => refKey(item) !== key)
    return kept.length < items.length ? kept : [...items, ref]
  }
}

export const clearEdit: SelectionEdit = () => []

/**
 * 配信済みの選択に、返っていない変更を送った順に重ねた見込みの選択。`null` は効き方が手元で分からない変更(サーバが決める)で、飛ばす。
 * `inFlight` が空なら `delivered` そのものを返す。
 */
export function expectedItems(delivered: SelectionRef[], inFlight: Iterable<SelectionEdit | null>): SelectionRef[] {
  let items = delivered
  for (const edit of inFlight) if (edit !== null) items = edit(items)
  return items
}
